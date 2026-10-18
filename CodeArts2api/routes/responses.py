# -*- coding: utf-8 -*-
"""routes/responses.py — OpenAI Responses 路由（/v1/responses）。

grok-build / Codex 等原生 Responses 客户端：请求侧经 adapters/responses.py 转
Chat，响应侧把上游 SSE 包成 Responses SSE。引擎件（签名/发送/重试/SSE 解析）
全在 server.py，本层只做协议形状。
"""
import json
import time
import uuid

from flask import Response, g, jsonify, request

from adapters import responses


def register(app, core):
    """挂载 responses 路由。core = server 模块（动态查找）。"""
    # 只绑 logger；其余引擎函数一律在请求期经 core.xxx 现查（同 routes/chat.py）
    logger = core.logger
    # ── OpenAI Responses 端点（grok-build / Codex 等原生 Responses 客户端）──────
    @app.route("/v1/responses", methods=["POST", "OPTIONS"])
    def responses_api():
        """Responses 协议入口：请求体经 responses_bridge 转成 Chat 后走既有上游
        管线（签名/鉴权/重试全部复用），响应侧把 Chat SSE 包回 Responses SSE
        （每帧带 sequence_number，grok 类型级强制）。"""
        if request.method == "OPTIONS":
            return core._cors(Response(""))

        body = request.get_data()
        if not body:
            return core._cors(jsonify({"error": {"message": "empty body"}})), 400
        try:
            req_json = json.loads(body)
        except Exception:
            return core._cors(jsonify({"error": {"message": "invalid JSON body"}})), 400
        if not isinstance(req_json, dict):
            return core._cors(jsonify({"error": {"message": "invalid JSON body"}})), 400

        chat_req = responses.responses_to_chat(req_json)
        if not chat_req.get("messages"):
            return core._cors(jsonify({"error": {"message": "empty input"}})), 400

        # 与 chat 路径同款的清洗/归一化/钳制（签名前完成）
        core._sanitize_tool_calls(chat_req)
        core._refresh_models_if_needed()
        # 模型列表由 server 动态同步（重绑定而非原地改），必须经 core 现查
        supported_models = core.SUPPORTED_MODELS
        agent_models = core.AGENT_MODELS
        model = chat_req.get("model", supported_models[0])
        if model not in supported_models:
            # 先给缓存一次同步纠正的机会：福利模型动态投放，同 chat 路径注释
            if not core._force_sync_models_for(model):
                return (
                    core._cors(
                        jsonify(
                            {
                                "error": {
                                    "message": (
                                        f"model '{model}' 不在当前账号可用模型列表中，"
                                        f"可用：{', '.join(supported_models)}"
                                    ),
                                    "type": "invalid_request_error",
                                    "param": "model",
                                    "code": "model_not_found",
                                }
                            }
                        )
                    ),
                    404,
                )
            logger.info("模型 %s 经强制同步后命中，继续处理请求", model)
        chat_req["model"] = model
        core._clamp_max_tokens(chat_req)

        benefit = model not in agent_models
        want_stream = bool(chat_req.get("stream", False))
        if not want_stream:
            # 同 chat 路径：统一向上游要流，非流式在此聚合成单个 Responses JSON
            chat_req["stream"] = True

        upstream, err = core._send_upstream(chat_req, model, benefit)
        if err is not None:
            return err

        if want_stream:
            # 同 chat 路径：生成器迭代时请求上下文已结束，先抓上下文字段
            rid, t0 = g.rid, g.t0
            is_sse = "text/event-stream" in (upstream.headers.get("Content-Type") or "").lower()

            def generate():
                if is_sse:
                    # 常规：Chat SSE 逐帧包成 Responses SSE
                    for frame in responses.chat_sse_to_responses(
                            upstream.iter_lines(decode_unicode=False), model):
                        yield frame
                    return
                # 上游偶尔 stream=true 仍回普通 JSON：聚合成单个 Chat JSON，
                # 再以最小 Responses SSE 序列发出，客户端拿到的仍是合法流。
                try:
                    result = core._parse_upstream_json(upstream)
                except Exception as error:
                    logger.error("[%s] [responses] 非 SSE 响应读取中断: %s: %s",
                                 rid, type(error).__name__, error)
                    for f in responses.response_json_to_sse(
                            responses.error_response_obj(
                                model, "upstream_error",
                                "upstream stream interrupted: %s" % error)):
                        yield f
                    return
                if isinstance(result, dict) and result.get("error"):
                    logger.error("[%s] [responses] [502] 上游业务错误: %s",
                                 rid, result.get("error"))
                    for f in responses.response_json_to_sse(
                            responses.error_response_obj(model, "upstream_error",
                                                  str(result.get("error")))):
                        yield f
                    return
                for f in responses.response_json_to_sse(
                        responses.chat_json_to_responses(result, model)):
                    yield f

            resp = Response(core._stream_log(generate(), rid, t0), status=200,
                            mimetype="text/event-stream")
            resp.headers["Cache-Control"] = "no-cache, no-transform"
            resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
            resp.headers["X-Accel-Buffering"] = "no"
            return core._cors(resp)

        # 非流式：聚合成单个 Responses JSON
        try:
            result = core._parse_upstream_json(upstream)
        except Exception as error:
            logger.error("[%s] [responses] 上游响应读取中断: %s: %s",
                         g.rid, type(error).__name__, error)
            return (
                core._cors(jsonify({"error": {"message": "upstream stream interrupted: %s" % error,
                                        "type": "upstream_error",
                                        "code": type(error).__name__}})),
                502,
            )
        if isinstance(result, dict) and result.get("error"):
            logger.error("[%s] [responses] [502] 上游业务错误: %s", g.rid, result.get("error"))
            return (
                core._cors(jsonify({"error": {"message": str(result.get("error")),
                                        "type": "upstream_error",
                                        "code": "upstream_error"}})),
                502,
            )
        return core._cors(jsonify(responses.chat_json_to_responses(result, model)))

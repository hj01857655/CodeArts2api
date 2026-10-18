# -*- coding: utf-8 -*-
"""routes/messages.py — Anthropic Messages 路由（/v1/messages）。

Claude Code 等原生 Anthropic 客户端：adapters/anthropic.py 双向翻译，引擎件
（签名/发送/重试/SSE 解析）全在 server.py。
"""
import json
import time
import uuid

from flask import Response, g, jsonify, request

from adapters import anthropic


def register(app, core):
    """挂载 messages 路由。core = server 模块（动态查找）。"""
    # 只绑 logger；其余引擎函数一律在请求期经 core.xxx 现查（同 routes/chat.py）
    logger = core.logger
    # ── Anthropic Messages 端点（Claude Code 等原生 Anthropic 客户端）──────────────
    # 与 /v1/responses 同构：anthropic_bridge 把 Anthropic ⇄ Chat 翻掉，复用
    # 既有上游管线（签名/鉴权/重试）；响应侧包成 Anthropic SSE。
    @app.route("/v1/messages", methods=["POST"])
    def messages_api():
        if request.method == "OPTIONS":
            return core._cors(Response(""))

        body = request.get_data()
        if not body:
            return core._cors(jsonify(anthropic.error_body("invalid_request_error", "empty body"))), 400
        try:
            req_json = json.loads(body)
        except Exception:
            return core._cors(jsonify(anthropic.error_body("invalid_request_error", "invalid JSON body"))), 400
        if not isinstance(req_json, dict):
            return core._cors(jsonify(anthropic.error_body("invalid_request_error", "invalid JSON body"))), 400

        chat_req = anthropic.anthropic_to_chat(req_json)
        if not chat_req.get("messages"):
            return core._cors(jsonify(anthropic.error_body("invalid_request_error", "empty messages"))), 400

        core._sanitize_tool_calls(chat_req)
        core._refresh_models_if_needed()
        # 模型列表由 server 动态同步（重绑定而非原地改），必须经 core 现查
        supported_models = core.SUPPORTED_MODELS
        agent_models = core.AGENT_MODELS
        model = chat_req.get("model", supported_models[0])
        if model not in supported_models:
            if not core._force_sync_models_for(model):
                return (
                    core._cors(jsonify(anthropic.error_body("invalid_request_error",
                        "model '%s' 不在当前账号可用模型列表中，可用：%s" % (model, ", ".join(supported_models))))),
                    404,
                )
            logger.info("模型 %s 经强制同步后命中，继续处理请求", model)
        chat_req["model"] = model
        core._clamp_max_tokens(chat_req)

        benefit = model not in agent_models
        want_stream = bool(chat_req.get("stream", False))
        if not want_stream:
            chat_req["stream"] = True

        upstream, err = core._send_upstream(chat_req, model, benefit)
        if err is not None:
            return err

        if want_stream:
            rid, t0 = g.rid, g.t0
            is_sse = "text/event-stream" in (upstream.headers.get("Content-Type") or "").lower()

            def generate():
                if is_sse:
                    for frame in anthropic.chat_sse_to_anthropic(
                            upstream.iter_lines(decode_unicode=False), model):
                        yield frame
                    return
                try:
                    result = core._parse_upstream_json(upstream)
                except Exception as error:
                    logger.error("[%s] [messages] 非 SSE 响应读取中断: %s: %s",
                                 rid, type(error).__name__, error)
                    yield anthropic.error_sse("api_error",
                        "upstream stream interrupted: %s" % error)
                    return
                if isinstance(result, dict) and result.get("error"):
                    logger.error("[%s] [messages] [502] 上游业务错误: %s",
                                 rid, result.get("error"))
                    yield anthropic.error_sse("api_error", str(result.get("error")))
                    return
                for frame in anthropic.chat_json_to_anthropic_sse(result, model):
                    yield frame

            resp = Response(core._stream_log(generate(), rid, t0), status=200,
                            mimetype="text/event-stream")
            resp.headers["Cache-Control"] = "no-cache, no-transform"
            resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
            resp.headers["X-Accel-Buffering"] = "no"
            return core._cors(resp)

        try:
            result = core._parse_upstream_json(upstream)
        except Exception as error:
            logger.error("[%s] [messages] 上游响应读取中断: %s: %s",
                         g.rid, type(error).__name__, error)
            return (
                core._cors(jsonify(anthropic.error_body("api_error", "upstream stream interrupted: %s" % error))),
                502,
            )
        if isinstance(result, dict) and result.get("error"):
            logger.error("[%s] [messages] [502] 上游业务错误: %s", g.rid, result.get("error"))
            return (
                core._cors(jsonify(anthropic.error_body("api_error", str(result.get("error"))))),
                502,
            )
        return core._cors(jsonify(anthropic.chat_json_to_anthropic(result, model)))

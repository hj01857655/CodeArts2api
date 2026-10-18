# -*- coding: utf-8 -*-
"""routes/chat.py — OpenAI Chat Completions 路由（/v1/chat/completions）。

签名/发送/重试统一走 server._send_upstream（三协议路由共用）；SSE 解析、
max_tokens 钳制、CORS 等引擎件也全在 server.py，本层只做协议形状。
core = server 模块动态查找，测试对 server 的打桩对路由层同样生效。
"""
import json
import time
import uuid

from flask import Response, g, jsonify, request


def register(app, core):
    """挂载 chat 路由。core = server 模块（动态查找）。"""
    # 只绑 logger；其余引擎函数一律在请求期经 core.xxx 现查——测试对
    # server._force_sync_models_for / server._refresh_models_if_needed /
    # server.SUPPORTED_MODELS 的打桩发生在 import 之后，导入期绑定会抓到原函数。
    logger = core.logger
    @app.route("/v1/chat/completions", methods=["POST", "OPTIONS"])
    def chat_completions():
        if request.method == "OPTIONS":
            return core._cors(Response(""))

        body = request.get_data()
        if not body:
            return core._cors(jsonify({"error": {"message": "empty body"}})), 400
        try:
            req_json = json.loads(body)
        except Exception:
            return core._cors(jsonify({"error": {"message": "invalid JSON body"}})), 400

        # 清洗悬空 tool_calls / 孤儿 tool 消息（客户端中断后上游会拒整条历史）
        core._sanitize_tool_calls(req_json)

        # 模型名归一化；请求聊天时也按短缓存周期同步一次模型列表
        core._refresh_models_if_needed()
        # 模型列表由 server 动态同步（重绑定而非原地改），必须经 core 现查，
        # 测试对 server.SUPPORTED_MODELS / server.AGENT_MODELS 的替换才生效
        supported_models = core.SUPPORTED_MODELS
        agent_models = core.AGENT_MODELS
        model = req_json.get("model", supported_models[0])
        if model not in supported_models:
            # 先给缓存一次同步纠正的机会：福利模型是动态投放的，后台刷新还没落地时
            # 本地会误判成"不存在"直接 404（实测 2026-10-14 05:01 那串 404 即此）。
            if not core._force_sync_models_for(model):
                return (
                    core._cors(
                        jsonify(
                            {
                                "error": {
                                    # 带上当前可用列表：原来只说"不在可用列表中"，
                                    # 得去翻 models-cache.json 才知道能填什么。
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
        req_json["model"] = model
        # 输出上限必须在签名前收敛：签名覆盖 body 哈希，钳制必须在重序列化之前完成
        core._clamp_max_tokens(req_json)

        # 通道判断：AgentCenter 老套餐模型用基础签名；其余（免费 benefit 模型）
        # 必须带 maas_type/model-id/model-name 三个签名头，否则上游报未注册。
        benefit = model not in agent_models

        # 客户端是否要流式
        want_stream = bool(req_json.get("stream", False))

        if not want_stream:
            # 实测 2026-10-14：GLM-5.2 / openpangu-2.0-flash 在 stream=false 下返回
            # message.content=""（正文丢在上游，usage 里却呯地计入 reasoning_tokens），
            # 而同样提问走流式能正常拿到结果。因此统一向上游要流，
            # 由下方 _parse_upstream_json 聚合成单个 JSON：对客户端仍是非流式。
            req_json["stream"] = True

        # 签名/发送/重试统一走 _send_upstream（与 responses 路由共用）
        upstream, err = core._send_upstream(req_json, model, benefit)
        if err is not None:
            return err

        if want_stream:
            # generate() 是惰性生成器，迭代时请求上下文已结束，不能再碰 flask.g
            # （否则抛 "Working outside of application context" → 客户端收到 HTML 500）。
            # 请求期就把上下文字段抓成局部变量供生成器闭包使用。
            rid, t0 = g.rid, g.t0

            def generate():
                """严格转发为 OpenAI SSE；仅对 benefit 流补齐缺失的结束原因。"""
                content_type = (upstream.headers.get("Content-Type") or "").lower()
                is_benefit = model not in agent_models
                saw_done = False
                saw_finish_reason = False

                # 上游偶尔会在请求 stream=true 时返回普通 JSON，包装成单个 SSE 帧。
                if "text/event-stream" not in content_type:
                    try:
                        result = core._parse_upstream_json(upstream)
                    except Exception as error:
                        # 状态码已发出，只能以错误帧收尾，别让流无声断掉
                        logger.error("[%s] 非 SSE 响应读取中断: %s: %s",
                                     rid, type(error).__name__, error)
                        yield b"data: " + json.dumps(
                            {"error": {"message": "upstream stream interrupted: %s" % error,
                                      "type": "upstream_error",
                                      "code": type(error).__name__}},
                            ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"
                        yield b"data: [DONE]\n\n"
                        return
                    for choice in result.get("choices") or []:
                        if choice.get("finish_reason") in (None, "other"):
                            choice["finish_reason"] = "stop"
                    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    yield b"data: " + payload + b"\n\n"
                    yield b"data: [DONE]\n\n"
                    return

                # iter_lines 在读响应体阶段抛的是原生传输异常（读超时、连接被复位、
                # chunked 流断裂）。此时状态码已经发出去，改不了，只能先记下来，
                # 循环结束后用 SSE 错误帧收尾，避免客户端只看到一条被掐断的流。
                broken = {"error": None}

                def upstream_lines():
                    try:
                        for raw_line in upstream.iter_lines(decode_unicode=False):
                            yield raw_line
                    except Exception as error:
                        broken["error"] = error

                for raw_line in upstream_lines():
                    if not raw_line:
                        continue
                    if not raw_line.startswith(b"data:"):
                        if raw_line.startswith(b":"):
                            yield raw_line + b"\n\n"
                        continue

                    payload = raw_line[5:].strip()
                    if payload == b"[DONE]":
                        # benefit 流需要先确认是否缺少 finish_reason；agent 流则保持
                        # 原来的即时转发行为，避免改变 agent 客户端状态机。
                        if not is_benefit:
                            if not saw_done:
                                yield b"data: [DONE]\n\n"
                                saw_done = True
                        else:
                            saw_done = True
                        continue
                    try:
                        chunk = json.loads(payload.decode("utf-8"))
                        # 上游错误帧（如 InferHub.001001005.400 param invalid）：
                        # 原样透传会被 Agent Runtime 当成空响应 → "no content and no
                        # tool calls" 无限重试。转成 OpenAI error 帧并终止流，让
                        # 客户端直接看到真实原因。
                        if chunk.get("error_code") or chunk.get("error_msg"):
                            err_text = "%s %s" % (chunk.get("error_code") or "",
                                                  chunk.get("error_msg") or "upstream error")
                            logger.error("[%s] 流中上游错误帧: %s", rid, err_text)
                            yield b"data: " + json.dumps(
                                {"error": {"message": err_text.strip(),
                                           "type": "upstream_error",
                                           "code": chunk.get("error_code")}},
                                ensure_ascii=False, separators=(",", ":")
                            ).encode("utf-8") + b"\n\n"
                            yield b"data: [DONE]\n\n"
                            saw_done = True
                            return
                        for choice in chunk.get("choices") or []:
                            reason = choice.get("finish_reason")
                            if reason is not None:
                                saw_finish_reason = True
                                if reason == "other":
                                    choice["finish_reason"] = "stop"
                            # DeepSeek benefit 流会发送 content:null、reasoning_content 有值。
                            # 部分 Agent Runtime 只接受字符串 content，会将 null chunk
                            # 误判为无响应；OpenAI 兼容格式中空字符串表达同样语义。
                            delta = choice.get("delta")
                            if isinstance(delta, dict) and delta.get("content") is None:
                                delta["content"] = ""
                        payload = json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                        yield b"data: " + payload + b"\n\n"
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue

                # 上游把流掐断了：先给客户端一个明确的错误帧，再把异常原样抛出，
                # 保留 _stream_log 的 "stream 中断" 日志与堆栈。这里必须跳过下面的
                # 补尾逻辑，否则会给半截回答补一个 finish_reason=stop，伪装成正常收尾。
                if broken["error"] is not None:
                    error = broken["error"]
                    if not saw_done:
                        yield b"data: " + json.dumps(
                            {"error": {"message": "upstream stream interrupted: %s" % error,
                                      "type": "upstream_error",
                                      "code": type(error).__name__}},
                            ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"
                        yield b"data: [DONE]\n\n"
                    raise error

                # 只对免费 benefit 模型补齐完全缺失的 finish_reason；不重发任何上游 chunk。
                if is_benefit and not saw_finish_reason:
                    yield b"data: " + json.dumps(
                        {
                            "id": "proxy-finish",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        }, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8") + b"\n\n"
                if is_benefit and not saw_done:
                    yield b"data: [DONE]\n\n"
                elif is_benefit:
                    # 上游 [DONE] 被延迟到这里，确保补块在 [DONE] 之前。
                    yield b"data: [DONE]\n\n"
                # agent 分支保持原有行为：如果上游异常断流、没有发送 [DONE]，
                # 代理仍补一个 [DONE]，与修改前的实现一致。
                if not is_benefit and not saw_done:
                    yield b"data: [DONE]\n\n"

            resp = Response(core._stream_log(generate(), rid, t0), status=200,
                            mimetype="text/event-stream")
            resp.headers["Cache-Control"] = "no-cache, no-transform"
            resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
            resp.headers["X-Accel-Buffering"] = "no"
            return core._cors(resp)

        # 非流式：无论上游返回 JSON 还是 SSE，都统一聚合成单个 JSON。
        # CodeArts 在排队、重试或特定模型场景下可能即使请求 stream=false
        # 仍返回 text/event-stream；直接转发会导致客户端把 data: 当作 JSON 解析。
        try:
            result = core._parse_upstream_json(upstream)
        except Exception as error:
            # 读正文时连接被掐：状态码还没发出去，可以正常报错。不接住会变成 Flask
            # 默认 500 HTML 页，客户端拿到的是 "500 Internal Server Error" 而非原因。
            logger.error("[%s] 上游响应读取中断: %s: %s", g.rid, type(error).__name__, error)
            return (
                core._cors(jsonify({"error": {"message": "upstream stream interrupted: %s" % error,
                                        "type": "upstream_error",
                                        "code": type(error).__name__}})),
                502,
            )
        if isinstance(result, dict) and result.get("error"):
            logger.error("[%s] [502] 上游业务错误: %s", g.rid, result.get("error"))
            resp = Response(
                json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                status=502,
                content_type="application/json; charset=utf-8",
            )
            return core._cors(resp)
        output = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        resp = Response(output, status=200, content_type="application/json; charset=utf-8")
        return core._cors(resp)

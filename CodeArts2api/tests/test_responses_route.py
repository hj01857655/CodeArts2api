# -*- coding: utf-8 -*-
"""/v1/responses 路由端到端验证（Flask test_client + mock 上游，不连真实上游）。

monkeypatch 掉 _upstream.post / _hmac_headers / 模型同步 / API_KEY，覆盖：
  1. /v1/responses 流式：Responses SSE 合法（seq 连续、生命周期、usage、items）
  2. /v1/responses 非流式：Responses JSON 合法
  3. /v1/responses 上游业务错误：502 透传
  4. /v1/chat/completions 流式回归：_send_upstream 重构不破坏老路由
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import server  # noqa: E402


class FakeUpstream:
    def __init__(self, raw, content_type="text/event-stream", status=200):
        self._raw = raw
        self.status_code = status
        self.headers = {"Content-Type": content_type}

    @property
    def content(self):
        return self._raw

    @property
    def text(self):
        return self._raw.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text)

    def iter_lines(self, decode_unicode=False):
        for block in self._raw.split(b"\n\n"):
            yield block


def sse(*objs):
    parts = []
    for o in objs:
        parts.append(o if isinstance(o, bytes)
                     else b"data: " + json.dumps(o, ensure_ascii=False).encode())
    return b"\n\n".join(parts) + b"\n\n"


def parse_frames(raw):
    events = []
    for block in raw.split(b"\n\n"):
        block = block.strip()
        if not block.startswith(b"data:"):
            continue
        events.append(json.loads(block[5:].decode("utf-8")))
    return events


def install_fake(make_upstream):
    server._upstream.post = (
        lambda url, data=None, headers=None, stream=None, timeout=None:
        make_upstream(data))
    server._hmac_headers = (
        lambda body, benefit=False, model_id="", session_id="": {})
    server._refresh_models_if_needed = lambda: None
    server._force_sync_models_for = lambda m: True
    server.SUPPORTED_MODELS = ["test-model"]
    server.AGENT_MODELS = []
    server.API_KEY = ""


def main():
    captured = {}
    chat_stream = sse(
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
         "choices": [{"index": 0, "delta": {"role": "assistant",
                                            "reasoning_content": "think "},
                      "finish_reason": None}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
         "choices": [{"index": 0, "delta": {"reasoning_content": "ok"},
                      "finish_reason": None}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
         "choices": [{"index": 0, "delta": {"content": "Hi!"}, "finish_reason": None}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
         "choices": [{"index": 0, "delta": {"tool_calls": [
             {"index": 0, "id": "call_1", "type": "function",
              "function": {"name": "get_weather", "arguments": '{"city": "BJ"}'}}]},
             "finish_reason": None}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
         "choices": [],
         "usage": {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17,
                   "completion_tokens_details": {"reasoning_tokens": 3},
                   "prompt_tokens_details": {"cached_tokens": 0}}},
        b"data: [DONE]",
    )

    def make_stream(data):
        captured["body"] = json.loads(data)
        return FakeUpstream(chat_stream)

    install_fake(make_stream)
    client = server.app.test_client()

    # ---------- 1) /v1/responses 流式 ----------
    resp_req = {
        "model": "test-model",
        "stream": True,
        "instructions": "sys prompt",
        "input": [{"type": "message", "role": "user", "content": "hello"}],
        "tools": [{"type": "function", "name": "get_weather", "description": "w",
                   "parameters": {"type": "object",
                                  "properties": {"city": {"type": "string"}}}}],
    }
    r = client.post("/v1/responses", json=resp_req)
    assert r.status_code == 200, (r.status_code, r.data[:500])
    assert "text/event-stream" in r.headers.get("Content-Type", "")

    # 请求侧：到上游的应该是 Chat 格式
    cb = captured["body"]
    assert cb["messages"][0] == {"role": "system", "content": "sys prompt"}
    assert cb["messages"][1] == {"role": "user", "content": "hello"}
    assert cb["stream"] is True
    assert cb["tools"][0]["function"]["name"] == "get_weather"

    events = parse_frames(r.data)
    seqs = [e["sequence_number"] for e in events]
    assert seqs == list(range(len(events))), seqs
    types = [e["type"] for e in events]
    print("responses 流事件序列（%d 帧）:" % len(events))
    for i, e in enumerate(events):
        print("  %2d %s" % (i, e["type"]))
    assert types[0] == "response.created"
    assert types[1] == "response.in_progress"
    assert types[-1] == "response.completed"
    final = events[-1]["response"]
    for k in ("id", "object", "created_at", "model", "status", "output"):
        assert k in final, k
    assert final["status"] == "completed"
    assert [o["type"] for o in final["output"]] == \
        ["reasoning", "message", "function_call"], final["output"]
    assert final["usage"]["total_tokens"] == 17
    assert final["usage"]["output_tokens_details"]["reasoning_tokens"] == 3
    fn = final["output"][2]
    assert fn["call_id"] == "call_1" and fn["arguments"] == '{"city": "BJ"}'

    # ---------- 2) /v1/responses 非流式 ----------
    chat_json = {"id": "c2", "object": "chat.completion", "created": 1, "model": "test-model",
                 "choices": [{"index": 0,
                              "message": {"role": "assistant", "content": "Hi!",
                                          "reasoning_content": "think"},
                              "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8,
                           "completion_tokens_details": {"reasoning_tokens": 1},
                           "prompt_tokens_details": {"cached_tokens": 0}}}

    def make_json(data):
        return FakeUpstream(json.dumps(chat_json).encode(),
                            content_type="application/json")

    install_fake(make_json)
    resp_req2 = dict(resp_req, stream=False)
    r = client.post("/v1/responses", json=resp_req2)
    assert r.status_code == 200, (r.status_code, r.data[:500])
    out = r.get_json()
    assert out["object"] == "response" and out["status"] == "completed"
    assert [o["type"] for o in out["output"]] == ["reasoning", "message"]
    assert out["usage"]["total_tokens"] == 8

    # ---------- 3) 上游业务错误 -> 502 ----------
    def make_err(data):
        return FakeUpstream(
            json.dumps({"error_code": "InferHub.400",
                        "error_msg": "param invalid"}).encode(),
            content_type="application/json", status=400)

    install_fake(make_err)
    r = client.post("/v1/responses", json=resp_req)
    assert r.status_code == 502, (r.status_code, r.data[:300])
    assert "param invalid" in r.get_json()["error"]["message"]

    # ---------- 4) /v1/chat/completions 回归 ----------
    install_fake(make_stream)
    r = client.post("/v1/chat/completions",
                    json={"model": "test-model", "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, (r.status_code, r.data[:300])
    assert b"data: " in r.data and b"[DONE]" in r.data

    print("ALL PASS")


if __name__ == "__main__":
    main()

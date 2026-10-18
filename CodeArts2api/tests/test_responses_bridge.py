# -*- coding: utf-8 -*-
"""adapters/responses 独立验证（不依赖 Flask / 上游）：
  1. 流式转换：reasoning + 正文 + tool_calls + usage + [DONE]
  2. 每帧 sequence_number 从 0 单调递增
  3. 事件生命周期与 Response/usage 必填字段
  4. 请求侧转换（instructions/input/tools/tool_choice/参数映射）
  5. 非流式转换
  6. 上游错误帧 -> response.failed
  7. 上游断流 -> response.incomplete
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from adapters import responses as rb  # noqa: E402


def sse(obj):
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n").encode()


def run_stream(frames, model="deepseek-v4.1-flash"):
    out = list(rb.chat_sse_to_responses(iter(frames), model))
    events = []
    for f in out:
        assert f.endswith(b"\n\n"), f
        line = f.decode("utf-8").strip()
        assert line.startswith("data: "), line
        events.append(json.loads(line[5:]))
    return events


def main():
    # ---------- 1) 完整流：思考 + 正文 + 工具调用 + usage ----------
    mock = [
        sse({"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "Let me "},
                          "finish_reason": None}]}),
        sse({"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [{"index": 0, "delta": {"reasoning_content": "think."}, "finish_reason": None}]}),
        sse({"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}]}),
        sse({"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [{"index": 0, "delta": {"tool_calls": [
                 {"index": 0, "id": "call_abc123", "type": "function",
                  "function": {"name": "get_weather", "arguments": ""}}]}, "finish_reason": None}]}),
        sse({"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [{"index": 0, "delta": {"tool_calls": [
                 {"index": 0, "function": {"arguments": '{"city":'}}]}, "finish_reason": None}]}),
        sse({"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [{"index": 0, "delta": {"tool_calls": [
                 {"index": 0, "function": {"arguments": '"Beijing"}'}}]}, "finish_reason": None}]}),
        sse({"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
        sse({"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [],
             "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                       "completion_tokens_details": {"reasoning_tokens": 20},
                       "prompt_tokens_details": {"cached_tokens": 10}}}),
        b"data: [DONE]\n",
    ]
    events = run_stream(mock)
    print("事件序列（%d 帧）:" % len(events))
    for i, e in enumerate(events):
        print("  %2d %s" % (i, e["type"]))

    seqs = [e["sequence_number"] for e in events]
    assert seqs == list(range(len(events))), seqs          # seq 从 0 连续递增

    types = [e["type"] for e in events]
    assert types[0] == "response.created"
    assert types[1] == "response.in_progress"
    assert types[-1] == "response.completed"
    for t in ("response.output_item.added", "response.reasoning_summary_text.delta",
              "response.output_text.delta", "response.function_call_arguments.delta",
              "response.output_text.done", "response.function_call_arguments.done",
              "response.output_item.done"):
        assert t in types, t

    completed = events[-1]["response"]
    for k in ("id", "object", "created_at", "model", "status", "output"):
        assert k in completed, k
    assert completed["object"] == "response"
    assert completed["status"] == "completed"
    u = completed["usage"]
    assert (u["input_tokens"], u["output_tokens"], u["total_tokens"]) == (100, 50, 150)
    assert u["input_tokens_details"]["cached_tokens"] == 10
    assert u["output_tokens_details"]["reasoning_tokens"] == 20
    out = completed["output"]
    assert [o["type"] for o in out] == ["reasoning", "message", "function_call"], out
    fn = out[2]
    assert fn["call_id"] == "call_abc123" and fn["name"] == "get_weather"
    assert fn["arguments"] == '{"city":"Beijing"}'

    # ---------- 2) 请求侧转换 ----------
    req = {
        "model": "m", "stream": True,
        "instructions": "You are a helper.",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "weather in Beijing?"}]},
            {"type": "function_call", "call_id": "call_abc123",
             "name": "get_weather", "arguments": '{"city":"Beijing"}'},
            {"type": "function_call_output", "call_id": "call_abc123", "output": "sunny 25C"},
        ],
        "tools": [
            {"type": "function", "name": "get_weather", "description": "w",
             "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}},
            {"type": "web_search_preview"},
        ],
        "tool_choice": "auto", "temperature": 0.7, "max_output_tokens": 2048,
        "reasoning": {"effort": "medium"},
    }
    chat = rb.responses_to_chat(req)
    assert chat["stream"] is True
    assert chat["messages"][0] == {"role": "system", "content": "You are a helper."}
    assert chat["messages"][1] == {"role": "user", "content": "weather in Beijing?"}
    assert chat["messages"][2]["role"] == "assistant"
    assert chat["messages"][2]["tool_calls"][0]["id"] == "call_abc123"
    assert chat["messages"][3] == {"role": "tool", "tool_call_id": "call_abc123",
                                   "content": "sunny 25C"}
    assert len(chat["tools"]) == 1                      # web_search 被丢弃
    assert chat["tools"][0]["function"]["name"] == "get_weather"
    assert chat["max_tokens"] == 2048 and chat["temperature"] == 0.7
    assert "reasoning" not in chat

    # ---------- 3) 非流式 ----------
    nr = rb.chat_json_to_responses({
        "model": "m",
        "choices": [{"message": {"role": "assistant", "reasoning_content": "think",
                                 "content": "hi",
                                 "tool_calls": [{"id": "call_1",
                                                 "function": {"name": "f", "arguments": "{}"}}]},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8,
                  "completion_tokens_details": {"reasoning_tokens": 1},
                  "prompt_tokens_details": {"cached_tokens": 0}},
    }, "m")
    assert nr["status"] == "completed"
    assert [o["type"] for o in nr["output"]] == ["reasoning", "message", "function_call"]
    assert nr["usage"]["total_tokens"] == 8

    # ---------- 4) 上游错误帧 -> response.failed ----------
    e2 = run_stream([
        sse({"choices": [{"index": 0, "delta": {"content": "partial"}}]}),
        sse({"error": {"message": "upstream 400: bad request", "code": "X"},
             "type": "upstream_error"}),
        b"data: [DONE]\n",
    ])
    assert e2[-1]["type"] == "response.failed", e2[-1]
    assert e2[-1]["response"]["error"]["message"] == "upstream 400: bad request"

    # ---------- 5) 上游断流 -> response.incomplete ----------
    class Boom:
        def __init__(self):
            self.n = 0

        def __iter__(self):
            return self

        def __next__(self):
            if self.n == 0:
                self.n = 1
                return sse({"choices": [{"index": 0, "delta": {"content": "abc"}}]})
            raise ConnectionError("upstream reset")

    b2 = run_stream([f for f in []] or [])  # placeholder，下面直接传迭代器
    b2 = [json.loads(f.decode("utf-8").strip()[5:])
          for f in rb.chat_sse_to_responses(Boom(), "m")]
    assert b2[-1]["type"] == "response.incomplete", b2[-1]
    assert b2[-1]["response"]["incomplete_details"]["reason"] == "stream_interrupted"

    print("ALL PASS")


if __name__ == "__main__":
    main()

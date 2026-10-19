# -*- coding: utf-8 -*-
"""Offline tests for the connection-level retry + max_tokens clamp in server.py.

把 _upstream / _hmac_headers 换成假对象，因此完全不碰网络，也不写 request.log。
"""
import io
import json
import logging
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              write_through=True)
os.environ["DUMP"] = "0"          # 不落盘 dumps
os.environ["CODEARTS_BENEFIT_AUTO_CLAIM"] = "0"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import requests  # noqa: E402
import server    # noqa: E402

# ── 测试夹具 ───────────────────────────────────────────────────────────────
records = []


class Capture(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())


logging.getLogger().handlers = [Capture()]
logging.getLogger().setLevel(logging.DEBUG)

server.API_KEY = ""
server._refresh_models_if_needed = lambda: None
server.CONN_RETRY_MAX = 2
server.CONN_RETRY_BACKOFF = 0.0
server._current_creds = lambda: {"ak": "AKTEST", "sk": "SKTEST", "token": None}

slept = []
_real_sleep = server.time.sleep
server.time.sleep = lambda s: slept.append(s)

SSE_OK = [
    b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"GLM-5.2",'
    b'"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}',
    b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"GLM-5.2",'
    b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
    b"data: [DONE]",
]


class FakeResp:
    def __init__(self, status=200, lines=None, text="", headers=None):
        self.status_code = status
        self.headers = headers or {"Content-Type": "text/event-stream"}
        self._lines = lines if lines is not None else SSE_OK
        self.text = text

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)

    def json(self):
        if self.text.startswith("{"):
            return json.loads(self.text)
        raise ValueError("not json")

    def close(self):
        pass


class HeartbeatResp:
    """并发闸门心跳的响应：{"status":"ok"} 才算登记成功。"""

    status_code = 200

    @staticmethod
    def json():
        return {"status": "ok"}


class FakeUpstream:
    """script 里放异常或 FakeResp；最后一项会一直重复。

    心跳（PUT /snap-manager/.../heartbeat）与对话（POST /api/v2/chat/...）
    分成两条队列：并发闸门默认开着，进来会先打一次 busy 心跳。混在一条
    队列里，心跳会先消费掉 script 的首项，对话看到的就不是预期的那次失败了。
    """

    def __init__(self, script):
        self.script = list(script)
        self.posts = []
        self.requests = []
        self.heartbeats = []

    def _next(self):
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def post(self, url, data=None, headers=None, stream=False, timeout=None):
        self.posts.append({"data": data, "headers": dict(headers or {})})
        return self._next()

    def request(self, method, url, data=None, headers=None, timeout=None):
        self.requests.append({"method": method, "headers": dict(headers or {})})
        if "heartbeat" in url:
            self.heartbeats.append({"method": method, "url": url,
                                    "headers": dict(headers or {})})
            return HeartbeatResp()
        return self._next()


sign_calls = []


def fake_hmac_headers(body, benefit=False, model_id="", session_id=""):
    sign_calls.append(model_id)
    return {"x-sdk-date": "20260101T000000Z-%d" % len(sign_calls),
            "Authorization": "SDK-HMAC-SHA256 stub"}


server._hmac_headers = fake_hmac_headers

CLIENT = server.app.test_client()
PAYLOAD = {"model": "GLM-5.2",
           "messages": [{"role": "user", "content": "hi"}],
           "stream": True, "max_tokens": 131072}

SSL_ERR = requests.exceptions.SSLError(
    "HTTPSConnectionPool(host='snap-access.cn-north-4.myhuaweicloud.com', port=443): "
    "Max retries exceeded with url: /api/v2/chat/completions (Caused by "
    "SSLError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING]')))")
READ_TIMEOUT = requests.exceptions.ReadTimeout("read timed out")
CONN_ERR = requests.exceptions.ConnectionError("Connection aborted, RemoteDisconnected")

passed, failed = [], []


def check(name, condition, detail=""):
    (passed if condition else failed).append(name)
    print("%-52s %s %s" % (name, "PASS" if condition else "FAIL", detail))


def call_chat(upstream):
    sign_calls.clear()
    slept.clear()
    server._upstream = upstream
    resp = CLIENT.post("/v1/chat/completions", json=PAYLOAD)
    body = resp.get_data(as_text=True)
    return resp, body


print("=== T1 首打 TLS 中断，重发后成功 ===")
u = FakeUpstream([SSL_ERR, FakeResp()])
resp, body = call_chat(u)
check("T1 上游打了 2 次", len(u.posts) == 2, "posts=%d" % len(u.posts))
check("T1 每次重发都重新签名", len(sign_calls) == 2, "sign=%d" % len(sign_calls))
check("T1 客户端拿到正常流", resp.status_code == 200 and '"content":"hi"' in body)
check("T1 max_tokens 被钳到 65536",
      json.loads(u.posts[0]["data"])["max_tokens"] == 65536,
      "sent=%s" % json.loads(u.posts[0]["data"])["max_tokens"])
check("T1 有重试 WARNING",
      any("连接级失败" in m for m in records),
      [m for m in records if "重试" in m][:1])

print("\n=== T2 一直中断，耗尽后返回 502 ===")
records.clear()
u = FakeUpstream([SSL_ERR])
resp, body = call_chat(u)
check("T2 上游打了 3 次(1+2 重试)", len(u.posts) == 3, "posts=%d" % len(u.posts))
check("T2 客户端收到 502", resp.status_code == 502, body[:90])
check("T2 不静默吞错", "upstream error" in body)

print("\n=== T3 读超时不重发（避免重复计费）===")
u = FakeUpstream([READ_TIMEOUT])
resp, body = call_chat(u)
check("T3 只打了 1 次", len(u.posts) == 1, "posts=%d" % len(u.posts))
check("T3 返回 502", resp.status_code == 502)

print("\n=== T4 连接被重置也重发 ===")
u = FakeUpstream([CONN_ERR, FakeResp()])
resp, body = call_chat(u)
check("T4 上游打了 2 次", len(u.posts) == 2, "posts=%d" % len(u.posts))
check("T4 客户端拿到正常流", resp.status_code == 200 and '"content":"hi"' in body)

print("\n=== T5 正常路径不变（一次成功不重试）===")
u = FakeUpstream([FakeResp()])
resp, body = call_chat(u)
check("T5 只打 1 次", len(u.posts) == 1, "posts=%d" % len(u.posts))
check("T5 只签 1 次", len(sign_calls) == 1, "sign=%d" % len(sign_calls))
check("T5 流正常", resp.status_code == 200 and body.rstrip().endswith("data: [DONE]"))

print("\n=== T6 上游返回非 200 业务错误仍走原退避分支 ===")
u = FakeUpstream([FakeResp(status=400, lines=[],
                            text='{"error_code":"TM.00001041","error_msg":"并发会话数已达上限(3个)"}')])
resp, body = call_chat(u)
check("T6 限流后重试并最终 502", len(u.posts) == 4, "posts=%d (1+3 退避)" % len(u.posts))
check("T6 透传上游状态", resp.status_code == 502 and "TM.00001041" in body)

print("\n=== T7 _signed_request（模型同步/福利领取）连接级重发 ===")
# 用假时钟推进时间，验证每次重发都重新计算 x-sdk-date 与签名
import datetime as _real_dt


class FakeDateTime(_real_dt.datetime):
    _tick = 0

    @classmethod
    def now(cls, tz=None):
        cls._tick += 1
        return _real_dt.datetime(2026, 10, 12, 12, 0, cls._tick % 60, tzinfo=tz)


class FakeDatetimeModule:
    datetime = FakeDateTime
    timezone = _real_dt.timezone


server.datetime = FakeDatetimeModule
server._upstream = FakeUpstream([SSL_ERR, FakeResp()])
r = server._signed_request("GET", server.OPENGW_CONFIG_URL)
check("T7 打了 2 次", len(server._upstream.requests) == 2,
      "calls=%d" % len(server._upstream.requests))
check("T7 两次时间戳不同（重新签名）",
      server._upstream.requests[0]["headers"]["x-sdk-date"]
      != server._upstream.requests[1]["headers"]["x-sdk-date"],
      "%s vs %s" % (server._upstream.requests[0]["headers"]["x-sdk-date"],
                    server._upstream.requests[1]["headers"]["x-sdk-date"]))
check("T7 签名值随时间戳变化",
      server._upstream.requests[0]["headers"]["authorization"]
      != server._upstream.requests[1]["headers"]["authorization"])

server.time.sleep = _real_sleep

print("\n=== T8 规范请求串没漂移（x-sdk-date 必须进 SignedHeaders）===")
import hashlib
import hmac as _hmac
import urllib.parse


class FixedDateTime(_real_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return _real_dt.datetime(2026, 10, 12, 12, 0, 0, tzinfo=tz)


class FixedDatetimeModule:
    datetime = FixedDateTime
    timezone = _real_dt.timezone


server.datetime = FixedDatetimeModule
server._current_creds = lambda: {"ak": "AKTEST", "sk": "SKTEST", "token": "TOKEN123"}


def reference_authorization(method, url, body, signed):
    """独立按 SDK-HMAC-SHA256 规范算一遍，作为 _signed_request 的对照。

    规范串以 tools/verify_real.py 对真实抓包验证过的形式为准：
    CanonicalRequest = method \\n uri \\n query \\n canonicalHeaders \\n signedHeaders \\n payloadHash
    """
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"
    canonical_uri = path if path.endswith("/") else path + "/"
    params = sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    enc = lambda v: urllib.parse.quote(str(v), safe="-_.~")
    canonical_query = "&".join(f"{enc(k)}={enc(v)}" for k, v in params)
    names = sorted(signed)
    canonical_headers = "".join(f"{n}:{signed[n].strip()}\n" for n in names)
    signed_headers = ";".join(names)
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_request = "\n".join(
        [method, canonical_uri, canonical_query, canonical_headers,
         signed_headers, payload_hash])
    string_to_sign = "\n".join(
        ["SDK-HMAC-SHA256", signed["x-sdk-date"],
         hashlib.sha256(canonical_request.encode()).hexdigest()])
    signature = _hmac.new(b"SKTEST", string_to_sign.encode(), hashlib.sha256).hexdigest()
    return (f"SDK-HMAC-SHA256 Access=AKTEST, SignedHeaders={signed_headers}, "
            f"Signature={signature}"), signed_headers


body = b'{"model":"GLM-5.2","messages":[]}'
u = FakeUpstream([FakeResp()])
server._upstream = u
server._signed_request("POST", server.TARGET, body=body,
                       extra_headers={"Agent-Type": "AgentCenter"})
sent = u.requests[-1]["headers"]
signed_subset = {k.lower(): v for k, v in sent.items()
                 if k.lower() in ("host", "content-type", "x-sdk-date",
                                  "x-security-token", "agent-type")}
expected, expected_signed_headers = reference_authorization(
    "POST", server.TARGET, body, signed_subset)
got_auth = sent.get("authorization") or sent.get("Authorization")
check("T8 x-sdk-date 在 SignedHeaders 内",
      "x-sdk-date" in (got_auth.split("SignedHeaders=")[1].split(",")[0]),
      got_auth.split("SignedHeaders=")[1].split(",")[0])
check("T8 x-security-token 在 SignedHeaders 内",
      "x-security-token" in got_auth.split("SignedHeaders=")[1].split(",")[0])
check("T8 签名与独立实现的规范串一致", got_auth == expected,
      "" if got_auth == expected else "got=%s\n      exp=%s" % (got_auth, expected))

print("\n=== T9 流正文被掐断：客户端要拿到错误帧，而不是无声断流 ===")
BREAK = requests.exceptions.ConnectionError(
    "('Connection aborted.', ConnectionResetError(10054, '远程主机强迫关闭了一个现有的连接。'))")


class BreakingResp(FakeResp):
    """seq 里的异常对象在迭代到它时抛出（模拟 iter_lines 读正文时连接断）。"""

    def __init__(self, seq, headers=None):
        FakeResp.__init__(self, lines=[], headers=headers)
        self._seq = seq

    def iter_lines(self, decode_unicode=False):
        for item in self._seq:
            if isinstance(item, BaseException):
                raise item
            yield item


def drain(resp):
    parts, raised = [], None
    try:
        for piece in resp.response:
            parts.append(piece if isinstance(piece, bytes) else piece.encode())
    except BaseException as exc:
        raised = type(exc).__name__
    return b"".join(parts).decode("utf-8", "replace"), raised


records.clear()
server._upstream = FakeUpstream([BreakingResp([SSE_OK[0], SSE_OK[1], BREAK])])
text, raised = drain(CLIENT.post("/v1/chat/completions", json=PAYLOAD))
check("T9 断流前的内容已透传", '"content":"hi"' in text)
check("T9 客户端收到错误帧", "upstream stream interrupted" in text and "ConnectionError" in text,
      text[-120:])
check("T9 错误帧后补 [DONE]", text.rstrip().endswith("data: [DONE]"))
check("T9 异常继续上抛（保留 done 日志与堆栈）", raised == "ConnectionError", raised)
check("T9 _stream_log 记了 stream 中断", any("stream 中断" in m for m in records))

print("\n=== T10 benefit 流中断不得伪装成正常收尾 ===")
records.clear()
benefit_payload = dict(PAYLOAD, model="deepseek-v4-flash-0731")
server._upstream = FakeUpstream([BreakingResp([SSE_OK[0], BREAK])])
text, raised = drain(CLIENT.post("/v1/chat/completions", json=benefit_payload))
check("T10 没有补 finish_reason=stop", '"finish_reason":"stop"' not in text)
check("T10 收到错误帧", "upstream stream interrupted" in text)

print("\n=== T11 非流式读正文中断 → 502 JSON（不是 500 HTML）===")


class ContentBoomResp(FakeResp):
    def __init__(self, error, headers=None):
        FakeResp.__init__(self, lines=[],
                          headers=headers or {"Content-Type": "application/json"})
        self._error = error

    @property
    def content(self):
        raise self._error


non_stream = dict(PAYLOAD, stream=False)
server._upstream = FakeUpstream([ContentBoomResp(BREAK)])
resp = CLIENT.post("/v1/chat/completions", json=non_stream)
body = resp.get_data(as_text=True)
check("T11 返回 502", resp.status_code == 502, "status=%s" % resp.status_code)
check("T11 是 JSON 且带原因", resp.is_json and "upstream stream interrupted" in body,
      body[:120])

print("\n=== T12 客户端断开（GeneratorExit）不能被当成上游中断 ===")
long_stream = FakeResp(lines=SSE_OK[:1] * 40)
server._upstream = FakeUpstream([long_stream])
records.clear()
resp = CLIENT.post("/v1/chat/completions", json=PAYLOAD)
iterator = resp.response.__iter__()
next(iterator)
try:
    iterator.close()          # 模拟客户端半路断开
    closed_cleanly = True
except BaseException as exc:  # pragma: no cover
    closed_cleanly = False
    print("   close() 抛了:", type(exc).__name__)
check("T12 close() 干净结束", closed_cleanly)
check("T12 记为 client 断开", any("client 断开" in m for m in records),
      [m for m in records if "断开" in m][:1])
check("T12 未误判为 stream 中断", not any("stream 中断" in m for m in records))

print("\n通过 %d / %d" % (len(passed), len(passed) + len(failed)))
if failed:
    print("失败:", failed)
    sys.exit(1)

# -*- coding: utf-8 -*-
"""lanes/session.py 离线测试：打桩心跳发送器与闸门，不连真实上游。

覆盖参考实现（Agent2API/agent2api session.rs）的五条规则 + 本地闸门语义：
  1. 周期 busy 续期失败**不释放槽位**
  2. 首次 busy 非 200 仍要注销自己造的那个 id
  3. 只注销自己造的 id（自造 32-hex，不用 IDE 的）
  4. idle 只发一次，且在最后一个在途 busy 之后
  5. 本地准入满员 → 409（不是 429）
外加：正文读完/连接关闭才还槽、客户端中断流也还槽、闸门 key 跨 AK 稳定。
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              write_through=True)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from lanes import session  # noqa: E402

passed, failed = [], []


def check(name, condition, detail=""):
    (passed if condition else failed).append(name)
    print("%-56s %s %s" % (name, "PASS" if condition else "FAIL", detail))


class Recorder:
    """记录每一次心跳 (status, session_id)，可按脚本决定返回值。"""

    def __init__(self, script=None):
        self.calls = []
        self.script = script or {}

    def __call__(self, status, session_id):
        self.calls.append((status, session_id))
        plan = self.script.get(status)
        if plan is None:
            plan = self.script.get("*", (200, {"status": "ok"}))
        code, body = plan

        class R:
            status_code = code

            @staticmethod
            def json():
                return body
        return R()

    @property
    def statuses(self):
        return [s for s, _ in self.calls]

    def same_id(self):
        return len(set(sid for _, sid in self.calls)) == 1


class FakeTimer:
    """替掉 threading.Timer：不真的等 30 秒，由测试手动 fire。

    cancel 要真的把自己从 pending 里摘掉——否则「end 后不再排新 timer」
    这类断言测到的是假件而不是被测代码。
    """

    def __init__(self, clock, fn):
        self._clock = clock
        self._fn = fn

    def cancel(self):
        if self in self._clock.pending:
            self._clock.pending.remove(self)

    def start(self):
        # 真实 Timer 是 start() 之后才开始跑；这里让 pending 反映「已启动」，
        # cancel() 才有东西可摘，end() 停没停续期才测得出来。
        self._clock.pending.append(self)

    def fire(self):
        self._fn()


class Clock:
    """手动时钟。fire_once() 只触发当前排着的那一轮续期。

    不能一次性把 pending 抽干再断言「还排着没」——_renew 是在函数末尾才调
    _schedule()，抽干时新的还没排进去，会看到假的空。
    """

    def __init__(self):
        self.pending = []

    def fire_once(self):
        pending, self.pending = self.pending, []
        for timer in pending:
            timer.fire()
        return len(pending)

    def fire(self):
        """连续触发到不再排新 timer（用于把续期全部跑完的场景）。"""
        for _ in range(100):
            if not self.fire_once():
                return
        raise AssertionError("续期没有收敛，疑似无限自排")


def make(script=None, **kw):
    """造一个 ChatSession，配 FakeClock。注意 Timer 的打桩要一直有效到
    fire()——所以不能在 make() 里恢复，由调用方用完自己 restore。"""
    rec = Recorder(script)
    clock = Clock()
    session.threading.Timer = lambda interval, fn: FakeTimer(clock, fn)
    chat = session.ChatSession(send=rec, **kw)
    return chat, rec, clock


_orig_timer = session.threading.Timer


def restore():
    session.threading.Timer = _orig_timer


# ── 规则 2：首次 busy 非 200 仍要注销自己造的 id ────────────────────────
chat, rec, clock = make({"busy": (500, {"status": "error"})})
chat.begin()
check("busy 失败时紧跟一次 idle 注销", rec.statuses == ["busy", "idle"], rec.statuses)

chat, rec, clock = make({"busy": (200, {"status": "busy"})})   # 200 但不是 ok
chat.begin()
check("200 但 status!=ok 同样判失败并注销", rec.statuses == ["busy", "idle"], rec.statuses)

chat, rec, clock = make({"busy": (200, {"status": "ok"})})
chat.begin()
check("busy 成功不立刻 idle", rec.statuses == ["busy"], rec.statuses)

# ── 规则 3：只注销自己造的 id ────────────────────────────────────────────
chat, rec, clock = make()
sid = chat.session_id
check("session id 是 32 位十六进制", len(sid) == 32 and all(c in "0123456789abcdef" for c in sid), sid[:12] + "…")
chat.begin()
chat.end()
check("全程只用一个自造 id", rec.same_id() and rec.calls[0][1] == sid)
chat.end()
chat.end()
check("重复 end() 是空操作", rec.statuses == ["busy", "idle"], rec.statuses)

# ── 规则 1：周期 busy 续期失败不释放槽位 ──────────────────────────────────
# 首次 busy 成功，之后所有 busy 一律失败
rec = Recorder()
clock = Clock()
seq = {"n": 0}


def scripted(status, session_id):
    rec.calls.append((status, session_id))
    seq["n"] += 1
    code, body = (200, {"status": "ok"}) if seq["n"] == 1 else (503, {"status": "error"})

    class R:
        status_code = code

        @staticmethod
        def json():
            return body
    return R()


session.threading.Timer = lambda interval, fn: FakeTimer(clock, fn)
chat = session.ChatSession(send=scripted)
chat.begin()
clock.fire_once()   # 续期 #1 失败
clock.fire_once()   # 续期 #2 失败
clock.fire_once()   # 续期 #3 失败
check("续期失败不提前 idle", "idle" not in rec.statuses, rec.statuses)
check("续期失败仍继续排下一次", len(clock.pending) > 0, "pending=%d" % len(clock.pending))
chat.end()
check("收尾时补上 idle", rec.statuses[-1] == "idle", rec.statuses)
check("busy 成功 1 次 + 续期 3 次，idle 只 1 次",
      rec.statuses.count("busy") == 4 and rec.statuses.count("idle") == 1, rec.statuses)

# ── 规则 4：idle 只发一次，且在最后一个在途 busy 之后 ────────────────────
chat, rec, clock = make()
chat.begin()
clock.fire_once()   # 在途 busy #2
chat.end()
check("end 只发一次 idle", rec.statuses.count("idle") == 1, rec.statuses)
check("busy→busy→idle 严格有序", rec.statuses == ["busy", "busy", "idle"], rec.statuses)
check("end 后续期线程已停（不再排新 timer）", clock.pending == [], clock.pending)

# end() 之后即使 timer 还被打中（极端竞态），也绝不能再发出 busy
chat, rec, clock = make()
chat.begin()
chat.end()
clock.fire_once()   # 试图续期一个已结束的会话
check("end 后的迟到续期不再发 busy", "busy" not in rec.statuses[2:], rec.statuses)
check("end 后迟到续期也不发 idle（只发一次）", rec.statuses.count("idle") == 1, rec.statuses)

# idle 之后绝不允许再出现 busy（否则槽位泄漏）
chat, rec, clock = make()
chat.begin()
clock.fire_once()
chat.end()
clock.fire_once()
check("idle 之后再无 busy", "busy" not in rec.statuses[rec.statuses.index("idle") + 1:], rec.statuses)

# ── 规则 5：本地准入满员 → 409 ───────────────────────────────────────────
gate = session.SessionGate(limit=3)
keys = [session.SessionGate.key_for("https://up/x", "acct-A") for _ in range(3)]
got = [gate.acquire(keys[0]) for _ in range(3)]
check("前 3 个请求都能进", all(got))
check("第 4 个被本地闸门挡下", gate.acquire(keys[0]) is False)
check("满员时 in_use == limit", gate.in_use(keys[0]) == 3, gate.in_use(keys[0]))
gate.release(keys[0])
check("还一个槽后能再进", gate.acquire(keys[0]) is True)
check("闸门状态码是 409 而非 429", session.GATE_STATUS == 409, session.GATE_STATUS)

# 闸门 key：换账号是另一个门口，不互相抢额度
kA = session.SessionGate.key_for("https://up/x", "acct-A")
kB = session.SessionGate.key_for("https://up/x", "acct-B")
kOther = session.SessionGate.key_for("https://other/x", "acct-A")
check("不同账号 key 不同", kA != kB)
check("不同 base_url key 不同", kA != kOther)
check("同账号 key 稳定（跨 AK 轮换）",
      kA == session.SessionGate.key_for("https://up/x", "acct-A"))
check("key 是 sha256 十六进制", len(kA) == 64)

# 满员不阻断：denied Slot 的 release 是空操作
gate_full = session.SessionGate(limit=1)
s_ok = session.acquire("https://up/x", "acct-A", gate=gate_full)
check("闸门为 1 时首个请求进得去", s_ok.ok is True)
s2 = session.acquire("https://up/x", "acct-A", gate=gate_full)
check("满员时 denied 有原因文本", s2.denied and "并发" in s2.denied, s2.denied)
check("denied 的 Slot 不可用", s2.ok is False)
check("denied Slot 不带 session（本就不该发上游）", s2.session_id == "")
before = gate_full.in_use(s_ok.key)
s2.release()   # 不能抛，也不能把别人的计数减错
check("denied Slot release 无副作用", gate_full.in_use(s_ok.key) == before == 1,
      gate_full.in_use(s_ok.key))

# ── 心跳整体关闭时仍发 user-session-id ──────────────────────────────────
orig_on = session.HEARTBEAT_ON
try:
    session.HEARTBEAT_ON = False
    gate3 = session.SessionGate(limit=3)
    slot = session.acquire("https://up/x", "acct-A", gate=gate3)
    check("关掉心跳仍给出 session_id", len(slot.session_id) == 32)
    check("关掉心跳仍占闸门槽", gate3.in_use(slot.key) == 1)
    check("关掉心跳无 ChatSession", slot._session is None)
    slot.release()
    check("关掉心跳 release 仍还槽", gate3.in_use(slot.key) == 0)
finally:
    session.HEARTBEAT_ON = orig_on

# ── TrackedResponse：正文读完 / 连接关闭 / 客户端中断才还槽 ──────────────
class FakeResp:
    def __init__(self, lines=None, content=b"body"):
        self._lines = lines if lines is not None else [b"a", b"b"]
        self._content = content
        self.closed = False

    @property
    def content(self):
        return self._content

    @property
    def text(self):
        return self._content.decode()

    def json(self):
        import json
        return json.loads(self._content)

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            yield line

    def close(self):
        self.closed = True


def slot_for(gate, key="k"):
    gate.acquire(key)
    return session.Slot(session_id="s" * 32, key=key, gate=gate)


gate = session.SessionGate(limit=3)
slot = slot_for(gate)
tr = session.TrackedResponse(FakeResp(), slot)
check("还槽前槽位仍占用", gate.in_use("k") == 1)
_ = tr.content
check("读 content 后还槽", gate.in_use("k") == 0)

gate = session.SessionGate(limit=3)
slot = slot_for(gate)
tr = session.TrackedResponse(FakeResp(), slot)
_ = tr.text
check("读 text 后还槽", gate.in_use("k") == 0)

gate = session.SessionGate(limit=3)
slot = slot_for(gate)
tr = session.TrackedResponse(FakeResp(content=b'{"a":1}'), slot)
_ = tr.json()
check("json() 后还槽", gate.in_use("k") == 0)

gate = session.SessionGate(limit=3)
slot = slot_for(gate)
tr = session.TrackedResponse(FakeResp(), slot)
seen = [line for line in tr.iter_lines()]
check("iter_lines 内容透传", seen == [b"a", b"b"], seen)
check("iter_lines 耗尽后还槽", gate.in_use("k") == 0)

gate = session.SessionGate(limit=3)
slot = slot_for(gate)
resp = FakeResp()
tr = session.TrackedResponse(resp, slot)
it = tr.iter_lines()
next(it)
it.close()      # 客户端中断
check("流被中断也还槽", gate.in_use("k") == 0)

gate = session.SessionGate(limit=3)
slot = slot_for(gate)
resp = FakeResp()
tr = session.TrackedResponse(resp, slot)
tr.close()
check("close 透传给真响应", resp.closed is True)
check("close 后还槽", gate.in_use("k") == 0)

# 重复还槽：TrackedResponse 出口可能被调多次（content 后 close）
gate = session.SessionGate(limit=3)
slot = slot_for(gate)
resp = FakeResp()
tr = session.TrackedResponse(resp, slot)
_ = tr.content
tr.close()
check("重复还槽不把计数减成负数", gate.in_use("k") == 0)
gate.acquire("other")
check("计数不为负（acquire 仍正常）", gate.in_use("other") == 1)

# 透传：status_code / headers 不经过上面五个出口也能拿到
gate = session.SessionGate(limit=3)
slot = slot_for(gate)
resp = FakeResp()
resp.status_code = 200
resp.headers = {"Content-Type": "text/event-stream"}
tr = session.TrackedResponse(resp, slot)
check("status_code 透传", tr.status_code == 200)
check("headers 透传", tr.headers["Content-Type"] == "text/event-stream")
check("透传读字段不还槽（流还没开始）", gate.in_use("k") == 1)

# 读正文时连接被掐：异常路径也必须还槽。漏一个槽就少一路并发，攒够
# limit 个之后服务自己把自己限死，而表面上只是「偶尔 409 变多」。
class BoomResp:
    status_code = 200
    headers = {"Content-Type": "application/json"}

    @property
    def content(self):
        raise ConnectionError("read aborted")

    def iter_lines(self, decode_unicode=False):
        raise ConnectionError("stream broken")

    def close(self):
        pass


gate = session.SessionGate(limit=3)
slot = slot_for(gate)
tr = session.TrackedResponse(BoomResp(), slot)
try:
    tr.content
except ConnectionError:
    pass
check("读 content 抛异常也还槽", gate.in_use("k") == 0, gate.in_use("k"))

gate = session.SessionGate(limit=3)
slot = slot_for(gate)
tr = session.TrackedResponse(BoomResp(), slot)
try:
    list(tr.iter_lines())
except ConnectionError:
    pass
check("迭代中抛异常也还槽", gate.in_use("k") == 0, gate.in_use("k"))

# 槽位归零即删 key：长时间跑不会堆死 key
gate = session.SessionGate(limit=3)
gate.acquire("tmp")
gate.release("tmp")
gate.release("tmp")   # 多还一次
check("key 归零即删", "tmp" not in gate._counts, list(gate._counts))

print()
print("通过 %d / %d" % (len(passed), len(passed) + len(failed)))
if failed:
    print("失败：")
    for name in failed:
        print("  - " + name)
    sys.exit(1)

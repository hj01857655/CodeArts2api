# -*- coding: utf-8 -*-
"""lanes/session.py — CodeArts 并发闸门：user-session-id + heartbeat busy/idle。

上游（snap-access）对每个账号只放 3 路并发对话，放不下时报
TM.00001041（HTTP 400）。这个闸门**不认「请求结束」**，只认

    PUT /snap-manager/v1/chat-session/heartbeat?status=busy|idle

（IDE 抓包 + 参考实现 Agent2API/agent2api 的 session.rs 双向确认）。

我们以前既不发心跳、请求里也不带 user-session-id，于是每个请求都让上游
**隐式开一个会话且永不注销**：3 个槽很快占满，之后一律 TM.00001041，
而我们的退避重试等的是别人释放，上游只肯按超时回收，于是白等 12s 回 502。

本模块提供两件东西：

  ChatSession   一次上游请求的生命周期：发请求前 busy，流式期间每 30s
                续期，收尾时 idle。session id **由我们自造**（UUIDv4 去
                连字符，32 位十六进制），绝不复用 IDE 的——idle 是注销
                语义，拿 IDE 的 id 去 idle 会把用户正在用的会话杀掉。

  SessionGate   本地准入闸：按账号计在途请求数，满员直接让调用方回 409。
                撞上游才知道满，客户端要多等一整轮退避才拿到 502；且必须
                回 409 而不是 429——429 会被客户端当成额度耗尽去长冷却，
                而这个条件几十毫秒内就会自己消失。

闸门 key 是 sha256(base_url + "\\n" + account_id)。**不能用 AK**：AK 每次
续期都换，拿它当 key 会让「同一账号」每刷一次期就变成一个新闸门口，3 路
上限被悄悄重置，等于没限。account_id 由 credential_provider 给
（auths/*.json 用文件名，IDE 会话用 userId/userName，都与 AK 轮换无关）。

参考实现的五条规则，本模块逐条落实（对应处有注释）：
  1. 周期 busy 续期失败**不释放槽位**
  2. 首次 busy 非 200 仍要注销自己造的那个 id
  3. 只注销自己造的 id
  4. idle 只发一次，且在最后一个在途 busy 落地之后
  5. 本地准入满员回 409
"""
import contextlib
import hashlib
import os
import threading
import time
import uuid

HEARTBEAT_PATH = "/snap-manager/v1/chat-session/heartbeat"
HEARTBEAT_INTERVAL = float(os.getenv("CODEARTS_HEARTBEAT_INTERVAL", "30"))
# 连续这么多次续期不上就 escalate 一次日志：上游大概率已把这个 session
# 回收，槽位悬空。此时无法强制替上游注销（id 是它认的，不是我们强插的），
# 只能把现象暴露出来——真出现 TM.00001041 时这条日志就是原因。
HEARTBEAT_MISS_LIMIT = 3
DEFAULT_SESSION_LIMIT = 3
SESSION_LIMIT = max(1, int(os.getenv("CODEARTS_SESSION_LIMIT",
                                     str(DEFAULT_SESSION_LIMIT))))
# 心跳整体开关。关掉后仍然发 user-session-id（那才是把请求绑到会话上的
# 关键），只是不主动 busy/idle——上游改接口时留的后路。
HEARTBEAT_ON = os.getenv("CODEARTS_SESSION_HEARTBEAT", "1").strip() not in (
    "0", "false", "False", "no", "")
# 本地闸门满员时的 HTTP 状态。429 会被客户端当成配额耗尽进入长冷却，
# 而这个条件几十毫秒内就会自己消失，409 才是「稍后再试」的正确语义。
GATE_STATUS = 409


def new_session_id() -> str:
    """自造 32 位十六进制 session id（对齐 IDE 的形状：UUIDv4 去连字符）。"""
    return uuid.uuid4().hex


def _accepted(resp) -> bool:
    """上游 200 且 body 为 {"status":"ok"} 才算登记成功。

    只判状态码不够：网关层 200 可以包着业务失败，把这种当成成功会让槽位
    从来没被登记过，我们却以为限流已经解除。
    """
    if resp is None or getattr(resp, "status_code", 0) != 200:
        return False
    try:
        return (resp.json() or {}).get("status") == "ok"
    except Exception:
        return False


def _default_send(status: str, session_id: str):
    """默认心跳发送器：走 server 的通用签名请求。

    用 _signed_request 而不是 _hmac_headers：后者的 canonical_uri 写死成
    /api/v2/chat/completions/，签不了心跳这条路径；而 _signed_request 按
    URL 现算，且 host 自动进签名头——正好符合上游「心跳签名要带 Host」的
    要求（区域 API 反而不带，两者不能混用一套签名函数）。

    延迟 import：server 在模块层 import 本模块，顶层互相 import 会成环。
    """
    import server
    return server._signed_request(
        "PUT", "%s%s?status=%s" % (server.BASE_URL, HEARTBEAT_PATH, status),
        b"{}",
        extra_headers={
            "accept": "application/json",
            "x-language": "zh-cn",
            "x-client-type": "kernel",
            "user-session-id": session_id,
            "x-snap-traceid": uuid.uuid4().hex,
        },
        timeout=30,
    )


class ChatSession:
    """一次上游请求的心跳会话：busy →（每 30s 续期）→ idle。

    线程安全：续期跑在 threading.Timer 链上，与请求线程的 end() 并发。
    """

    def __init__(self, session_id=None, send=None, logger=None, rid="",
                 interval=HEARTBEAT_INTERVAL):
        self.session_id = session_id or new_session_id()
        self._send = send or _default_send
        self._log = logger
        # rid 只为把心跳日志归到具体请求上（续期线程里拿不到 flask.g）。
        # 心跳是后台行为，请求已结束时它可能还在跑，因此 rid 缺失不报警。
        self._rid = rid
        self._interval = interval
        self._lock = threading.Lock()
        # 心跳之间也要串行：否则 end() 的 idle 可能插到在途 busy 之前，
        # 变成「先注销后登记」，槽位又悬空一个（规则 4）。
        self._io_lock = threading.Lock()
        self._busy = 0          # 已成功的 busy 次数；>0 才需要 idle 收尾
        self._misses = 0
        self._closing = False
        self._timer = None

    # ── 内部 ──────────────────────────────────────────────────────────────
    def _prefix(self):
        """日志前缀。[session] 段让多路并发时能区分是哪一条会话。"""
        rid = ("[%s] " % self._rid) if self._rid else ""
        return rid + "[session %s] " % self.session_id[:8]

    def _warn(self, fmt, *args):
        if self._log is not None:
            self._log.warning(self._prefix() + fmt, *args)

    def _error(self, fmt, *args):
        if self._log is not None:
            self._log.error(self._prefix() + fmt, *args)

    def _post(self, status) -> bool:
        """发一次心跳。心跳之间串行（_io_lock），否则 end() 的 idle 可能插到
        在途 busy 之前，变成「先注销后登记」，槽位又悬空一个（规则 4）。

        _closing 的判断必须放在 _io_lock **里面**：放在外面的话，end() 已经
        把 _closing 置上、锁也拿到了，一个刚要发 busy 的 renew 会照发不误，
        于是 idle 之后又来个 busy——上游把这个 session 重新登记，而我们的
        会话已经结束，没人再注销它，槽位就此泄漏。
        """
        with self._io_lock:
            if self._closing and status == "busy":
                return False
            try:
                resp = self._send(status, self.session_id)
            except Exception as error:
                self._warn("heartbeat %s 异常: %s: %s", status,
                           type(error).__name__, error)
                return False
            if not _accepted(resp):
                self._warn("heartbeat %s 未被接受: HTTP %s", status,
                           getattr(resp, "status_code", "?"))
                return False
            return True

    def _schedule(self):
        """排下一次续期（daemon 线程，进程退出不阻塞）。"""
        with self._lock:
            if self._closing:
                return
            if self._timer is not None:
                self._timer.cancel()
            timer = threading.Timer(self._interval, self._renew)
            timer.daemon = True
            self._timer = timer
        timer.start()

    def _renew(self):
        """周期续期。

        失败**不释放槽位**（规则 1）：一次网络抖动不该把在途请求废掉，
        续不上时上游通常仍会继续受理（只是可能被它自己的空闲超时回收），
        而收尾时照常 idle。这里只把连续失败记下来 escalate 日志。
        """
        with self._lock:
            if self._closing:
                return
        ok = self._post("busy")
        with self._lock:
            if self._closing:
                return
            if ok:
                self._busy += 1
                self._misses = 0
            else:
                self._misses += 1
                if self._misses >= HEARTBEAT_MISS_LIMIT:
                    misses, self._misses = self._misses, 0
                    self._error("连续 %d 次续期失败，上游可能已回收该 session；"
                                "若随后出现 TM.00001041 即此因", misses)
        self._schedule()

    # ── 外部 ──────────────────────────────────────────────────────────────
    def begin(self) -> bool:
        """发请求前登记 busy。失败返回 False，但**不阻塞请求**。

        失败仍要注销自己造的这个 id（规则 2）：上游可能已把它记成半开
        会话，不注销就白占一个槽。注销也失败就只能算了——那是上游侧的
        事，本地闸门照样兜底。
        """
        if not self._post("busy"):
            self._post("idle")
            return False
        with self._lock:
            self._busy += 1
            self._misses = 0
        self._schedule()
        return True

    def end(self):
        """收尾：停续期线程，发且只发一次 idle。

        放在 finally 里调用，重复调用是空操作。idle 由 _io_lock 保证落在
        最后一个在途 busy 之后（规则 4）。
        """
        with self._lock:
            self._closing = True
            timer, self._timer = self._timer, None
            need_idle = self._busy > 0
            self._busy = 0
        if timer is not None:
            timer.cancel()
        if need_idle:
            self._post("idle")


class SessionGate:
    """本地准入闸：按账号计在途上游请求数。"""

    def __init__(self, limit=DEFAULT_SESSION_LIMIT):
        self.limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._counts = {}

    @staticmethod
    def key_for(base_url: str, account_id: str) -> str:
        """闸门 key。换 base_url 或换账号都是另一个门口，不会互相抢额度。"""
        raw = "%s\n%s" % (base_url or "", account_id or "")
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def in_use(self, key: str) -> int:
        with self._lock:
            return self._counts.get(key, 0)

    def acquire(self, key: str) -> bool:
        """占一个槽；满员返回 False（调用方应回 409）。"""
        with self._lock:
            used = self._counts.get(key, 0)
            if used >= self.limit:
                return False
            self._counts[key] = used + 1
            return True

    def release(self, key: str) -> None:
        with self._lock:
            used = self._counts.get(key, 0) - 1
            if used > 0:
                self._counts[key] = used
            else:
                # 归零就删掉：否则长时间跑下来 _counts 里堆满死 key，
                # in_use()/排障时一屏都是 0。
                self._counts.pop(key, None)


GATE = SessionGate(SESSION_LIMIT)


class Slot:
    """一次上游请求持有的闸门槽位（含心跳会话）。

    denied 非空表示没占到槽，文本即原因，此时**不要**发上游请求。
    release() 幂等，可以放心放在 finally 里。
    """

    __slots__ = ("denied", "session_id", "_session", "_key", "_gate", "_released")

    def __init__(self, denied="", session_id="", chat=None, key="", gate=None):
        self.denied = denied
        self.session_id = session_id
        self._session = chat
        self._key = key
        self._gate = gate
        self._released = False

    @property
    def ok(self) -> bool:
        return not self.denied

    @property
    def key(self) -> str:
        """闸门 key（日志/排障用；denied 的 Slot 也带它，好在拒绝时报在途数）。"""
        return self._key

    def release(self):
        """还槽 + 注销心跳。重复调用是空操作（读 body 与 close 都会调）。"""
        if self._released:
            return
        self._released = True
        if self._session is not None:
            self._session.end()
        if self._gate is not None:
            self._gate.release(self._key)


def _current_rid() -> str:
    """取当前请求的 rid（心跳日志归因用）。不在请求上下文中返回空串。

    心跳跑在 Timer 线程上，那里拿不到 flask.g；rid 在 acquire 时抓一次就够。
    """
    from flask import g
    try:
        return getattr(g, "rid", "") or ""
    except RuntimeError:
        # Working outside of application/request context
        return ""


def acquire(base_url: str, account_id: str, logger=None, send=None, gate=None):
    """占一个闸门槽位并登记 busy。

    满员时返回 denied 的 Slot；否则返回带 session_id 的 Slot，调用方把
    session_id 塞进 user-session-id 头即可。busy 登记失败不阻断请求
    （见 ChatSession.begin），只记日志。
    """
    gate = gate if gate is not None else GATE
    key = SessionGate.key_for(base_url, account_id)
    if not gate.acquire(key):
        # 不塞 gate：这个槽位压根没占到，release() 不该去动计数
        return Slot(denied="本机上游并发已满（%d 路）" % gate.limit, key=key)

    chat = ChatSession(send=send, logger=logger, rid=_current_rid()) if HEARTBEAT_ON else None
    slot = Slot(session_id=(chat.session_id if chat else new_session_id()),
                chat=chat, key=key, gate=gate)
    if chat is not None:
        try:
            chat.begin()
        except Exception as error:      # 心跳坏了不该让请求失败
            if logger is not None:
                logger.warning("[session %s] busy 登记异常: %s: %s",
                               chat.session_id[:8], type(error).__name__, error)
    return slot


class TrackedResponse:
    """包住上游响应：正文读完或连接关闭时归还槽位并注销心跳。

    为什么必须包一层：三条路由都是在 `_send_upstream` 返回**之后**才消费
    响应体（流式尤其晚，客户端断在哪儿都可能）。在 `_send_upstream` 里用
    finally 还槽，会把整个流式期间的槽位白白放掉，闸门就形同虚设；
    而把 release 抄进三条路由的六个分支，迟早会漏一处。收口在这里，
    `content` / `text` / `json()` / `iter_lines()` / `close()` 五条出口
    全覆盖，Slot.release() 自身幂等，重复触发无副作用。
    """

    def __init__(self, response, slot):
        self._response = response
        self._slot = slot

    def __getattr__(self, name):
        # status_code / headers / encoding 等一律透传给真响应。
        # 这两个下划线名要先挡住，否则构造期被读取会无限递归。
        if name in ("_response", "_slot"):
            raise AttributeError(name)
        return getattr(self._response, name)

    @property
    def content(self):
        # 读正文时连接被掐会抛；槽位必须在异常路径上也还掉，否则每出一次
        # 读失败就漏一个槽，攒够 3 个之后整条服务全回 409。
        try:
            return self._response.content
        finally:
            self._slot.release()

    @property
    def text(self):
        try:
            return self._response.text
        finally:
            self._slot.release()

    def json(self, **kwargs):
        try:
            return self._response.json(**kwargs)
        finally:
            self._slot.release()

    def iter_lines(self, *args, **kwargs):
        try:
            for line in self._response.iter_lines(*args, **kwargs):
                yield line
        finally:
            # 提前 break / 生成器被回收也会走到这里：客户端中断同样要还槽
            self._slot.release()

    def close(self):
        try:
            self._response.close()
        finally:
            self._slot.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@contextlib.contextmanager
def hold(base_url: str, account_id: str, logger=None, send=None, gate=None):
    """with 语句版：还槽 + 注销心跳自动收尾。

    供非 `_send_upstream` 的路径用（例如将来给模型同步也加闸）。
    `_send_upstream` 不能用它——那里槽位要活到响应体读完，得手动管。
    """
    slot = acquire(base_url, account_id, logger=logger, send=send, gate=gate)
    try:
        yield slot
    finally:
        slot.release()


def snapshot(gate=None) -> dict:
    """排障用：当前各闸门口的在途数。"""
    gate = gate if gate is not None else GATE
    with gate._lock:
        return {"limit": gate.limit,
                "in_use": dict(gate._counts),
                "heartbeat": HEARTBEAT_ON,
                "interval": HEARTBEAT_INTERVAL}

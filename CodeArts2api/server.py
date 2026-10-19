# -*- coding: utf-8 -*-
"""
CodeArts OpenAI 兼容代理
=========================
把华为云 CodeArts 模型接口（openpangu-2.0-pro）包装成标准 OpenAI
Chat Completions API，供 Claude Code / OpenCode / Cline / Cherry Studio
等任何支持自定义 OpenAI Base URL 的工具直接使用。

分包结构（协议层 / 通道层 / 翻译层 / 面板层分离）：
  routes/chat.py           /v1/chat/completions
  routes/responses.py      /v1/responses（经 adapters/responses.py 翻译）
  routes/anthropic.py      /v1/messages（经 adapters/anthropic.py 翻译）
  adapters/responses.py    Responses ⇄ Chat 翻译桥（去 _bridge 后缀）
  adapters/anthropic.py    Anthropic Messages ⇄ Chat 翻译桥
  admin/panel.py           面板页 + /admin/api/*（overview/checkin/reload-models）
  admin/checkin.py         后台每日自动签到线程
  lanes/benefit.py         免费额度通道（签到/余额/免费模型同步）
  lanes/credential_provider.py  凭证装载与自动续期
  lanes/oauth.py           OAuth2 PKCE 浏览器登录（独立入口：python lanes/oauth.py）
  tests/                   离线测试（打桩 _upstream / _hmac_headers，不连真实上游）

server.py 只留公共设施：签名、上游发送与重试、模型同步、dumps 落盘、
请求日志与路由挂载。路由体一律经 register(app, core) 的 core
（即本模块）动态查找引擎函数，测试对 server 上这些名字的打桩因此对
路由层同样生效。

启动方式:
    pip install -r requirements.txt
    python server.py

默认监听:  http://127.0.0.1:8787
OpenAI Base URL: http://127.0.0.1:8787/v1
"""
import datetime
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.parse
from logging.handlers import RotatingFileHandler

import requests
from dotenv import load_dotenv
from flask import Flask, Response, g, jsonify, request
from requests.adapters import HTTPAdapter

from adapters import anthropic, responses as responses_adapter
from admin import checkin as checkin_loop, panel as admin_route
from lanes import benefit, credential_provider, oauth, session  # noqa: F401  (oauth 仅登录时用)
from routes import anthropic as anthropic_route
from routes import chat as chat_route
from routes import responses as responses_route

load_dotenv()

def _current_creds() -> dict:
    return credential_provider.cred_and_env()

# CodeArts 目标端点（从流量分析确认）
BASE_URL = "https://snap-access.cn-north-4.myhuaweicloud.com"
TARGET = BASE_URL + "/api/v2/chat/completions"
HOST = "snap-access.cn-north-4.myhuaweicloud.com"
AGENT_LIST_URL = BASE_URL + "/v1/agent-center/agents/useragents?offset=0&limit=100"
AGENT_DETAIL_PATH = "/v1/agent-center/agents/detail"

# 自动同步失败时的兜底列表（仅老套餐模型，走 AgentCenter 通道）；正常启动会用云端返回的列表覆盖它
FALLBACK_AGENT_MODELS = ["openpangu-2.0-pro", "openpangu-2.0-flash", "GLM-5.2"]
# 免费模型兜底列表（IDE 流量分析确认）：走 maas_type=benefit 通道，签名时必须额外携带
# maas_type / model-id / model-name 三个头，否则报 "model is not registered"；
# 老套餐模型带上这些头反而会报 "unsupported model"，因此两条通道严格区分。
BENEFIT_MODELS = [
    "glm-5.3-flash",
    "deepseek-v4-pro-0813",
    "deepseek-v4-flash-0731",
]
FALLBACK_MODELS = FALLBACK_AGENT_MODELS + BENEFIT_MODELS
# opengw 免费额度网关（IDE 流量分析确认）：永久 AK/SK 即可访问，gateway/config 返回免费模型列表
OPENGW_BASE = "https://opengw.developer.huaweicloud.com"
OPENGW_CONFIG_URL = OPENGW_BASE + "/api/v1/gateway/config"
# 模型同步时自动领取限时福利（幂等；不领取则福利模型调用必报 benefit not found）
BENEFIT_AUTO_CLAIM = os.getenv("CODEARTS_BENEFIT_AUTO_CLAIM", "1").strip() not in ("0", "false", "False", "no", "")
# 上游业务错误码：并发限流可退避重试，积分耗尽不可重试直接透传语义
UPSTREAM_RETRY_CODES = {"TM.00001041"}
# STS 临时凭证过期（APIG.0602）与 AK 校验失败（APIG.0301）：
# 这类错误重试同一把凭证永远的结果一样，必须先换凭证
UPSTREAM_AUTH_CODES = {"APIG.0602", "APIG.0301"}
UPSTREAM_RETRY_MAX = 3
UPSTREAM_RETRY_BACKOFF = 2.0
UPSTREAM_CREDIT_CODES = {"TM.00001050"}
# 上游网关偶发 TLS 中断（SSLEOFError: UNEXPECTED_EOF_WHILE_READING）与连接重置。
# 代理在拿到响应之前重发是安全的，客户端本身也会重发整条请求，有界重试只是把
# 这次重发收在代理里，省掉一次客户端往返和一整轮 502。
CONN_RETRY_MAX = int(os.getenv("CODEARTS_CONN_RETRY_MAX", "2"))
CONN_RETRY_BACKOFF = float(os.getenv("CODEARTS_CONN_RETRY_BACKOFF", "0.5"))
# 上游并发闸门（每账号 3 路，满时报 TM.00001041）。闸门只认 heartbeat
# busy/idle，不认「请求结束」——不发心跳则每个请求占一个槽永不归还。
# 详见 lanes/session.py 的模块 docstring。
SESSION_GATE_LIMIT = session.SESSION_LIMIT
SESSION_GATE_ENABLED = os.getenv("CODEARTS_SESSION_GATE", "1").strip() not in (
    "0", "false", "False", "no", "")
# 上游 InferHub 对 max_tokens 有硬上限（实测 65536 通过、66560 起整请求被拒，
# 报流内错误帧 InferHub.001001005.400 "The request param is invalid"）。
# 而 AgentCenter/opengw 下发的 model_parameters.max_tokens 是上下文口径的值
# （131072 起），客户端读到后照抄发送必然全量失败，因此入站钳到该上限。
MAX_TOKENS_CAP = int(os.getenv("CODEARTS_MAX_TOKENS_CAP", "65536"))
# 模型缓存与代理程序放在同一目录，便于迁移、备份和排查
MODEL_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models-cache.json"
)
# AgentCenter 通道模型（决定是否用 benefit 签名；不在其中的模型一律按 benefit 处理，
# 这样 IDE 以后新增免费模型无需改代码，直接传模型名即可）
AGENT_MODELS = list(FALLBACK_AGENT_MODELS)
SUPPORTED_MODELS = list(FALLBACK_MODELS)
MODEL_DETAILS = {}

# 代理监听地址；改端口只需改这里（或设环境变量），启动日志会同步变化
LISTEN_HOST = os.getenv("CODEARTS_PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.getenv("CODEARTS_PROXY_PORT", "8787"))
# API Key 鉴权（与 qoder2api 同一套约定）：不设则无鉴权；设了则 /v1 与 /admin/api
# 必须携带 Authorization: Bearer <key>，不匹配返回 401。面板页豁免，key 注入页面后
# 由前端自动携带调用管理接口。
API_KEY = os.getenv("API_KEY", "").strip()

app = Flask(__name__)

# 上游连接池：复用 TLS 连接，避免每次请求重新握手（对话与模型同步都走这里）
_upstream = requests.Session()
_adapter = HTTPAdapter(pool_connections=4, pool_maxsize=16, max_retries=0)
_upstream.mount("https://", _adapter)
_upstream.mount("http://", _adapter)

# ── 请求日志（与 qoder2api 同一标准：request.log 轮转 + rid + in:/done: 双行）──
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "request.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)
# werkzeug 访问行在响应*开始*时打（流式会误导，且与 done 行重复），禁掉
logging.getLogger("werkzeug").disabled = True

# ── 请求/响应原文落盘（默认 dumps/，DUMP_DIR 改路径，DUMP=0 关闭）─────────────
# 只落 body 原文，绝不落 HTTP header：Authorization 里是明文网关 key，且 body 需可直接重放
DUMP_DIR = os.environ.get("DUMP_DIR", "") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "dumps")
DUMP_ON = os.environ.get("DUMP", "1").strip() != "0"
DUMP_MAX_BYTES = 512 * 1024   # 单方向落盘上限，防超大响应撑爆磁盘
DUMP_MAX_FILES = int(os.environ.get("DUMP_MAX_FILES", "200"))  # ≤0 表示不轮转

_seq_lock = threading.Lock()
_req_seq = 0


def _next_req_id():
    global _req_seq
    with _seq_lock:
        _req_seq += 1
        return "r%04d" % _req_seq


def _dump_trim():
    """dump 数量超过 DUMP_MAX_FILES 时按 mtime 删最旧（明文提示词会无限增长）。"""
    if DUMP_MAX_FILES <= 0:
        return
    try:
        entries = sorted((e for e in os.scandir(DUMP_DIR) if e.is_file()),
                         key=lambda e: e.stat().st_mtime)
    except OSError:
        return
    for stale in entries[:len(entries) - DUMP_MAX_FILES]:
        try:
            os.remove(stale.path)
        except OSError:
            pass


# dump 文件名前缀启动段：rid 每进程从 r0001 重新计数，旧版同名 rid 跳重启后直接覆盖旧证据
_BOOT_TAG = time.strftime("%m%d-%H%M%S", time.localtime()) + "-p%d" % os.getpid()


def _dump_save(rid, kind, data):
    """把请求/响应原文落盘到 dumps/<rid>.<kind>.txt（DUMP=0 时跳过）。"""
    if not DUMP_ON or not data:
        return
    try:
        os.makedirs(DUMP_DIR, exist_ok=True)
        text = data if isinstance(data, str) else data.decode("utf-8", "replace")
        truncated = len(text) > DUMP_MAX_BYTES
        path = os.path.join(DUMP_DIR, "%s.%s.%s.txt%s" % (_BOOT_TAG, rid, kind, ".truncated" if truncated else ""))
        with open(path, "w", encoding="utf-8") as f:
            f.write(text[:DUMP_MAX_BYTES])
        _dump_trim()
    except OSError as e:
        logger.warning("[%s] 落盘失败: %s", rid, e)


@app.before_request
def _log_request():
    """rid/t0 在最早的 before_request 赋值（401 短路也要有 done 行可关联）。"""
    g.rid = _next_req_id()
    g.t0 = time.time()
    if request.path in ("/", "/admin", "/favicon.ico"):
        return
    body = request.get_data(as_text=True)
    summary = ""
    try:
        j = json.loads(body)
        summary = "model=%s msgs=%d tools=%d stream=%s" % (
            j.get("model"), len(j.get("messages") or []),
            len(j.get("tools") or []), j.get("stream"))
    except Exception:
        pass
    # UA 是日志里唯一的客户端归因维度（API key 全客户端共用、ip 恒为 127.0.0.1）；
    # 折掉换行防伪造多行日志
    ua = (request.headers.get("User-Agent") or "-").replace("\r", " ").replace("\n", " ")
    logger.info("[%s] in: %s %s | %s | ua=%s | body=%dB",
                g.rid, request.method, request.path, summary or "n/a", ua, len(body))
    _dump_save(g.rid, "req", body)
    return None


@app.after_request
def _log_response(response):
    """非流式记 done 并落盘 dumps/<rid>.resp.txt；流式响应不能在这里读取（会整块缓冲），
    完成行与落盘由 _stream_log 在流末发出。"""
    if request.path in ("/", "/admin", "/favicon.ico"):
        return response
    if response.is_streamed:
        return response
    try:
        resp_body = response.get_data(as_text=True)
    except Exception:
        return response
    logger.info("[%s] done: status=%d %dB %.1fs",
                getattr(g, "rid", "?"), response.status_code,
                len(resp_body), time.time() - getattr(g, "t0", time.time()))
    _dump_save(getattr(g, "rid", "?"), "resp", resp_body)
    return response


def _stream_log(gen, rid, t0):
    """包裹 SSE 生成器：TTFT、字节数、原文落盘 dumps/<rid>.resp.txt、done/断开/中断日志。
    rid/t0 必须在请求上下文中取出后传入（生成器被消费时 g 已销毁）。"""
    total = 0
    ttft = None
    buf = [] if DUMP_ON else None
    try:
        for piece in gen:
            if ttft is None:
                ttft = time.time() - t0
            total += len(piece)
            if buf is not None:
                buf.append(piece)
            yield piece
        if buf is not None:
            _dump_save(rid, "resp", b"".join(buf))
        logger.info("[%s] done: stream %dB %.1fs ttft=%s",
                    rid, total, time.time() - t0,
                    ("%.1fs" % ttft) if ttft is not None else "-")
    except GeneratorExit:
        logger.warning("[%s] client 断开: stream %dB %.1fs", rid, total, time.time() - t0)
        raise
    except Exception as e:
        logger.error("[%s] stream 中断: %s (%dB %.1fs)", rid, e, total, time.time() - t0)
        raise


def _is_conn_retryable(error: BaseException) -> bool:
    """连接级失败可以在代理侧重发；读超时不行。

    可重发：TLS 被中断（SSLEOFError）、连接被重置、连不上。这些情况下代理一个
    响应字节都没拿到，重发最多是让上游多做一次它本来也会替客户端做的事。
    不可重发：读超时——请求已经进入推理阶段，重发会把同一次生成再算一遍。
    """
    if isinstance(error, requests.exceptions.ConnectTimeout):
        return True
    if isinstance(error, requests.exceptions.Timeout):
        return False
    return isinstance(error, requests.exceptions.ConnectionError)


def _signed_request(method: str, url: str, body: bytes = b"", extra_headers=None, timeout=30):
    """发送带华为云 SDK-HMAC-SHA256 签名的请求，连接级失败时换新连接重发。"""
    creds = _current_creds()
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"
    canonical_uri = path if path.endswith("/") else path + "/"
    params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    params.sort(key=lambda item: (item[0], item[1]))
    quote = lambda value: urllib.parse.quote(str(value), safe="-_.~")
    canonical_query = "&".join(
        f"{quote(key)}={quote(value)}" for key, value in params
    )

    headers = {}
    for key, value in (extra_headers or {}).items():
        headers[key.lower()] = str(value)
    headers["host"] = parsed.netloc
    headers.setdefault("content-type", "application/json")
    if creds.get("token"):
        headers["x-security-token"] = creds["token"]
    # 先占位：x-sdk-date 必须进 SignedHeaders，否则签名与规范头不匹配（上游 401）
    headers["x-sdk-date"] = ""

    signed_names = sorted(headers)
    signed_headers = ";".join(signed_names)
    payload_hash = hashlib.sha256(body).hexdigest()

    # x-sdk-date 参与签名，且时间戳超出允许的时钟偏差会被拒，所以每次重发都重新签一遍
    for attempt in range(CONN_RETRY_MAX + 1):
        sdk_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        headers["x-sdk-date"] = sdk_date
        canonical_headers = "".join(
            f"{name}:{headers[name].strip()}\n" for name in signed_names
        )
        canonical_request = (
            f"{method.upper()}\n{canonical_uri}\n{canonical_query}\n"
            f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )
        string_to_sign = (
            f"SDK-HMAC-SHA256\n{sdk_date}\n"
            f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
        )
        signature = hmac.new(
            creds["sk"].encode(), string_to_sign.encode(), hashlib.sha256
        ).hexdigest()
        headers["authorization"] = (
            f"SDK-HMAC-SHA256 Access={creds['ak']}, SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )
        try:
            return _upstream.request(
                method.upper(), url, data=body or None, headers=headers, timeout=timeout
            )
        except Exception as error:
            if attempt < CONN_RETRY_MAX and _is_conn_retryable(error):
                delay = CONN_RETRY_BACKOFF * (attempt + 1)
                logger.warning(
                    "[重试] %s %s 连接级失败（%s），%.1fs 后第 %d/%d 次重发",
                    method.upper(), parsed.netloc, type(error).__name__,
                    delay, attempt + 1, CONN_RETRY_MAX)
                time.sleep(delay)
                continue
            raise


def _hmac_headers(body: bytes, benefit: bool = False, model_id: str = "",
                  session_id: str = "") -> dict:
    """构造华为云 SDK-HMAC-SHA256 签名请求头（仅适用于 POST /api/v2/chat/completions）。

    benefit=True 时按 IDE 免费模型通道附加 maas_type / model-id / model-name 三个头，
    这三个头必须参与签名，否则上游返回 "The model is not registered"。

    session_id 非空时附带 user-session-id 头并参与签名：上游靠它把请求归到
    我们已 busy 的那个会话上，槽位才会随 idle 归还。签名必须覆盖它，否则
    改了头不重签，上游按签名里的 SignedHeaders 校验会发现头集合不匹配。
    """
    creds = _current_creds()
    now = datetime.datetime.now(datetime.timezone.utc)
    sdk_date = now.strftime("%Y%m%dT%H%M%SZ")

    # 注意：华为云签名规范要求消息头名称一律转小写再参与签名
    headers = {
        "host": HOST,
        "content-type": "application/json",
        "x-sdk-date": sdk_date,
    }
    if creds.get("token"):
        headers["x-security-token"] = creds["token"]
    if benefit and model_id:
        headers["maas_type"] = "benefit"
        headers["model-id"] = model_id
        headers["model-name"] = model_id
    if session_id:
        headers["user-session-id"] = session_id

    signed_names = sorted(headers.keys())
    canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in signed_names)
    signed_headers = ";".join(signed_names)

    # CanonicalRequest: POST + URI(带尾斜杠) + 空查询串 + 规范头 + 签名头 + body哈希
    canonical_uri = "/api/v2/chat/completions/"
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_request = (
        f"POST\n{canonical_uri}\n\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )

    # StringToSign: 算法 + 时间戳 + CanonicalRequest哈希
    hashed_canonical = hashlib.sha256(canonical_request.encode()).hexdigest()
    string_to_sign = f"SDK-HMAC-SHA256\n{sdk_date}\n{hashed_canonical}"

    signature = hmac.new(creds["sk"].encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()

    headers["Authorization"] = (
        f"SDK-HMAC-SHA256 Access={creds['ak']}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


def _load_model_cache() -> bool:
    """加载本地模型缓存，返回是否成功。"""
    global SUPPORTED_MODELS, MODEL_DETAILS
    try:
        with open(MODEL_CACHE_PATH, "r", encoding="utf-8") as file:
            cached = json.load(file)
        models = cached.get("models", [])
        if not models:
            return False
        SUPPORTED_MODELS = [item["id"] for item in models if item.get("id")]
        MODEL_DETAILS = {item["id"]: item for item in models if item.get("id")}
        # 缓存里的 AgentCenter 模型参与通道判断；旧缓存无 channel 字段时默认
        # 视为 agent 通道；不在缓存里的新模型一律按 benefit 处理。
        AGENT_MODELS.clear()
        AGENT_MODELS.extend(
            item["id"]
            for item in models
            if item.get("channel", "agent") == "agent"
        )
        return bool(SUPPORTED_MODELS)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _save_model_cache(models) -> None:
    os.makedirs(os.path.dirname(MODEL_CACHE_PATH), exist_ok=True)
    payload = {
        "updated_at": int(time.time()),
        "models": models,
    }
    temp_path = MODEL_CACHE_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, MODEL_CACHE_PATH)


def _refresh_benefit_models() -> list:
    """从 opengw 免费额度网关动态获取免费模型列表（IDE 流量分析确认的接口）。

    返回模型详情列表；失败时返回空列表，由调用方回退到 BENEFIT_MODELS 兜底。
    """
    # 先领取限时福利（幂等）：未领取时福利模型调用必报 InferHub.4004.200 benefit not found。
    # 领取失败只记日志，不阻断模型列表同步。
    if BENEFIT_AUTO_CLAIM:
        try:
            benefit.claim()
        except Exception as error:
            logger.warning("限时福利自动领取失败（不影响模型列表同步）: %s", error)
    try:
        response = _signed_request("GET", OPENGW_CONFIG_URL, timeout=30)
        response.raise_for_status()
        result = response.json().get("result") or {}
        models = result.get("models") or []
        discovered = []
        for model in models:
            model_id = model.get("model_id")
            if not model_id or any(item["id"] == model_id for item in discovered):
                continue
            discovered.append(
                {
                    "id": model_id,
                    "object": "model",
                    "owned_by": "codearts",
                    "channel": "benefit",
                    "name": model.get("model_name") or model_id,
                    "description": "免费额度模型（签到领取）",
                    "context_window": model.get("context_window"),
                    "max_tokens": model.get("max_tokens"),
                    "supports_images": False,
                }
            )
        return discovered
    except (requests.RequestException, ValueError, TypeError, KeyError) as error:
        logger.warning("免费模型列表同步失败，将使用兜底列表: %s", error)
        return []


def _refresh_models() -> bool:
    """从 CodeArts AgentCenter 获取当前账号可用模型（仅老套餐通道）。"""
    global SUPPORTED_MODELS, MODEL_DETAILS
    try:
        # 与 CodeArts CLI 流量分析一致：AgentCenter 接口需要这个路由头。
        response = _signed_request(
            "GET",
            AGENT_LIST_URL,
            extra_headers={
                "agent-type": "AgentCenter",
                "x-language": "zh-cn",
                "accept": "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        agents = data.get("agents", [])
        if not agents:
            return False

        # 优先主 Agent；若某个详情无模型，再尝试其他主 Agent。
        candidates = sorted(
            agents,
            key=lambda item: (
                not bool(item.get("is_primary_agent")),
                item.get("agent_order") is None,
                item.get("agent_order") or 999999,
            ),
        )
        discovered = []
        for agent in candidates:
            agent_id = agent.get("agent_id")
            if not agent_id:
                continue
            detail_url = (
                f"{BASE_URL}{AGENT_DETAIL_PATH}?agent_id="
                f"{urllib.parse.quote(str(agent_id), safe='')}"
            )
            detail_response = _signed_request(
                "GET",
                detail_url,
                extra_headers={
                    "agent-type": "AgentCenter",
                    "x-language": "zh-cn",
                    "accept": "application/json",
                },
                timeout=30,
            )
            if detail_response.status_code != 200:
                continue
            detail = detail_response.json()
            for model in (detail.get("gpts", {}).get("models", []) or []):
                model_id = model.get("model_id") or model.get("model_alias")
                if not model_id or any(item["id"] == model_id for item in discovered):
                    continue
                params = model.get("model_parameters") or {}
                discovered.append(
                    {
                        "id": model_id,
                        "object": "model",
                        "owned_by": "codearts",
                        "channel": "agent",
                        "name": model.get("model_alias") or model.get("model_name") or model_id,
                        "description": model.get("model_desc") or "",
                        "context_window": params.get("context_window"),
                        "max_tokens": params.get("max_tokens"),
                        "supports_images": params.get("supports_images", False),
                        "enable_queue": params.get("enable_queue", False),
                    }
                )
            if discovered:
                # 主 Agent 已拿到完整模型列表，避免不必要请求。
                break

        if not discovered:
            return False
        # benefit 免费模型不在 AgentCenter 列表里，从 opengw 网关动态同步；
        # 同步失败时回退到 BENEFIT_MODELS 兜底，保证 /v1/models 可见。
        benefit_models = _refresh_benefit_models()
        if not benefit_models:
            benefit_models = [
                {"id": model_id, "channel": "benefit", "object": "model",
                 "owned_by": "codearts", "name": model_id}
                for model_id in BENEFIT_MODELS
            ]
        known = {item["id"] for item in discovered}
        for item in benefit_models:
            if item["id"] not in known:
                discovered.append(item)
        SUPPORTED_MODELS = [item["id"] for item in discovered]
        MODEL_DETAILS = {item["id"]: item for item in discovered}
        AGENT_MODELS.clear()
        AGENT_MODELS.extend(
            item["id"] for item in discovered if item.get("channel") == "agent"
        )
        _save_model_cache(discovered)
        logger.info("已自动同步模型: %s", ", ".join(SUPPORTED_MODELS))
        return True
    except (requests.RequestException, ValueError, TypeError, KeyError, OSError) as error:
        logger.warning("自动同步模型失败，将使用缓存或兜底列表: %s", error)
        return False


_refresh_trigger_lock = threading.Lock()
_refresh_in_progress = False

# 模型名查不到时的强制同步节流（见 _force_sync_models_for）
FORCED_SYNC_MIN_INTERVAL = 20.0
_last_forced_sync = 0.0
_forced_sync_lock = threading.Lock()


def _refresh_models_if_needed() -> None:
    """模型列表短时缓存，避免 Cherry Studio 频繁轮询时重复请求。

    缓存过期时不再阻塞当前聊天请求：完全没有可用模型（首启动无缓存）才同步
    刷新；否则触发后台刷新，当前请求沿用旧列表，避免上游变慢拖住对话。
    """
    global _refresh_in_progress
    # 进程刚启动时先把缓存详情装载到内存；不能只依赖兜底模型名。
    if not MODEL_DETAILS:
        _load_model_cache()
    try:
        age = time.time() - os.path.getmtime(MODEL_CACHE_PATH)
    except OSError:
        age = float("inf")
    if age <= 300:
        return
    if not SUPPORTED_MODELS:
        if not _refresh_models():
            _load_model_cache()
        return
    with _refresh_trigger_lock:
        if _refresh_in_progress:
            return
        _refresh_in_progress = True

    def _background_refresh():
        global _refresh_in_progress
        try:
            _refresh_models()
        finally:
            _refresh_in_progress = False

    threading.Thread(target=_background_refresh, daemon=True, name="model-refresh").start()


def _force_sync_models_for(model: str) -> bool:
    """请求的模型不在列表里时，**同步**再同步一次模型清单，返回是否因此命中。

    为什么需要：`_refresh_models_if_needed()` 的策略是缓存过期就只起后台刷新、
    当前请求照用旧列表（避免上游变慢拖住对话）。福利模型是动态投放的，于是"上游
    刚放出某个模型"到"本地缓存追上"之间有一段窗口，期间每次请求都会被本地判定
    成 model_not_found 直接 404 —— 实测 2026-10-14 05:01 那串 404 就是这么来的：
    `deepseek-v4.1-flash` 05:00:48 那次同步还没有，05:07:30 才进缓存。

    带节流：一个模型名查不到，可能是客户端配置写错了并在重试。没有这个窗口就会
    变成"每次坏请求都打一遍上游"。
    """
    global _last_forced_sync
    now = time.time()
    with _forced_sync_lock:
        if now - _last_forced_sync < FORCED_SYNC_MIN_INTERVAL:
            return model in SUPPORTED_MODELS
        _last_forced_sync = now
    if _refresh_models():
        return model in SUPPORTED_MODELS
    return model in SUPPORTED_MODELS


def _normalize_finish_reason(value):
    """把 CodeArts 的结束原因转换为 OpenAI 客户端能识别的值。"""
    if value in (None, "other"):
        return "stop"
    return value


def _parse_sse_bytes(raw: bytes):
    """把上游 SSE 聚合为一个 OpenAI chat.completion JSON。"""
    text_parts = []
    reasoning_parts = []
    tool_calls = {}
    result = {
        "id": None,
        "object": "chat.completion",
        "created": None,
        "model": None,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": ""},
            "finish_reason": "stop",
        }],
    }
    usage = None
    last_finish_reason = None

    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith(b"data:"):
            continue
        payload = line[5:].lstrip()
        if payload == b"[DONE]":
            continue
        try:
            chunk = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue

        # CodeArts 可能以 HTTP 200 返回业务错误，需要保留错误信息。
        if chunk.get("error_code") or chunk.get("error_msg"):
            return {
                "error": {
                    "message": chunk.get("error_msg") or "CodeArts upstream error",
                    "type": "upstream_error",
                    "code": chunk.get("error_code"),
                }
            }

        for key in ("id", "created", "model"):
            if chunk.get(key) is not None:
                result[key] = chunk[key]
        if chunk.get("usage"):
            usage = chunk["usage"]

        for choice in chunk.get("choices") or []:
            reason = choice.get("finish_reason")
            if reason is not None:
                last_finish_reason = _normalize_finish_reason(reason)
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                text_parts.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning_parts.append(delta["reasoning_content"])
            for call in delta.get("tool_calls") or []:
                index = call.get("index", 0)
                current = tool_calls.setdefault(index, {
                    "id": call.get("id"),
                    "type": call.get("type", "function"),
                    "function": {"name": "", "arguments": ""},
                })
                if call.get("id"):
                    current["id"] = call["id"]
                if call.get("type"):
                    current["type"] = call["type"]
                function = call.get("function") or {}
                if function.get("name"):
                    current["function"]["name"] += function["name"]
                if function.get("arguments"):
                    current["function"]["arguments"] += function["arguments"]

    message = result["choices"][0]["message"]
    message["content"] = "".join(text_parts)
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
    result["choices"][0]["finish_reason"] = last_finish_reason or ("tool_calls" if tool_calls else "stop")
    if usage:
        result["usage"] = usage
    return result


def _parse_upstream_json(upstream):
    """兼容上游普通 JSON 和错误地返回 SSE 的情况。"""
    raw = upstream.content
    try:
        result = json.loads(raw.decode("utf-8"))
        if isinstance(result, dict) and result.get("choices"):
            for choice in result["choices"]:
                choice["finish_reason"] = _normalize_finish_reason(choice.get("finish_reason"))
        return result
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _parse_sse_bytes(raw)


def _cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.before_request
def check_api_key():
    if not API_KEY or request.method == "OPTIONS":
        return
    if request.path in ("/", "/admin", "/favicon.ico"):
        return
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if token != API_KEY:
        return _cors(jsonify({"error": {"message": "Invalid API key", "type": "invalid_request_error"}})), 401


@app.after_request
def after_request(resp: Response) -> Response:
    return _cors(resp)


def _capped_max_tokens(value):
    """把云端下发的输出上限收敛到上游实际接受的值；非数值原样返回。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return min(int(value), MAX_TOKENS_CAP)


def _clamp_max_tokens(req_json: dict) -> None:
    """把客户端要的输出上限钳到上游接受的范围（原地修改 req_json）。

    超出时 InferHub 不返回 4xx，而是在已建立的流里塞一个
    InferHub.001001005.400 错误帧，Agent Runtime 会当成空响应无限重试；
    所以必须在转发前修正，不能透传报错。
    """
    for field in ("max_tokens", "max_completion_tokens"):
        value = req_json.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value > MAX_TOKENS_CAP:
            req_json[field] = MAX_TOKENS_CAP
            logger.warning("[%s] %s %d→%d：超过上游上限，超出部分会被 InferHub 判为非法参数",
                           g.rid, field, int(value), MAX_TOKENS_CAP)


def _model_payload(model_id: str) -> dict:
    """生成 OpenAI 模型对象，并附带常见能力元数据。"""
    detail = MODEL_DETAILS.get(model_id, {})
    context_window = detail.get("context_window")
    # 对外上报的输出上限必须是上游真正接受的值，否则客户端会按云端下发的
    # 上下文口径值（131072+）发请求，直接被 InferHub 判为非法参数。
    max_tokens = _capped_max_tokens(detail.get("max_tokens"))
    supports_images = bool(detail.get("supports_images", False))
    payload = {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "codearts",
        "name": detail.get("name") or model_id,
        "description": detail.get("description") or "",
        # 常见 OpenAI 兼容客户端会读取其中一部分；未知字段会被安全忽略。
        "context_window": context_window,
        "context_length": context_window,
        "max_tokens": max_tokens,
        "max_output_tokens": max_tokens,
        "supports_images": supports_images,
        "vision": supports_images,
        "supports_vision": supports_images,
        "supports_function_calling": True,
        "supports_tool_calling": True,
        "supports_reasoning": True,
        "input_modalities": ["text", "image"] if supports_images else ["text"],
        "output_modalities": ["text"],
        "supported_parameters": [
            "temperature",
            "top_p",
            "max_tokens",
            "stream",
            "tools",
            "tool_choice",
            "response_format",
        ],
    }
    return {key: value for key, value in payload.items() if value is not None}


@app.route("/v1/models", methods=["GET", "OPTIONS"])
def list_models():
    if request.method == "OPTIONS":
        return _cors(Response(""))
    _refresh_models_if_needed()
    return jsonify({
        "object": "list",
        "data": [_model_payload(model_id) for model_id in SUPPORTED_MODELS],
    })


@app.route("/v1/models/<path:model_id>", methods=["GET", "OPTIONS"])
def get_model(model_id):
    """提供标准 OpenAI 单模型详情接口，方便客户端二次读取能力。"""
    if request.method == "OPTIONS":
        return _cors(Response(""))
    _refresh_models_if_needed()
    if model_id not in SUPPORTED_MODELS:
        return _cors(jsonify({
            "error": {
                "message": f"model '{model_id}' not found",
                "type": "invalid_request_error",
                "code": "model_not_found",
            }
        })), 404
    return jsonify(_model_payload(model_id))


def _sanitize_tool_calls(req_json: dict) -> None:
    """清洗 messages 里悬空的 tool_calls / 孤儿 tool 消息（原地修改 req_json）。

    上游对结构校验比 OpenAI 严格：assistant 带 tool_calls 后必须紧跟对应
    tool_call_id 的 tool 消息，缺一个就报
    InferHub.malformed_json.400 "insufficient tool messages following tool_calls message"。

    客户端被中断时最容易留下这种悬空结构（assistant 发起工具调用但结果没落地），
    下一请求整条历史都会被拒。处理原则：只动消息结构，绝不碰 function.name 等字段名。
    """
    msgs = req_json.get("messages")
    if not isinstance(msgs, list):
        return

    # tool_call_id -> 是否有对应的 tool 回复消息
    answered = {
        m.get("tool_call_id")
        for m in msgs
        if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id")
    }

    # tool_call_id -> 对应的 assistant 调用是否存在（用于识别孤儿 tool 消息）
    called = set()
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    called.add(tc["id"])

    out = []
    dropped_assistant = 0
    stripped_assistant = 0
    dropped_tool = 0
    for m in msgs:
        if not isinstance(m, dict):
            out.append(m)
            continue

        if m.get("role") == "tool":
            # 找不到对应 assistant 调用的 tool 消息：上游同样会拒
            if m.get("tool_call_id") not in called:
                dropped_tool += 1
                continue
            out.append(m)
            continue

        if m.get("role") == "assistant" and m.get("tool_calls"):
            dangling = [tc for tc in m["tool_calls"]
                        if isinstance(tc, dict) and tc.get("id") not in answered]
            if dangling:
                if m.get("content"):
                    # 有文本：只摘掉悬空 tool_calls，保留这段 assistant 内容
                    m = {k: v for k, v in m.items() if k != "tool_calls"}
                    stripped_assistant += 1
                else:
                    # 无内容：整条丢弃，否则留下一个空 assistant
                    dropped_assistant += 1
                    continue
        out.append(m)

    fixed = dropped_assistant + stripped_assistant + dropped_tool
    if fixed:
        req_json["messages"] = out
        logger.warning(
            "[%s] 清洗悬空 tool 结构: 丢弃 assistant=%d 摘除 tool_calls=%d 丢弃 tool=%d，"
            "msgs %d→%d", g.rid, dropped_assistant, stripped_assistant, dropped_tool,
            len(msgs), len(out))


def _session_account_id() -> str:
    """取并发闸门的账号身份（跨 AK 轮换稳定）。取不到返回空串。

    空串时所有请求挤同一个闸门口——比没有闸门好（仍能挡住并发），但跨账号
    会互相挤。credential_provider 两条装载路径都会填这个字段。
    """
    creds = _current_creds()
    return (creds.get("_account_id") or "").strip()


def _gate_reject(slot):
    """本地闸门满员：409（不是 429）。

    429 会被客户端当成额度耗尽进入长冷却，而这个条件几十毫秒内就会自己
    消失；409 的语义（冲突/稍后再试）才对得上。
    """
    logger.warning("[%s] [409] 本地并发闸门已满: %s（在途 %d/%d）",
                   getattr(g, "rid", "?"), slot.denied,
                   session.GATE.in_use(slot.key), SESSION_GATE_LIMIT)
    return None, (
        _cors(jsonify({"error": {
            "message": ("上游并发已满（每账号 %d 路）：%s。请稍后重试，"
                        "或调大 CODEARTS_SESSION_LIMIT" % (SESSION_GATE_LIMIT, slot.denied)),
            "type": "rate_limit_error",
            "code": "local_concurrency_limit",
        }})),
        session.GATE_STATUS,
    )


def _send_upstream(req_json: dict, model: str, benefit: bool):
    """签名并发给上游，含连接级/限流/凭证三类重试。

    chat 与 responses 两条路由共用。成功返回 (upstream, None)；
    最终失败返回 (None, (status, response))，response 已包好 CORS，
    调用方直接 return 即可。

    并发闸门：进来先占一个槽并 busy 心跳，返回的 upstream 是
    session.TrackedResponse——正文读完或连接关闭时才还槽并 idle。
    之所以不在这里 finally：三条路由都在本函数返回**之后**才消费响应体
    （流式尤其晚），提前还槽等于闸门只覆盖到「上游收下请求」那一刻。
    """
    # 重新序列化 body（确保与签名时一致）
    body = json.dumps(req_json, ensure_ascii=False).encode("utf-8")

    slot = None
    if SESSION_GATE_ENABLED:
        slot = session.acquire(BASE_URL, _session_account_id(), logger=logger)
        if not slot.ok:
            return _gate_reject(slot)

    attempt = 0
    conn_attempt = 0
    auth_retry = False
    while True:
        # x-sdk-date 参与签名，每次重发都要重新签一遍，避免复用旧时间戳
        headers = _hmac_headers(body, benefit=benefit, model_id=model,
                                session_id=(slot.session_id if slot else ""))
        try:
            upstream = _upstream.post(
                TARGET, data=body, headers=headers, stream=True, timeout=(10, 600)
            )
        except Exception as e:
            # TLS 中断/连接重置：一个响应字节都没拿到，换新连接重发
            if conn_attempt < CONN_RETRY_MAX and _is_conn_retryable(e):
                conn_attempt += 1
                delay = CONN_RETRY_BACKOFF * conn_attempt
                logger.warning("[%s] [重试] 上游连接级失败（%s），%.1fs 后第 %d/%d 次重发",
                               g.rid, type(e).__name__, delay, conn_attempt, CONN_RETRY_MAX)
                time.sleep(delay)
                continue
            logger.error("[%s] 上游请求异常: %s", g.rid, e)
            if slot:
                slot.release()
            return None, (_cors(jsonify({"error": {"message": f"upstream error: {e}"}})), 502)

        if upstream.status_code == 200:
            # 槽位活到响应体读完；TrackedResponse 收口 content/text/json/iter_lines/close
            return (session.TrackedResponse(upstream, slot) if slot else upstream), None

        # 响应尚未开始，此处重试对客户端完全透明（流式同样适用）
        detail = ""
        error_code = ""
        error_msg = ""
        try:
            detail = upstream.text[:2000]
            err = upstream.json()
            error_code = str(err.get("error_code") or "")
            error_msg = str(err.get("error_msg") or "")
        except Exception:
            pass

        # 积分耗尽：重试无意义，透传上游语义（402 避免客户端自动重试循环）
        if error_code in UPSTREAM_CREDIT_CODES:
            logger.warning("[%s] [402] 上游积分耗尽: %s %s", g.rid, error_code, error_msg)
            if slot:
                slot.release()
            return None, (_cors(jsonify({"error": {
                "message": error_msg or "upstream credits exhausted",
                "type": "insufficient_quota",
                "code": error_code,
            }})), 402)

        # 并发限流：退避重试
        if error_code in UPSTREAM_RETRY_CODES and attempt < UPSTREAM_RETRY_MAX:
            attempt += 1
            delay = UPSTREAM_RETRY_BACKOFF * attempt
            logger.warning("[%s] [重试] 上游 %s，%.0fs 后第 %d/%d 次重试",
                           g.rid, error_code, delay, attempt, UPSTREAM_RETRY_MAX)
            time.sleep(delay)
            continue

        # 凭证失效：_current_creds() 有 30s 缓存，不强制重读就会拿同一把死 STS 反复重签。
        # 这里强制重读（内部会触发一次 refresh）后重发，对客户端透明。
        auth_bad = (upstream.status_code == 401
                    or error_code in UPSTREAM_AUTH_CODES
                    or "expired" in error_msg.lower())
        if auth_bad and not auth_retry:
            auth_retry = True
            logger.warning("[%s] [凭证] 上游判凭证失效（HTTP %s %s），强制重读凭证后重试一次",
                           g.rid, upstream.status_code, error_code or error_msg[:60])
            credential_provider.load_credentials(force=True)
            continue
        if auth_bad and auth_retry:
            logger.error("[%s] [凭证] 重读凭证后仍被上游拒绍（%s）：需重新登录（python oauth.py），或看上方 credential_provider 的刷新失败原因",
                         g.rid, error_code or upstream.status_code)

        logger.error("[%s] [502] 上游返回非 200: status=%d body=%.500s",
                     g.rid, upstream.status_code, detail)
        if slot:
            slot.release()
        return None, (
            _cors(
                jsonify(
                    {
                        "error": {
                            "message": f"upstream {upstream.status_code}: {detail}"
                        }
                    }
                )
            ),
            502,
        )


# ── 兼容旧引用（协议层与通道层已分包，旧平铺模块名不再可用）──────────────────
# 旧代码/探针里 `import anthropic_bridge` / `import responses_bridge` 的写法
# 已失效；这里以属性别名兜住 `server.anthropic_bridge` 一类的访问。
anthropic_bridge = anthropic
responses_bridge = responses_adapter


# ── 路由层挂载（routes/ 协议面 + admin/ 面板面）：本文件只做调度 ───────────────
# register(app, core) 里 core 传本模块：路由经模块属性动态查找引擎函数
# （_send_upstream / _clamp_max_tokens / _sanitize_tool_calls / SUPPORTED_MODELS 等），
# 测试对 server 上这些名字的打桩替换因此对路由层同样生效。
import sys as _sys

_core = _sys.modules[__name__]
chat_route.register(app, _core)
responses_route.register(app, _core)
anthropic_route.register(app, _core)
admin_route.register(app, _core)


if __name__ == "__main__":
    # 启动时先同步一次真实模型列表（失败则回退到缓存/兜底列表），再打印。
    if not _refresh_models():
        _load_model_cache()
    creds = _current_creds()
    if creds.get("token"):
        source = "auths 文件" if creds.get("_auth_file") else "CodeArts IDE 会话"
        print(f"凭证来源: {source}", credential_provider.token_notice(creds))
    else:
        print("凭证来源: 未找到凭证，请运行 python oauth.py 登录或在 CodeArts IDE 中登录")
    print(f"CodeArts OpenAI 兼容代理已启动: http://{LISTEN_HOST}:{LISTEN_PORT}")
    print(f"OpenAI Base URL: http://{LISTEN_HOST}:{LISTEN_PORT}/v1")
    print("鉴权:", f"已启用 API_KEY（Bearer）" if API_KEY else "未设置 API_KEY（无鉴权）")
    if SESSION_GATE_ENABLED:
        print(f"并发闸门: 本地准入 {SESSION_GATE_LIMIT} 路（满员回 409），"
              f"heartbeat {'开' if session.HEARTBEAT_ON else '关'}"
              f"/{session.HEARTBEAT_INTERVAL:g}s")
    else:
        print("并发闸门: 已关闭（CODEARTS_SESSION_GATE=0）")
    print("可用模型:", SUPPORTED_MODELS)
    print("按 Ctrl+C 停止")
    if BENEFIT_AUTO_CLAIM:
        # 后台每日签到：admin/checkin.py 的 benefit_checkin_loop(core)
        threading.Thread(target=checkin_loop.benefit_checkin_loop, args=(_core,),
                         daemon=True, name="benefit-checkin").start()
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, threaded=True)

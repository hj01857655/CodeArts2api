# -*- coding: utf-8 -*-
"""
CodeArts 免费额度签到/查询工具
==============================
对应 IDE 里的免费模型福利机制（opengw.developer.huaweicloud.com，抓包确认）：

    GET  /api/v1/benefit/claim        签到状态（返回上次签到时间）
    POST /api/v1/benefit/claim        签到领取当日免费额度（空 body）
    GET  /api/v1/user/tokens/balance  查询免费额度余额
    GET  /api/v1/gateway/config       免费模型列表

用法（凭证由 CodeArts IDE 会话自动读取，无需配置）：

    python lanes/benefit.py            # 全部：tokens 签到 + 积分签到 + 各项余额
    python lanes/benefit.py status     # 只查询 tokens 签到状态，不领取
    python lanes/benefit.py claim      # 只领每日 1000 万福利 Token
    python lanes/benefit.py credit     # 只领每日 1000 积分 + 查积分余额
    python lanes/benefit.py models     # 查询免费模型列表

配合 Windows 计划任务可实现每日自动签到，例如每天 09:05 执行：
    schtasks /create /tn CodeArtsBenefit /tr "python C:\\...\\lanes\\benefit.py claim" /sc daily /st 09:05
"""
# 作为独立入口（python lanes/benefit.py）运行时仓库根不在 sys.path 上，
# 兜底把仓库根加进来，否则 `from lanes import credential_provider` 失败。
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

import datetime  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import urllib.parse  # noqa: E402

import requests  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from lanes import credential_provider  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

def _current_creds() -> dict:
    return credential_provider.cred_and_env()

OPENGW_BASE = "https://opengw.developer.huaweicloud.com"
CLAIM_URL = OPENGW_BASE + "/api/v1/benefit/claim"
BALANCE_URL = OPENGW_BASE + "/api/v1/user/tokens/balance"
CONFIG_URL = OPENGW_BASE + "/api/v1/gateway/config"

# 积分福利网关（每日签到领1000积分），与 opengw 不同源，AK/SK 签名即可调用
SNAP_HOST = "snap-access.cn-north-4.myhuaweicloud.com"
CREDIT_CAMPAIGN_ID = 1  # 「每日签到领1000 积分」活动 ID


def signed_request(method: str, url: str, body: bytes = b"",
                   extra_headers: dict = None, with_token: bool = True) -> requests.Response:
    """发送带华为云 SDK-HMAC-SHA256 签名的请求（与 IDE 一致：host/content-type/x-sdk-date）。

    extra_headers 里的自定义头会一并纳入签名；header 名必须全小写（如 agent-type），
    大写会导致 APIG 401 签名失败。
    """
    creds = _current_creds()
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"
    canonical_uri = path if path.endswith("/") else path + "/"
    params = sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    quote = lambda value: urllib.parse.quote(str(value), safe="-_.~")
    canonical_query = "&".join(f"{quote(k)}={quote(v)}" for k, v in params)

    headers = {
        "host": parsed.netloc,
        "content-type": "application/json",
    }
    sdk_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers["x-sdk-date"] = sdk_date
    if with_token and creds.get("token"):
        headers["x-security-token"] = creds["token"]
    for name, value in (extra_headers or {}).items():
        headers[name.lower()] = value

    signed_names = sorted(headers)
    canonical_headers = "".join(f"{name}:{headers[name].strip()}\n" for name in signed_names)
    signed_headers = ";".join(signed_names)
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_request = (
        f"{method.upper()}\n{canonical_uri}\n{canonical_query}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    string_to_sign = (
        f"SDK-HMAC-SHA256\n{sdk_date}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )
    signature = hmac.new(creds["sk"].encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"SDK-HMAC-SHA256 Access={creds['ak']}, SignedHeaders={signed_headers}, Signature={signature}"
    )
    return requests.request(
        method.upper(), url, data=body or None, headers=headers, timeout=30
    )


def _unwrap(response: requests.Response) -> dict:
    """校验 opengw 响应（error_code=0000 表示成功），返回 result 字段。"""
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
    data = response.json()
    if data.get("error_code") not in (None, "0000", 0):
        raise RuntimeError(f"业务错误 {data.get('error_code')}: {data.get('error_msg')}")
    return data.get("result") or {}


def claim_status() -> dict:
    """查询签到状态。create_time 即最近一次签到时间（毫秒时间戳）。"""
    return _unwrap(signed_request("GET", CLAIM_URL))


def claim() -> dict:
    """签到领取当日免费额度。"""
    return _unwrap(signed_request("POST", CLAIM_URL))


def balance() -> dict:
    """查询免费额度余额。"""
    return _unwrap(signed_request("GET", BALANCE_URL))


def snap_request(method: str, path: str, body: str = "", query: str = ""):
    """调用积分福利网关（snap-access），AK/SK 签名即可，无需 IDE 登录态。

    自定义头 agent-type 必须全小写，否则 APIG 401 签名失败。
    返回 (HTTP 状态码, JSON)。
    """
    url = f"https://{SNAP_HOST}{path}" + (f"?{query}" if query else "")
    # STS 临时密钥必须携带 x-security-token，否则 IAM 报 "access key not found"
    resp = signed_request(method, url, body.encode("utf-8"),
                          extra_headers={"agent-type": "PromptCenter", "x-language": "zh-cn"})
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {}


def credit_activities() -> list:
    """查询福利活动列表（含每日签到 campaignId=1 的 claimable 状态）。"""
    code, data = snap_request("GET", "/v1/ops/delivery", query="channel=IDE")
    if code != 200:
        raise RuntimeError(f"查询福利活动失败 HTTP {code}: {str(data)[:300]}")
    return (data.get("data") or {}).get("items") or []


def credit_balance() -> dict:
    """查询积分余额（usageTotalPackageCredit：总额/已用/剩余）。"""
    code, data = snap_request("GET", "/snap-manager/v1/statistics/plugin")
    if code != 200:
        raise RuntimeError(f"查询积分余额失败 HTTP {code}: {str(data)[:300]}")
    for m in data.get("metrics") or []:
        if m.get("name") == "usageTotalPackageCredit":
            return {"total": m.get("package_credit_amount"),
                    "used": m.get("package_credit_used"),
                    "remain": m.get("package_credit_remain")}
    return {}


def show_credit_balance() -> None:
    info = credit_balance()
    if info:
        print(f"积分余额: {info.get('remain'):,} / {info.get('total'):,}（已用 {info.get('used'):,}）")
    else:
        print("积分余额: 未获取到（可能尚未升级积分计费模式）")


def do_credit_claim() -> None:
    """领取「每日签到领1000 积分」：查 claimable → claim → confirm，幂等安全。"""
    day = next((i for i in credit_activities() if i.get("campaignId") == CREDIT_CAMPAIGN_ID), None)
    if not day:
        print("积分签到: 未找到每日签到活动")
        return
    if not day.get("claimable"):
        print(f"积分签到: 今日已领取或不可领取（status={day.get('status')}），跳过")
        return
    key = f"claim_{CREDIT_CAMPAIGN_ID}_{int(datetime.datetime.now().timestamp() * 1000)}"
    body = json.dumps({"campaignId": CREDIT_CAMPAIGN_ID, "idempotentKey": key, "channel": "IDE"})
    code, data = snap_request("POST", "/v1/ops/claim", body=body)
    if code == 200 and data.get("code") == 0:
        # 客户端同流程：claim 成功后需 confirm 确认到账
        ccode, cdata = snap_request("POST", "/v1/ops/confirm",
                                    body=json.dumps({"campaignId": CREDIT_CAMPAIGN_ID}))
        print(f"积分签到: 领取成功（confirm HTTP {ccode}）")
    else:
        print(f"积分签到: 领取失败 HTTP {code}: {str(data)[:300]}")


def models() -> list:
    """查询免费模型列表。"""
    return (_unwrap(signed_request("GET", CONFIG_URL)) or {}).get("models") or []


def _fmt_time(ms) -> str:
    if not ms:
        return "从未签到"
    local = datetime.datetime.fromtimestamp(ms / 1000)
    return local.strftime("%Y-%m-%d %H:%M:%S")


def show_balance() -> None:
    info = balance()
    total_quota = info.get("total_quota") or 0
    remain = info.get("total_balance") or 0
    used = info.get("used_amount") or 0
    percent = (remain / total_quota * 100) if total_quota else 0
    print(f"免费额度余额: {remain:,} / {total_quota:,}（已用 {used:,}，剩余 {percent:.1f}%）")


def show_status() -> None:
    """人读的今日状态。

    三个数据源是三件事，不能互相代替（以前只打一行、标签还叫“最近签到时间”，
    会让人误判“没签到”而重复跑 claim）：
      • 权益包记录（opengw /benefit/claim）：09-17 建的一次性记实，不随每日变化
      • 每日额度（daily_*）：今天 1000 万 token 到账没有
      • 积分活动（campaignId=1）：今天的 1000 积分领了没有
    每一项单独降级：一个接口拖（snap-access 偶发 TLS 中断）不应拖死整个 status。
    """
    try:
        info = claim_status()
        created = _fmt_time(info.get("create_time"))
        updated = _fmt_time(info.get("update_time"))
        note = "（一次性，不随每日变化，不代表签到）" if info.get("create_time") == info.get("update_time") else ""
        print(f"权益包: 创建于 {created}，更新 {updated}{note}")
    except Exception as error:
        print(f"权益包: 查询失败 {str(error)[:120]}")

    try:
        b = balance()
        limit = b.get("daily_token_limit") or b.get("total_quota") or 0
        used = b.get("daily_tokens_used") if b.get("daily_tokens_used") is not None else b.get("used_amount")
        remain = b.get("total_balance") or 0
        got = "已到账" if limit else "未到账"
        print(f"每日额度: {got} {limit:,}（已用 {used if used is not None else '?'}，余额 {remain:,}）")
    except Exception as error:
        print(f"每日额度: 查询失败 {str(error)[:120]}")

    try:
        day = next((i for i in credit_activities() if i.get("campaignId") == CREDIT_CAMPAIGN_ID), None)
        if day is None:
            print("积分签到: 未找到每日签到活动")
        else:
            state = day.get("status") or "?"
            print(f"积分签到: status={state} claimable={day.get('claimable')}"
                  f" → {'今日已领' if state in ('CLAIMED', 'CONFIRMED', 'CONSUMED') else '今日可领'}")
    except Exception as error:
        print(f"积分签到: 查询失败 {str(error)[:120]}")


def do_claim() -> None:
    info = claim()
    # 注意：这条回的是权益包记录的创建时间，不是“本次领取时间”
    print(f"已请求领取权益，权益包创建于: {_fmt_time(info.get('create_time'))}")


def show_models() -> None:
    for model in models():
        ctx = model.get("context_window")
        out = model.get("max_tokens")
        print(f"  {model.get('model_id')}  (上下文 {ctx:,} / 最大输出 {out:,})" if ctx else
              f"  {model.get('model_id')}")


def main(argv: list) -> int:
    action = argv[1] if len(argv) > 1 else "default"
    notice = credential_provider.token_notice(_current_creds())
    if notice:
        print("提示:", notice)
    try:
        if action == "status":
            show_status()
        elif action == "claim":
            do_claim()
            show_balance()
        elif action == "credit":
            do_credit_claim()
            show_credit_balance()
        elif action == "models":
            print("免费模型列表:")
            show_models()
        else:
            # 默认：tokens 签到 + 积分签到 + 各项余额
            show_status()
            do_claim()
            show_balance()
            do_credit_claim()
            show_credit_balance()
    except RuntimeError as error:
        print(f"失败: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

# -*- coding: utf-8 -*-
"""
CodeArts 免费额度签到/查询工具
==============================
对应 IDE 里的免费模型福利机制（opengw.developer.huaweicloud.com，流量分析确认）：

    GET  /api/v1/benefit/claim        签到状态（返回上次签到时间）
    POST /api/v1/benefit/claim        签到领取当日免费额度（空 body）
    GET  /api/v1/user/tokens/balance  查询免费额度余额
    GET  /api/v1/gateway/config       免费模型列表

用法（凭证由 CodeArts IDE 会话自动读取，无需配置）：

    python lanes/benefit.py            # 全部：tokens 签到 + 积分签到 + 各项余额
    python lanes/benefit.py status     # 只查询 tokens 签到状态，不领取
    python lanes/benefit.py claim      # 只领每日 1000 万福利 Token
    python lanes/benefit.py credit     # 只领每日 1000 积分 + 查积分余额
    python lanes/benefit.py invite     # 拿邀请链接（不代领奖励）
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
    """查询福利活动列表（原样返回 /v1/ops/delivery?channel=IDE 的 items）。"""
    code, data = snap_request("GET", "/v1/ops/delivery", query="channel=IDE")
    if code != 200:
        raise RuntimeError(f"查询福利活动失败 HTTP {code}: {str(data)[:300]}")
    return (data.get("data") or {}).get("items") or []


def activity_view(item: dict) -> dict:
    """把上游活动条目转成 CLI/面板共用的展示结构。

    manual=True 表示这个活动要真实事件才推进（学生认证传证件、邀请拉真人），
    面板上标出来让人知道该去哪操作——不是脚本漏领。

    状态只描述**接口说出口的事**，不推断原因。`claimable=false` +
    `status=null` 至少有三种情况：从没参与、材料在审核、审核被拒——
    上游没有区分它们的字段，也没有「查询认证进度」的接口。所以这里只能写
    「未可领」，不能写「未参与」（那是我们猜的，猜错就是面板骗人）。
    """
    if not isinstance(item, dict):
        return {}
    state = item.get("status") or ""
    if item.get("claimable"):
        todo = "待领取"
    elif state in CREDIT_SETTLED:
        todo = "已结清"
    elif state:
        todo = state
    else:
        # 上游既没说能领、也没给状态。别显示成「已结清」——那会让人误以为
        # 积分到过账；也别显示成「未参与」——那需要认证进度，接口查不到。
        todo = "未可领"
    return {
        "id": item.get("campaignId"),
        "type": item.get("type") or "",
        "title": item.get("title") or item.get("type") or "",
        "amount": item.get("benefitAmount"),
        # 要真实事件才推进的两类：脚本造不出触发条件，只能人去操作
        "manual": item.get("type") in MANUAL_EVENT_TYPES,
        "claimable": bool(item.get("claimable")),
        "status": state,
        "state": todo,
        "trigger_mode": _trigger_mode(item),
    }


def credit_activity_views() -> list:
    """活动列表的展示结构，按 campaignId 排序保证顺序稳定。

    上游返回顺序不保证（实测是 3/4/2/1），面板和 CLI 都得排一下才不乱跳。
    """
    views = [v for v in (activity_view(i) for i in credit_activities()) if v]
    return sorted(views, key=lambda v: v["id"] or 0)


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


# 可自动领取的活动类型。type 只用于**展示分组**（面板标「需人工」），
# 不再作为领取门槛——见下方 _claimable_campaigns 的说明。
KNOWN_TYPES = ("USER_LOGIN", "NEW_USER_REGISTER", "STUDENT_CERTIFIED", "INVITE_USER")
# 要真实事件才推进的活动：脚本造不出触发条件，只能人去操作。
#   STUDENT_CERTIFIED  triggerEvent=student.certified  上传证件等审核
#   INVITE_USER        triggerEvent=invite.success      真人通过邀请链接注册
# 注意这只是**展示标记**，不是领取门槛——资格一旦被服务端认可
# （claimable 翻 true），领取动作和签到一样是纯收款。
MANUAL_EVENT_TYPES = ("STUDENT_CERTIFIED", "INVITE_USER")
# 已结清状态：CONFIRMED=已确认到账，CONSUMED=已确认且已用完。
# 一次性奖励走到这两个状态就不必再查了（NEW_USER_REGISTER 只发一次）。
CREDIT_SETTLED = ("CONFIRMED", "CONSUMED", "CLAIMED")
CREDIT_CHANNEL = "IDE"
# 邀请码生成端点。注意是 **POST-only**：GET 会回 `method GET not found` 404。
INVITE_PATH = "/v1/ops/invite/generate"
# 邀请活动的默认活动 ID。只作兜底——正常情况下从 delivery 列表里按
# type=INVITE_USER 找，campaignId 是自增字段，会随活动上下线漂。
INVITE_CAMPAIGN_ID = 3
# 服务端自动发放的活动：不用也不该 claim。NEW_USER_REGISTER 实测就是
# AUTO_GRANT——上游直接发到账，我们打一次 claim 只会白费一个请求。
# 排除它还防另一头：上游哪天给 AUTO_GRANT 活动也标 claimable=true 时，
# 我们会照旧不碰它，等它自己到账。
AUTO_GRANT_MODE = "AUTO_GRANT"


def _trigger_mode(item: dict) -> str:
    return (item.get("extra") or {}).get("triggerMode") or ""


def _claimable_campaigns(items: list) -> list:
    """挑出「能领」或「领了没确认」的活动，按 campaignId 排序保证顺序稳定。

    门槛只有一个：**上游说 claimable**。不按 type 白名单筛。

    为什么不再排除学生认证/邀请：这两个活动的 triggerEvent 分别是
    student.certified / invite.success，必须由真实事件（传证件审核通过、
    真人通过邀请链接注册）才会把 claimable 翻成 true——脚本造不出这个事件，
    服务端也会拒绝直接领取。而一旦 claimable=true，说明真实事件已经发生了，
    此时 claim→confirm 只是收款，不涉及替用户作身份声明或拉新。
    换句话说 type 白名单防不住「脚本代劳」，只会挡住「事后收款」。

    上游没有「查询认证状态」的接口（/v1/ops/* 全是活动/用量/事件类），
    认证进度只能去账号中心页面看；claimable 由 false 翻 true 就是
    服务端认可资格的唯一信号。

    真正要防的是 AUTO_GRANT：服务端自己会发的活动，我们不该去 claim。
    """
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # benefitUnit 空表示上游没给单位，别因此把活动漏掉；给了但不是 CREDIT
        # 的（比如送 token 的）不走积分领取链。
        unit = item.get("benefitUnit")
        if unit and unit != "CREDIT":
            continue
        if not item.get("campaignId"):
            continue
        if _trigger_mode(item) == AUTO_GRANT_MODE:
            continue
        # claimable=还有得领；status=CLAIMED=领了但没 confirm（上次可能断在中间）
        if item.get("claimable") or item.get("status") == "CLAIMED":
            out.append(item)
    return sorted(out, key=lambda i: i.get("campaignId") or 0)


def _campaign_label(item: dict) -> str:
    return "%s(#%s)" % (item.get("title") or item.get("type"),
                        item.get("campaignId"))


def do_credit_claim() -> str:
    """领取可自动领取的积分活动：claim → confirm → **回读验证**。

    返回一行中文汇报，供后台签到循环打印、也供面板展示。

    为什么要回读：claim 返回 200 只说明上游「受理了」，不代表积分真到账。
    L0NE-6/CodeArts-Daily 的四步闭环（delivery→claim→confirm→回读）就是为
    了避免「已受理但没到账」——只认 CONFIRMED/CONSUMED 算成功，其余算失败重试。

    另有两处防护：
      · claim 响应里的 campaignId 必须与请求一致，对不上就不进 confirm
        （否则可能把 A 活动的 claim 结果 confirm 到 B 活动上）；
      · status=CLAIMED 但 claimable=False 时跳过 claim 只补 confirm
        （上次断在两步之间，幂等补救）。
    """
    items = credit_activities()
    todo = _claimable_campaigns(items)
    if not todo:
        # 无可领 ≠ 无事可报：AUTO_GRANT 的奖励服务端自己发，同样到账了，
        # 汇报里要算上——用户在问「我的积分到没到」，不是「你领了没」。
        settled = [i for i in items if (i.get("status") or "") in CREDIT_SETTLED]
        if not settled:
            return "当前没有可领取的积分活动"
        return "今日福利已确认到账（%s）" % "、".join(
            _campaign_label(i) for i in settled)

    now_ms = int(datetime.datetime.now().timestamp() * 1000)
    done, failed = [], []
    for item in todo:
        cid = item.get("campaignId")
        label = _campaign_label(item)
        need_claim = bool(item.get("claimable"))

        if need_claim:
            key = f"claim_{cid}_{now_ms}"
            code, data = snap_request("POST", "/v1/ops/claim",
                                      body=json.dumps({"campaignId": cid,
                                                       "idempotentKey": key,
                                                       "channel": CREDIT_CHANNEL}))
            if code != 200 or data.get("code") != 0:
                failed.append("%s 领取失败 HTTP %s %s" % (label, code, str(data)[:120]))
                continue
            returned = str(data.get("campaignId")) if isinstance(data, dict) else ""
            if returned != str(cid):
                failed.append("%s 领取响应活动 ID 不匹配（要 %s 回 %s），未确认"
                              % (label, cid, returned or "空"))
                continue

        # claim 成功（或本来就只差 confirm）：确认到账
        ccode, cdata = snap_request("POST", "/v1/ops/confirm",
                                    body=json.dumps({"campaignId": cid}))
        if ccode != 200 or cdata.get("code") != 0:
            failed.append("%s confirm 失败 HTTP %s %s" % (label, ccode, str(cdata)[:120]))
            continue

        # 回读验证：状态没变成 CONFIRMED/CONSUMED 就不算成功
        after = next((i for i in credit_activities()
                      if i.get("campaignId") == cid), None)
        state = (after or {}).get("status") or ""
        if state in CREDIT_SETTLED:
            done.append("%s %s 积分" % (label, item.get("benefitAmount") or "?"))
        else:
            failed.append("%s confirm 后状态仍为 %s，未确认到账" % (label, state or "空"))

    parts = []
    if done:
        parts.append("已领取 " + "、".join(done))
    if failed:
        parts.append("失败 " + "；".join(failed))
    return "；".join(parts) if parts else "无可领取的积分活动"


def invite_campaign_id(items: list = None) -> int:
    """从活动列表里找邀请活动的 campaignId，找不到就退回硬编码值。

    为什么不直接用常量：campaignId 是服务端自增字段，会随活动上下线漂
    （邀请活动实测是 3，但 11-15 就结束了）。按 type=INVITE_USER 现找，
    找到几个就取最小的那个（最早的、通常是主活动）。

    参数 items 主要给测试用——生产路径传 None，函数自己去查。
    """
    if items is None:
        items = credit_activities()
    ids = sorted(i.get("campaignId") or 0 for i in items
                 if isinstance(i, dict) and i.get("type") == "INVITE_USER"
                 and i.get("campaignId"))
    return ids[0] if ids else INVITE_CAMPAIGN_ID


def generate_invite_code(campaign_id: int = None) -> dict:
    """生成（或取回）邀请码与邀请链接。

    这里**只拿链接**，绝不顺带 claim 邀请活动。邀请奖励必须由真实好友
    通过链接完成注册才会产生——服务端对直接领取邀请活动会返回
    「该活动类型需要先通过触发接口激活，无法直接领取」。造不出这个事件，
    也不该替用户去拉新。

    接口是幂等的「生成或取回」：同一个账号同一个活动反复调用返回同一条
    邀请码，不会每次换新链接。
    """
    if campaign_id is None:
        campaign_id = invite_campaign_id()
    code, data = snap_request("POST", INVITE_PATH,
                              body=json.dumps({"campaignId": campaign_id}))
    if code != 200:
        raise RuntimeError(f"生成邀请链接失败 HTTP {code}: {str(data)[:300]}")
    # ops 族的信封是 code=0 表示成功；上游偶尔 HTTP 200 但 code 非 0
    if isinstance(data, dict) and data.get("code") not in (None, 0):
        raise RuntimeError(f"生成邀请链接失败 {data.get('code')}: "
                           f"{data.get('message') or str(data)[:200]}")
    payload = (data or {}).get("data")
    if not isinstance(payload, dict):
        raise RuntimeError(f"邀请链接响应缺少 data: {str(data)[:300]}")
    return payload


def show_invite(campaign_id: int = None) -> None:
    """打印邀请码与链接，并说明为什么脚本不代领奖励。"""
    info = generate_invite_code(campaign_id)
    url = info.get("inviteUrl") or info.get("invite_url") or ""
    code = info.get("inviteCode") or info.get("invite_code") or ""
    if not url and not code:
        print("邀请链接: 未获取到（上游没返回 inviteCode / inviteUrl）")
        return
    cid = info.get("campaignId") if info.get("campaignId") is not None else campaign_id
    print(f"邀请活动: #{cid}")
    if code:
        print(f"邀请码: {code}")
    if url:
        print(f"邀请链接: {url}")
    used = info.get("activationCount")
    limit = info.get("maxActivations")
    if used is not None or limit is not None:
        print(f"已邀请: {used if used is not None else '?'} / "
              f"{limit if limit is not None else '?'} 人")
    valid = info.get("valid")
    if valid is False:
        print("注意: 上游标记该邀请码已失效（valid=false）")
    # 边界说清楚：链接发出去就行，奖励等服务端自己发
    print("说明: 把链接发给好友注册即可，奖励由服务端在好友注册成功后自动到账；"
          "脚本不代领邀请奖励（上游要求真实邀请事件）。")


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
        # 逐条列出所有活动。要真实事件的两类（学生认证/邀请好友）单独一行，
        # 标出该去哪操作——不是脚本漏领，是资格还没被服务端认可。
        views = credit_activity_views()
        plain = [v for v in views if not v["manual"]]
        if not plain:
            print("积分活动: 未找到活动")
        for v in plain:
            print("积分活动: %s(#%s) %s 积分 → %s" % (
                v["title"], v["id"], v["amount"] if v["amount"] is not None else "?",
                v["state"]))
        manual = [v for v in views if v["manual"]]
        if manual:
            print("需真实事件（资格认可后自动领）: " + "、".join(
                "%s %s 积分→%s" % (v["title"],
                                   v["amount"] if v["amount"] is not None else "?",
                                   v["state"])
                for v in manual))
    except Exception as error:
        print(f"积分活动: 查询失败 {str(error)[:120]}")


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
            print(do_credit_claim())
            show_credit_balance()
        elif action == "invite":
            # 可选传活动 ID：invite 3。默认从列表里现找，找不到用常量兜底
            if len(argv) > 2 and argv[2].isdigit():
                show_invite(int(argv[2]))
            else:
                show_invite()
        elif action == "models":
            print("免费模型列表:")
            show_models()
        else:
            # 默认：tokens 签到 + 积分签到 + 各项余额
            show_status()
            do_claim()
            show_balance()
            print(do_credit_claim())
            show_credit_balance()
    except RuntimeError as error:
        print(f"失败: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

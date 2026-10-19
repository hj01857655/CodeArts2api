# -*- coding: utf-8 -*-
"""管理面板接口离线测试：/admin/api/overview 的积分活动、邀请链接与并发闸门字段。

打桩 benefit.* 与 session.snapshot，不连上游。顺带校验 panel.html 的
render 契约——面板读的每个字段都必须真的存在于 overview 里，否则改后端时
前端会静默显示「—」（JS 里 data.x ?? "—" 不报错，只会一直显示占位符）。

面板面已从 server.py 拆到 admin/panel.py，缓存也在那；本测试打桩的
`admin.panel._invite_cache` 等就是它们的新家。仍经 server.app 发请求，
这样挂载链路（register(app, core) 的 core 动态查找）一并验到。
"""
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              write_through=True)
os.environ["DUMP"] = "0"
os.environ["CODEARTS_BENEFIT_AUTO_CLAIM"] = "0"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import server  # noqa: E402
from admin import panel as admin_panel  # noqa: E402
from lanes import benefit  # noqa: E402

server.API_KEY = ""
server._refresh_models_if_needed = lambda: None
server._current_creds = lambda: {"ak": "AK" + "X" * 24, "sk": "SKTEST", "token": None}

passed, failed = [], []


def check(name, condition, detail=""):
    (passed if condition else failed).append(name)
    print("%-56s %s %s" % (name, "PASS" if condition else "FAIL", detail))


# 上游实测返回顺序是 3/4/2/1（不保证有序）。注意 stub 打在上游层
# （credit_activities），排序交给真的 credit_activity_views——否则 overview
# 拿到的就是已经排好的视图，排序这条断言等于没验。
_RAW = [
    # triggerMode 照实测填：只有新人注册礼是 AUTO_GRANT，其余 MANUAL_CLAIM
    {"campaignId": 3, "type": "INVITE_USER", "title": "推荐码上有礼",
     "benefitAmount": 1000, "benefitUnit": "CREDIT", "claimable": False, "status": None,
     "extra": {"triggerMode": "MANUAL_CLAIM", "triggerEvent": "invite.success"}},
    {"campaignId": 4, "type": "NEW_USER_REGISTER", "title": "新用户注册送 4000 积分",
     "benefitAmount": 4000, "benefitUnit": "CREDIT", "claimable": False, "status": "CONSUMED",
     "extra": {"triggerMode": "AUTO_GRANT", "triggerEvent": "user.register"}},
    {"campaignId": 2, "type": "STUDENT_CERTIFIED", "title": "学生认证领4000积分",
     "benefitAmount": 4000, "benefitUnit": "CREDIT", "claimable": False, "status": None,
     "extra": {"triggerMode": "MANUAL_CLAIM", "triggerEvent": "student.certified"}},
    {"campaignId": 1, "type": "USER_LOGIN", "title": "每日签到领1000积分",
     "benefitAmount": 1000, "benefitUnit": "CREDIT", "claimable": False, "status": "CONFIRMED",
     "extra": {"triggerMode": "MANUAL_CLAIM", "triggerEvent": "user.login"}},
]
UPSTREAM_ORDER = [3, 4, 2, 1]


def _raw(cid):
    for item in _RAW:
        if item["campaignId"] == cid:
            return item
    return None


def stub_benefit():
    """按真实字段形状打桩，避免测试桩和上游脱节。"""
    server.benefit.claim_status = lambda: {"create_time": 1758000000000}
    server.benefit.balance = lambda: {"total_quota": 10000000, "total_balance": 9999444,
                                      "used_amount": 556, "daily_token_limit": 10000000,
                                      "daily_tokens_used": 556}
    server.benefit.credit_balance = lambda: {"total": 20588.0, "used": 16246.06,
                                             "remain": 4341.94}
    server.benefit.credit_activities = lambda: [dict(_raw(c)) for c in UPSTREAM_ORDER]
    server.benefit.credit_activity_views = benefit.credit_activity_views


def get_overview():
    resp = server.app.test_client().get("/admin/api/overview")
    return resp, (json.loads(resp.data) if resp.status_code == 200 else None)


stub_benefit()
admin_panel._credit_cache = {"at": 0.0, "data": None}
admin_panel._activity_cache = {"at": 0.0, "data": None}
admin_panel._invite_cache = {"at": 0.0, "data": None}

# 邀请链接的桩：形状照上游 data 字段填（inviteCode/inviteUrl/激活数/valid）
server.benefit.generate_invite_code = lambda campaign_id=None: {
    "id": 42, "inviteCode": "ABC123", "inviteUrl": "https://example/i/ABC123",
    "campaignId": 3, "status": "VALID", "maxActivations": 50,
    "activationCount": 2, "valid": True}

resp, data = get_overview()
check("overview 200", resp.status_code == 200, resp.status_code)
check("overview 是 JSON", isinstance(data, dict))

b = data.get("benefit") or {}
check("benefit 带 credit_activities", "credit_activities" in b, sorted(b))
check("活动有 4 条", len(b.get("credit_activities") or []) == 4,
      len(b.get("credit_activities") or []))
check("活动按 campaignId 排序（上游返回顺序不保证）",
      [a["id"] for a in b.get("credit_activities") or []] == [1, 2, 3, 4],
      [a["id"] for a in b.get("credit_activities") or []])

# ── 面板 render() 读的每个字段都必须真的存在 ────────────────────────────
acts = b.get("credit_activities") or []
check("活动条目带 title", all("title" in a for a in acts))
check("活动条目带 amount", all("amount" in a for a in acts))
check("活动条目带 state（面板直接显示）", all("state" in a for a in acts))
check("活动条目带 manual（面板标「需真实事件」）", all("manual" in a for a in acts))
check("活动条目带 trigger_mode", all("trigger_mode" in a for a in acts))
check("活动条目带 claimable（决定徽标配色）", all("claimable" in a for a in acts))
check("每日签到/新人礼的 manual 为假",
      [a["manual"] for a in acts if a["id"] in (1, 4)] == [False, False])
check("学生认证/邀请的 manual 为真",
      [a["manual"] for a in acts if a["id"] in (2, 3)] == [True, True])

# ── 积分余额三个字段（顶部统计卡） ──────────────────────────────────────
for field in ("credit_total", "credit_used", "credit_remain"):
    check("benefit.%s 存在" % field, field in b, sorted(b))

# ── 并发闸门字段（面板 renderSession 读的） ─────────────────────────────
s = data.get("session") or {}
for field in ("enabled", "limit", "in_use", "heartbeat", "interval"):
    check("session.%s 存在" % field, field in s, sorted(s))
check("session.limit 默认 3", s.get("limit") == 3, s.get("limit"))
check("session.heartbeat 默认开", s.get("heartbeat") is True, s.get("heartbeat"))
# in_use 的真实形状是 {闸门 key: 在途数}（多账号时每把钥匙一路），不是总数。
# 面板要把 values 加起来才是全局在途数——之前桩里写成了 int，测试过了但线上
# 渲染出来是 [object Object]。
check("session.in_use 是 dict（按闸门 key 分）", isinstance(s.get("in_use"), dict),
      type(s.get("in_use")).__name__)

# ── 邀请链接字段（面板 renderInvite 读的） ──────────────────────────────
inv = b.get("invite")
check("benefit.invite 存在", "invite" in b, sorted(b))
check("benefit.invite 是 dict", isinstance(inv, dict), type(inv).__name__)
for field in ("inviteCode", "inviteUrl", "campaignId", "valid",
              "activationCount", "maxActivations"):
    check("invite.%s 存在" % field, field in inv, sorted(inv))
check("invite 查询失败也有 dict 兜底",
      isinstance(admin_panel._invite_link_cached(server), dict))

# ── 缓存：第二次不打上游 ────────────────────────────────────────────────
calls = []
server.benefit.credit_activities = lambda: (calls.append(1), [dict(i) for i in _RAW])[1]
server.app.test_client().get("/admin/api/overview")
check("活动走短缓存（60s 内不重复打上游）", len(calls) == 0, len(calls))
admin_panel._activity_cache["at"] = 0.0
server.app.test_client().get("/admin/api/overview")
check("缓存过期后重新查询", len(calls) == 1, len(calls))

# ── 查询失败不能让 overview 500 ────────────────────────────────────────
def boom():
    raise RuntimeError("snap-access TLS 中断")


server.benefit.credit_activities = boom
admin_panel._activity_cache = {"at": 0.0, "data": None}
resp, data = get_overview()
check("活动查询失败 overview 仍 200", resp.status_code == 200, resp.status_code)
check("失败时活动为空列表", (data.get("benefit") or {}).get("credit_activities") == [],
      (data.get("benefit") or {}).get("credit_activities"))

# ── benefit 模块自身的排序与状态判定（不打桩 overview，直接打桩上游） ────
_orig_activities = benefit.credit_activities
benefit.credit_activities = lambda: [dict(i) for i in _RAW]
views = benefit.credit_activity_views()
check("credit_activity_views 按 campaignId 排序",
      [v["id"] for v in views] == [1, 2, 3, 4], [v["id"] for v in views])
check("status 为 null 判为「未可领」而非「已结清」",
      [v["state"] for v in views if v["id"] in (2, 3)] == ["未可领", "未可领"],
      [v["state"] for v in views if v["id"] in (2, 3)])
# 关键：不能推断认证进度。claimable=false + status=null 覆盖「从没参与」
# 「审核中」「被拒」三种情况，上游没有字段区分，也没有查询接口。
check("未可领不等于未参与（不推断认证进度）",
      "未参与" not in [v["state"] for v in views])
check("CONSUMED 判为已结清",
      next(v["state"] for v in views if v["id"] == 4) == "已结清")
check("CONFIRMED 判为已结清",
      next(v["state"] for v in views if v["id"] == 1) == "已结清")
check("manual 只对学生认证与邀请为真",
      sorted(v["id"] for v in views if v["manual"]) == [2, 3],
      sorted(v["id"] for v in views if v["manual"]))
check("trigger_mode 从 extra 里取出来",
      next(v["trigger_mode"] for v in views if v["id"] == 4) == "AUTO_GRANT",
      next(v["trigger_mode"] for v in views if v["id"] == 4))
_orig_view = benefit.activity_view
check("claimable=true 判为待领取",
      benefit.activity_view({"campaignId": 1, "type": "USER_LOGIN", "claimable": True,
                             "title": "每日签到领1000积分", "benefitAmount": 1000})["state"] == "待领取")
check("非 dict 条目被忽略（不炸）", benefit.activity_view(None) == {})
benefit.activity_view = _orig_view
benefit.credit_activities = _orig_activities

# ── panel.html 契约：render 引用的 id 都必须在 DOM 里 ───────────────────
html = open(os.path.join(ROOT, "panel.html"), encoding="utf-8").read()
import re  # noqa: E402

used = set(re.findall(r'\$\("([A-Za-z0-9_]+)"\)', html))
used |= set(re.findall(r'getElementById\("([A-Za-z0-9_]+)"\)', html))
declared = set(re.findall(r'id="([A-Za-z0-9_]+)"', html))
missing = sorted(used - declared)
check("panel.html 里 $(\"id\") 全部有对应元素", not missing, missing)

# overview 返回但面板没渲染的字段（防止前后端漂移）
rendered_ids = used
check("积分余额已接到面板",
      {"sCredit", "sCreditDetail"} <= rendered_ids, sorted({"sCredit", "sCreditDetail"} - rendered_ids))
check("并发闸门已接到面板",
      {"sessInUse", "sessHeartbeat", "sessionBadge"} <= rendered_ids)
check("活动列表已接到面板",
      {"activityList", "activityCount"} <= rendered_ids)
check("邀请卡片已接到面板",
      {"inviteCode", "inviteUrl", "inviteCount", "inviteBadge", "inviteNote"} <= rendered_ids,
      sorted({"inviteCode", "inviteUrl", "inviteCount", "inviteBadge", "inviteNote"} - rendered_ids))

# 邀请链接也要走缓存：幂等接口，刷一次面板打一次上游没意义
invite_calls = []
server.benefit.generate_invite_code = lambda campaign_id=None: (
    invite_calls.append(1), {"inviteCode": "ABC123", "inviteUrl": "u"})[1]
admin_panel._invite_cache = {"at": 0.0, "data": None}
server.app.test_client().get("/admin/api/overview")
server.app.test_client().get("/admin/api/overview")
check("邀请链接走短缓存（1h 内不重复打上游）", len(invite_calls) == 1, len(invite_calls))
admin_panel._invite_cache["at"] = 0.0
server.app.test_client().get("/admin/api/overview")
check("邀请缓存过期后重新查询", len(invite_calls) == 2, len(invite_calls))

# 上游抽风不能让 overview 500，也不能让面板拿不到字段
def invite_boom(campaign_id=None):
    raise RuntimeError("snap-access TLS 中断")


server.benefit.generate_invite_code = invite_boom
admin_panel._invite_cache = {"at": 0.0, "data": None}
resp, data = get_overview()
check("邀请查询失败 overview 仍 200", resp.status_code == 200, resp.status_code)
check("邀请失败时带 error 字段（面板显示原因）",
      "error" in (data.get("benefit") or {}).get("invite", {}),
      (data.get("benefit") or {}).get("invite"))

print()
print("通过 %d / %d" % (len(passed), len(passed) + len(failed)))
if failed:
    print("失败：")
    for name in failed:
        print("  - " + name)
    sys.exit(1)

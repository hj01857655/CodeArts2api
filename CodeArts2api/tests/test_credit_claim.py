# -*- coding: utf-8 -*-
"""lanes/benefit.py 积分活动领取逻辑离线测试（打桩 snap_request，不连上游）。

覆盖 do_credit_claim 的四步闭环与领取门槛，以及邀请链接（只拿链接、不代领）：
  1. 门槛是「上游说 claimable」，不按 type 白名单筛——学生认证/邀请在资格
     被认可后（claimable 翻 true）同样能领，那只是收款
  2. AUTO_GRANT 活动一律不 claim（服务端自己发，白打请求）
  3. claim 返回 200 但 campaignId 不匹配 → 不进 confirm
  4. confirm 后回读状态未变 → 判失败（避免「已受理但没到账」当成成功）
  5. status=CLAIMED 但 claimable=False → 跳过 claim 只补 confirm（幂等补救）
  6. 全部已结清 → 不写任何请求
  7. benefitUnit 非 CREDIT 的活动不领
  8. 邀请链接：POST /v1/ops/invite/generate，只拿链接，绝不碰 claim/confirm
"""
import contextlib as _ctx
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              write_through=True)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from lanes import benefit  # noqa: E402

passed, failed = [], []


def check(name, condition, detail=""):
    (passed if condition else failed).append(name)
    print("%-58s %s %s" % (name, "PASS" if condition else "FAIL", detail))


def campaign(cid, ctype, status="", claimable=False, amount=1000, unit="CREDIT",
             title=None, trigger_mode="MANUAL_CLAIM"):
    item = {"campaignId": cid, "type": ctype, "status": status,
            "claimable": claimable, "benefitAmount": amount, "benefitUnit": unit,
            "title": title or ctype}
    # 上游把 triggerMode 放在 extra 里；默认 MANUAL_CLAIM（每日签到就是它）
    if trigger_mode is not None:
        item["extra"] = {"triggerMode": trigger_mode}
    return item


class FakeSnap:
    """按脚本回放 snap_request；记录每次 (method, path, body)。

    deliveries 是一个 list 的 list：第 n 次 GET /v1/ops/delivery 返回第 n 个，
    用完后重复最后一个——这样「claim 前读一次、confirm 后回读一次」能拿到
    不同状态，正是验证回读逻辑的关键。
    """

    def __init__(self, deliveries, claim_result=None,
                 confirm_result=(200, {"code": 0})):
        self.deliveries = list(deliveries)
        self.claim_result = claim_result
        self.confirm_result = confirm_result
        self.calls = []
        self._reads = 0

    def __call__(self, method, path, body="", query=""):
        self.calls.append((method, path, body))
        if path.startswith("/v1/ops/delivery"):
            items = self.deliveries[min(self._reads, len(self.deliveries) - 1)]
            self._reads += 1
            return 200, {"data": {"items": items}}
        if path == "/v1/ops/claim":
            # 默认按请求里的 campaignId 回显（真实上游的行为）；
            # 需要构造"回错 id"的场景时才用固定的 claim_result。
            if self.claim_result is None:
                try:
                    cid = json.loads(body).get("campaignId")
                except Exception:
                    cid = None
                return 200, {"code": 0, "campaignId": cid}
            return self.claim_result
        if path == "/v1/ops/confirm":
            return self.confirm_result
        return 200, {}

    def paths(self, method=None):
        return [p for m, p, _ in self.calls if method is None or m == method]

    def bodies(self, path):
        return [b for m, p, b in self.calls if p == path]

    def reset(self):
        self.calls = []
        self._reads = 0


def run(snap):
    benefit.snap_request = snap
    snap.reset()
    return benefit.do_credit_claim()


# ── 1. 门槛是 claimable，不按 type 筛 ───────────────────────────────────
# 学生认证和邀请：资格被服务端认可后 claimable 翻 true，这时领只是收款。
# type 白名单防不住「脚本造触发事件」（服务端会拒绝），只会挡住事后收款。
snap = FakeSnap([
    [campaign(1, "USER_LOGIN", claimable=True, amount=1000),
     campaign(2, "STUDENT_CERTIFIED", claimable=True, amount=4000),
     campaign(3, "INVITE_USER", claimable=True, amount=1000),
     campaign(4, "NEW_USER_REGISTER", claimable=True, amount=4000,
              trigger_mode="AUTO_GRANT")],
    # 回读：claim 的都变成 CONFIRMED
    [campaign(1, "USER_LOGIN", status="CONFIRMED"),
     campaign(2, "STUDENT_CERTIFIED", status="CONFIRMED"),
     campaign(3, "INVITE_USER", status="CONFIRMED"),
     campaign(4, "NEW_USER_REGISTER", status="CONFIRMED",
              trigger_mode="AUTO_GRANT")],
])
msg = run(snap)
claimed = snap.bodies("/v1/ops/claim")
check("claimable 的三个活动都 claim 了", len(claimed) == 3, len(claimed))
check("AUTO_GRANT 活动不 claim（服务端自己发）",
      not any('"campaignId": 4' in b or '"campaignId":4' in b for b in claimed), claimed)
check("学生认证资格认可后也领", any('"campaignId": 2' in b or '"campaignId":2' in b for b in claimed))
check("邀请好友资格认可后也领", any('"campaignId": 3' in b or '"campaignId":3' in b for b in claimed))
check("回读验证通过才报成功", "已领取" in msg, msg)
check("领了的三个都出现在汇报里", "#1" in msg and "#2" in msg and "#3" in msg, msg)
check("每个都走到 confirm", len(snap.bodies("/v1/ops/confirm")) == 3)
check("claim 带幂等键", all("idempotentKey" in b for b in claimed))
check("claim 带 channel=IDE", all("IDE" in b for b in claimed))

# ── 2. claim 200 但 campaignId 不匹配 → 不进 confirm ────────────────────
snap = FakeSnap([
    [campaign(1, "USER_LOGIN", claimable=True)],
    [campaign(1, "USER_LOGIN", claimable=True)],
], claim_result=(200, {"code": 0, "campaignId": 99}))
msg = run(snap)
check("campaignId 不匹配时判失败", "不匹配" in msg, msg)
check("campaignId 不匹配时不 confirm", snap.bodies("/v1/ops/confirm") == [])

# ── 3. confirm 后回读状态没变 → 判失败 ─────────────────────────────────
snap = FakeSnap([
    [campaign(1, "USER_LOGIN", claimable=True)],
    [campaign(1, "USER_LOGIN", claimable=True)],   # 回读：仍是 claimable
])
msg = run(snap)
check("回读状态未变判失败", "未确认到账" in msg, msg)
check("回读失败不算已领取", "已领取" not in msg, msg)

# ── 4. status=CLAIMED 但 claimable=False → 只补 confirm ────────────────
snap = FakeSnap([
    [campaign(1, "USER_LOGIN", status="CLAIMED", claimable=False)],
    [campaign(1, "USER_LOGIN", status="CONFIRMED")],
])
msg = run(snap)
check("CLAIMED 状态补 confirm 而不重新 claim", snap.bodies("/v1/ops/claim") == [])
check("补 confirm 后回读成功", "已领取" in msg, msg)

# ── 5. 全部已结清 → 不写任何请求 ───────────────────────────────────────
# campaign 4 实测 triggerMode=AUTO_GRANT（服务端自动发），它结清了也不该
# 出现在「待你操作」的汇报里。
snap = FakeSnap([
    [campaign(1, "USER_LOGIN", status="CONFIRMED"),
     campaign(4, "NEW_USER_REGISTER", status="CONSUMED", trigger_mode="AUTO_GRANT")],
])
msg = run(snap)
check("已结清时不发写请求",
      snap.bodies("/v1/ops/claim") == [] and snap.bodies("/v1/ops/confirm") == [])
check("已结清时汇报明确", "已确认到账" in msg, msg)
check("CONSUMED 也算结清（新人礼已用完）", "CONSUMED" in msg or "#4" in msg, msg)

# ── 6. benefitUnit 非 CREDIT 不领 ──────────────────────────────────────
snap = FakeSnap([
    [campaign(1, "USER_LOGIN", claimable=True, unit="TOKEN")],
])
msg = run(snap)
check("非 CREDIT 单位不领", snap.bodies("/v1/ops/claim") == [], msg)
check("非 CREDIT 时不报成功", "已领取" not in msg, msg)

# benefitUnit 为空（上游没给）不应被漏掉
snap = FakeSnap([
    [campaign(1, "USER_LOGIN", claimable=True, unit="")],
    [campaign(1, "USER_LOGIN", status="CONFIRMED")],
])
msg = run(snap)
check("benefitUnit 为空仍可领", len(snap.bodies("/v1/ops/claim")) == 1, msg)

# ── 7. 没有任何活动 ────────────────────────────────────────────────────
snap = FakeSnap([[]])
msg = run(snap)
check("无活动时不报成功", "已领取" not in msg, msg)
check("无活动时不发写请求", not snap.paths("POST"))

# ── 8. claim HTTP 失败 ─────────────────────────────────────────────────
snap = FakeSnap([[campaign(1, "USER_LOGIN", claimable=True)]],
                claim_result=(500, {"code": 1, "msg": "boom"}))
msg = run(snap)
check("claim HTTP 500 判失败", "失败" in msg and "500" in msg, msg)
check("claim 失败不 confirm", snap.bodies("/v1/ops/confirm") == [])

# ── 9. 部分成功要分别汇报 ──────────────────────────────────────────────
snap = FakeSnap([
    [campaign(1, "USER_LOGIN", claimable=True),
     campaign(3, "INVITE_USER", claimable=True)],
    [campaign(1, "USER_LOGIN", status="CONFIRMED"),
     campaign(3, "INVITE_USER", claimable=True)],   # 3 号没到账
])
msg = run(snap)
check("部分成功时成功与失败都列出", "已领取" in msg and "失败" in msg, msg)
check("失败的那条带上活动号", "#3" in msg, msg)

# ── 10. 分类函数单测 ───────────────────────────────────────────────────
items = [campaign(1, "USER_LOGIN", claimable=True),
         campaign(2, "STUDENT_CERTIFIED", claimable=True),
         campaign(4, "NEW_USER_REGISTER", status="CLAIMED", trigger_mode="AUTO_GRANT")]
picked = benefit._claimable_campaigns(items)
# 4 号是 AUTO_GRANT：服务端自己发的活动，连「补 confirm」都不该我们干。
check("_claimable_campaigns 不看 type，claimable 就挑",
      [i["campaignId"] for i in picked] == [1, 2], [i["campaignId"] for i in picked])
check("_claimable_campaigns 按 campaignId 排序",
      [i["campaignId"] for i in benefit._claimable_campaigns(
          [campaign(9, "USER_LOGIN", claimable=True),
           campaign(2, "USER_LOGIN", claimable=True)])] == [2, 9])
check("AUTO_GRANT 活动不进待领列表",
      benefit._claimable_campaigns(
          [campaign(4, "NEW_USER_REGISTER", claimable=True,
                    trigger_mode="AUTO_GRANT")]) == [])
check("AUTO_GRANT 但状态是 CLAIMED 也不补 confirm",
      benefit._claimable_campaigns(
          [campaign(4, "NEW_USER_REGISTER", status="CLAIMED", claimable=False,
                    trigger_mode="AUTO_GRANT")]) == [])
check("triggerMode 缺失时照常按 claimable 判断",
      [i["campaignId"] for i in benefit._claimable_campaigns(
          [campaign(7, "USER_LOGIN", claimable=True, trigger_mode=None)])] == [7])
check("CREDIT_SETTLED 含 CONSUMED", "CONSUMED" in benefit.CREDIT_SETTLED)
check("MANUAL_EVENT_TYPES 只含学生认证与邀请",
      benefit.MANUAL_EVENT_TYPES == ("STUDENT_CERTIFIED", "INVITE_USER"),
      benefit.MANUAL_EVENT_TYPES)

# ── 11. 邀请链接：只拿链接，绝不顺带 claim ───────────────────────────────
# 邀请奖励必须由真实好友通过链接注册才会产生。服务端对直接领取邀请活动会回
# 「该活动类型需要先通过触发接口激活，无法直接领取」——所以这里只能拿链接。
class FakeInvite:
    """只回 invite/generate；任何 claim/confirm 调用都会炸。"""

    def __init__(self, deliveries, invite_result):
        self.deliveries = deliveries
        self.invite_result = invite_result
        self.calls = []

    def __call__(self, method, path, body="", query=""):
        self.calls.append((method, path, body))
        if path.startswith("/v1/ops/delivery"):
            return 200, {"data": {"items": self.deliveries}}
        if path == benefit.INVITE_PATH:
            return self.invite_result
        raise AssertionError("邀请链接功能不该调用 %s %s" % (method, path))

    def bodies(self, path):
        return [b for m, p, b in self.calls if p == path]


INVITE_ITEMS = [
    campaign(1, "USER_LOGIN", status="CONFIRMED"),
    campaign(2, "STUDENT_CERTIFIED"),
    campaign(3, "INVITE_USER", amount=1000, title="推荐码上有礼"),
    campaign(18, "INVITE_USER", amount=188, title="国庆邀请有礼"),
]
INVITE_OK = (200, {"code": 0, "message": "ok", "data": {
    "id": 42, "inviteCode": "ABC123", "inviteUrl": "https://example/i/ABC123",
    "campaignId": 3, "status": "VALID", "maxActivations": 50,
    "activationCount": 2, "valid": True}})

snap = FakeInvite(INVITE_ITEMS, INVITE_OK)
benefit.snap_request = snap
cid = benefit.invite_campaign_id(INVITE_ITEMS)
check("邀请活动 ID 从列表里现找（多个取最小）", cid == 3, cid)
info = benefit.generate_invite_code(cid)
check("邀请码取到", info.get("inviteCode") == "ABC123", info.get("inviteCode"))
check("邀请链接取到", info.get("inviteUrl") == "https://example/i/ABC123")
check("邀请码请求体带 campaignId",
      '"campaignId": 3' in snap.bodies(benefit.INVITE_PATH)[0],
      snap.bodies(benefit.INVITE_PATH)[0])
check("邀请功能不碰 claim/confirm",
      all(p in (benefit.INVITE_PATH,) or p.startswith("/v1/ops/delivery")
          for _, p, _ in snap.calls),
      [p for _, p, _ in snap.calls])
check("INVITE_PATH 是 POST-only 的那一个",
      benefit.INVITE_PATH == "/v1/ops/invite/generate", benefit.INVITE_PATH)

# 没有 INVITE_USER 活动时退回常量，不该抛异常
check("列表里没有邀请活动时退回常量兜底",
      benefit.invite_campaign_id([campaign(1, "USER_LOGIN")])
      == benefit.INVITE_CAMPAIGN_ID)

# 上游 HTTP 200 但 code 非 0（ops 族的成功判据是 code=0）
benefit.snap_request = FakeInvite(INVITE_ITEMS, (200, {"code": 40001, "message": "no"}))
try:
    benefit.generate_invite_code(3)
    check("code 非 0 判失败", False, "没抛异常")
except RuntimeError as error:
    check("code 非 0 判失败", "40001" in str(error), str(error)[:80])

# HTTP 非 200
benefit.snap_request = FakeInvite(INVITE_ITEMS, (500, {}))
try:
    benefit.generate_invite_code(3)
    check("HTTP 500 判失败", False, "没抛异常")
except RuntimeError as error:
    check("HTTP 500 判失败", "500" in str(error), str(error)[:80])

# data 缺失（上游返回空信封）
benefit.snap_request = FakeInvite(INVITE_ITEMS, (200, {"code": 0, "data": None}))
try:
    benefit.generate_invite_code(3)
    check("data 缺失判失败", False, "没抛异常")
except RuntimeError as error:
    check("data 缺失判失败", "data" in str(error), str(error)[:80])

# 打印函数：链接/码/激活数都要出现，且明确说「不代领」
import contextlib as _ctx
benefit.snap_request = FakeInvite(INVITE_ITEMS, INVITE_OK)
buf = io.StringIO()
with _ctx.redirect_stdout(buf):
    benefit.show_invite(3)
out = buf.getvalue()
check("show_invite 打印邀请码", "ABC123" in out, out.replace("\n", " | ")[:120])
check("show_invite 打印邀请链接", "https://example/i/ABC123" in out)
check("show_invite 打印激活进度", "2 / 50" in out, out.replace("\n", " | ")[:160])
check("show_invite 说明不代领奖励", "不代领" in out)

# valid=false 要提示失效
benefit.snap_request = FakeInvite(INVITE_ITEMS, (200, {"code": 0, "data": {
    "inviteCode": "X", "inviteUrl": "u", "valid": False}}))
buf = io.StringIO()
with _ctx.redirect_stdout(buf):
    benefit.show_invite(3)
check("valid=false 时提示失效", "失效" in buf.getvalue(), buf.getvalue()[:120])

# 一条都没返回时不谎称拿到链接
benefit.snap_request = FakeInvite(INVITE_ITEMS, (200, {"code": 0, "data": {}}))
buf = io.StringIO()
with _ctx.redirect_stdout(buf):
    benefit.show_invite(3)
check("空 data 不谎称拿到链接", "未获取到" in buf.getvalue(), buf.getvalue()[:120])

print()
print("通过 %d / %d" % (len(passed), len(passed) + len(failed)))
if failed:
    print("失败：")
    for name in failed:
        print("  - " + name)
    sys.exit(1)

# -*- coding: utf-8 -*-
"""admin/checkin.py — 后台每日自动签到线程。

放 admin/ 而不是 lanes/：它是**编排**（查状态 → 领 Token → 领积分 → 打日志），
不是单纯的接口调用。真正的领取动作全在 lanes/benefit.py。

从 server.py 搬出来时保留了一个细节：`_last_checkin_date` 只在**领取成功**后
才标记。查不到状态、上游挂了都不标记，下一小时自动重试——否则一次 TLS 抽风
会让当天再也不签。
"""
import datetime
import time

from admin.panel import _invalidate_credit_caches

_last_checkin_date = None


def benefit_checkin_loop(core):
    """后台每日自动签到领取限时福利（幂等），成功后打印余额。

    启动即签到一次，之后每小时检查是否跨入新的一天；跨了就再签。
    先查签到状态，当天已签则跳过；领取失败不标记日期，下一小时自动重试。

    注意：这里走的 `benefit.claim()` 是 opengw 的**福利 Token 通道**。2026-10 起
    该通道对相当一部分账号稳定返回 `4000 invalid channel, group not found`
    （渠道按账号下线/迁移，不是签名问题——同一套 AK/SK 打 snap-access 的
    `/v1/ops/*` 完全正常）。因此这个循环失败是常态，真正的每日收益来自
    `do_credit_claim()` 那条积分线；详见 lanes/benefit.py 的模块说明。
    """
    global _last_checkin_date
    logger = core.logger
    benefit = core.benefit
    while True:
        today = datetime.date.today()
        if _last_checkin_date != today:
            # 两条通道互不依赖，必须分开 try：福利 Token 通道（opengw）下线时
            # 它会稳定抛 4000，如果和积分放在同一个 try 里，异常会直接跳过
            # 下面整段积分领取——那才是现在唯一还能拿到收益的通道。
            # 两条通道各自返回 (汇报文本, 是否已有确定结论)。只有「领到 / 今日已领」
            # 才算有结论；通道不可达不算，否则一次上游抽风会把当天永久标记为已签。
            token_text, token_done = _claim_token_channel(benefit)
            credit_text, credit_done = _claim_credit_channel(benefit)
            if token_done or credit_done:
                _last_checkin_date = today
            detail = token_text + credit_text
            print(f"[签到] {today}{detail}" if detail
                  else f"[签到] {today} 两条通道均未取得结果，1 小时后重试")
        time.sleep(3600)


def _claim_token_channel(benefit):
    """福利 Token 通道（opengw），返回 (汇报文本, 是否已有确定结论)。

    该通道 2026-10 起对部分账号稳定返回 `4000 invalid channel, group not found`
    （渠道按账号下线）。这是「渠道没开」而非「今天没签到」，所以判为
    done=True：渠道不存在的话，下一小时再来也只是再失败一次。
    """
    try:
        before = time.time()
        status = benefit.claim_status()
        last_ms = status.get("create_time")
        if last_ms and datetime.datetime.fromtimestamp(
                last_ms / 1000).date() == datetime.date.today():
            # 上游记录说今天签过就够了，不再多打一次 balance（同一渠道多半一起挂）
            return "", True
        claim_result = benefit.claim()
        claim_ms = (claim_result or {}).get("create_time")
        text = ""
        try:
            info = benefit.balance()
            text = " Token %s/%s" % (
                format(info.get("total_balance") or 0, ","),
                format(info.get("total_quota") or 0, ","))
        except Exception:
            pass
        if claim_ms and claim_ms / 1000 < before - 10:
            return text + "（福利今日已签）", True
        return text + "（福利领取成功）", True
    except Exception as error:
        message = str(error)
        if "invalid channel" in message or "group not found" in message:
            return " 福利 Token 通道未开通，已跳过", True
        # 签名/网络类错误：确实没结论，留到下一小时重试
        return " 福利 Token 通道失败（%s）" % message, False


def _claim_credit_channel(benefit):
    """积分通道（snap-access /v1/ops/*），返回 (汇报文本, 是否已有确定结论)。"""
    try:
        credit_status = benefit.do_credit_claim()
    except Exception as error:
        return "，积分领取失败: %s" % error, False
    credit_text = ""
    try:
        _invalidate_credit_caches()
        credits = benefit.credit_balance()
        if credits:
            credit_text = "，积分 %s/%s" % (
                format(credits.get("remain"), ","), format(credits.get("total"), ","))
    except Exception:
        pass
    # do_credit_claim 返回的一定是结论（已领取 / 今日已确认 / 失败原因），
    # 只有它在内部抛异常才算没结论。
    return credit_text + "（%s）" % credit_status, True

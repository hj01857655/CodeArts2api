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
    """
    global _last_checkin_date
    logger = core.logger
    benefit = core.benefit
    while True:
        today = datetime.date.today()
        if _last_checkin_date != today:
            try:
                # 先查签到状态，当天已签则跳过
                try:
                    status = benefit.claim_status()
                    last_ms = status.get("create_time")
                    if last_ms and datetime.datetime.fromtimestamp(last_ms / 1000).date() == today:
                        _last_checkin_date = today
                        print(f"[签到] {today} 当天已签到，跳过")
                        time.sleep(3600)
                        continue
                except Exception:
                    pass  # 查不到状态就继续尝试领取
                before = time.time()
                claim_result = benefit.claim()
                _last_checkin_date = today
                info = benefit.balance()
                total = info.get("total_quota") or 0
                remain = info.get("total_balance") or 0
                claim_ms = (claim_result or {}).get("create_time")
                # 积分通道（每日 1000 积分，agent 模型消耗）与福利同一条日志汇报，幂等安全
                credit_text = ""
                try:
                    credit_status = benefit.do_credit_claim()
                    _invalidate_credit_caches()
                    credits = benefit.credit_balance()
                    if credits:
                        credit_text = f"，积分 {credits.get('remain'):,}/{credits.get('total'):,}（{credit_status}）"
                except Exception as credit_error:
                    credit_text = f"，积分查询失败: {credit_error}"
                if claim_ms and claim_ms / 1000 < before - 10:
                    print(f"[签到] {today} 当天已签到，Token {remain:,}/{total:,}{credit_text}")
                else:
                    print(f"[签到] {today} 福利领取成功，Token {remain:,}/{total:,}{credit_text}")
            except Exception as error:
                print(f"[签到] {today} 限时福利领取失败: {error}")
        time.sleep(3600)

# -*- coding: utf-8 -*-
"""admin/panel.py — 面板页面与 /admin/api/* 接口。

只做「把 server 各处的状态汇成一个 JSON」，不含业务逻辑：
  · 凭证有效期 → credential
  · 免费额度 + 积分余额/活动 + 邀请链接 → benefit
  · 并发闸门快照 → session
  · 模型列表 → SUPPORTED_MODELS

三个短缓存（积分余额 60s / 活动 60s / 邀请链接 1h）放在这里，因为只有
面板这一个消费方。签到成功后由调用方把 `at` 清零强制刷新，让面板立刻看到
「待领取 → 已结清」的变化。

上游两个 host 都会间歇 TLS 抽风（snap-access 更甚），所以每一项单独降级：
一个接口挂掉不该让整个 overview 500，也不该拖死其余卡片。
"""
import datetime
import json
import os
import time

from flask import Response, jsonify, request

PANEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "panel.html")

_credit_cache = {"at": 0.0, "data": None}
_activity_cache = {"at": 0.0, "data": None}
_invite_cache = {"at": 0.0, "data": None}


def _credit_balance_cached(core, max_age: int = 60) -> dict:
    """积分余额短缓存；面板刷新不必每次打上游。"""
    now = time.time()
    if _credit_cache["data"] is None or now - _credit_cache["at"] > max_age:
        try:
            _credit_cache["data"] = core.benefit.credit_balance()
        except Exception as error:
            core.logger.warning("积分余额查询失败: %s", error)
            if _credit_cache["data"] is None:
                _credit_cache["data"] = {}
        _credit_cache["at"] = now
    return _credit_cache["data"]


def _credit_activities_cached(core, max_age: int = 60) -> list:
    """积分活动列表短缓存，结构与 benefit.credit_activity_views() 一致。

    活动状态一天只翻一次，缓存 60 s 足够；签到成功后由调用方把 `at` 清零强制刷新，
    让面板立刻看到「待领取 → 已结清」的变化。
    """
    now = time.time()
    if _activity_cache["data"] is None or now - _activity_cache["at"] > max_age:
        try:
            _activity_cache["data"] = core.benefit.credit_activity_views()
        except Exception as error:
            core.logger.warning("积分活动查询失败: %s", error)
            if _activity_cache["data"] is None:
                _activity_cache["data"] = []
        _activity_cache["at"] = now
    return _activity_cache["data"]


def _invite_link_cached(core, max_age: int = 3600) -> dict:
    """邀请链接短缓存（默认一小时）。

    邀请码是「生成或取回」的幂等接口，同一个账号反复调返回同一条，
    没必要每次刷面板都打一次上游。缓存里也存错误信息，避免上游抽风时
    面板每次刷新都干等一个 30 s 超时。
    """
    now = time.time()
    if _invite_cache["data"] is None or now - _invite_cache["at"] > max_age:
        info = {}
        try:
            info = core.benefit.generate_invite_code()
        except Exception as error:
            core.logger.warning("邀请链接查询失败: %s", error)
            info = {"error": str(error)[:200]}
        _invite_cache["data"] = info
        _invite_cache["at"] = now
    return _invite_cache["data"]


def _invalidate_credit_caches() -> None:
    """清零三个缓存的时间戳，下次读取强制回上游。

    签到成功后调用：面板要立刻看到「待领取 → 已结清」和新的积分余额。
    """
    _credit_cache["at"] = 0.0
    _activity_cache["at"] = 0.0


def _cred_info(core) -> dict:
    """凭证卡：来源 / AK 掩码 / 过期时间与剩余有效期。"""
    creds = core._current_creds()
    notice = core.credential_provider.token_notice(creds)
    expires_at = creds.get("expires_at") or ""
    remain_text = "—"
    valid = bool(creds.get("token"))
    if expires_at and valid:
        try:
            exp = datetime.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            remain = (exp - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            if remain > 0:
                hours = int(remain // 3600)
                mins = int((remain % 3600) // 60)
                remain_text = f"{hours} 小时 {mins} 分钟"
            else:
                remain_text = "已过期"
                valid = False
        except ValueError:
            pass
    return {
        "source": "auths 文件" if creds.get("_auth_file") else ("CodeArts IDE 会话" if valid else "未找到"),
        "ak": creds.get("ak", ""),
        "expires_at": expires_at,
        "valid": valid,
        "remain_text": remain_text,
        "notice": notice or "正常",
    }


def _benefit_info(core) -> dict:
    """benefit 卡：每日额度 + 积分余额 + 活动列表 + 邀请链接。

    四项各自 try：opengw（额度）和 snap-access（积分/活动/邀请）是两个 host，
    挂哪个都不该让整份 overview 失败。
    """
    benefit_info = {}
    try:
        status = core.benefit.claim_status()
        # create_time 为毫秒时间戳，直接透传（前端 new Date 按毫秒解析）
        benefit_info["last_checkin"] = status.get("create_time") or None
    except Exception:
        benefit_info["last_checkin"] = None
    try:
        bal = core.benefit.balance()
        benefit_info.update(bal)
    except Exception:
        pass
    # 积分通道（snap-access 网关）与福利额度同属 benefit，一并返回
    credits = _credit_balance_cached(core)
    if credits:
        benefit_info["credit_total"] = credits.get("total")
        benefit_info["credit_used"] = credits.get("used")
        benefit_info["credit_remain"] = credits.get("remain")
    # 积分活动：全部列出来（按 claimable 领，不按 type 筛）
    benefit_info["credit_activities"] = _credit_activities_cached(core)
    # 邀请链接：只拿链接给人分享，不代领奖励
    benefit_info["invite"] = _invite_link_cached(core)
    return benefit_info


def register(app, core):
    """挂载面板页与 /admin/api/*。core = server 模块（动态查找）。"""
    logger = core.logger

    @app.route("/", methods=["GET"])
    @app.route("/admin", methods=["GET"])
    def serve_panel():
        with open(PANEL_PATH, "r", encoding="utf-8") as f:
            html = f.read()
        # 将 key 注入面板页，前端 fetch 自动携带；面板仅监听本机，注入面不扩大暴露面
        if core.API_KEY:
            html = html.replace(
                "</head>",
                f"<script>window.__API_KEY__ = {json.dumps(core.API_KEY)};</script>\n</head>",
                1,
            )
        return Response(html, content_type="text/html; charset=utf-8")

    @app.route("/admin/api/overview", methods=["GET", "OPTIONS"])
    def admin_overview():
        if request.method == "OPTIONS":
            return core._cors(Response(""))
        core._refresh_models_if_needed()
        models = []
        # SUPPORTED_MODELS 由 server 动态同步（重绑定而非原地改），必须经 core 现查
        for mid in core.SUPPORTED_MODELS:
            detail = core.MODEL_DETAILS.get(mid, {})
            models.append({"id": mid, "channel": detail.get("channel", "agent"),
                           "name": detail.get("name", mid)})
        # 并发闸门：满员时面板能直接看到是哪几路在跑，不用去翻日志
        snap = core.session.snapshot()
        return jsonify({
            "service": "CodeArts2api",
            "credential": _cred_info(core),
            "models": models,
            "benefit": _benefit_info(core),
            "schedule": {"auto_checkin": core.BENEFIT_AUTO_CLAIM},
            "session": {
                "enabled": core.SESSION_GATE_ENABLED,
                "limit": snap["limit"],
                "in_use": snap["in_use"],
                "heartbeat": snap["heartbeat"],
                "interval": snap["interval"],
            },
        })

    @app.route("/admin/api/checkin", methods=["POST", "OPTIONS"])
    def admin_checkin():
        if request.method == "OPTIONS":
            return core._cors(Response(""))
        try:
            core.benefit.claim()
            messages = []
            bal = core.benefit.balance()
            remain = bal.get("total_balance") or 0
            total = bal.get("total_quota") or 0
            messages.append(f"Token {remain:,}/{total:,}")
            try:
                credit_status = core.benefit.do_credit_claim()
                _invalidate_credit_caches()
                credits = core.benefit.credit_balance()
                if credits:
                    messages.append(f"积分 {credits.get('remain'):,}/{credits.get('total'):,}（{credit_status}）")
            except Exception as credit_error:
                logger.warning("积分领取失败（不影响 Token 签到）: %s", credit_error)
            return jsonify({"ok": True, "message": "签到成功：" + "，".join(messages)})
        except Exception as e:
            return jsonify({"ok": False, "message": f"签到失败: {e}"}), 500

    @app.route("/admin/api/reload-models", methods=["POST", "OPTIONS"])
    def admin_reload_models():
        if request.method == "OPTIONS":
            return core._cors(Response(""))
        ok = core._refresh_models()
        return jsonify({"ok": ok, "message": f"已同步 {len(core.SUPPORTED_MODELS)} 个模型" if ok else "同步失败，使用缓存"})

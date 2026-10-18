# -*- coding: utf-8 -*-
"""
CodeArts OAuth2 PKCE 登录 → 保存 auths/codearts-{user_id}.json。

脱离 IDE 独立获取华为云 CodeArts 临时凭证（AK/SK/securityToken + refresh_token），
与 Go 版 cmd/login 对齐。凭证落盘后由 credential_provider.py 自动加载和续期。

用法：
    python lanes/oauth.py              # 本机有浏览器，自动打开
    python lanes/oauth.py --print-only # 服务器模式，只打印链接，手动粘贴回调 URL
"""
# 作为独立入口（python lanes/oauth.py）运行时仓库根不在 sys.path 上，
# 兜底把仓库根加进来，否则 `from lanes import credential_provider` 失败。
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

import base64  # noqa: E402
import hashlib  # noqa: E402
import http.server  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import secrets  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.parse  # noqa: E402
import webbrowser  # noqa: E402

import requests  # noqa: E402

from lanes import credential_provider  # noqa: E402

CLIENT_ID = "codearts-agent"
PORTAL_HOST = "https://codearts.huaweicloud.com/portal"
STS_TOKEN_URL = "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens"
REDIRECT_PATH = "/oauth/callback"
PLUGIN_NAME = "snap_AIIDE"
PLUGIN_VERSION = "5.2.0"
AUTHS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auths")


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64u_int(value: int, length: int = 32) -> str:
    return _b64u(value.to_bytes(length, "big"))


def generate_pkce() -> tuple:
    """生成 PKCE code_verifier + code_challenge (S256)。"""
    verifier = _b64u(os.urandom(64))
    challenge = _b64u(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def generate_dpop_key() -> dict:
    """生成 DPoP ES256/P-256 密钥对，返回 JWK（含私钥 d）。"""
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    nums = private_key.private_numbers()
    pub = private_key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64u_int(pub.x),
        "y": _b64u_int(pub.y),
        "d": _b64u_int(nums.private_value),
    }


def build_authorize_url(ticket_id: str, code_challenge: str, port: int) -> str:
    """构造华为云 OAuth 授权链接（对齐 huaweicloud.authentication 扩展）。"""
    params = urllib.parse.urlencode({
        "theme": "2",
        "locale": "zh-cn",
        "uri_scheme": CLIENT_ID,
        "client_id": CLIENT_ID,
        "port": port,
        "code_challenge": code_challenge,
        "code_challenge_method": "SHA-256",
        "ticket_id": ticket_id,
        "plugin-name": PLUGIN_NAME,
        "plugin-version": PLUGIN_VERSION,
    })
    return f"{PORTAL_HOST}/authorize?{params}"


def exchange_code(code: str, verifier: str, port: int, dpop_jwk: dict) -> dict:
    """用授权码换 STS 临时凭证（OAuth2 authorization_code grant）。"""
    redirect_uri = f"http://127.0.0.1:{port}{REDIRECT_PATH}"
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    })
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "DPoP": credential_provider._dpop_jwt(dpop_jwk, STS_TOKEN_URL),
        "User-Agent": "CodeArts2api/1.0",
    }
    resp = requests.post(STS_TOKEN_URL, data=data, headers=headers, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"换码失败 HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    # 兼容新旧通道字段名
    creds = body.get("credentials") or body.get("credential") or {}
    access_key = creds.get("access_key_id") or creds.get("access")
    secret_key = creds.get("secret_access_key") or creds.get("secret")
    token = creds.get("security_token") or creds.get("securityToken")
    if not (access_key and secret_key and token):
        raise RuntimeError(f"换码响应缺少凭证: {resp.text[:300]}")
    return {
        "user_id": body.get("user_id", ""),
        "user_name": body.get("user_name", ""),
        "domain_id": body.get("domain_id", ""),
        "access_key_id": access_key,
        "secret_access_key": secret_key,
        "security_token": token,
        "expiration": creds.get("expiration") or creds.get("expires_at", ""),
        "refresh_token": body.get("refresh_token", ""),
    }


def decode_user_from_refresh_token(refresh_token: str) -> dict:
    """从 refresh_token JWT 的 user_profile 字段解出用户身份。

    refresh_token 是 RS256 JWT，payload 里嵌了 user_profile（无签名 JWT），
    包含 principal_id（=user_id）、account_id（=domain_id）、account_name（=user_name）。
    只解码不验签——用户信息仅用于显示和文件命名。
    """
    parts = refresh_token.split(".")
    if len(parts) < 2:
        return {}
    payload_bytes = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
    payload = json.loads(payload_bytes)
    user_profile = payload.get("user_profile", "")
    if not user_profile:
        return {}
    # user_profile 本身也是 JWT（纯 payload，无签名头）
    profile_parts = user_profile.split(".")
    profile_payload = profile_parts[1] if len(profile_parts) >= 2 else profile_parts[0]
    profile_bytes = base64.urlsafe_b64decode(profile_payload + "=" * (-len(profile_payload) % 4))
    profile = json.loads(profile_bytes)
    return {
        "user_id": profile.get("principal_id", ""),
        "user_name": profile.get("account_name", ""),
        "domain_id": profile.get("account_id", ""),
    }


def save_auth(result: dict, verifier: str, dpop_jwk: dict) -> str:
    """保存凭证到 auths/codearts-{user_id}.json（与 Go 版 auth.Auth 结构对齐）。"""
    os.makedirs(AUTHS_DIR, exist_ok=True)
    user_id = result["user_id"] or "unknown"
    path = os.path.join(AUTHS_DIR, f"codearts-{user_id}.json")
    payload = {
        "user_id": user_id,
        "user_name": result["user_name"],
        "domain_id": result["domain_id"],
        "cloud_dragon_token": result["security_token"],
        "access_key_id": result["access_key_id"],
        "secret_access_key": result["secret_access_key"],
        "expiration": result["expiration"],
        "refresh_token": result["refresh_token"],
        "code_verifier": verifier,
        "client_id": CLIENT_ID,
        "dpop_private_key": dpop_jwk,
        "updated_at": int(time.time()),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    """处理 OAuth 回调：portal 先带 secret+redirect（307 跳转），再带 code。"""

    code = None
    code_event = threading.Event()

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = query.get("code", [""])[0]
        redirect = query.get("redirect", [""])[0]
        # portal 首次回调带 redirect，需 307 跳回（登录页链路的一环）
        if not code and redirect:
            self.send_response(307)
            self.send_header("Location", redirect)
            self.end_headers()
            return
        if code:
            CallbackHandler.code = code
            CallbackHandler.code_event.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h3>登录成功，可关闭此页面。</h3>".encode("utf-8"))
            return
        self.send_response(400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<h3>登录失败：缺少 code</h3>".encode("utf-8"))

    def log_message(self, *args):
        pass


def main() -> int:
    print_only = "--print-only" in sys.argv

    ticket_id = secrets.token_hex(16)
    verifier, challenge = generate_pkce()
    dpop_jwk = generate_dpop_key()

    port = 0
    server = None
    if not print_only:
        server = http.server.HTTPServer(("127.0.0.1", 0), CallbackHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

    login_url = build_authorize_url(ticket_id, challenge, port)
    print("=" * 60)
    print("  CodeArts Agent 登录（华为云账号）")
    print("=" * 60)
    print("登录链接：")
    print(f"  {login_url}")
    print()

    if not print_only:
        webbrowser.open(login_url)
        print("浏览器已打开，完成登录后自动换取凭证...")
        CallbackHandler.code_event.wait(timeout=300)
        server.shutdown()
        code = CallbackHandler.code
        if not code:
            print("登录超时（5 分钟）")
            return 1
    else:
        print("打开上面链接完成登录后，将回调 URL 粘贴到这里：")
        callback_url = input().strip()
        code = urllib.parse.parse_qs(
            urllib.parse.urlparse(callback_url).query
        ).get("code", [""])[0]
        if not code:
            print("回调 URL 中缺少 code")
            return 1

    result = exchange_code(code, verifier, port, dpop_jwk)
    # STS authorization_code 响应不含用户信息，从 refresh_token JWT 的 user_profile 解出
    if not result["user_id"]:
        info = decode_user_from_refresh_token(result["refresh_token"])
        if info:
            result.update(info)
    path = save_auth(result, verifier, dpop_jwk)
    print(f"\n✅ 登录成功：user_id={result['user_id']} name={result['user_name']}")
    print(f"凭证已保存：{path}")
    if result["expiration"]:
        print(f"STS 有效期至：{result['expiration']}（到期前自动 refresh 续期）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

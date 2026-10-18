# -*- coding: utf-8 -*-
"""从 CodeArts 系列 IDE 本地会话库读取网关凭证，并支持自动刷新。

CodeArts 模型网关只认 IDE 登录会话签发的临时凭证（accessKey + secretKey +
securityToken，数小时过期），不认 IAM 永久 AK/SK。

凭证存于 secret://huaweicloud.authentication/HuaweiCloudSession（os_crypt
AES-GCM 加密，IDE 每次刷新会话后都会更新）。会话内带可轮转的 refresh_token
与 DPoP 私钥，token 过期时可调用 sts 令牌端点自助刷新：
POST https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens
    （form 表单：client_id + code_verifier + grant_type=refresh_token
      + refresh_token；DPoP ES256 JWT 头）
"""
import base64
import json
import os
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import requests

_CACHE = {"at": 0.0, "value": None}
_DPAPI = None

STS_TOKEN_URL = "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens"
_CLIENT_ID = "codearts-agent"
_REFRESH_BEFORE = 600
# oauth.py 生成的凭证文件目录；优先于 IDE 会话加载
AUTHS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auths")


def _dpapi_unprotect(blob):
    """Windows DPAPI 解密（仅 Windows 可用）。"""
    global _DPAPI
    if sys.platform != "win32":
        raise RuntimeError("DPAPI only on Windows")
    if _DPAPI is None:
        import ctypes
        from ctypes import wintypes

        class _BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_byte))]

        _DPAPI = (ctypes.windll.crypt32, ctypes.windll.kernel32, ctypes, _BLOB)
    crypt32, kernel32, ctypes, _BLOB = _DPAPI

    blob_in = _BLOB(len(blob), ctypes.cast(
        ctypes.create_string_buffer(blob), ctypes.POINTER(ctypes.c_byte)))
    blob_out = _BLOB()
    if not crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out)):
        raise RuntimeError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _os_crypt_key(storage_dir):
    """读取并解密 os_crypt 主密钥（AES-256-GCM key）。"""
    local_state = os.path.join(storage_dir, "Local State")
    if not os.path.exists(local_state):
        return None
    try:
        with open(local_state, encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("os_crypt", {}).get("encrypted_key")
        if not raw:
            return None
        raw = base64.b64decode(raw)
        if not raw.startswith(b"DPAPI"):
            return None
        return _dpapi_unprotect(raw[5:])
    except Exception:
        return None


def _raw_to_bytes(val):
    if isinstance(val, str):
        try:
            obj = json.loads(val)
            if isinstance(obj, dict) and obj.get("type") == "Buffer":
                return bytes(obj["data"])
        except ValueError:
            pass
        return val.encode("latin-1")
    return bytes(val)


def _decrypt_session(storage_dir):
    """解密 HuaweiCloudSession，返回会话 dict 或 None。"""
    db = os.path.join(storage_dir, "User", "globalStorage", "state.vscdb")
    if not os.path.exists(db):
        return None
    key = _os_crypt_key(storage_dir)
    if not key:
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT value FROM ItemTable WHERE key=?",
                ('secret://{"extensionId":"huaweicloud.authentication","key":"HuaweiCloudSession"}',),
            )
            row = cur.fetchone()
        finally:
            con.close()
        if not row:
            return None
        buf = _raw_to_bytes(row[0])
        if not buf.startswith(b"v10"):
            return None
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        plain = AESGCM(key).decrypt(buf[3:15], buf[15:], None)
        return json.loads(plain.decode("utf-8"))
    except Exception:
        return None


def _encrypt_session(storage_dir, session):
    """把会话 dict 加密回写 state.vscdb（v10 格式）。"""
    db = os.path.join(storage_dir, "User", "globalStorage", "state.vscdb")
    key = _os_crypt_key(storage_dir)
    if not key:
        return False
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        plain = json.dumps(session, ensure_ascii=False).encode("utf-8")
        blob = b"v10" + nonce + AESGCM(key).encrypt(nonce, plain, None)
        con = sqlite3.connect(db, timeout=10)
        try:
            cur = con.cursor()
            cur.execute(
                "UPDATE ItemTable SET value=? WHERE key=?",
                (blob,
                 'secret://{"extensionId":"huaweicloud.authentication","key":"HuaweiCloudSession"}',
                 ),
            )
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()
    except Exception:
        return False


def _session_to_cred(session):
    ak = (session.get("accessKey") or "").strip()
    sk = (session.get("secretKey") or "").strip()
    token = (session.get("securitytoken")
             or session.get("securityToken") or "").strip()
    if not (ak and sk and token):
        return None
    return {
        "ak": ak,
        "sk": sk,
        "token": token,
        "expires_at": session.get("expires_at") or "",
    }


def _needs_refresh(session):
    exp = session.get("expires_at")
    if not exp or not session.get("refresh_token"):
        return False
    try:
        deadline = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        remain = (deadline - datetime.now(timezone.utc)).total_seconds()
        return remain <= _REFRESH_BEFORE
    except ValueError:
        return False


def _b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _dpop_jwt(private_jwk, url):
    """生成 DPoP proof JWT（ES256）。"""
    import secrets

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    def _pad(s):
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    def _b64int(value, length):
        return _b64u(value.to_bytes(length, "big"))

    d = int.from_bytes(_pad(private_jwk["d"]), "big")
    x = int.from_bytes(_pad(private_jwk["x"]), "big")
    y = int.from_bytes(_pad(private_jwk["y"]), "big")
    key = ec.EllipticCurvePrivateNumbers(
        d, ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1())).private_key()

    header = {
        "alg": "ES256",
        "typ": "dpop+jwt",
        "jwk": {
            "kty": "EC",
            "crv": "P-256",
            "x": private_jwk["x"],
            "y": private_jwk["y"],
        },
    }
    payload = {
        "htm": "POST",
        "htu": url,
        "iat": int(time.time()),
        "jti": secrets.token_hex(16),
    }
    seg = _b64u(json.dumps(header, separators=(",", ":")).encode()) + "." \
          + _b64u(json.dumps(payload, separators=(",", ":")).encode())
    der_sig = key.sign(seg.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der_sig)
    # 低 S 归一化（JOSE/RFC 9449 要求；Go 版 signDpopProof 同样做此归一化）
    _p256_n = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
    if s > _p256_n // 2:
        s = _p256_n - s
    # 签名 = base64url(r || s)，不能分开编码再拼接（base64 按 3 字节分组，跨边界不同）
    sig_raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return seg + "." + _b64u(sig_raw)


def _try_refresh(storage_dir, session):
    """用 refresh_token 换新临时凭证；失败返回 None。"""
    refresh_token = session.get("refresh_token")
    pkce = ((session.get("loginContext") or {}).get("pkcePair") or {})
    dpop = ((session.get("loginContext") or {}).get("dpopKeyPair") or {})
    verifier = pkce.get("codeVerifier") or pkce.get("code_verifier")
    private_jwk = dpop.get("privateKeyJwk") or dpop.get("private_key_jwk")
    if not (refresh_token and verifier and private_jwk):
        # 缺任一项就得不出 DPoP proof；不打原因的话看起来像“没反应”，只会看到上游持续 401
        missing = [n for n, v in (("refresh_token", refresh_token),
                                 ("loginContext.pkcePair.codeVerifier", verifier),
                                 ("loginContext.dpopKeyPair.privateKeyJwk", private_jwk)) if not v]
        print(f"[credential_provider] IDE 会话凭证跳过刷新：缺 {'、'.join(missing)}")
        return None
    try:
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "DPoP": _dpop_jwt(private_jwk, STS_TOKEN_URL),
            "User-Agent": "CodeArts2api/1.0",
        }
        data = urllib.parse.urlencode({
            "client_id": _CLIENT_ID,
            "code_verifier": verifier,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
        resp = requests.post(STS_TOKEN_URL, data=data, headers=headers, timeout=30)
        if resp.status_code != 200:
            print(f"[credential_provider] 令牌刷新失败 HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        body = resp.json()
        creds = body.get("credentials") or {}
        new_token = body.get("refresh_token")
        if not (creds.get("access_key_id") and creds.get("security_token") and new_token):
            return None
        updated = dict(session)
        updated["accessKey"] = creds["access_key_id"]
        updated["secretKey"] = creds["secret_access_key"]
        updated["securitytoken"] = creds["security_token"]
        updated["expires_at"] = creds.get("expiration") or ""
        updated["refresh_token"] = new_token
        _encrypt_session(storage_dir, updated)
        print("[credential_provider] 会话凭证已自动刷新")
        return updated
    except Exception as error:
        print(f"[credential_provider] 令牌刷新异常: {error}")
        return None


def _scan(storage_dir):
    """按优先级取一个 IDE 目录下的凭证（将过期时自动刷新）。"""
    session = _decrypt_session(storage_dir)
    if session:
        if _needs_refresh(session):
            refreshed = _try_refresh(storage_dir, session)
            if refreshed:
                session = refreshed
        cred = _session_to_cred(session)
        if cred:
            return cred
    state = _read_state(storage_dir)
    if state:
        origin = (state.get("userInfoKey") or {}).get("originData") or {}
        cred = _session_to_cred(origin)
        if cred:
            return cred
    return None


def _read_state(storage_dir):
    """读取明文 userInfoKey（旧凭证，作回退）。"""
    db = os.path.join(storage_dir, "User", "globalStorage", "state.vscdb")
    if not os.path.exists(db):
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT value FROM ItemTable WHERE key=?",
                ("huaweicloud.codearts-snap",),
            )
            row = cur.fetchone()
            return json.loads(row[0]) if row else None
        finally:
            con.close()
    except (OSError, ValueError):
        return None


def _ide_dirs():
    return [
        os.path.expandvars(r"%APPDATA%\codearts-agent"),
        os.path.expandvars(r"%APPDATA%\codearts"),
    ]


def _load_auth_files():
    """从 auths/ 目录加载 oauth.py 生成的凭证，按需自动刷新。"""
    if not os.path.isdir(AUTHS_DIR):
        return None
    for name in sorted(os.listdir(AUTHS_DIR)):
        if not name.startswith("codearts-") or not name.endswith(".json"):
            continue
        path = os.path.join(AUTHS_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        ak = (data.get("access_key_id") or "").strip()
        sk = (data.get("secret_access_key") or "").strip()
        token = (data.get("cloud_dragon_token") or "").strip()
        if not (ak and sk and token):
            continue
        cred = {
            "ak": ak,
            "sk": sk,
            "token": token,
            "expires_at": data.get("expiration") or "",
            "refresh_token": data.get("refresh_token") or "",
            "code_verifier": data.get("code_verifier") or "",
            "dpop_private_key": data.get("dpop_private_key") or {},
            "client_id": data.get("client_id") or _CLIENT_ID,
            "_auth_file": path,
        }
        if _needs_refresh(cred):
            refreshed = _refresh_auth_file(cred)
            if refreshed:
                cred = refreshed
        return cred
    return None


def _refresh_auth_file(cred):
    """刷新 auths/*.json 凭证并回写文件。"""
    refresh_token = cred.get("refresh_token")
    verifier = cred.get("code_verifier")
    dpop_jwk = cred.get("dpop_private_key")
    client_id = cred.get("client_id") or _CLIENT_ID
    if not (refresh_token and verifier and dpop_jwk and dpop_jwk.get("d")):
        # 注意：dpop_private_key 必须是含 d 的 JWK 对象，空值/字符串都会落到这里
        missing = [n for n, v in (("refresh_token", refresh_token),
                                 ("code_verifier", verifier),
                                 ("dpop_private_key.d", dpop_jwk.get("d") if isinstance(dpop_jwk, dict) else None)) if not v]
        print(f"[credential_provider] auth 文件跳过刷新：缺 {'、'.join(missing)}")
        return None
    try:
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "DPoP": _dpop_jwt(dpop_jwk, STS_TOKEN_URL),
            "User-Agent": "CodeArts2api/1.0",
        }
        data = urllib.parse.urlencode({
            "client_id": client_id,
            "code_verifier": verifier,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
        resp = requests.post(STS_TOKEN_URL, data=data, headers=headers, timeout=30)
        if resp.status_code != 200:
            print(f"[credential_provider] auth 文件令牌刷新失败 HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        body = resp.json()
        creds = body.get("credentials") or {}
        new_token = body.get("refresh_token")
        if not (creds.get("access_key_id") and creds.get("security_token") and new_token):
            return None
        path = cred["_auth_file"]
        with open(path, "r", encoding="utf-8") as f:
            original = json.load(f)
        original["access_key_id"] = creds["access_key_id"]
        original["secret_access_key"] = creds["secret_access_key"]
        original["cloud_dragon_token"] = creds["security_token"]
        original["expiration"] = creds.get("expiration") or ""
        original["refresh_token"] = new_token
        original["updated_at"] = int(time.time())
        with open(path, "w", encoding="utf-8") as f:
            json.dump(original, f, ensure_ascii=False, indent=2)
        print("[credential_provider] auth 文件凭证已自动刷新")
        cred["ak"] = creds["access_key_id"]
        cred["sk"] = creds["secret_access_key"]
        cred["token"] = creds["security_token"]
        cred["expires_at"] = creds.get("expiration") or ""
        cred["refresh_token"] = new_token
        return cred
    except Exception as error:
        print(f"[credential_provider] auth 文件令牌刷新异常: {error}")
        return None


def load_credentials(force=False):
    """读取最新网关凭证（含自动刷新）；找不到返回 None。

    优先读 auths/*.json（oauth.py 生成），其次读 IDE 会话。
    """
    now = time.time()
    if not force and _CACHE["value"] and now - _CACHE["at"] < 30:
        return _CACHE["value"]
    cred = _load_auth_files()
    if not cred:
        for storage_dir in reversed(_ide_dirs()):
            cred = _scan(storage_dir)
            if cred:
                break
    _CACHE["at"] = now
    _CACHE["value"] = cred
    return cred


def cred_and_env():
    """读取 IDE 会话凭证；找不到返回空凭证（上游会拒绝并给出明确错误）。"""
    return load_credentials() or {"ak": "", "sk": "", "token": "", "expires_at": ""}


def token_notice(cred):
    """返回凭证过期/缺失的友好提示文本。"""
    if not cred:
        return "（本地未找到 CodeArts 会话凭证，请在 CodeArts IDE/网页中登录后再试）"
    expires_at = cred.get("expires_at")
    if not expires_at or not cred.get("token"):
        return ""
    try:
        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        remain = (exp - datetime.now(timezone.utc)).total_seconds()
        if remain <= 0:
            return "（会话凭证已过期且无法自动刷新，请在 CodeArts IDE/网页中重新登录）"
        if remain < 300:
            return f"（会话凭证即将过期，剩余 {int(remain // 60)} 分钟）"
    except ValueError:
        pass
    return ""
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CodeArts 对话接口签名复现/验证工具（SDK-HMAC-SHA256 v9）。

用途：给定一份完整的原始 HTTP 请求（含完整 body），用已解密的 AK/SK 复算
Authorization 头，与流量分析中的 Signature 逐字节比对，判定协议细节。

关键歧义点（本工具同时实现两种变体）：
  华为云官方 SDK-HMAC-SHA256 v3 规范：CanonicalRequest 用 \\n 连接
  内核 JS 源码 buildCanonicalRequest ：[m,uri,query,headers,signedHeaders,payloadHash].join(" ")
  → 用 --join space|newline 切换；比对成功即证明实际实现采用哪种。

用法：
  python codearts_sign.py --request <完整请求文件> [--creds <解密出的 session.json>] [--join space|newline]
  python codearts_sign.py --selftest          # 只跑算法自检（无网络、无凭证）

凭证来源：CodeArts/re/capture/secret_storage/*.json（oscrypt_decrypt.py 产出）。
本工具不修改任何文件，不发起网络请求。
"""
import argparse, hashlib, hmac, json, os, re, sys

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def url_encode(s: str) -> str:
    """华为 SDK 风格：UTF-8 逐字节大写 hex，未转义 -_.~（源码 hexTable + noEscape 集）。"""
    safe = set("-_.~")
    out = []
    for b in s.encode("utf-8"):
        c = chr(b)
        if b < 128 and (c.isalnum() or c in safe):
            out.append(c)
        else:
            out.append("%%%02X" % b)
    return "".join(out)


def parse_request(path: str):
    raw = open(path, "r", encoding="utf-8", errors="replace").read()
    raw = raw.replace("\r\n", "\n")
    head, _, body = raw.partition("\n\n")
    lines = head.split("\n")
    req = {"method": lines[0].split()[0], "target": lines[0].split()[1], "headers": {}, "body": body}
    for ln in lines[1:]:
        if ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        req["headers"][k.strip()] = v.strip()
    return req


def collect_signed_headers(req, signed_list: str):
    """按流量分析给出的 SignedHeaders 顺序取头值（华为要求按字典序小写签名）。"""
    hdrs = {}
    for name in signed_list.split(";"):
        name = name.strip()
        if not name:
            continue
        hdrs[name] = req["headers"].get(name, "")
    return hdrs


def sign(ak: str, sk: str, req, signed_list: str, join_mode: str, payload_hash: str):
    from urllib.parse import urlparse
    u = urlparse(req["target"])
    canonical_uri = url_encode(u.path) or "/"
    if not canonical_uri.endswith("/"):
        canonical_uri += "/"
    canonical_qs = u.query

    hdrs = collect_signed_headers(req, signed_list)
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(hdrs.items()))
    signed_headers = ";".join(sorted(hdrs.keys()))

    sep = " " if join_mode == "space" else "\n"
    cr = sep.join([req["method"].upper(), canonical_uri, canonical_qs,
                   canonical_headers, signed_headers, payload_hash])
    date = req["headers"].get("x-sdk-date", "")
    date_stamp = date[:8]
    sts = " ".join(["SDK-HMAC-SHA256", date, hashlib.sha256(cr.encode("utf-8")).hexdigest()])
    sig = hmac.new(sk.encode("utf-8"), sts.encode("utf-8"), hashlib.sha256).hexdigest()
    auth = f"SDK-HMAC-SHA256 Access={ak}, SignedHeaders={signed_headers}, Signature={sig}"
    return {
        "canonical_request": cr,
        "string_to_sign": sts,
        "signature": sig,
        "authorization": auth,
        "signed_headers": signed_headers,
    }


def load_creds(path=None):
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "re", "capture", "secret_storage")
    cands = sorted([os.path.join(d, f) for f in os.listdir(d)]) if os.path.isdir(d) else []
    if path:
        cands = [path] + cands
    for p in cands:
        try:
            obj = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        s = obj.get("session") or obj
        if s.get("accessKey") and s.get("secretKey"):
            return s["accessKey"], s["secretKey"], p
    return None, None, None


def selftest():
    """算法自检：用 RFC 4231 风格向量验证 HMAC/SHA256 链路，并打印两种 join 的差异。"""
    sk = "test-secret-key"
    cr_space = " ".join(["POST", "/api/v2/chat/completions/", "", "host:x\n", "host", EMPTY_SHA256])
    cr_nl = cr_space.replace(" ", "\n")
    a = sign("AK", sk, {"method": "POST", "target": "/api/v2/chat/completions",
                        "headers": {"host": "x", "x-sdk-date": "20261001T054141Z"}},
             "host;x-sdk-date", "space", EMPTY_SHA256)
    b = sign("AK", sk, {"method": "POST", "target": "/api/v2/chat/completions",
                        "headers": {"host": "x", "x-sdk-date": "20261001T054141Z"}},
             "host;x-sdk-date", "newline", EMPTY_SHA256)
    print("HMAC-SHA256('test-secret-key','abc') =",
          hmac.new(sk.encode(), b"abc", hashlib.sha256).hexdigest())
    print("\n[空格 join] CR =", repr(cr_space))
    print("[空格 join] sig =", a["signature"])
    print("\n[换行 join] CR =", repr(cr_nl))
    print("[换行 join] sig =", b["signature"])
    print("\n两者是否不同：", a["signature"] != b["signature"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--request")
    ap.add_argument("--creds")
    ap.add_argument("--join", choices=["space", "newline", "both"], default="both")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest or not args.request:
        selftest()
        if not args.request:
            print("\n提示：加 --request <完整请求文件> 做真实比对（需含完整 body）。")
        return

    ak, sk, cred_path = load_creds(args.creds)
    if not ak:
        print("未找到凭证：请先运行 tools/oscrypt_decrypt.py 生成 secret_storage/*.json", file=sys.stderr)
        sys.exit(2)
    print(f"凭证: {creds_path if False else cred_path}")
    print(f"  AK={ak[:6]}…  SK len={len(sk)}\n")

    req = parse_request(args.request)
    auth_hdr = req["headers"].get("Authorization", "")
    m = re.search(r"SignedHeaders=([^,]+),\s*Signature=([0-9a-f]+)", auth_hdr)
    if not m:
        print("未从 Authorization 头解析出 SignedHeaders/Signature", file=sys.stderr)
        sys.exit(3)
    signed_list, expect_sig = m.group(1), m.group(2)
    body = req["body"]
    payload_hash = hashlib.sha256(body.encode("utf-8")).hexdigest() if body.strip() else EMPTY_SHA256
    print(f"body 长度: {len(body)} 字节（流量分析 Content-Length: {req['headers'].get('Content-Length')}）")
    print(f"payloadHash: {payload_hash}\n")

    modes = ["space", "newline"] if args.join == "both" else [args.join]
    for mode in modes:
        r = sign(ak, sk, req, signed_list, mode, payload_hash)
        ok = r["signature"] == expect_sig
        print(f"[{mode:7s}] sig = {r['signature']}")
        print(f"{'':13s}期望 = {expect_sig}")
        print(f"{'':13s}==> {'✅ 完全一致' if ok else '❌ 不一致'}\n")
        if ok:
            print("CanonicalRequest:\n" + r["canonical_request"])
            print("\nStringToSign:\n" + r["string_to_sign"])


if __name__ == "__main__":
    main()

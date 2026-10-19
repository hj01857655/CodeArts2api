#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_real.py —— 真正的端到端签名验证
body: Reqable 流量分析（完整未打码，50010 字节）
签名头: req_chat_completions.txt（明文，同一次请求）
"""
import json, hashlib, hmac, sys, io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

ROOT = r'E:\VSCodeSpace\ZCode\CodeArts'
BODY = r'C:\Users\12925\AppData\Roaming\Reqable\capture\1790833292162351-170-2-req_raw-body.reqable'
CREDS = ROOT + r'\re\capture\secret_storage\HuaweiCloudSession.json'

body_bytes = open(BODY, 'rb').read()
body = body_bytes.decode('utf-8')

creds = json.load(open(CREDS, encoding='utf-8'))
AK, SK = creds['accessKey'], creds['secretKey']

SIGNED_HEADERS = ("client-ip;created-time;host;maas_type;model-id;model-name;"
                  "user-msg-id;user-session-id;x-language;x-ot-client-type;"
                  "x-ot-client-version;x-ot-function;x-ot-parent-session-id;"
                  "x-ot-session-id;x-ot-span-id;x-ot-trace-id;x-sdk-date;"
                  "x-security-token;x-snap-traceid")
REAL_SIG = "d571da0250271f69b72c72490d8c807881b408265a067112f9b87c563d4e6dfb"

H = {
    'client-ip': '198.18.0.1',
    'created-time': '2026-10-01T05:41:41Z',
    'host': 'snap-access.cn-north-4.myhuaweicloud.com',
    'maas_type': 'benefit',
    'model-id': 'deepseek-v4-flash-0731',
    'model-name': 'deepseek-v4-flash-0731',
    'user-msg-id': 'msg_0f5fb802c001nlspSJSEPwLvMM',
    'user-session-id': 'ses_silent',
    'x-language': 'zh-cn',
    'x-ot-client-type': 'IDE',
    'x-ot-client-version': '26.9.500',
    'x-ot-function': 'agent-general',
    'x-ot-parent-session-id': 'ses_silent',
    'x-ot-session-id': 'ses_f0a048070ffe5i6tt63yopV9aI',
    'x-ot-span-id': '5fe3a4e3aab547ab',
    'x-ot-trace-id': '589c6465ecc849cf96f746fc2cfdb1c3',
    'x-sdk-date': '20261001T054141Z',
    'x-security-token': creds['securitytoken'],
    'x-snap-traceid': 'd5f96055c3e24749850fddbad11d24b1_b999bc3f99c04904',
}


def sha(s):
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def hm(key, msg):
    if isinstance(key, str):
        key = key.encode('utf-8')
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).hexdigest()


print(f"body 字节数 : {len(body_bytes)}  (流量分析 Content-Length: 50010)")
print(f"AK          : {AK[:6]}****（从 HuaweiCloudSession.json 运行时读取）")
print(f"真实 Signature: {REAL_SIG}\n")

payload_hash = sha(body)
print(f"payloadHash = Hex(sha256(body))")
print(f"            = {payload_hash}\n")

ch = ''
for n in SIGNED_HEADERS.split(';'):
    ch += f'{n}:{H[n]}\n'

for mode in ('newline', 'space'):
    sep = '\n' if mode == 'newline' else ' '
    # 源码 buildCanonicalRequest(x,J,Z,Y,G,z) = [x,J,Z,Y,G,z].join(sep)
    #   x=method  J=CanonicalURI  Z=CanonicalQueryString  Y=canonicalHeaders
    #   G=SignedHeaders 列表    z=payloadHash
    cr = sep.join(['POST', '/api/v2/chat/completions/', '',
                   ch, SIGNED_HEADERS, payload_hash])
    sts = sep.join(['SDK-HMAC-SHA256', H['x-sdk-date'], sha(cr)])
    sig = hm(SK, sts)

    ok = sig == REAL_SIG
    print(f"[{mode:8}] Signature = {sig}  {'✅ 命中真实签名！' if ok else ''}")
    if ok:
        print(f"\n{'='*60}")
        print("端到端验证成功！SDK-HMAC-SHA256 v9 已 100% 复现")
        print(f"  join 分隔符  : {repr(sep)} ({mode})")
        print("  payloadHash  : Hex(sha256(body))  ✅")
        print("=" * 60)
        break
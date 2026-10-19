#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""穷举 SDK-HMAC-SHA256 v9 的实现细节组合，定位 main.log 5 条签名的 0/5 根因。

穷举维度（源码级）：
  - CanonicalURI：带/不带尾斜杠
  - CanonicalRequest 分隔符：\\n / 空格
  - StringToSign 分隔符：\\n / 空格
  - payloadHash：EMPTY_BODY_SHA256 / sha256("") / sha256("{}") / "UNSIGNED-PAYLOAD"
  - canonical headers 尾部换行：有/无
  - header 名大小写：全小写 / 原样
  - 参与签名的 host 值：带端口 / 不带
  - STS 中日期：完整 X-Sdk-Date / 仅日期部分
目标：找出与 5 条日志签名至少 1 条完全一致的组合。
"""
import io, sys
import hashlib, hmac, itertools, json, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
CREDS = r'E:\VSCodeSpace\ZCode\CodeArts\re\capture\secret_storage\codearts_safeStorage.json'
MAINLOG = r'C:\Users\12925\AppData\Roaming\codearts\app-logs\logs\main.log'

c = open(MAINLOG, encoding='utf-8', errors='replace').read()
pat = re.compile(
    r'"headers":\{"Accept":"[^"]*","Content-Type":"([^"]*)",'
    r'"X-Security-Token":"([^"]*)","host":"([^"]*)","X-Sdk-Date":"([^"]*)",'
    r'"Authorization":"SDK-HMAC-SHA256 Access=([^,]+), SignedHeaders=([^,]+), Signature=([0-9a-f]{64})"'
)
rows = pat.findall(c)
creds = json.load(open(CREDS, encoding='utf-8'))
s = creds.get('session') or creds
AK, SK = s['accessKey'], s['secretKey']
print(f'样本 {len(rows)} 条；AK 匹配: {AK == rows[0][4]}\n')

# 只用第 1 条做穷举（命中即可推广）
ct, tok, host, date, _ak, signed, sig0 = rows[0]
PATH = '/snap-manager/v1/package/overview'
print(f'样本1: date={date} host={host}\n  期望 sig={sig0}\n')

HASH_OPTS = {
    'EMPTY': EMPTY,
    'sha256_empty': hashlib.sha256(b'').hexdigest(),
    'sha256_{}': hashlib.sha256(b'{}').hexdigest(),
    'UNSIGNED': 'UNSIGNED-PAYLOAD',
}
found = []
combos = 0
for uri_slash, cr_sep, sts_sep, hname, ph, hdr_tail, lower_host in itertools.product(
        [True, False], ['\n', ' '], ['\n', ' '], ['lower', 'asis'],
        list(HASH_OPTS), [True, False], [True, False]):
    combos += 1
    uri = PATH + ('/' if uri_slash else '')
    headers_sel = {}
    for k in signed.split(';'):
        if k == 'content-type': headers_sel[k] = ct
        elif k == 'x-security-token': headers_sel[k] = tok
        elif k == 'x-sdk-date': headers_sel[k] = date
        elif k == 'host': headers_sel[k] = host
    keys = sorted(headers_sel.keys(), key=lambda x: x.lower() if hname == 'lower' else x)
    ch = ''.join(f'{(k.lower() if hname=="lower" else k)}:{headers_sel[k]}\n' for k in keys)
    if not hdr_tail and ch.endswith('\n'):
        ch = ch[:-1]
    sh = ';'.join(keys)
    cr = cr_sep.join(['GET', uri, '', ch, sh, HASH_OPTS[ph]])
    sts = sts_sep.join(['SDK-HMAC-SHA256', date, hashlib.sha256(cr.encode()).hexdigest()])
    sig = hmac.new(SK.encode(), sts.encode(), hashlib.sha256).hexdigest()
    if sig == sig0:
        found.append((uri_slash, repr(cr_sep), repr(sts_sep), hname, ph, hdr_tail, lower_host, cr, sts))

print(f'穷举 {combos} 种组合')
if found:
    print(f'\n🎉 命中 {len(found)} 种：')
    for f in found[:3]:
        print(f'  尾斜杠={f[0]} CR分隔={f[1]} STS分隔={f[2]} 头名={f[3]} payloadHash={f[4]} 头尾换行={f[5]}')
        print(f'    CR  = {f[7]!r}')
        print(f'    STS = {f[8]!r}')
else:
    print('\n❌ 全部未命中 → 差异不在这些维度')
    # 打印一条基准 CR 供人工核对
    headers_sel = {'content-type': ct, 'host': host, 'x-sdk-date': date, 'x-security-token': tok}
    keys = sorted(headers_sel)
    ch = ''.join(f'{k}:{headers_sel[k]}\n' for k in keys)
    sh = ';'.join(keys)
    cr = '\n'.join(['GET', PATH + '/', '', ch, sh, EMPTY])
    print('\n基准 CanonicalRequest:')
    print(repr(cr))
    print('\n基准 StringToSign:')
    print(repr('\n'.join(['SDK-HMAC-SHA256', date, hashlib.sha256(cr.encode()).hexdigest()])))

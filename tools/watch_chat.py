#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
watch_chat.py —— 监听 Reqable 流量分析目录，捕获 chat/completions 的请求与响应。

用法:
  python watch_chat.py [监控秒数]     默认 240 秒

触发方式: 监控期间在 CodeArts 里发一条消息（如「你好」）。
预期捕获 3 个请求:
  1. 普通对话   msgs=2 tools=14
  2. 标题生成   msgs=3 tools=0
  3. 记忆抽取   msgs=2 tools=9   ← 核心目标，取其 SSE 响应
"""
import io, json, os, sys, time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

CAP = r'C:\Users\12925\AppData\Roaming\Reqable\capture'
DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 240

if not os.path.isdir(CAP):
    print(f'目录不存在: {CAP}')
    sys.exit(1)

known = set(os.listdir(CAP))
print(f'监听 {CAP}，窗口 {DURATION} 秒')
print(f'基线 {len(known)} 个文件。请现在打开 Reqable 流量分析 → 在 CodeArts 发一条消息。\n')

deadline = time.time() + DURATION
hits = []

while time.time() < deadline:
    try:
        cur = set(os.listdir(CAP))
    except OSError:
        time.sleep(0.5)
        continue
    for n in sorted(cur - known):
        p = os.path.join(CAP, n)
        if not os.path.isfile(p):
            continue
        if 'req_raw-body' in n or 'res-raw-body' in n:
            hits.append(n)
    known |= cur
    if hits:
        time.sleep(3)   # 等同批文件落盘完
    else:
        time.sleep(0.5)

print(f'\n捕获 {len(hits)} 个文件\n')

REQ, RES = [], []
for n in hits:
    (REQ if 'req_raw-body' in n else RES).append(n)

print('=' * 60)
print(f'请求体 {len(REQ)} 个 / 响应体 {len(RES)} 个')
print('=' * 60)

for n in sorted(REQ):
    p = os.path.join(CAP, n)
    d = open(p, 'rb').read()
    try:
        o = json.loads(d)
    except Exception:
        print(f'  {n}: 非 JSON ({len(d)}B)')
        continue
    if o.get('model') != 'deepseek-v4-flash-0731':
        continue
    print(f'\n>>> model={o["model"]}  {len(d)}B')
    print(f'    msgs={len(o.get("messages", []))}  '
          f'tools={len(o.get("tools") or [])}  '
          f'user_prompt={o.get("user_prompt")!r}')
    print(f'    {n}')

print('\n' + '=' * 60)
print('响应（重点看记忆抽取是否有 SSE）')
print('=' * 60)
for n in sorted(RES):
    p = os.path.join(CAP, n)
    d = open(p, 'rb').read()
    base = n.replace('-res-raw-body.reqable', '')
    has_req = os.path.exists(os.path.join(CAP, base + '-req_raw-body.reqable'))
    tag = ''
    if has_req:
        try:
            o = json.loads(open(os.path.join(CAP, base + '-req_raw-body.reqable'), 'rb').read())
            tag = f'  <- {o.get("user_prompt")!r} tools={len(o.get("tools") or [])}'
        except Exception:
            pass
    sse = d.count(b'data:')
    print(f'  {len(d):>7}B  SSE帧~{sse:<4} {n}{tag}')
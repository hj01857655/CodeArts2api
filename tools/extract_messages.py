#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从内核日志的 bus-publish 事件中提取流量分析那次会话的完整 messages。

背景：body 的 messages[] 内容被拆散落盘为 message.part.updated 事件
（text/reasoning/tool part 各一行，含完整原文）。本工具按消息顺序重组。

用法: extract_messages.py <日志文件> <sessionID>
产物: re/capture/recovered/messages_<sessionID>.json
"""
import json, os, re, sys

LOG = sys.argv[1] if len(sys.argv) > 1 else \
    r'C:\Users\12925\.codeartsdoer\codearts-data\log\kernel-codeartsdoer-incognito-2026-10-01T134108-10348-0.log'
SID = sys.argv[2] if len(sys.argv) > 2 else 'ses_f0a048070ffe5i6tt63yopV9aI'
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 're', 'capture', 'recovered')


def main():
    os.makedirs(OUT, exist_ok=True)
    content = open(LOG, encoding='utf-8', errors='replace').read()
    rows = [l for l in content.split('\n') if SID in l and 'bus-publish' in l]

    msgs = {}   # messageID -> {role, parts:[]}
    order = []
    for line in rows:
        # 事件 JSON 被外层引号包裹，需按括号配平截取
        m = re.search(r'bus-publish "(\{.*)$', line)
        if not m:
            continue
        raw = m.group(1)
        depth, end, in_str, esc = 0, None, False, False
        for i, ch in enumerate(raw):
            if in_str:
                if esc: esc = False
                elif ch == '\\': esc = True
                elif ch == '"': in_str = False
                continue
            if ch == '"': in_str = True
            elif ch == '{': depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if not end:
            continue
        blob = raw[:end]
        # 去掉日志层对字符串值的打码（形如 "****"）会破坏 JSON；先尝试直接解析
        try:
            ev = json.loads(blob)
        except Exception:
            # 打码替换掉了引号内内容 → 修复：把 **** 换成占位串
            fixed = re.sub(r':\s*\*\*\*\*', ':"__MASKED__"', blob)
            fixed = re.sub(r'\*\*\*\*"', '"', fixed)
            try:
                ev = json.loads(fixed)
            except Exception as e:
                print(f'  [skip] 解析失败: {e} | 片段: {blob[:120]}')
                continue
        props = ev.get('properties', {})
        if ev.get('type') == 'message.updated':
            info = props.get('info', {})
            mid = info.get('id')
            if mid and mid not in msgs:
                msgs[mid] = {'id': mid, 'role': info.get('role'), 'parts': []}
                order.append(mid)
        elif ev.get('type') == 'message.part.updated':
            part = props.get('part', {})
            mid = part.get('messageID')
            if mid:
                if mid not in msgs:
                    msgs[mid] = {'id': mid, 'role': None, 'parts': []}
                    order.append(mid)
                msgs[mid]['parts'].append(part)

    print(f'sessionID = {SID}')
    print(f'重组出 {len(order)} 条消息\n')
    out_msgs = []
    for mid in order:
        m = msgs[mid]
        role = m['role'] or ('user' if any(p.get('metadata', {}).get('isUserInput') for p in m['parts']) else 'assistant')
        entry = {'role': role, 'id': mid, 'parts': []}
        for p in m['parts']:
            t = p.get('type')
            if t == 'text':
                entry['parts'].append({'type': 'text', 'text': p.get('text', '')})
            elif t == 'reasoning':
                entry['parts'].append({'type': 'reasoning', 'text': p.get('text', '')})
            elif t == 'tool':
                entry['parts'].append({'type': 'tool', 'tool': p.get('tool'),
                                       'callID': p.get('callID'),
                                       'state': p.get('state')})
        out_msgs.append(entry)
        kinds = [p['type'] for p in entry['parts']]
        total = sum(len(p.get('text', '')) for p in entry['parts'])
        print(f'  [{role:9s}] {mid}  parts={kinds}  text总长={total}')

    fp = os.path.join(OUT, f'messages_{SID}.json')
    json.dump(out_msgs, open(fp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'\n已存: {os.path.normpath(fp)}')
    print(f'文件大小: {os.path.getsize(fp)} 字节')
    return out_msgs


if __name__ == '__main__':
    main()

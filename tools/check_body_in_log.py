#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""决定性检查：内核日志里是否含 body 所需的 system prompt 与 tools schema。

body 50010 字节的构成（据流量分析）：
  - 标量字段（model/max_tokens/tool_stream/…）      ~200 B
  - messages[0].content = system prompt            ~13000 token 量级
  - messages[1].content = user 消息                 ~1.1k token
  - tools[9] = 9 个工具的完整 JSON Schema           占比最大

本脚本逐项核对日志中是否存在这三块的原文。
"""
import re, sys

LOG = r'C:\Users\12925\.codeartsdoer\codearts-data\log\kernel-codeartsdoer-incognito-2026-10-01T134108-10348-0.log'
SID = 'ses_f0a048070ffe5i6tt63yopV9aI'

c = open(LOG, encoding='utf-8', errors='replace').read()
print(f'日志大小: {len(c):,} 字符\n')

# 1) system prompt 特征串（流量分析里 system message 的开头）
sys_probes = [
    'You are CodeArts, a dedicated code agent',
    '# Tone and style',
    'IMPORTANT: DO NOT ADD ***ANY*** COMMENTS unless asked',
    'inferhub-provider/deepseek-v4-flash-0731',
    '<env>',
]
print('=== 1) system prompt 原文是否在日志 ===')
for p in sys_probes:
    n = c.count(p)
    print(f'  {"✅" if n else "❌"} {p[:52]:54s} x{n}')

# 2) tools schema 特征串
tool_probes = [
    '"type":"function"',
    '"function":{"name"',
    '"description":"Read the contents of a file or directory',
    'ZodString',
    'additionalProperties',
    '"required"',
]
print('\n=== 2) tools JSON Schema 是否在日志 ===')
for p in tool_probes:
    n = c.count(p)
    print(f'  {"✅" if n else "❌"} {p[:52]:54s} x{n}')

# 3) body 顶层标量（tool_stream 已在 tools 查过，这里补）
print('\n=== 3) body 顶层标量 ===')
for p in ['"tool_choice"', '"stream":true', '"user_prompt"', '"prompt_cache_key"', '"chat_id"']:
    n = c.count(p)
    print(f'  {"✅" if n else "❌"} {p:54s} x{n}')

# 4) user 消息（memory extraction 指令）
print('\n=== 4) user 消息原文 ===')
for p in ['You are now acting as the memory extraction subagent',
          'Recent Conversation Window', 'Memory Types']:
    n = c.count(p)
    print(f'  {"✅" if n else "❌"} {p[:52]:54s} x{n}')

# 5) 结论
print('\n=== 结论 ===')
has_sys = c.count('You are CodeArts, a dedicated code agent') > 0
has_tools = c.count('"function":{"name"') > 0
if not has_sys and not has_tools:
    print('  日志不含 system prompt 与 tools schema → 无法从日志重建 body')
elif not has_tools:
    print('  日志有 system prompt 但无 tools schema → 仍缺 tools（占 body 最大部分）')
else:
    print('  两块都在 → 可尝试重建')

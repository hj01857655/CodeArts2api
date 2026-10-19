#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""重写 07 号文档 §2.4~§2.6：撤回自证循环结论，补入端到端验证成功"""
import io, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

P = r'E:\VSCodeSpace\ZCode\CodeArts\docs\codearts-07-对话接口协议.md'
d = open(P, encoding='utf-8').read()
orig = len(d)

NEW = """### 2.4 payloadHash 环节：**已确证**（2026-10-01）

签名覆盖 body 的 `sha256`。源码 `buildPayloadHash`：

```js
static buildPayloadHash(x){
  if (x.headers[this.HeaderContentSha256]) return x.headers[this.HeaderContentSha256];
  if (x.data) return this.Hex(JSON.stringify(x.data));
  return this.EMPTY_BODY_SHA256
}
```

本次请求未带 `X-Sdk-Content-Sha256`，走第二条分支。实测值：

```
payloadHash = Hex(sha256(body))
            = 4286662f895fdfd8484412e1d9c634a80423df0b8adbc13459f12f198618427a
```

→ **确证：`payloadHash = Hex(sha256(JSON.stringify(data)))`，与最终 HTTP body 字节完全一致**（无尾随换行、无键序差异、无二次序列化）。依据是 §2.7 的端到端签名命中——payloadHash 是 CR 的第 6 段，值错则签名必错。

> **⚠️ 已作废的一次错误验证（留档警示）**
>
> 曾用 `re/capture/sign_sample.json` 的 `payload_hash` 字段做「3/3 复现」并判定结清，**该结论无效**。原因是 `tools/extract_sign_sample.py:65` 中：
>
> ```python
> ph = hashlib.sha256(body.encode('utf-8')).hexdigest()
> kept.append({..., 'payload_hash': ph, ...})
> ```
>
> `payload_hash` 是**提取脚本自己算出来存进去的**，拿它当「真值」去比对等于 `sha256(body) == sha256(body)`，是**自证循环**，必然全过。
>
> **教训**：验证哈希必须用**外部真值**（真实签名的反向约束，或服务端返回值），不能用同源计算结果。`tools/analyze_samples.py` 同属此类无效脚本，已删除。

**日志侧 body 不是「截断」，是 redaction 打码**：`JSON.parse` 在 char 669/3504 失败的原因是日志层把敏感子串替换为 `****`（`read tool` → `re****l`），并非长度不足。证据：`body_len` 记录值与实际字符串长度完全相等（14401/14401、60954/60954、45346/45346）。因此**日志 body 不可用于任何哈希复算**——好在本节已由 Reqable 流量分析解决（§2.7）。

### 2.5 端到端验证：**成功（2026-10-01 结清）**

用同一次请求的「Reqable 完整 body」+「流量分析明文签名头」+「解密出的 SK」，逐字节复算出与线上完全一致的 Signature。

```bash
cd CodeArts/tools && python verify_real.py
```

```
真实 Signature : d571da0250271f69b72c72490d8c807881b408265a067112f9b87c563d4e6dfb
复算 Signature : d571da0250271f69b72c72490d8c807881b408265a067112f9b87c563d4e6dfb  ✅ 命中
```

**完整算法（可直接实现）**：

```
1. payloadHash  = Hex(sha256(body))

2. canonicalHeaders = "".join(f"{name}:{value}\n" for name in SignedHeaders)

3. CanonicalRequest = "\n".join([
       "POST",                          // method
       "/api/v2/chat/completions/",     // CanonicalURI —— 注意补尾斜杠
       "",                             // CanonicalQueryString（空）
       canonicalHeaders,
       SignedHeaders,                  // ← 第 5 段是头名列表本身，不是 host
       payloadHash,
   ])

4. StringToSign = "\n".join([
       "SDK-HMAC-SHA256",
       xSdkDate,                       // 如 20261001T054141Z
       Hex(sha256(CanonicalRequest)),
   ])

5. Signature = Hex(HMAC-SHA256(key=SK, msg=StringToSign))   // 单次，非嵌套
```

**三个易踩的实现细节**（初版复算全部踩中，逐一由源码纠正）：

| # | 陷阱 | 源码依据 | 正确做法 |
|---|---|---|---|
| 1 | CanonicalURI 少尾斜杠 | `if(Y[Y.length-1]!=="/") Y=Y+"/"` | `/api/v2/chat/completions` → `.../completions/` |
| 2 | HMAC 误做两层嵌套 | `y1.hmacSHA256=(x,J)=>createHmac("sha256",x).update(J).digest("hex")` | **单次** HMAC，key = SK，不做 signingKey 派生 |
| 3 | 第 5 段误填 `host` | `buildCanonicalRequest(x,J,Z,Y,G,z)` = `[x,J,Z,Y,G,z].join(sep)`，调用处 `…(x.method, D, E, T, j, b)`，`j` = SignedHeaders 列表 | 第 5 段 = **SignedHeaders 分号串**；`host` 只出现在 canonicalHeaders 内，不单独成段 |

> `host` 在 `CanonicalRequest` 中出现两次（canonicalHeaders 内一次），这是华为云 SDK-HMAC-SHA256 系列的固有特征，非笔误。

### 2.6 body 数据源：Reqable 流量分析落盘

内核日志的 `chat body` 打码后不可用于哈希复算，本节数据改由 **Reqable** 提供。

| 项 | 值 |
|---|---|
| 落盘目录 | `%APPDATA%\\Reqable\\capture\\` |
| 文件命名 | `<微秒时间戳>-<序号>-<n>-req_raw-body.reqable` |
| 本次样本 | `1790833292162351-170-2-req_raw-body.reqable` |
| 字节数 | **50010**（与流量分析 `Content-Length: 50010` 精确吻合） |
| 打码 | **无**，完整原始 body |

body 结构（实测）：

```json
{ "model": "deepseek-v4-flash-0731", "max_tokens": 32000,
  "tool_stream": true, "stream": true, "user_prompt": "Memory Extraction",
  "messages": [system(23796字符), user(16226字符)],
  "tools": [ ... 9 个 ... ] }
```

同源佐证（body 与签名头均属 `traceId=589c6465…` / `user-msg-id=msg_0f5fb802…` / `created-time=05:41:41Z`）：

| 字段 | body 侧 | 签名头侧 | 一致 |
|---|---|---|---|
| `Content-Length` | 50010 字节 | 50010 | ✅ |
| `user_prompt` | `Memory Extraction` | §8 语义复原 | ✅ |

> 注：Reqable 的 `capture/` 目录只落盘 body，**不含请求头**；签名头仍需从 `re/capture/req_chat_completions.txt` 取。二者靠上表字段配对。

"""

start = d.find('### 2.4 payloadHash 验证')
end = d.find('## 3. 请求体 schema')
if start == -1 or end == -1 or end < start:
    print(f'定位失败 start={start} end={end}')
    sys.exit(1)

print(f'替换区间 [{start},{end}) 长度 {end-start}')
d = d[:start] + NEW + d[end:]
open(P, 'w', encoding='utf-8', newline='\n').write(d)
print(f'完成 {orig} -> {len(d)} 字节')
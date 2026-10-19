# 07 对话接口协议：模型调用面（实测 + 源码双证）

> 完整请求：`re/capture/req_chat_completions.txt`（body 50,010 字节）。
> 源码：`re/bun_extract/root_chunk-9bq6rhqj.js`（inferhub provider 实现）、`root_chunk-pt9mn0ta.js`（签名器）。
> 本文修正了 04 号的一处误记（CanonicalRequest 的 join 分隔符），已在 04 号就地标注。
> **2026-10-01 修订**：端到端签名已 **100% 复现**（§2.6 结清）；撤回一次「自证循环」的伪验证结论并留档警示（§2.4）；「body 截断」归因修正为 redaction 打码；§2.3 原重复编号已拆分。

## 1. 端点与方法

| 项 | 值 |
|---|---|
| 端点 | `POST /api/v2/chat/completions`（04 号记的 `/chat/completions` 是无版本前缀的路径模板，**实际线上带 `/api/v2` 前缀**） |
| Host | `snap-access.cn-north-4.myhuaweicloud.com`（04 §3.1 主域） |
| User-Agent | `ai-sdk/provider-utils/4.0.21 runtime/bun/1.3.14`（**AI SDK v5 + Bun 运行时自报**） |
| Content-Type | `application/json` |
| Accept-Encoding | `gzip, deflate, br, zstd` |
| 协议形态 | OpenAI Chat Completions 兼容 + 华为私有扩展头 |

模型标识：`model-id`/`model-name` = `deepseek-v4-flash-0731`，providerID = `inferhub-provider`（provider npm 名 `@ai-sdk/inferhub-provider`）。

## 2. 鉴权：SDK-HMAC-SHA256（v9 签名器）实锤

完整请求头（明文可见 AK 与签名）：

```
Authorization: SDK-HMAC-SHA256 Access=HSTAY4HFGFXIOXPMWZVI,
  SignedHeaders=client-ip;created-time;host;maas_type;model-id;model-name;
                user-msg-id;user-session-id;x-language;x-ot-client-type;
                x-ot-client-version;x-ot-function;x-ot-parent-session-id;
                x-ot-session-id;x-ot-span-id;x-ot-trace-id;x-sdk-date;
                x-security-token;x-snap-traceid,
  Signature=d571da0250271f69b72c72490d8c807881b408265a067112f9b87c563d4e6dfb
x-security-token: hQpjbi1ub3J0aC00AQAABXFIU1RBWTRIRkdGWElPWFBNV1pWSZXsx3vwU…（STS token）
```

**证实 04 §1.1 的 v9 签名器就是对话面实际使用的那一个**。

### 2.1 修正：CanonicalRequest 用换行 join

源码（`root_chunk-pt9mn0ta.js`）逐字取出，模板字符串内含真实换行：

```js
static buildCanonicalRequest(x,J,Z,Y,G,z){ return [x,J,Z,Y,G,z].join(`\n`) }   // ← 换行
static getStringToSign(x,J,Z){       return [x,J,Z].join(`\n`) }               // ← 换行
static getStringToSignByInfo(x,J,Z,Y){ return [x,J,Z,Y].join(` `) }            // ← V11 是空格
static buildCanonicalHeaders(x){ … Z += `${key.toLocaleLowerCase()}:${value}\n` … }
```

- **v9（SDK-HMAC-SHA256）**：CanonicalRequest 与 StringToSign **均为换行 join**（与华为云官方 SDK-HMAC-SHA256 v3 规范一致）。
- **V11-HMAC-SHA256**：CanonicalRequest 换行 join，但 StringToSign 是**空格** join（`getStringToSignByInfo`）——两种算法在此处不一致，是最容易踩的坑。
- 04 号原文把 v9 两处都记成了空格 join，**2026-10-01 已就地修正**。

### 2.2 头集合构造（`getInferhubHcHeaders`，9bq6rhqj.js）

```js
y.headers = {
  "X-Security-token": accessToken || "",
  "x-ot-trace-id":           headers["x-ot-trace-id"]           || uuid(),
  "x-ot-span-id":            headers["x-ot-span-id"]            || uuid(),
  "x-snap-traceid":          headers["x-snap-traceid"]          || uuid(),
  "x-ot-session-id":         headers["x-ot-session-id"]         || uuid(),
  "x-ot-parent-session-id":  headers["x-ot-parent-session-id"]  || "",
  "user-session-id":         headers["user-session-id"]         || uuid(),
  "x-ot-function":           headers["x-ot-function"]           || "",
  ...(headers.maas_type && { maas_type: headers.maas_type }),
  "X-Language":              语言码(),
  ...(headers["X-Remote-Origin-Url"] && { … }),
  "user-msg-id":  headers["user-msg-id"]  || "",
  "created-time": headers["created-time"] || "",
  "model-id":     headers["model-id"]     || "",
  "model-name":   headers["model-name"]   || "",
  "x-ot-client-type":    isCli ? "CLI" : (headers["x-ot-client-type"]    || cfg.clientType    || ""),
  "x-ot-client-version": isCli ? VERSION  : (headers["x-ot-client-version"] || cfg.clientVersion || ""),
  "client-ip": clientIp(),
};
let m = O1.sign(y, new x1(accessKeyId, secretAccessKey));   // ← v9 签名器
return m;                                                   // 含 Authorization + X-Security-token
```

要点：
- **AK/SK 来自 `userInfo.accessKeyId/accessSecretKey`**，回退环境变量 `CODEARTS_CLI_AK` / `CODEARTS_CLI_SK`（CLI 形态注入）。
- 签名后 `Object.fromEntries(Object.entries(m).filter(([k]) => !["X-Security-token","Authorization"].includes(k)))` 仅用于 debug 日志，**返回的是完整头集**（含 Authorization 与 X-Security-token）。
- 20 个 SignedHeaders 中 **`X-Security-token` 未参与签名**（头名在列表里是 `x-security-token` 小写形式，值为 STS token，参与计算）；`Content-Type`/`User-Agent`/`Accept-Encoding`/`Connection` **不参与签名**。

### 2.3 骨架验证（GET 侧）：**5/5 已复现（2026-10-01）**

> 本节与 §2.5 是**两条互补的验证路径**：本节用**空 body 的 GET 请求**确证算法骨架（join/排序/尾斜杠/EMPTY_BODY_SHA256），§2.6 用 **POST 完整 body** 确证 payloadHash 取值环节。两者合起来构成完整覆盖。

**验证样本（`%APPDATA%\codearts\app-logs\logs\main.log`）**：

5 条 `GET https://snap-access.cn-north-4.myhuaweicloud.com/snap-manager/v1/package/overview` 的完整 axios 请求配置（含明文 Authorization），其 AK 正是 05 号从 `codearts_safeStorage.json` 解出的那对凭证。

**复算结果：换行 join 5/5 完全一致**（空格 join 0/5，反证分隔符为换行）：

| # | X-Sdk-Date | 期望 Signature | 复算 | |
|---|---|---|---|---|
| 1 | 20260917T134947Z | `084efd46…7295` | `084efd46…7295` | ✅ |
| 2 | 20260917T135004Z | `356027b9…fdb8` | `356027b9…fdb8` | ✅ |
| 3 | 20260917T135016Z | `af3861dc…27c2` | `af3861dc…27c2` | ✅ |
| 4 | 20260917T135228Z | `4d61a406…1443` | `4d61a406…1443` | ✅ |
| 5 | 20260917T135646Z | `4c82bc13…c16` | `4c82bc13…c16` | ✅ |

**实测 CanonicalRequest / StringToSign（样本 1，可直接复用）**：

```
GET\n
/snap-manager/v1/package/overview/\n          ← ★ 尾斜杠
\n                                            ← 空 query
content-type:application/json\n
host:snap-access.cn-north-4.myhuaweicloud.com\n
x-sdk-date:20260917T134947Z\n
x-security-token:hQpjbi1ub3J0aC0…W8b6F5\n
\n
content-type;host;x-sdk-date;x-security-token\n
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855   ← EMPTY_BODY_SHA256

StringToSign = SDK-HMAC-SHA256\n20260917T134947Z\nd394b2ae28d98633006e878ee9161e50d719d00033b529eb2188c24956830c7e
```

→ **`SDK-HMAC-SHA256` v9 算法全链路 100% 确证**：换行 join、CanonicalURI 尾斜杠、空 body 用 EMPTY_BODY_SHA256、头名小写排序、`SignedHeaders` 分号连排、`x-security-token` 参与签名。

**⚠️ 三个曾导致 0/5 的实现陷阱（已修正，勿重蹈）**：

1. **urlEncode 不能整条 path 丢进去** —— 源码是**逐段** `urlEncode` 后用 `/` 连接；若对整条路径编码，`/` 变 `%2F`，签名永不命中。这是本次 0/5 → 5/5 的真实根因。
2. **取头值必须用小写键名** —— `SignedHeaders` 列表里是小写（`content-type`），用 `headers['Content-Type']` 取会拿到空串，同样导致不命中。
3. **StringToSign 分隔符不可想当然用空格** —— 源码 `getStringToSign` 是 `.join("\n")`；只有 V11 的 `getStringToSignByInfo` 才用空格。

```bash
cd CodeArts/tools
python verify_from_mainlog.py     # GET 骨架端到端复算，5/5 命中
python brute_sign_variants.py     # 256 种实现细节穷举（定位上述陷阱）
python verify_real.py             # POST 对话面端到端复算，逐字节命中（§2.5）
```

**对 POST（chat/completions）的适用性**：本节 5 条为 GET，验证的是**算法骨架**（join/排序/尾斜杠/payloadHash 占位）。POST 侧额外的 `payloadHash = Hex(sha256(body))` 取值环节，已由 §2.6 **逐字节命中**确证。

| 环节 | 验证来源 | 状态 |
|---|---|---|
| join 分隔符（换行） | main.log 5/5 + POST 命中 | ✅ |
| CanonicalURI 尾斜杠 | main.log 5/5 + POST 命中 | ✅ |
| 头小写排序 + 分号连排 | main.log 5/5 | ✅ |
| payloadHash（空 body 常量） | main.log 5/5 | ✅ |
| **payloadHash（POST body）** | **签名逐字节命中** | ✅ |
| HMAC 单次不嵌套 | POST 命中 | ✅ |
| x-security-token 参与签名 | main.log 5/5 | ✅ |
| urlEncode 逐段编码 | main.log 5/5 | ✅ |
| 20 头签名集（对话面） | 请求头 + `getInferhubHcHeaders` 源码 | ✅ |

### 2.4 凭证一致性

**已确证（可复现）**：

| 检查项 | 结果 |
|---|---|
| 请求头 `Access=` 的 AK | `HSTAY4HFGFXIOXPMWZVI` |
| 05 号解出的 `HuaweiCloudSession.json` 的 `accessKey` | `HSTAY4HFGFXIOXPMWZVI` —— **完全一致** |
| 请求头 `x-security-token` | `hQpjbi1ub3J0aC0…` —— 与解出的 `securitytoken` 同前缀同族 |
| 对应 `expiresAt` | `2026-10-01T07:41:13.921Z`（请求时刻落在有效期内，**凭证对得上**） |

→ 完整 body 已获得（§2.7），端到端验证已完成（§2.6）。

**日志交叉印证（2026-10-01，`kernel-codeartsdoer-incognito-2026-10-01T134108-10348-0.log`）**：

内核日志与完整请求头**逐字段吻合**：

```
service=chat-param-plugin
  "inferhub OT headers, x-ot-function:agent-general,
   x-ot-session-id: ses_f0a048070ffe5i6tt63yopV9aI,
   x-ot-parent-session-id: ses_silent,
   x-ot-trace-id: 589c6465ecc849cf96f746fc2cfdb1c3,
   x-snap-traceid: d5f96055c3e24749850fddbad11d24b1_b999bc3f99c04904,
   x-ot-span-id: 5fe3a4e3aab547ab, maas_type: benefit, agent: build,
   user-msg-id: msg_0f5fb802c001nlspSJSEPwLvMM, created-time: 2026-10-01T05:41:41Z, …"
```

| 字段 | 请求头 | 日志 |一致 |
|---|---|---|---|
| `x-ot-trace-id` | `589c6465ecc849cf96f746fc2cfdb1c3` | 同 | ✅ |
| `x-ot-span-id` | `5fe3a4e3aab547ab` | 同 | ✅ |
| `x-ot-session-id` | `ses_f0a048070ffe5i6tt63yopV9aI` | 同 | ✅ |
| `x-ot-parent-session-id` | `ses_silent` | 同 | ✅ |
| `x-snap-traceid` | `d5f96055…d24b1_b999bc3f99c04904` | 同 | ✅（双段格式再获印证） |
| `user-msg-id` | `msg_0f5fb802c001nlspSJSEPwLvMM` | 同 | ✅ |
| `created-time` / `X-Sdk-Date` | `2026-10-01T05:41:41Z` / `20261001T054141Z` | 同 | ✅ |
| 端点 | `/api/v2/chat/completions` | `url:https://snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions` | ✅ |
| `agent` | （system prompt 声明 `build`） | `agent: build` | ✅ |

→ **请求头与端点已双源确证**（请求头 + 内核日志），且日志额外确认了 `agent: build` 与 proxy 决策字段（`shouldBypass:false`）。

**body 各落点可得性（2026-10-01 修订）**：

| 落点 | 结论 |
|---|---|
| 10-01 内核日志 | 记录了完整头集（5 条 `header-debug` + 5 条 `Infer traceId`），但 **`chat body` 命中 0 次** —— body 仅在 `DEBUG_CHAT_BODY=1` 或日志级 DEBUG 时打印（源码 `if (process.env.DEBUG_CHAT_BODY==="1" \|\| log.getLevel()==="DEBUG")`），当时未开启 |
| 09-22 内核日志 | 有 59 条 `chat body`，已提取为 `sign_sample.json` 三份样本。①`header-debug` 按源码 filter 主动剔除 Authorization；②body 部分子串被打码为 `****`（**非长度截断**，详见 §2.7）→ **打码内容不可用于哈希复算** |
| 搜索 `SignedHeaders=` 命中 95 处 | **全部假阳性** —— 是某 agent 会话读取 `codearts2api/docs/reverse-engineering.md` 时的工具输出内容（文档正文里的示例），非真实请求头 |
| mitm 项目（`E:\VSCodeSpace\Kiro\mitm`） | `python/mitm_capture.py` 存在，但 `wire_dump/` 为空，无 10-01 落盘 |
| 本工作区 capture | 仅本次归档的截断版（body 省略为 `{…}`） |

> **2026-10-01 修订**：原标题「body 在任何本地落点均不可得」**不成立**。`sign_sample.json` 三份 body 结构与长度完整，足以确证 payloadHash 算法（§2.4）。真正缺失的是**未打码的同源 body + 同源 Signature 配对**，用于端到端 Signature 复算（§2.5）。

**已排除的错误路径**（避免后人重走）：

1. **手工重建 body** —— 不可行。system prompt 与 9 个 tools schema 任一字节差异都会让 sha256 完全不同，验证必然假阴性；且日志侧 `****` 打码片段无法还原。（本次已删除误建的重编文件。）
2. **从 `AtomCode2api/` 找签名实现** —— 无关。那是 AtomCode 项目（Rust TUI + GitCode 域，签名 `atomcode-signing-v1` 闭源 overlay），与 CodeArts 无关。
3. **拿 `codearts2api` 文档当独立验证** —— 那是另一项目 2026-08-03 的既有逆向记录（v26.7.0），本文已按「转引」标注，非本工作区实测。

**与 codearts2api 文档的两处冲突**（以本工作区实测为准）：

| 项 | codearts2api 文档（2026-08-03, v26.7.0） | 本工作区实测 | 判定 |
|---|---|---|---|
| refresh_token 有效期 | 30 天（`exp−iat=2592000s`） | **24.15 天**（`2086983s`，02 号解出真实 JWT） | 实测为准；差异或因策略变更或该文档记录有误 |
| 示例模型 | `glm-4.7` | `deepseek-v4-flash-0731` | 模型由 `/v1/gateway/config` 动态下发，随时间/灰度变化 |
| 版本 | 26.7.0 | **26.9.500** | 本机当前版本 |

### 2.5 payloadHash 环节：**已确证**（2026-10-01）

签名覆盖 body 的 `sha256`。源码 `buildPayloadHash`：

```js
static buildPayloadHash(x){
  if (x.headers[this.HeaderContentSha256]) return x.headers[this.HeaderContentSha256];
  if (x.data) return this.Hex(JSON.stringify(x.data));
  return this.EMPTY_BODY_SHA256
}
```

请求未带 `X-Sdk-Content-Sha256`，走第二条分支：

```
payloadHash = Hex(sha256(body))
            = 4286662f895fdfd8484412e1d9c634a80423df0b8adbc13459f12f198618427a
```

→ **`payloadHash = Hex(sha256(JSON.stringify(data)))`，与最终 HTTP body 字节完全一致**（无尾随换行、无键序差异、无二次序列化）。payloadHash 是 CanonicalRequest 的第 6 段，取值错误则签名必不匹配。

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

**日志侧 body 不是「截断」，是 redaction 打码**：`JSON.parse` 在 char 669/3504 失败的原因是日志层把敏感子串替换为 `****`（`read tool` → `re****l`），并非长度不足。证据：`body_len` 记录值与实际字符串长度完全相等（14401/14401、60954/60954、45346/45346）。因此**日志 body 不可用于任何哈希复算**（§2.7）。

### 2.6 端到端验证：**成功（2026-10-01 结清）**

用完整 body + 完整请求头 + 解密出的 SK 复算，Signature 与线上逐字节一致。

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

2. canonicalHeaders = "".join(f"{name}:{value}
" for name in SignedHeaders)

3. CanonicalRequest = "
".join([
       "POST",                          // method
       "/api/v2/chat/completions/",     // CanonicalURI —— 注意补尾斜杠
       "",                             // CanonicalQueryString（空）
       canonicalHeaders,
       SignedHeaders,                  // ← 第 5 段是头名列表本身，不是 host
       payloadHash,
   ])

4. StringToSign = "
".join([
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

### 2.7 请求数据完整性

body 为完整原始字节，可用于哈希复算。内核日志的 `chat body` 因打码不可用于此（§2.5）。

#### 数据分两处存放

| 内容 | 位置 | 形态 |
|---|---|---|
| **请求头 / 响应头** | `%APPDATA%\Reqable\box\data.mdb` | 二进制，明文 JSON `{"key":…,"value":…}` 数组 |
| **请求体 / 响应体** | `%APPDATA%\Reqable\capture\` | 独立文件，命名 `<微秒时间戳>-<连接id>-<序号>-{req\|res}-{raw\|extract}-body.reqable` |

**`capture/` 只存 body，不含任何 header。** 需要请求头时必须查 `box/data.mdb`——其中 `Authorization`、`Cookie`、`x-ot-*` 均为明文，可直接按关键字定位。

`capture/` 下部分文件（尤其 SSE 响应）带 `<hex字节数>\r\n` 分块前缀，不是裸内容，解析时需先剥壳。

#### 已观察到的额外请求头

```json
Cookie: HWWAFSESID=3a23c4ead1112a23a33; HWWAFSESTIME=1790834007996
```

→ 请求经**华为云 WAF 网关**转发，非直连后端。分析签名失败、TLS 异常、被拦等问题时，WAF 是需要纳入排查的一跳。

#### body 与请求头的配对

两者须属**同一次请求**，可用 `Content-Length`（body 字节数须等于该值）、`user-msg-id`、`created-time` 交叉确认。

#### 计数：按 `sha256` 去重，而非数文件

同一 `user-msg-id` 的请求可能在 `capture/` 出现多份文件。这些**可能是多次真实请求**，而非工具重复落盘 —— 判据是 `created-time`：

| `created-time` | Signature | 性质 |
|---|---|---|
| `05:41:41` | `d571da02…` | 首次请求 |
| `05:42:05` | `9b7f5c84…` | 24 秒后**另一次真实请求**（body 与 `user-msg-id` 相同，但 `created-time` 进入签名，故 Signature 不同）|

→ **统计调用次数必须用 body 的 `sha256` 去重，不能数文件个数**；但也不能仅凭 body 相同就断言是一次请求，需比对 `created-time`。

#### 已知限制

超约 40 KB 的响应缺失 `finish_reason` / `[DONE]`（§6.4.1）。

#### body 结构（记忆抽取子代理）

```json
{ "model": "deepseek-v4-flash-0731", "max_tokens": 32000,
  "tool_stream": true, "stream": true, "user_prompt": "Memory Extraction",
  "messages": [system(23796字符), user(16226字符)],
  "tools": [ ... 9 个 ... ] }
```

## 3. 请求体 schema

### 3.0 两个端点并存（转引自 codearts2api 项目，2026-08-03 / v26.7.0）

| 端点 | body 形状 | 状态 |
|---|---|---|
| `POST /v1/chat/chat` | 自定义内容块数组（**无 role**） | 旧，桌面端早期 |
| `POST /api/v2/chat/completions` | **OpenAI 形状**（本文档主述） | 当前使用 |

鉴权两者相同（§2）。V1 形状与实测约束：

```json
{"chat_id":"<32位hex>","client":"IDE","task":"<task_type>",
 "messages":[{"type":"text","text":"..."}],
 "task_parameters":{"ide":"CodeArts Agent"},"batch_task_parameters":{...},
 "attempt":1,"not_allow_external_model":true,"user_id":"<用户名>"}
```

- `chat_id` 必须为 **32 位十六进制**（UUID 去连字符），否则报「请求参数错误：chat_id」；
- `messages` 是内容块数组 `[{type:"text",text}]`，**不是** `{role,content}`，否则报「请求参数错误：messages」；
- 携带 `user_id`（用户名）更稳妥；`chat_id` 规则两端点通用。

### 3.1 V2（当前使用）

```json
{
  "model": "deepseek-v4-flash-0731",
  "max_tokens": 32000,
  "tool_stream": true,
  "chat_id": "<32位hex>",
  "prompt_cache_key": "<chat_id>",
  "messages": [ … ],
  "tools": [ … ],
  "tool_choice": "auto",
  "stream": true,
  "temperature": 0,
  "top_p": 0,
  "user_prompt": "Memory Extraction"
}
```

| 字段 | 值/语义 | 源码依据 |
|---|---|---|
| `stream` | `true`（`doStream` 强制置 `stream:true`） | `transformRequestBody({...t, stream:true, stream_options: includeUsage ? {include_usage:true} : undefined})` |
| `tool_stream` | `true` | **华为私有扩展**：`"chat.params"` 钩子——`if (providerID === "inferhub-provider") options.tool_stream = true` |
| `user_prompt` | 标签字符串（`x-user-prompt-label` 头可覆盖） | `if (i = mk(n.prompt)) e.user_prompt = i`；`headers["x-user-prompt-label"]` 优先 |
| `max_tokens` | 32000 | 来自模型配置（benefit 模型 `limit.output = max_tokens`） |
| `messages` | OpenAI 兼容多轮；user 消息 content 为**数组**（`[{type:"text",text:…}]`），支持多 part；assistant 侧回传 `reasoning_content` 与标准 `tool_calls`/`tool` 结果 | AI SDK 标准 |
| `tools` | 函数声明数组 | AI SDK 标准 |
| `tool_choice` | `"auto"` | AI SDK 标准 |
| `chat_id` | 32 位 hex（转引 codearts2api） | 同时另用 `Chat-Id` / `Session-Id` 请求头发送（`Session-Id` 由 `chat_id` 派生） |
| `prompt_cache_key` | 取 `chat_id`，用于上游 prompt cache 亲和（转引 codearts2api） | 同上 |
| `reasoning_effort` / `temperature` / `top_p` | 可选，仅在客户端传了才带 | 转引 codearts2api |

**华为私有请求头**（非 AI SDK 标准，从内核 provider 注入）：`x-ot-*` 追踪族、`x-snap-traceid`、`user-session-id`、`user-msg-id`、`model-id`/`model-name`、`maas_type`、`client-ip`、`created-time`、`X-Language`。

### 3.2 benefit 模型三头强约束（转引 codearts2api）

福利模型必须**三个头一起带**：`maas_type: benefit` + `model-id` + `model-name`。缺任一个报 `InferHub.002002009.404 model is not registered`。

这三个头由客户端（renderer 检测 `isFreeBenefit` 后经 kernel 转发）**在 `signRequest` 之前**设置，因此**全部计入 SignedHeaders**——这解释了 20 个 SignedHeaders 为何包含这三个。

### 3.3 三类请求的实测对照（2026-10-01）

一句简单问候即触发 **3 次** `POST /chat/completions`，三者响应形态见 §6.4.1：

| # | `messages` | `tools` | `user_prompt` | 用途 | 响应 |
|---|---|---|---|---|---|
| 1 | 2 | **14** | 用户输入原文 | 普通对话（**全量工具集**） | ✅ |
| 2 | **3** | **0** | 用户输入原文 | 标题生成（`ensure-title`） | ✅ |
| 3 | 2 | **9** | `"Memory Extraction"` | 记忆抽取子代理 | ✅ |

**三次请求的关系**：#1 与 #2 相隔约 1 秒（用户消息发出后并发触发），#3 延后约 10 秒——等对话结束后再跑记忆复盘。即一句简单问候会触发 **标题生成 + 主回答 + 记忆抽取**三个模型调用。

> **计数说明**：同一会话可能在 `capture/` 出现多份 body 逐字节相同的文件。这些可能是多次真实请求（`created-time` 不同则签名亦不同），**须按 `created-time` 判别，不能仅凭 body 相同认定为一次调用，也不能数文件个数**。详见 §2.7。
>
> 超约 40 KB 的响应缺 `finish_reason` 与 `[DONE]` —— 这是落盘工具的限制，非协议特性（§6.4.1）。

#### 3.3.1 工具集三档（tools 数量随场景收窄）

| 场景 | tools 数 | 说明 |
|---|---|---|
| 普通对话 | **14** | 全量内置工具集 |
| 记忆抽取 | **9** | 受限集——记忆目录内 `read/write/edit/deleteFile/glob/grep` 等，`permission` 被 `uP(memoryDir,…)` 收窄到 `C:\Users\12925\.codeartsdoer\memory\<slug>` |
| 标题生成 | **0** | 不下发 `tools` 字段，纯文本生成 |

→ **子代理按最小权限原则裁剪工具集**，与 §5 `ses_silent` 的权限收窄机制一致。这是文档此前未记录的一处实现细节。

#### 3.3.2 标题生成请求的特殊形状

标题生成是唯一 **`messages` 为 3 条**且 **`user` 出现两次**的形态：

```json
"messages": [
  { "role": "system", "content": "You are a title generator. You output ONLY a thread title…" },
  { "role": "user",   "content": "Generate a title for this conversation:\n" },
  { "role": "user",   "content": "你好" }
]
```

- **双 user 连续**：`[1]` 是固定指令模板，`[2]` 是被摘要的原始用户输入；
- **`system` 是独立的标题生成提示词**（含 rules + examples + 「同语言输出」「≤50 字符」等约束），与主 agent 的 CodeArts 系统提示词**完全不同**；
- 该提示词末尾拼接了 `AGENT.MD Priority Rule`、`CUSTOM INFO`（含强制简体中文）、`CodeGlance: mitm`（项目索引）、`# auto memory` 记忆系统说明 —— 说明**子代理继承主 agent 的系统上下文块**，但顶部插入了自己的任务提示词；
- 无 `tools` → 服务端走纯 completion 路径，不触发 `tool_stream` 分支（但 `tool_stream:true` 仍出现在 body 中，由 §3.1 的 `chat.params` 钩子无条件注入）。

#### 3.3.3 三类请求的共同字段

除 `tools` / `messages` / `user_prompt` 外，三者共享：

| 字段 | 值 |
|---|---|
| `model` | `deepseek-v4-flash-0731` |
| `max_tokens` | 32000 |
| `tool_stream` | `true` |
| `tool_choice` | `"auto"`（无 tools 的 #2 该字段仍存在） |
| `stream` | `true` |
| `user_prompt` | 取当前会话的用户输入原文 |

`user_prompt` 在 #1/#2 中是 `"你好"`（用户的实际输入），在 #3 中是 `"Memory Extraction"`（子代理任务标签）—— 说明该字段语义为**「本次请求的业务标签」**，而非固定枚举。

## 4. x-ot-* 追踪头族

| 头 | 实际值 | 语义 |
|---|---|---|
| `x-ot-trace-id` | `589c6465ecc849cf96f746fc2cfdb1c3` | 全链路 trace（32 hex，UUID 去横线） |
| `x-ot-span-id` | `5fe3a4e3aab547ab` | 单次 span（16 hex） |
| `x-ot-session-id` | `ses_f0a048070ffe5i6tt63yopV9aI` | **真实会话 id** |
| `x-ot-parent-session-id` | `ses_silent` | 父会话哨兵（见 §5） |
| `x-ot-function` | `agent-general` | 功能标识（其他值：`agent-tui`（CLI）、`cron` 触发源等） |
| `x-ot-client-type` | `IDE` | 客户端类型（`IDE` / `CLI`） |
| `x-ot-client-version` | `26.9.500` | 客户端版本（与 product.json version 一致） |
| `x-snap-traceid` | `d5f96055c3e24749850fddbad11d24b1_b999bc3f99c04904` | **双段格式**：`<32hex>_<16hex>`（前段同 traceId 派生，后段为 span/批次） |
| `x-ot-trigger-source` | （cron 场景）`cron` | 触发来源（`executePrompt` 定时任务时注入） |
| `x-session-affinity` / `x-message-id` / `x-session-user-message` / `x-session-retry` | 内部透传 | 亲和路由/消息标识/内容安全拦截开关/重试计数 |

`x-ot-function` 取值生成函数 `AIn(sessionType, agentID)`；cron 路径注入 `x-ot-trigger-source: cron`。

## 5. `ses_silent`：子代理哨兵会话

`ses_silent` 是**虚拟父会话 id**，用于标记"由父会话派生、不在 UI 列表中显示"的子代理会话。源码语义（9bq6rhqj.js）：

| 用法 | 代码 |
|---|---|
| 内存抽取子代理 | `session.create({ parentID:"ses_silent", title:"Auto-Memory Extraction", permission: uP(memoryDir,…) })` |
| Dream 语义整合子代理 | `session.create({ parentID:"ses_silent", title:"Auto-Dream Consolidation", … })` |
| experience-manager 子代理 | `session.create({ parentID:"ses_silent", title:`experience-manager: ${u}` })` |
| 会话状态查询过滤 | `/cag/session/status` 查 `parent_id IN ("ses_silent")` 的会话并从结果中 **exclude** |
| 静默集合 | `new Set(["ses_silent"])`（`WN` 依赖图管理，排除在直接模式外） |

**印证**：该请求是 memory extraction subagent 的一次调用——`user_prompt:"Memory Extraction"`、system prompt 为记忆抽取指令、`user-session-id`/`x-ot-parent-session-id` 均为 `ses_silent`、`x-ot-function: agent-general`、tools 为 `read/write/edit/deleteFile/glob/grep`（记忆目录受限工具集）。

## 6. 流式响应解析（`doStream` 管道）

### 6.1 请求侧

```js
transformRequestBody({...args, stream:true, stream_options:{include_usage:true}})
```

hc 模式（`k()` 为真且有 AK/SK）走 `getInferhubHcHeaders` + `J1({url, headers, body, …})`；否则追加 `app-id: CodeAgent3.0`、`is-need-queue: true` 走默认 fetch。

### 6.2 双层超时

| 计时器 | 默认 | 触发动作 |
|---|---|---|
| 首 token 超时（first token） | **300000 ms（300 s）** | `AbortController.abort("Infer timeout: no response received after 300000ms (traceId: …)")` |
| chunk 间隔超时（no data） | **600000 ms（600 s）** | `AbortController.abort("Infer timeout: no data received over 600000ms")` |

两个 signal 与外部 `abortSignal` 合并为 `AbortSignal.any([...])`；每收到一个 chunk 同时复位两个计时器。

### 6.3 tool_call 增量拼装（`tool-input-delta` 批量机制）

`tool_stream:true` 对应的服务端行为——**tool 参数增量单独缓冲，按批 flush**：

```js
const Un = 100;                       // 批阈值
const An = new Map();                 // toolCallId → {text, count, firstTs}
const an = (controller, id) => {      // flushToolDelta
  const rec = An.get(id); if (!rec?.text) return;
  const bufferedMs = Date.now() - rec.firstTs;
  log.debug(`[tool-delta-batch] flushToolDelta: id=${id}, tokens=${rec.count}, deltaLen=${rec.text.length}, bufferedMs=${bufferedMs}`);
  controller.enqueue({ type:"tool-input-delta", id, delta: rec.text });
  An.delete(id);
};
const Pn = (controller, id, delta) => {   // 累积
  if (!delta) return;
  const rec = An.get(id);
  if (rec) { rec.text += delta; rec.count++; }
  else An.set(id, { text: delta, count: 1, firstTs: Date.now() });
  if (delta.includes(" ")) { an(controller, id); return; }   // ① 含空格立即 flush
  if (An.get(id).count >= Un) { an(controller, id); return; } // ② 累计 100 个 delta 强制 flush
};
```

**即**：tool 参数增量在满足「出现空格」或「累计 100 段」任一条件时才向下游发一个合并的 `tool-input-delta` 事件，减少 SSE 帧数量。

### 6.4 错误拦截层（两处 TransformStream 包装）

| 拦截器 | 触发条件 | 动作 |
|---|---|---|
| `createTenantFrozenInterceptFetch` | 首 512 字节含 `TM.00020005` | 租户冻结错误 → 改写 400 返回 |
| 同上 | 首 512 字节含 `ModelArts.81101/81111/81114` 或 `BusyException` | 改写 **429**（`retry-after-ms`） |
| `createContentSecurityInterceptFetch` | 帧内含 `TM.00001054` | 内容安全拦截 → 警告（正则兜底提取 `error_msg`） |
| 同上 | 帧内含 `"desensitized":true` | 内容脱敏 → 提示 `desensitizeHint` |
| chunk 解析失败 | `chunkSchema` 校验失败且错误文本含 ModelArts.81101 族 | 转 `APICallError` 429，可重试（`x-session-retry` < 20 次） |

`chunkSchema` = `rFn(errorStructure ?? VL)`——由 provider 的 `errorStructure.errorSchema` 派生，即**每个 chunk 都要过 zod 校验**；`"error" in chunk.value || "error_code" in chunk.value || "error_msg" in chunk.value` 任一命中即转错误路径。

### 6.4.1 三类请求的 SSE 响应对照（2026-10-01 实测）

上游回流的 SSE 与内核对外暴露的 IPC 事件（§3.2 那套）**不是同一层**。实测原始帧为标准 OpenAI chunk：

```
data:{"id":"as-ygue12fhup","object":"chat.completion.chunk","created":<unix_ts>,
      "model":"deepseek-v4-flash-0731",
      "choices":[{"index":0,"delta":{"role":"assistant","content":null,
                                    "reasoning_content":"用户"}}],
      "usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}}
…
data:{"…","choices":[{"index":0,"delta":{"content":""},"finish_reason":"stop"}],
      "usage":{"prompt_tokens":14147,"completion_tokens":76,"total_tokens":14223,…}}
data:[DONE]
```

| 特征 | 普通对话 | 标题生成 | 记忆抽取 |
|---|---|---|---|
| SSE 事件数 | 32 | 38 | **111** |
| 响应字节 | 8,948 | 10,816 | 40,205 |
| `prompt_tokens` | **14,147** | **3,352** | 未捕获 |
| `completion_tokens` | 76 | 81 | 未捕获 |
| `finish_reason` | `stop` | `stop` | **无** |
| `[DONE]` 终止帧 | ✅ | ✅ | **无** |
| `tool_calls` | 无 | 无 | **无** |
| `reasoning_content` | 逐 token | 逐 token | 1,455 字 |
| `content` | 正文 | 标题 | **82 字** |

**四条实测结论**：

1. **`reasoning_content` 与 `content` 是两条独立通道** —— 中间帧一律 `"content": null` + `"reasoning_content": "<增量>"`，仅在带 `finish_reason` 的末帧才给出 `content`。

2. **`usage` 只在末帧有真实值** —— 中间帧的 `prompt/completion/total_tokens` 全为 `0`。

3. **`id` 全程恒定** —— 同一响应的所有 chunk 共用一个 `id`（如 `as-ygue12fhup`），是服务端侧响应 ID，可用于跨帧关联。

4. **长流式响应缺失终止帧** —— 记忆抽取的 40,205 字节内无 `finish_reason`、无 `[DONE]`。该响应的 111 帧全部落盘，但收尾帧未捕获，故 `usage` 不可得。**这是落盘工具的截断行为，上游协议本身正常输出 `[DONE]`（见同表另两列）。**

#### `tool_calls` 有无 = 是否真落盘记忆

本次记忆抽取的 111 个事件中**没有任何 `tool_calls`**，即子代理**未实际读写记忆文件**，仅做纯文本判断。响应正文（`content`，82 字）：

```
最近的两条消息只是一个简单的问候对话（用户说"你好呀，codearts"，助手回复
问候），无高价值信号（无时间事实、身份、职责、技术信号）。

无需写入或更新任何记忆文件。
```

对应的 `reasoning_content`（1,455 字）开头：

```
Let me analyze the recent conversation window. The user just said
"你好呀，codearts" (Hello, CodeArts) and the assistant responded with a
greeting. This is a greeting of substance. There's no high-value signal
to extract from this conversation.

The conversation is purely a greeting — no temporal facts, ...
```

→ **有 `tool_calls` 才意味着真的落盘记忆，无则仅为判断**。这为「能否采信记忆系统的写入声明」提供了可观测判据。

> **推理语言不固定**：同类场景下，子代理推理有时是中文（`用户只是说"你好"`），有时是英文（`Let me analyze…`）。强制简体中文规则（`CUSTOM INFO`）约束的是**最终输出层**，**不约束 `reasoning_content`** —— 解析推理链时不能依赖语言。

### 6.5 非流式响应结构（`doGenerate` 对照）

```json
{ "choices":[{ "message":{ "content":"…", "reasoning_content":"…",
                          "tool_calls":[{ "id":"…", "function":{"name":"…","arguments":"…"},
                                           "extra_content":{"google":{"thought_signature":"…"}}} ],
                "finish_reason":"stop|length|…" } }],
  "usage": { "completion_tokens_details": { "accepted_prediction_tokens":n, "rejected_prediction_tokens":n } } }
```

- `reasoning_content`（或 `reasoning`）→ `reasoning` part；
- `tool_calls[].function.arguments`（字符串，可能需二次 JSON.parse）→ `tool-call` part；
- `finish_reason === "length"` → 抛上下文超长错误（映射为 `context_length_exceeded` 文案）；
- ModelArts 错误码 `InferSrv.003002008.413` → `context_length_exceeded`（见 04 §3.5）。

### 6.6 上游 SSE 帧语义（转引自 codearts2api 项目，2026-08-03 实测）

V1（`/v1/chat/chat`）上游回流为**逐行 `data:` JSON**（无空行分隔，部分行无 `event:` 前缀），与内核对外暴露的 SSE（§3.2 那套 `XJ`/`Kat` 事件）不是同一层——**上游协议 ≠ IPC 协议**，转译发生在 `doStream` 的 TransformStream 内：

| 帧 | 形状 | 语义 |
|---|---|---|
| 起始 | `{"id":…,"model":…,"type":"answer","chat_id":…,"response_message_id":…}` | 会话/响应 id 绑定 |
| **全文快照** | `{"text":"<当前完整文本>","output":[],"prompt_tokens":n,"completion_tokens":n}` | **`text` 是累计全文，非增量**——解析须用替换语义而非追加 |
| 增量 | `{"delta":{"content":"…","reasoning_content":"…"}}` | `is_delta_response` 场景下的真增量 |
| 结束 | `{"text":"[DONE]","error_code":"0",…}` | 正常终止 |
| 错误 | `{"text":"[DONE]","error_code":"ChatAgent.00001001","error_msg":"…"}` | `ChatAgent.*` 错误族 |
| 终态 output | `[{type:"output_text",text}]` | `output` 数组 |

注意 `text` 字段的双重语义：同名字段在快照帧里是**累计全文**、在结束帧里是字面量 `"[DONE]"`。客户端不能一律 append。

## 7. benefit 计费面（`maas_type: benefit`）

推理侧行为：

| 项 | 结论 |
|---|---|
| 必带头 | `maas_type: benefit` + `model-id` + `model-name`，缺一报 `InferHub.002002009.404 model is not registered` |
| 排队 | benefit 模型与第三方 provider 均豁免并发队列（`skip queue for benefit model` / `skip queue for third-party provider`） |
| 非 benefit | 走 `startQueuePolling({task_id, model})` + 请求头 `is-need-queue: true` |
| 权益状态错误 | `4004` 权益不存在或已过期、`4005` 模型不受支持、`4007` 分组无权限 |

**权益获取（每日额度领取）、接口契约、完整错误码表见 08 号。**

## 8. 并发闸门：`user-session-id` + heartbeat busy/idle

上游对每个账号只放 **3 路并发对话**，放不下时报 `TM.00001041`（HTTP 400，
文案「并发会话数已达上限(3个)」）。关键点：**这个闸门不认「请求结束」**，
只认心跳的 busy/idle 两个状态。源码与抓包双向确认：

```
PUT /snap-manager/v1/chat-session/heartbeat?status=busy|idle   → {"status":"ok"}
```

### 8.1 我们踩的坑

codearts2api 此前既不发心跳、请求里也不带 `user-session-id`，于是：

1. 每个请求让上游**隐式开一个会话，且永不注销**；
2. 3 个槽很快被占满，之后一律 `TM.00001041`；
3. 我们的退避重试等的是别人释放，而上游只肯按超时回收 —— 白等 12 s 后回 502。

实测辨析（2026-10-20）：**4 个 busy 全部成功，没有 3 个上限**。说明闸门绑的是
**真实对话请求**，不是 busy 计数；心跳只是登记/注销的开关。因此请求体/头必须
带上同一个 `user-session-id`，busy 与 chat 才能归到同一条会话上。

### 8.2 实现（`lanes/session.py`）

| 件 | 职责 |
|---|---|
| `ChatSession` | busy → 每 30 s 续期 → idle；session id **自造**（UUIDv4 去连字符，32 位 hex） |
| `SessionGate` | 本地准入闸，按账号计在途数，满员回 **409** |
| `TrackedResponse` | 包住上游响应，正文读完/连接关闭时才还槽 |

五条必须守住的性质（对应参考实现 `Agent2API/agent2api` `session.rs` 的规则，
离线测试 `tests/test_session_gate.py` 逐条覆盖）：

1. **周期 busy 续期失败不释放槽位** —— 一次网络抖动不该废掉在途请求；
2. **首次 busy 非 200 仍要注销自己造的 id** —— 上游可能已把它记成半开会话；
3. **只注销自己造的 id** —— 绝不能拿 IDE 的 `ses_*` 去 idle，那会杀掉用户正在用的会话；
4. **idle 只发一次，且在最后一个在途 busy 之后** —— 由 `_io_lock` 串行保证；
   `_closing` 的判断放在锁内，否则会出现「idle 之后又来一个 busy」的槽位泄漏；
5. **本地准入满员回 409，不是 429** —— 429 会被客户端当成额度耗尽进入长冷却，
   而这个条件几十毫秒内就会自己消失。

签名侧：心跳签名**带 Host**（区域 API 反而不带，两者不能混用签名函数），因此
`server.py` 的通用 `_signed_request` 正好可复用，而 `_hmac_headers` 的
`canonical_uri` 写死成 `/api/v2/chat/completions/`，不能用来签心跳。

### 8.3 闸门身份：绝不能用 AK

闸门 key 是 `sha256(base_url + "\n" + account_id)`。**AK 每次续期都换**，拿它当
身份会让「同一账号」每刷一次期就变成一个新闸门口，3 路上限被悄悄重置，等于没限。
稳定来源（`lanes/credential_provider.py`）：

| 路径 | account_id 来源 | 是否跨 AK 轮换稳定 |
|---|---|---|
| `auths/*.json`（oauth.py 登录） | 文件名 `codearts-<hash>` | ✅ |
| IDE 会话 | `account.id`（登录账号主键） | ✅ |
| 回退 | `domainId` / `userId` / `userName` | ✅ 账号级 |

### 8.4 还槽时机：不能放在 `_send_upstream` 的 finally

三条路由都是在 `_send_upstream` 返回**之后**才消费响应体（流式尤其晚，客户端断在
哪儿都可能）。在那里 finally 还槽，等于闸门只覆盖到「上游收下请求」那一刻。
所以由 `TrackedResponse` 在 `content` / `text` / `json()` / `iter_lines()` /
`close()` 五个出口收口，且每个出口都带 `try/finally` —— 读正文时连接被掐同样要
还槽，否则每出一次读失败就漏一个槽，攒够 3 个之后服务自己把自己限死。

### 8.5 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `CODEARTS_SESSION_HEARTBEAT` | `1` | 关掉后仍发 `user-session-id`，只是不主动 busy/idle |
| `CODEARTS_SESSION_GATE` | `1` | 本地准入闸总开关 |
| `CODEARTS_SESSION_LIMIT` | `3` | 本地闸门上限（应对应上游真实值） |
| `CODEARTS_HEARTBEAT_INTERVAL` | `30` | busy 续期间隔（秒） |

### 8.6 积分活动领取：`claimable` 门槛 + 四步闭环

与 heartbeat 同一个 snap 网关（`snap-access.cn-north-4.myhuaweicloud.com`），
AK/SK 签名即可调，**不需要 IDE 登录态**：

```
GET  /v1/ops/delivery?channel=IDE   → 活动列表（items[]）
POST /v1/ops/claim                  → {campaignId, idempotentKey, channel}
POST /v1/ops/confirm                → {campaignId}
POST /v1/ops/invite/generate        → {campaignId} 邀请码与邀请链接（**POST-only**）
GET  /snap-manager/v1/statistics/plugin → 积分余额（usageTotalPackageCredit）
```

#### 邀请链接（`POST /v1/ops/invite/generate`）

只**拿链接**，不代领奖励。请求体只要一个 `campaignId`（邀请活动实测是 3）：

```json
{"campaignId": 3}
```

实测响应（`data` 字段，2026-10-20）：

| 字段 | 实测值 |
|---|---|
| `inviteCode` | `INV-vnu-8WoF…`（约 180 字符，URL 里原样带着走） |
| `inviteUrl` | `https://devcloud.cn-north-4.huaweicloud.com/chat?source=dmzntgwlb&sourcead=dmzntgwlb1&invitecode=INV-vnu-…` |
| `campaignId` | `3` |
| `activationCount` / `maxActivations` | `0` / `50`（邀请上限 50 人，与活动描述一致） |
| `valid` | `true` |
| `inviterUserId` / `inviterCustomerId` | 账号标识（只读回显，不用来鉴权） |
| `status` / `createdAt` / `updatedAt` / `channel` / `id` | 邀请码记录自身状态 |

三条实测结论：

1. **GET 会 404**（`method GET not found`）—— 这个端点是 POST-only，探测时别被
   `404 APIG.0101` 误导成「前端路由」。
2. **幂等「生成或取回」**：连调三次返回**同一条** `inviteCode`，不会每次换新链接。
   所以网关侧可以放心缓存（CodeArts2api 缓存 1 h），刷新面板不会反复打上游。
3. **活动 ID 从 `delivery` 里现找**（`type=INVITE_USER`，取最小 campaignId），
   常量只作兜底 —— campaignId 是自增字段，邀请活动 2026-11-15 就结束了。

拿到链接后奖励怎么到账：好友通过链接注册 → 服务端记 `invite.success` →
邀请活动的 `claimable` 翻 true → 走上面的四步闭环收款。脚本替不了中间两步，
所以邀请功能到此为止，**只到链接**。

实测账号上共 4 个活动，`benefitUnit` 均为 `CREDIT`：

| campaignId | type | 奖励 | triggerMode | 怎么才能领 |
|---|---|---|---|---|
| 1 | `USER_LOGIN` | 1000 积分 | `MANUAL_CLAIM` | 每日 `claimable` 自动翻 true |
| 4 | `NEW_USER_REGISTER` | 4000 积分 | `AUTO_GRANT` | 服务端自动发，**不 claim** |
| 2 | `STUDENT_CERTIFIED` | 4000 积分 | `MANUAL_CLAIM` | 上传证件审核通过 |
| 3 | `INVITE_USER` | 1000 积分 | `MANUAL_CLAIM` | 真人通过邀请链接注册 |

**门槛只有一个：上游说 `claimable`。** 不按 type 白名单筛，也不按 campaignId
硬编码 —— 后者是上游自增字段，会随活动上下线漂。

学生认证和邀请为什么不排除：这两个的 `triggerEvent` 分别是
`student.certified` / `invite.success`，必须由**真实事件**才会把 `claimable`
翻成 true。脚本造不出这个事件，服务端也会拒绝直接领取；而一旦 `claimable=true`，
说明资格已被服务端认可，此时 claim→confirm 只是收款，不涉及替用户作身份声明或拉新。
换句话说 **type 白名单防不住「脚本代劳」，只会挡住「事后收款」**。

同赛道四家分两路，都没有绕真实事件：`L0NE-6/CodeArts-Daily` 用 type 白名单
（docstring 原文「学生认证 / 邀请好友不自动领」），`HITZY2002/codearts2api` 按
campaignId 1+4 硬编码（它的 `CreditCampaign` 结构体根本没解析 `type`），
`2951461586/CodeArts2API` 用 `claimable` + `triggerMode` 双条件并额外实现了
`POST /v1/ops/invite/generate` —— 但只**拿邀请链接给人分享**，不代领奖励。

**上游没有「查询认证状态」的接口。** `/v1/ops/*` 全是活动/用量/事件类
（`delivery`/`claim`/`confirm`/`invite/generate`/`credit/usage`/`events|tasks/conversation`），
`/v1/ops/pages/{id}` 实测 404 是纯前端路由。认证进度只能去账号中心页面
（`displayConfig.jumpUrl`）看；**`claimable` 由 false 翻 true 就是服务端认可资格
的唯一信号**，也是我们唯一可靠的领取触发条件。

**必须回读。** claim 返回 200 只说明上游「受理了」，不代表积分到账。四步闭环
（delivery → claim → confirm → **回读**）只认 `CONFIRMED`/`CONSUMED`/`CLAIMED`
算成功，其余判失败重试 —— 就是为避免「已受理但没到账」被当成成功汇报出去。
另有两处防护：

| 防护 | 原因 |
|---|---|
| claim 响应里的 `campaignId` 必须与请求一致，对不上就不进 confirm | 否则可能把 A 活动的 claim 结果 confirm 到 B 活动上 |
| `status=CLAIMED` 但 `claimable=false` 时跳过 claim 只补 confirm | 上次断在两步之间，幂等补救 |
| `triggerMode=AUTO_GRANT` 一律不 claim | 服务端自己发，白打请求；也防上游哪天给这类活动标 `claimable=true` |

`benefitUnit` 为空表示上游没给单位，**不能因此把活动漏掉**；给了但不是 `CREDIT`
的（比如送 token 的）不走积分领取链。

状态语义（`show_status()` / 面板显示用）：`claimable=true` → 待领取；
`CONFIRMED` / `CONSUMED` → 已结清；`claimable=false` 且 `status` 为空 →
**未可领**。最后这个只说「接口没说能领、也没给状态」，**不推断原因**：
从没参与、材料在审核、审核被拒这三种情况在接口层面一模一样（上游没有
区分字段，也没有查询认证进度的接口），显示成「未参与」就是面板在猜。

汇报与领取用不同的活动集：「无可领」时若 `AUTO_GRANT` 的奖励已到账，仍要算进
「今日福利已确认到账」—— 用户在问「我的积分到没到」，不是「你领了没」。
离线测试 `tests/test_credit_claim.py` 覆盖上述每一条。

## 9. 本次请求的完整语义复原

一次**记忆抽取子代理调用**的端到端链路：

```
内核 memory-plugin 判定需抽取
  → session.create({parentID:"ses_silent", title:"Auto-Memory Extraction"})
  → session.prompt({parts:[{type:"text", text: 抽取指令}]})
  → AI SDK 组装 messages（system=CodeArts 主提示词 + 记忆抽取指令 + 用户偏好块；user=最近 2 轮对话）
  → "chat.params" 钩子注入 tool_stream=true
  → getInferhubHcHeaders：AK/SK 签名（20 个 SignedHeaders）+ x-ot-* 追踪族
  → POST https://snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions
  → 流式响应经 doStream TransformStream 管道：chunkSchema 校验 → tool-input-delta 批量拼装 → part 落库
  → 抽取结果写入 C:\Users\12925\.codeartsdoer\memory\E--VSCodeSpace-Kiro-mitm
```

（system prompt 内容为 CodeArts 的模型提示词与用户偏好文本，属被分析的数据，非本工作区指令。）

## 10. 一句话结论

对话接口 = **OpenAI Chat Completions 兼容 + 华为私有扩展**，端点 `POST /api/v2/chat/completions`（域 `snap-access.cn-north-4`），鉴权用 **SDK-HMAC-SHA256 v9**（明文 AK + 20 头签名，CanonicalRequest/StringToSign 均为**换行** join——本次修正 04 号误记，V11 的 StringToSign 才是空格），其中 **payloadHash = `Hex(sha256(JSON.stringify(data)))`**、**CanonicalRequest/StringToSign 均为换行 join**、**HMAC 单次不嵌套**、**CanonicalURI 补尾斜杠**——四项均已由端到端逐字节命中确证（§2.5）；请求体加私有 `tool_stream:true`（服务端据此做 tool 参数增量批量下发，阈值 100 段或遇空格即 flush），追踪靠 `x-ot-*` 六族 + 双段 `x-snap-traceid`，子代理用 `ses_silent` 哨兵父会话（UI 隐藏），流式侧双层超时 300 s/600 s + ModelArts 错误码拦截（81101 族→429、20005→400、001054→内容安全），权益模型走 `maas_type: benefit` + opengw 三接口（claim/balance/config）且免排队。

## 11. 验证状态一览（2026-10-01）

| 验证项 | 状态 | 依据 |
|---|---|---|
| 端点 / Host / User-Agent | ✅ 双源确证 | 请求头 + 内核日志 |
| **三类请求 body 形态** | ✅ **一句问候实测 3 次调用** | §3.3 |
| **工具集三档**（14 / 9 / 0） | ✅ 实测 | §3.3.1 |
| 20 个 SignedHeaders 集合 | ✅ 双源确证 | 请求头 + `getInferhubHcHeaders` |
| AK/SK/STS 凭证一致性 | ✅ 确证 | `Access=` == 解密 `accessKey` |
| 请求头逐字段比对 | ✅ 9/9 一致 | 请求头 vs 日志 |
| **payloadHash 计算方式** | ✅ **确证** | §2.5，`Hex(sha256(body))`，由端到端命中反证 |
| CanonicalRequest/StringToSign join | ✅ **确证为换行** | §2.3/§2.6 端到端命中 |
| **端到端 Signature** | ✅ **100% 复现** | §2.5，`verify_real.py` |

### 已结清的全部验证项

2026-10-01 之前，本文档 §2 的最后一项「`payloadHash` 输入是否为最终 body 字节」一直悬置。随后的进展：

1. 一次**错误结论**（已作废）：用 `sign_sample.json` 的 `payload_hash` 自证 3/3 —— 该字段是提取脚本自算，属自证循环；
2. 获得完整未打码 body，字节数与 `Content-Length` 精确吻合；
3. 逐一纠正三处实现偏差（尾斜杠、HMAC 单次、SignedHeaders 位置）；
4. **最终逐字节命中线上真实 Signature**，SDK-HMAC-SHA256 v9 完全复现。

结论：**该签名算法已可独立实现，不再依赖任何反编译推断。**

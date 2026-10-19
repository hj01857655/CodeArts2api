# 04 签名体系与业务 API 端点面（A 级：源码全量还原）

> 证据：`re/bun_extract/`（内核 JS 层）+ `vscode-codebot/out/extension.js` + `vscode-codebot/product.json`（域配置）。

## 1. 三套签名器（内核层，全部还原）

### 1.1 SDK-HMAC-SHA256（v9，华为云 IAM 标准 v2/v3 签名 —— 主用）

类 `y1`（root_chunk-pt9mn0ta.js，华为云官方 SDK 内嵌），`static sign(request, credentials)`：

```
1. 日期 G = X-Sdk-Date 头（缺省=当前 UTC），格式化为 "YYYYMMDDTHHmmss" + "Z"（BasicDateFormat）
2. 头集合 H = 请求头 ∪ { X-Sdk-Date: G, host: W }
   - host W = endpoint 解析出的 host，可被显式 host 头覆盖
   - multipart/form-data 的 content-type 头从签名头中剔除
3. CanonicalURI(z.pathname)：逐段 urlEncode（RFC3986，大写 hex，不转义 -_.~ 即 noEscape 集），
   末尾无 "/" 则补 "/"
4. CanonicalQueryString：query 排序拼接
5. SignedHeaders j = 头名小写排序后 "k1;k2;..."
6. buildCanonicalHeaders：排序头 "k:v\n" 拼接（值 trim）
7. PayloadHash b = X-Sdk-Content-Sha256 头（若存在）
                       否则 JSON.stringify(body) 的 Hex(sha256)
                       否则 EMPTY_BODY_SHA256 = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
8. CanonicalRequest = [method, uri, query, headers, signedHeaders, payloadHash].join("\n")   ← 换行 join（源码 `.join(\`\n\`)`；2026-10-01 由 07 号流量分析复核实测修正，此前误记为空格 join）
9. hex8 = Hex(sha256(CanonicalRequest))
10. StringToSign = [ "SDK-HMAC-SHA256", G, hex8 ].join("\n")   ← 同为换行 join（源码 `.join(\`\n\`)`）
11. Signature    = Hex(HMAC-SHA256(sk, StringToSign))
12. Authorization = `SDK-HMAC-SHA256 Access=${ak}, SignedHeaders=${j}, Signature=${sig}`
```

头常量：`X-Sdk-Date` / `X-Sdk-Content-Sha256` / `host`。urlEncode 为逐字节 UTF-8 大写 hex（与 AWS 的 %00 大写风格一致）。

### 1.2 V11-HMAC-SHA256（派生 AK/SK 签名，v11 新体系）

类 `mX`（DerivedAKSKSigner，同文件）：

```
1. 日期 W（X-Sdk-Date，同 1.1 格式）
2. Wx（签名域） = W[0:8] + "/" + regionId + "/" + derivedAuthServiceName
3. CanonicalRequest 构造同 1.1（方法/URI/query/头/签名头/负载哈希，**换行 join**）
4. StringToSign = [ "V11-HMAC-SHA256", W, Wx, Hex(sha256(CanonicalRequest)) ].join(" ")   ← 注意：V11 的 StringToSign 是**空格** join（源码 `getStringToSignByInfo` 用 `.join(\` \`)`），与 v9 的换行 join 不同
5. 派生密钥  = HKDF-HMAC-SHA256(ikm=sk, salt=ak, info=Wx)  → Hex 小写（getDerKeySHA256）
6. Signature = Hex(HMAC-SHA256(派生密钥, StringToSign))
7. Authorization = `V11-HMAC-SHA256 Credential=${ak}/${Wx}, SignedHeaders=${b}, Signature=${sig}`
```

HKDF：`extract = HMAC(sk, ak)`，`expand(Y, info=Wx, len=DERIVATION_KEY_LENGTH, ceil 上限)`，失败抛 `Failed to derive AK …`。**v11 特征：sk 不再直接入 HMAC 链，而是与 ak 做 HKDF 派生（info 含 region+派生服务名）——同一 AK/SK 在不同服务名下的签名互不通用。**

### 1.3 AWS4-HMAC-SHA256（google-auth-library 内嵌，供 Google 系 provider）

类 `u70`/`d70.AwsRequestSigner`（root_chunk-qy5ebw5c.js，`google-auth-library` 原样内嵌）：

```
1. amzDate = ISO "YYYYMMDDTHHMMSSZ"，x-amz-date 头
2. CanonicalRequest = method\nuri\nquery\ncanonicalHeaders(k:v\n…)\nsignedHeaders\npayloadHash
3. CredentialScope = date/region/service/aws4_request（service = host 首段）
4. StringToSign = AWS4-HMAC-SHA256\namzDate\nscope\nHex(sha256(CanonicalRequest))
5. 密钥链 k = HMAC(HMAC(HMAC(HMAC("AWS4"+sk, date), region), service), "aws4_request")
6. Authorization = `AWS4-HMAC-SHA256 Credential=${ak}/${scope}, SignedHeaders=…, Signature=…`
   + 临时凭证时加 x-amz-security-token 头
```

配套 IMDS 角色凭证链（`securityCredentialsUrl`/`imdsV2SessionTokenUrl`）在内，但云场景不会走。

### 1.4 调用方映射

| 签名器 | 调用点 | 用途 |
|---|---|---|
| SDK-HMAC-SHA256 (y1) | root_chunk-pt9mn0ta.js 内 SDK 请求层（5 处 AKSKSigner） | 华为云 IAM/各服务 API 主签名 |
| V11 (mX) | 同文件 DerivedAKSKSigner（3 处） | v11 派生体系（按服务名派生密钥） |
| AWS4 (u70) | root_chunk-sb5g03zb.js `signer.sign` | Google AI provider（vertex 路径） |

## 2. 凭证注入面（内核）

- 环境变量（Constants H4，root_chunk-pt9mn0ta.js）：
  `HUAWEICLOUD_SDK_AK` / `HUAWEICLOUD_SDK_SK` / `HUAWEICLOUD_SDK_PROJECT_ID` / `HUAWEICLOUD_SDK_DOMAIN_ID`；
  `DEFAULT_IAM_ENDPOINT=https://iam.myhuaweicloud.com`；`DEFAULT_PROFILE_DIR_NAME=.huaweicloud`
- OAuth 换发的临时凭证（02 号）：`access_key_id`/`secret_access_key`/`security_token`+`expiration`；扩展侧 `getGlobalCredentialClient()` 提供，请求前 `sendRequest` 取凭证；GET/DELETE 参数走 query、其余走 body（签名覆盖差异见上）。
- **TokenAuth 面**（同文件）：`CreateTemporaryAccessKeyByTokenRequest`（body.auth={methods, token}）——**用登录 token 直接换临时 AK/SK**（`iam.myhuaweicloud.com` 的 CreateTemporaryAccessKeyByToken 接口族）。
- 旁路：`W3_TOKEN` / `W3_USER` 环境变量可注入 `x-auth-token` / `x-current-user`（内核 JS 层 18 处 x-auth-token 消费点）。

## 3. 业务 API 端点面（扩展 product.json 域配置，A 级实值）

### 3.1 主域矩阵（commercialVersionDomain.newFramework.productDomain）

| 键 | 实值 |
|---|---|
| snapApigDomain（主） | `https://snap-access.cn-north-4.myhuaweicloud.com` |
| snapApigBackupDomain | `https://snap-access.cn-north-4.myhuaweicloud.cn`（.cn TLD 备份） |
| checkNetworkUrl | 同上 snap-access |
| codeGenDomain / chatDomain | 同上 snap-access |
| knowledgeRepoListDomain / assetListDomain | `snap-access…/ragGateway` |
| policyDomain / policyCenterDomain / agentModeDomain / agentCenterUrl | 同上 snap-access |
| toolkitDomain | `https://iam.myhuaweicloud.com` |
| iamStsOpenDomain | `https://sts.cn-north-4.myhuaweicloud.com`（02 号 OAuth） |
| grayUserDomain | `https://ide.cn-north-4.myhuaweicloud.com` |
| codeartsWebUrl / NewUrl | devcloud.cn-north-4 / codearts.huaweicloud.com |
| 熔断域（circuit breaker） | `snapengine-apig.cn-north-7`（shouldUseCircuitBreaker 仅对该域） |

**IDE 信息头域名白名单**（shouldAddIdeInfoHeader 命中即注入 `ide-name/ide-version/plugin-name/plugin-version`）：
`snapengine-apig.cn-north-7` / `snap-access.cn-north-4` / `snap-access.ap-southeast-1` / `snapengine-apighk.cn-north-229`（港澳灰度）。

### 3.2 权益/配置 API（双域分叉）

`uue()` 按版本分叉（gamma 版→snapengine-apig.cn-north-7 全 URL；否则→opengw.developer 相对路径）：

| API | 路径 |
|---|---|
| 领取权益 | `/api/v1/benefit/claim` |
| 余额查询 | `/api/v1/user/tokens/balance` |
| 网关配置 | `/api/v1/gateway/config` |

### 3.3 AgentCenter（智能体中心）

基址 `policyCenterDomain`（snap-access）：
- `/PromptCenterService/v1/agent-center/agents`（gptsUrl）
- `/v1/agent-center/agents/useragents`（v1/v2 双版本，灰度切换）
- `/v1/agent-center/generation`（tCleverMake）
- llmVendorsListUrl 同域

### 3.4 鉴权头族（按 Agent-Type 分流）

| Agent-Type | 场景 | 头 |
|---|---|---|
| `ChatAgent` / `CodeAgent` | 对话/代码生成 dot 上报（twe/iq/tet） | `x-auth-token`、`x-snap-traceid`、`plugin-name`（`<name>_vscode`）、`plugin-version`、`ide-name`、`ide-version`、`content-type:application/json` |
| `AgentCenter` | 智能体中心请求 | + `X-Language`（zh-cn/…）、`Agent-Type` |
| `RagAgent` | RAG/知识库（ragGateway、codebase 索引） | + `x-current-user`、`rejectUnauthorized:false`（**RAG 客户端关 TLS 校验**，与 app-settings `skipTlsVerification:true` 呼应） |
| `MemoryAgent` | 记忆服务 | `X-Referer: MemoryAgent` |
| `CodeBase` | 代码库服务 | `X-Referer: CodeBase` + `x-snap-traceid` |
| 长连接 | 心跳 | `heartbeat-enable:true`、`model-name`、`created-time`、`is_confidential`（KIA 标记）、`kernel-version`、`maas_type`（灰度） |
| 文件上传 | uploadLocalKnowledgeFile | `X-Language`、`x-snap-traceid` |

**x-auth-token 来源**（Pr()，五分支）：
1. `isDefaultToken` → `developersToken`（产品配置内置 token）
2. `isInnerVersion` → 用户 `cloudDragonToken`（华为内网云）
3. `isHcsVersion` → HCS 登录服务 token
4. Hc/Hcs 版 → 空串（"token is transferred by the toolkit"，走 AK/SK 签名）
5. 公共云版 → 门户用户 token（getUserInfo 取）

版本形态：gamma / inner（华为内）/ hcs（HCS 专有云）/ hc（公共云）/ commercial —— `PLUGIN_ENV="hc"` 时内核强制 Basic Auth 密码（03 §4）。

### 3.5 模型调用端点（内核 provider 层）

- **OpenAI 兼容**：`config.url({path:"/chat/completions", modelId})`（23 处），SSE 流式；错误码 `InferSrv.003002008.413` → `context_length_exceeded`（"Input too long"）；complexity-classifier 侧通道（InferSrv `/chat/completions`，任务复杂度分类）。
- **Anthropic 兼容**：`/v1/messages` + `/v1/messages/batches`（anthropic-beta `message-batches-20…` 头，12 处）。
- **provider 基址族**（google-auth 内嵌 SDK 暴露）：`api.x.ai/v1`（Grok）、`api.mistral.ai/v1`、`https://${host}/v1beta1/projects/${p}/locations/${l}/publishers/google`（Vertex）——内核原生内嵌多家 provider SDK，基址可配置覆盖。
- 模型元数据路由：`/v1/model/builtin`、`/v1/model/thinking-variants`、`/open-models`。

## 4. 网络韧性设计

- **熔断器**：`rpt.getInstance(snapApigDomain).sendRequestWithBreaker({options, isNeedBody, client, originalDomain, backupDomain, timeout, description, isSSE, cacheFallback})`——主域熔断自动切 backupDomain（`.myhuaweicloud.cn`），SSE 与缓存回退单列。
- **TLS**：RAG 客户端显式 `rejectUnauthorized:false` + 自签证书（requestCert）；应用级 `skipTlsVerification:true` 为默认值。
- **超时**：标准 60s（token 请求），SSE 长连接单列。
- **追踪**：`x-snap-traceid` 全链路（It() 生成），请求/响应两侧 dot 上报（含 request_id/response_id）。

## 5. 一句话结论

签名面 = **华为云 IAM 双代签名（SDK-HMAC-SHA256 v9 直签 + V11-HMAC-SHA256 HKDF 派生密钥）+ 内嵌 Google AWS4 链（Vertex 用）**，凭证来源是 02 号 OAuth 换发的临时 AK/SK 三元组（或 token 直换临时 AK/SK 的 CreateTemporaryAccessKeyByToken 旁路）。业务面 = **snap-access.cn-north-4 单主域 + 双域备份（.cn TLD）+ 熔断切换**，鉴权按 Agent-Type 头分流（ChatAgent/CodeAgent/AgentCenter/RagAgent/MemoryAgent/CodeBase），x-auth-token 按版本形态五分支取值（公共云走门户 token，Hc 版走 AK/SK 签名，内网走 cloudDragon，gamma 走 developersToken 内置 token）；模型调用是 OpenAI `/chat/completions` + Anthropic `/v1/messages` 双协议，内嵌 x.ai/Mistral/Vertex 多 provider SDK。

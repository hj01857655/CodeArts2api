# 02 认证链与 OAuth2 协议（A 级：流量分析 + 源码双证）

> 证据：`re/capture/req_oauth2_tokens.txt`（2026-09-30 17:55 UTC 实抓刷新请求）+ `huaweicloud.authentication/dist/plugin.js`（3.3MB）源码定位。
> 解析产物：`re/capture/decoded/*.json`（三层 JWT 全展开）。

## 1. 流量分析请求解剖（POST /v1/oauth2/tokens）

`Host: sts.cn-north-4.myhuaweicloud.com`，`User-Agent: axios/1.13.1`，`Connection: close`，body 为 `application/x-www-form-urlencoded`：

| 字段 | 值 | 语义 |
|---|---|---|
| client_id | `codearts-agent` | 与 uriScheme 同值（`CLIENT_ID = env.uriScheme`，源码实锤） |
| grant_type | `refresh_token` | 刷新换发（登录用 `authorization_code`） |
| code_verifier | 128 位 hex | **PKCE 验证器——刷新时仍携带**（与登录绑定的同一 verifier） |
| refresh_token | RS256 JWT（1216 字符） | 见 §2 |
| DPoP 头 | ES256 dpop+jwt | 见 §3 |

### 1.1 refresh_token JWT（RS256，kid `cn-north-4:5`）

```json
{
  "exp": 1792685518,            // 2026-10-22T16:11:58Z
  "iat": 1790598535,            // 2026-09-28T12:28:55Z（安装当日签发，寿命 24 天）
  "iss": "cn-north-4",
  "type": "refreshToken",
  "client_id": "codearts-agent",
  "user_profile": "<内嵌 JWT，见 1.2>",
  "cnf": { "jkt": "-fpNBBTZ0O2aNjWAk3LNgEHWGWNOTnkwfVh7IEOn0lI" },   // DPoP 密钥绑定（key thumbprint）
  "jti": "d23d9e9c-ebcd-442b-9fa9-04abad0b7823",
  "federation": null
}
```

**关键**：`cnf.jkt` = DPoP 公钥 thumbprint → refresh_token 与本地 DPoP 密钥对绑定，换机/换密钥即失效（与 ZCode 的静态签名门不同，这里走标准 DPoP 绑定）。

### 1.2 user_profile 内嵌 JWT（无签名头，纯 payload）

```json
{
  "account_id": "086b0271090025320f7cc013fac68b40",
  "account_name": "hj01857655",
  "features_switches": { "enable_pdp5": true },
  "principal_id": "086b0271c580f3ee1f88c01314631a3a",
  "principal_is_root_user": true,
  "principal_urn": "iam::086b0271090025320f7cc013fac68b40:user:hj01857655"
}
```

### 1.3 DPoP 头 JWT（ES256 / dpop+jwt）

```
header: { alg: ES256, typ: "dpop+jwt",
          jwk: { kty: EC, crv: P-256, x: "84JbP-K1qztyDIwsSdbJB0IoXWHf_z8dU34TJAvAxyk", y: … } }
payload: { htm: "POST",
           htu: "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens",
           iat: 1790790913,          // 2026-09-30T17:55:13Z = 流量分析时刻（每次请求现签）
           jti: "27c9dd74…" (64hex) }
sig: 64 bytes (r||s)
```


## 2. 换发响应与凭证有效期

### 2.1 响应结构

`POST /v1/oauth2/tokens` 成功时返回两组产物：

```json
{
  "credentials": {
    "access_key_id":    "HSTA81F9LNPMISM6EQ7N",
    "secret_access_key": "…",
    "security_token":   "hQpjbi1ub3J0…",   // STS token，556 字符
    "expiration":       "2026-10-01T11:52:43.51Z"
  },
  "refresh_token": "eyJ0eXAiOiJKV1Qi…"      // RS256 JWT
}
```

| 产物 | 用途 | 有效期 |
|---|---|---|
| `credentials`（AK/SK/STS）| 业务 API 签名 | **约 2 小时**（实测 2:00:10.5）|
| `refresh_token` | 换发新凭证 | 约 **28 天**（JWT `exp`）|

### 2.2 STS token 是短时效凭证

`security_token` 有效期仅 2 小时，且 `refresh_token` **一次性消耗**——用过的 refresh_token 再提交返回：

```json
{"error_code":"STS5.1806","error_msg":"invalid refresh token: 'the refresh token has been used'"}
```

换发成功后客户端用新 `refresh_token` 覆盖旧的（refresh_token 也在响应里返回，形成轮换链）。

STS 过期后调业务接口的报错形态（APIG 网关）：

| 情况 | 响应 |
|---|---|
| 缺 `x-auth-token` | `APIG.0301` `x-auth-token not found` |
| STS 已过期/无效 | `APIG.0301` `decrypt token fail` |
| 伪造 token | `APIG.0301` `The IAM service is currently unavailable: system error` |

### 2.3 两种 grant_type

| grant_type | 用途 | 必填 |
|---|---|---|
| `authorization_code` | 首次登录（浏览器回调换发）| `code` + `redirect_uri` |
| `refresh_token` | 续期 | `refresh_token`（+ 同一 `code_verifier`）|

首次登录请求实测：

```
client_id     = codearts-agent
code          = gWfs2VB4MCwihNvgyS5TyL51A63fFuX4
code_verifier = 5f4a834b0545bd87…
grant_type    = authorization_code
redirect_uri  = http://127.0.0.1:61394/oauth/callback
```

→ 回调走**本地回环端口**（动态分配），端口号每次登录变化。

### 2.4 DPoP 头是强制校验项

缺 DPoP 头直接被拒：

```json
{"error_msg":"Orchestration error: Invalid header parameter: DPoP, required",
 "error_code":"APIGW.0106"}
```

DPoP JWT 实测解码：

```json
// header
{"alg":"ES256","typ":"dpop+jwt",
 "jwk":{"kty":"EC","x":"ySegdvr…","y":"sLiwG6E…","crv":"P-256"}}

// payload
{"htm":"POST",
 "htu":"https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens",
 "iat":1790848351,
 "jti":"055f345d8d6a000c…"}       // 32B 随机 hex
```

签名段 86 字符（ES256 64 字节 DER→JOSE raw）。`htu` 绑定完整 URL，`htm` 绑定方法——**换路径或换方法即失效**。

## 3. 登录全链路（实测 40+ 跳，含 9 次 302）

登录经**华为云账号体系（CAS/IDM）+ 华为云 IAM OAuth2** 两层串联，中间夹二维码扫码与风控上报。

### 3.1 主链路

| # | 端点 | 状态 | 作用 |
|---|---|---|---|
| 1 | `codearts.huaweicloud.com/portal/authorize` | 200 | 发起授权（PKCE）|
| 2 | `auth.huaweicloud.com/authui/login` | 200 | 跳登录页 |
| 3 | `auth.huaweicloud.com/authui/isLogin` | 200 | 查登录态 |
| 4 | `auth.huaweicloud.com/authui/qrcode` | 200 | **取扫码二维码** |
| 5 | `auth.huaweicloud.com/authui/qrcode/status` | 200 | **轮询扫码结果** |
| 6 | `oauth-login1.cloud.huawei.com/oauth2/v3/authorize` | 200 | 转华为账号 OAuth |
| 7 | `id1.cloud.huawei.com/CAS/remoteLogin` | **302** | CAS 单点 |
| 8 | `oauth-login1.cloud.huawei.com/oauth2/v3/loginCallback` | 200 | 账号侧回调 |
| 9 | `id1.cloud.huawei.com/UnifiedIDMPortal/ajaxHandler/login` | 200 | **账号密码登录（×6 次）** |
| 10 | `auth.huaweicloud.com/authui/riskData` | 200 | 风控数据上报 |
| 11 | `auth.huaweicloud.com/authui/casLogin` | **302** | 带账号侧 `code` 回 IAM |
| 12 | `auth.huaweicloud.com/authui/v1/oauth2/authorize` | **302** | IAM 签发授权码 |
| 13 | `127.0.0.1:<port>/oauth/callback` | **307** | **本地回环接收 code** |
| 14 | `sts.cn-north-4…/v1/oauth2/tokens` | 200 | **换发 STS 凭证** |
| 15 | `snap-access…/v1/sessions` | 200 | 上报设备会话 |

### 3.2 PKCE 参数贯穿全程

```
client_id             = codearts-agent
code_challenge_method = SHA-256
code_challenge        = 8h17B20apJm0PmgaAW…      ← 全程不变
redirect_uri          = http://127.0.0.1:61394/oauth/callback
response_type         = code
scope                 = openid
```

`code_challenge` 从第 1 跳到第 13 跳**始终是同一个值**，与 `code_verifier` 配对校验。

### 3.3 两套 client_id

| 阶段 | client_id | 归属 |
|---|---|---|
| 发起 / 签发 | `codearts-agent` | CodeArts 自有 |
| 账号登录中间跳 | `103493351` | 华为账号 OAuth |

中间跳的 `redirect_uri` 指向 `auth.huaweicloud.com/authui/casLogin?service=…`，把账号侧的 `code` 转发回 IAM——**两层 OAuth 通过 `service` 参数串联**。

### 3.4 双重 302 → 307 回环

```
#90  auth.huaweicloud.com/authui/v1/oauth2/authorize
     ?code_challenge=…&state=2f8e0916…
  → 302  127.0.0.1:61394/oauth/callback?code=6z0XftO7…&state=2f8e0916…

#93  auth.huaweicloud.com/authui/v1/oauth2/authorize
     ?code_challenge=…&state=a05474f3…
  → 302  127.0.0.1:61394/oauth/callback?code=gWfs2VB4…&state=a05474f3…

#94  127.0.0.1:61394/oauth/callback?code=gWfs2VB4…
  → 307  codearts.huaweicloud.com/portal/login
```

同一 `code_challenge` 触发了**两次**授权请求（`state` 不同），最终以第二个 `code`（`gWfs2VB4…`）换发成功。第一次的 `code=6z0XftO7…` 未被使用。

### 3.5 回环端口动态分配

`127.0.0.1:61394` 每次登录重新分配。IDE 需在本地监听该端口接收 `code`，端口写死在 `redirect_uri` 里，因此授权请求与本地监听必须**预先对齐**。

### 3.6 换发成功后会话上报

`POST snap-access/v1/sessions` 上报设备信息：

```json
{"device_name":"DESKTOP-J76NQ29","device_type":"Windows",
 "ip_address":"…","client_type":"CodeArts Agent",
 "client_version":"26.9.500","status":0}
```

返回 `session id + user_id + domain_id`，供后续业务接口关联。

### 3.7 风控上报（`/authui/riskData`）

登录过程中在**密码登录之后、`casLogin` 之前**上报设备指纹。

```http
POST https://auth.huaweicloud.com/authui/riskData
Content-Type: application/x-www-form-urlencoded
```

| 字段 | 长度 | 内容 |
|---|---|---|
| `devID` | 84 hex = 42 字节 | 设备指纹标识 |
| `hwMeta` | 2,004 字符 | base64url 加密的硬件/环境元数据 |

实测值：

```
devID  = 2049379c377af42bbe5ec338e0b4973a9
         d3dca9005f50a74f6b1f559fc9e2eca9c
         728a14836c37f3361b
hwMeta = JxEAXQAYBc4AAAAAANefAAUBAAzNzg3MfXx7enloiokHAQAGzc3My8QFBwEA…
```

响应：`{"retDesc":"store success.","retCode":"200"}`

#### devID 的生命周期

**登录前即已生成**，且贯穿整条链路——实测同一值出现在 27 处请求中，从第 1 跳 `portal/authorize` 一直到最后。

#### hwMeta 的结构

base64url 解码后为 protobuf 风格 varint 字段，值均较小（`0x1e`、`0x3c` 等），是**序号 + 短值**的键值对，记录设备型号、浏览器指纹、屏幕参数等。

采集逻辑在华为账号的 CAS 脚本中（`id1.cloud.huawei.com/CAS/static_rss_red/rss_*.js`，登录过程中加载 10 个），**不在 CodeArts 侧**。

> 缺失该上报不影响登录，仅用于服务端风控判定。

## 4. 登录流程（源码闭环，plugin.js）

```
[UI 点登录]
  → attemptLogin()：env=prod, version 校验
  → LoginBackend：起本地 HTTP 回调服务 127.0.0.1:<随机端口≥10000>
  → 浏览器打开门户登录 URL（auth.huaweicloud.com/authui/login.html 系）：
      …&uri_scheme=codearts-agent&client_id=codearts-agent&port=<n>
        &code_challenge=<s256>&code_challenge_method=S256
  → 用户在门户完成认证（华为账号/IAM）
  → 门户回调 → 本地服务收到 authenticationCode → 307 重定向门户
      /login?login_succeed=true&uri_scheme=codearts-agent&locale=…
  → getTokenByAuthorizationCode(code, pkcePair, port)：
      POST {iamStsHost}/v1/oauth2/tokens
      body: client_id + code + code_verifier + grant_type=authorization_code
            + redirect_uri=http://127.0.0.1:<port>/oauth/callback
      headers: DPoP: <现签>
  → 响应 credentials: { access_key_id, secret_access_key, security_token, expiration }
    + refresh_token + user 信息（resolveUserInfo）
  → 持久化（§4）→ 进入定时续期（computeSafelyRenewTokenInterval 基于 expiration）
```

刷新（`refreshToken(verifier, token)`）：同端点，body = client_id + code_verifier + grant_type=refresh_token + refresh_token，DPoP 现签。即 §1 抓到的请求。

**PKCE 细节**：`loginContext = { pkcePair: PKCEGenerator.generate(), dpopKeyPair: generateDpopKeyPair() }` 在登录开始时生成，code_verifier 全程不变（登录+每次刷新都用它）；DPoP jti 每请求随机、iat 每请求现取。

## 5. DPoP 密钥生成与绑定（源码闭环）

```js
generateDpopKeyPair()  // ES256, crv=P-256, extractable
  → { privateKeyJwk, publicKeyJwk }
generateDpopJWE(keyPair, payload)
  → SignJWT(payload).setProtectedHeader({alg:"ES256", typ:"dpop+jwt", jwk:publicKeyJwk}).sign(priv)
generateDpopPayload(htm, htu)
  → { htm, htu, iat: now, jti: 随机64hex }
```

服务端侧：refresh_token 的 `cnf.jkt` 与请求 DPoP 头 jwk 的 thumbprint 必须一致；错误码 **STS5.1804 InvalidDPoPHeader**（"DPoP proof has expired" 等）、**STS5.1806 InvalidRefreshToken**。

## 6. 凭证存储（secretStorage）

- VS Code `SecretStorage`，键 **`HuaweiCloudSession`**，值为 `JSON.stringify(session)`。
- 落盘文件：`%APPDATA%\codearts\secretStorage\8f4ebc74ae0f0ba3a7c5d904c80b9c22e70ad97173ca7dfff9ec180c6ca6be37.enc`（Windows 上由 DPAPI 加密整个 SecretStorage 库）。
- session 对象字段（源码扫出）：

| 字段 | 内容 |
|---|---|
| access_key_id / secret_access_key / security_token | **IAM 临时 AK/SK/STS token**（之后所有 API 用它做 HMAC-SHA256 签名——内核 JS 层有 sigv4 变体签名实现 `Credential=${access_key}…`） |
| expiration | 过期时刻（驱动续期定时器） |
| domain_id / user_id / user_name | 账号信息 |
| refresh_token | RS256 JWT（§1.1） |
| pkcePair | { codeVerifier, codeChallenge, codeChallengeMethod } —— **code_verifier 明文持久化** |
| dpopKeyPair | { privateKeyJwk, publicKeyJwk } —— **DPoP 私钥 JWK 明文持久化**（仅靠 DPAPI 层保护） |

会话对象（`HuaweiCloudAuthentication`，extends 标准 `Authentication`）：`new B({id:user_id, label:user_name}, [], "", ak, sk, sts_token, expiration, domain_id, renewInterval, …, refresh_token)`——对 VS Code 暴露为 **AK/SK 型会话**（非 Bearer token 型）。

## 7. 端点与环境配置（源码闭环）

| 键 | prod 值 |
|---|---|
| iamStsHost | `https://sts.cn-north-4.myhuaweicloud.com` |
| iamLogin | `https://auth.huaweicloud.com/authui/login.html` |
| portalHost（cn） | codearts 门户；intlPortalHost | `https://codearts.ap-southeast-1.huaweicloud.com/portal` |
| IAM_TOKEN_API | `/v1/oauth2/tokens` |
| AUTH_REDIRECT_URL | `/oauth/callback` |
| env | `prod`（另有 beta/test 分支） |

登录态上下文：`login.status` = unsigned / signing / signed；命令面：Login / Login_And_Checking_Status / RenewSession / Logout；菜单 `AccountsContext`。

## 8. 内核侧第二 OAuth 面（agentkernelServer）

`re/bun_extract/`（312 文件）中 **`/v1/oauth2/tokens` 出现 4 次**——内核自带一套 OAuth 客户端，且发现：

- `~/.config/opencode/auth.json`（XDG_DATA_HOME 优先）作为**备用凭证落点**（`appendFileSync` 写入）；
- 内核自带本地回调服务器（`Bun.serve({port:nW})`，`redirectUri: http://localhost:<port>/auth/callback`，带 HTML 完成页）；
- 内核侧 `IAM_ENDPOINT=https://iam.myhuaweicloud.com` + `HUAWEICLOUD_SDK_AK/SK/PROJECT_ID/DOMAIN_ID` **环境变量注入面**（extension.js 定义 `ENV_HUAWEICLOUD_SDK_*` 常量）——即扩展可向内核进程注入静态 AK/SK 旁路 OAuth。

## 9. 一句话结论

CodeArts Agent 认证 = **华为 IAM 标准 OAuth2（授权码+PKCE S256+DPoP ES256 双绑定），换发产物是 IAM 临时 AK/SK 三元组而非 Bearer token**；全部业务 API（含 Agent 对话）用 AK/SK 做 HMAC-SHA256 签名（内核层 sigv4 变体实现）。本地仅 DPAPI 一层保护（secretStorage.enc），私钥与 verifier 明文存于其中。刷新请求与 ZCode 的 V4 签名门无同源关系——是纯标准协议栈。

## 10. 后续钩子

- [ ] 解出 secretStorage.enc（DPAPI + VS Code keychain 派生），核对 session 全字段实值
- [ ] 抓一次完整登录（authorization_code 腿）对照 §2
- [ ] 内核 AK/SK 签名实现逐函数还原（对照华为 IAM 签名规范：CanonicalRequest/HMAC 链）
- [ ] 业务 API 鉴权头实测（IDE 信息头 `shouldAddIdeInfoHeader` 的域名清单已见 4 个 snap 域）

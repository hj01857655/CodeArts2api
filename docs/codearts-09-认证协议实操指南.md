# CodeArts 认证协议实操指南

> 面向用途：复现一次登录、调用业务接口、判断凭证失效。
> 协议细节见 02 号（协议结构）、04 号（签名算法）、07 号（对话接口）。
>
> 全部参数与错误码均为实测值，非推断。

---

## 一、协议速览

```
①浏览器授权    codearts.huaweicloud.com/portal/authorize   (PKCE)
      ↓  经 CAS/IDM 账号体系 + IAM 两层串联
②本地回环      127.0.0.1:<动态端口>/oauth/callback        → 拿 code
③凭证换发      sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens
      ↓  返回
   AK / SK / STS token（2 小时） + refresh_token（28 天）
④业务调用      opengw / snap-access，带 SDK-HMAC-SHA256 签名
```

| 凭证 | 有效期 | 作用 |
|---|---|---|
| STS token | **2 小时** | 所有业务 API 的签名与鉴权 |
| refresh_token | 28 天 | 换发新 STS，**一次性消耗** |
| AK/SK | 随 STS | 签名密钥对 |

---

## 二、复现一次登录

### 2.1 前置：本地回环监听

IDE 必须在本地监听一个**动态端口**，并把该端口写进授权请求的 `redirect_uri`。授权与监听必须预先对齐，否则收不到 code。

```
监听地址：http://127.0.0.1:<port>/oauth/callback
```

### 2.2 发起授权

```http
GET https://codearts.huaweicloud.com/portal/authorize
    ?client_id=codearts-agent
    &response_type=code
    &scope=openid
    &code_challenge_method=SHA-256
    &code_challenge=<base64url(sha256(code_verifier))>
    &redirect_uri=http%3A%2F%2F127.0.0.1%3A<port>%2Foauth%2Fcallback
    &state=<随机>
```

`code_challenge` **全程不可变**，直到回环取到 code。

### 2.3 接收 code

授权链末端会 302/307 到本地回环：

```
GET /oauth/callback?code=gWfs2VB4MCwihNvgyS5TyL51A63fFuX4&state=a05474f3…
```

`state` 必须与发起时一致，否则视为无效。

> **注意**：实测同一 `code_challenge` 会触发**两次**授权请求（`state` 不同），返回两个不同的 `code`，**只有第二个被用于换发**。取到第一个 code 后应继续等待，直到链路稳定。

### 2.4 换发凭证

```http
POST https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens
Content-Type: application/x-www-form-urlencoded
DPoP: <ES256 JWT>

client_id=codearts-agent
&code=gWfs2VB4MCwihNvgyS5TyL51A63fFuX4
&code_verifier=<发起时的 code_verifier>
&grant_type=authorization_code
&redirect_uri=http://127.0.0.1:<port>/oauth/callback
```

**DPoP 头是强制的**，缺失直接被拒（见 §4.1）。

#### DPoP JWT 构造

```
header : {"alg":"ES256","typ":"dpop+jwt","jwk":{"kty":"EC","crv":"P-256","x":…,"y":…}}
payload: {"htm":"POST",
          "htu":"https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens",
          "iat":<unix秒>,
          "jti":<32字节随机 hex>}
签名   : ECDSA-P256-SHA256，JOSE raw 格式（r||s，各 32 字节，base64url 无填充）
```

`htu` 绑定**完整 URL**，`htm` 绑定方法 —— 换路径或换方法即失效。

### 2.5 响应

```json
{ "credentials": { "access_key_id": "…", "secret_access_key": "…",
                   "security_token": "…", "expiration": "…" },
  "refresh_token": "…" }
```

### 2.6 续期

```http
POST /v1/oauth2/tokens
DPoP: <同上>

client_id=codearts-agent
&code_verifier=<原 verifier>
&grant_type=refresh_token
&refresh_token=<上次的 refresh_token>
```

⚠️ **refresh_token 一次性**：用过即失效，错误码 `STS5.1806`。每次续期都要用**响应里新返回的 refresh_token**，形成轮换链。

---

## 三、调用业务接口

### 3.1 签名

见 04 号 / 07 号。要点：

```
payloadHash  = Hex(sha256(body))
CanonicalReq = [method, uri+"/", query, canonicalHeaders,
                SignedHeaders, payloadHash].join("\n")
StringToSign = ["SDK-HMAC-SHA256", x-sdk-date,
                Hex(sha256(CanonicalReq))].join("\n")
Signature    = Hex(HMAC-SHA256(key=SK, msg=StringToSign))   ← 单次，不嵌套
```

三个易错点：
1. URI **必须补尾斜杠**
2. HMAC **只做一层**，不做 signingKey 派生
3. CanonicalRequest 第 5 段是 **SignedHeaders 列表本身**，不是 host

### 3.2 两层凭证的适用范围

| 端点族 | 域 | AK | 签名头数 |
|---|---|---|---|
| 内核 `/api/v1/*` | `opengw.developer.huaweicloud.com` | `HSTAY4…` | 20 |
| 扩展 `/v1/*` | `snap-access.cn-north-4.myhuaweicloud.com` | `HSTAZB…` | 13 |

**用错 AK 会报 `decrypt token fail`**，不是签名错。

扩展层额外需要 5 个 IDE 特征头：`agent-type`、`ide-name`、`ide-version`、`plugin-name`、`plugin-version`。

---

## 四、故障判定

### 4.1 STS 换发阶段

| 响应 | 含义 | 处理 |
|---|---|---|
| `APIGW.0106` `Invalid header parameter: DPoP, required` | DPoP 头缺失 | 补 DPoP |
| `STS5.1806` `invalid refresh token: 'the refresh token has been used'` | refresh_token 已用过 | 改用上次的响应里那个；若无则重新走授权码 |
| `APIG.0101` `The API does not exist` | 路径错 | 核对 `/v1/oauth2/tokens` |

### 4.2 业务调用阶段

| 响应 | 含义 |
|---|---|
| `APIG.0301` `x-auth-token not found` | 认证头没发出去，或空值被网关丢弃 |
| `APIG.0301` `decrypt token fail` | **STS 已过期或无效** —— 需续期 |
| `APIG.0301` `The IAM service is currently unavailable` | token 伪造/格式错 |

> **`decrypt token fail` 与签名错误无关**：签名错会返回不同的错误。这个码只表示「token 解不开」，通常是 2 小时到期。

### 4.3 快速自检

```
STS 是否过期？  → 看 credentials.expiration，与当前时间比
AK 是否对层？    → 内核层 / 扩展层用不同 AK
DPoP 是否完整？  → header.htu 必须与实际 URL 逐字符一致
code_challenge  → 全程必须是同一个值
```

---

## 五、凭证存储

| 位置 | 内容 |
|---|---|
| 扩展 `secretStorage` | refresh_token、DPoP 私钥、PKCE 对（系统密钥链加密）|
| 内核 `~/.codeartsdoer` | 解密后的 STS 会话 |
| `re/capture/secret_storage/` | 离线归档的解密产物（**勿入库**）|

DPoP 私钥与 refresh_token 通过 `cnf.jkt`（公钥 thumbprint）绑定 —— **换机或换密钥对即失效**。

---

## 六、安全注意

1. STS token 虽只有 2 小时有效期，但泄露后可在有效期内直接调用业务接口
2. refresh_token 28 天有效期，泄露等于长期凭证泄露
3. `AK` 是长期标识，虽不能单独签名，但会暴露账号归属
4. 解密产物（`HuaweiCloudSession.json` 等）**不要提交到版本库**

---

## 七、相关文档

| 编号 | 内容 |
|---|---|
| 02 | 认证链协议结构、登录全链路、DPoP 绑定 |
| 04 | 三种签名器完整还原 |
| 07 | 对话接口（签名应用示例）|
| 08 | 权益与额度接口（内核层 + 扩展层）|
# 08 benefit 权益与计费面：每日额度自动领取

> 归属：推理资源权益体系。模型调用面见 07 号 §3.2（`maas_type: benefit` 三头强约束）与 §7（benefit 计费面）。
> 本文仅描述机制本身：领取时机、接口契约、失败影响、排队豁免。

## 1. 权益获取是自动的，无需手动签到

客户端启动时自动领取每日额度，调用链固定为三步：

```
getGatewayConfig()   →  拉取可用模型清单
        ↓
claimBenefit()       →  领取当日额度
        ↓
合并为 benefit-model provider（is_free_benefit: true + limit:{context, output}）
```

**领取失败则整组模型不加载**：

```js
if (!await claimBenefit()) {
  log.warn("claimBenefit failed, skip loading benefit models");
  return;                       // ← 直接返回，不进入模型合并
}
```

即领取失败的表现不是降级，而是免费模型整体不可用，调用时报 `4004`。

## 2. balance 接口与额度判定

`POST /api/v1/user/tokens/balance` 返回当前额度状态。

### 2.1 响应字段

```json
{
  "error_code": "0000",
  "error_msg": "success",
  "result": {
    "channel": "codearts",
    "daily_token_limit": 10000000,
    "daily_tokens_used": 58686,
    "total_quota": 10000000,
    "used_amount": 58686,
    "total_balance": 9941314,
    "monthly_token_limit": 0,
    "monthly_tokens_used": 58686,
    "expire_time": 0,
    "create_time": 1789653100754,
    "update_time": 1789653100754,
    "domain_id": "…",
    "user_id": "…"
  }
}
```

| 字段 | 含义 |
|---|---|
| `daily_token_limit` | 每日额度（实测 **10,000,000**）|
| `daily_tokens_used` | 当日已消耗 |
| `total_quota` | 总配额，等于 `daily_token_limit` |
| `used_amount` | 已消耗总量，等于 `daily_tokens_used` |
| `total_balance` | **剩余额度 = `daily_token_limit` − `daily_tokens_used`** |
| `monthly_token_limit` | 月度限额，**`0` 表示不限** |
| `monthly_tokens_used` | 本月已消耗 |
| `expire_time` | 过期时间，**`0` 表示不过期** |
| `create_time` / `update_time` | 权益创建 / 更新时间（毫秒时间戳）|

**恒等式**：`total_balance == daily_token_limit − daily_tokens_used`（实测校验成立）。

### 2.2 额度耗尽的判据

| 判据 | 说明 |
|---|---|
| **`total_balance` 归零** | 当日额度用尽 |
| **`daily_tokens_used >= daily_token_limit`** | 等价条件 |
| **推理返回错误码 `4291`** | 「配额不足或月度token限额已超，请提升配额或等待重置」|

`4291` 是唯一明确表示额度耗尽的错误码：

```
4291  配额不足或月度token限额已超，请提升配额或等待重置
      Insufficient quota or monthly token limit exceeded
```

### 2.3 计费口径：**cache read 不计入消耗**

服务端 `used_amount` 只统计 input + output，**不包含 cache read**。

实测对照：

| 口径 | 数值 |
|---|---|
| 服务端 `used_amount` | 58,686 |
| 本地 `opencode.db` 累计 input + output | 5,374,537 |
| 本地 cache read | 30,464,768 |

→ 本地统计比服务端高两个数量级。**判断额度必须以 `balance` 为准，不能用本地 token 累加。**

### 2.4 刷新时机

balance 在会话期间被重复调用（同一时间窗内观察到 4 次以上），属**周期性轮询刷新**，而非仅启动时查询一次。因此剩余额度在界面上的显示会随使用动态变化。

## 3. 接口契约

| 接口 | 路径 | 方法 | 用途 |
|---|---|---|---|
| claim | `/api/v1/benefit/claim` | POST | **领取当日额度** |
| balance | `/api/v1/user/tokens/balance` | POST | 查询余额 |
| config | `/api/v1/gateway/config` | POST | 拉取可用模型清单 |

**域名**（二选一）：

| 环境 | 域|
|---|---|
| 公共云（默认） | `opengw.developer.huaweicloud.com` |
| gamma 版 | `snapengine-apig.cn-north-7.myhuaweicloud.com` |

域选择条件：`hcMode && process.env.IS_PROD !== "true"` 时走 gamma 域，否则走公共云域。

**请求特征**：

- `POST` body 固定为 `{}`（空 JSON 对象）
- 超时 **10 秒**（`setTimeout(() => ctl.abort(), 1e4)`）
- 签名走 SDK-HMAC-SHA256 v9（与 07 号 §2 同源），额外注入 `X-Security-token`
- 判成功看 `error_code === "0000"`，而非 HTTP 状态码

## 4. config 返回的可用模型

`POST /api/v1/gateway/config` 返回该账号可用的 benefit 模型清单。

### 4.1 响应结构

```json
{
  "error_code": "0000",
  "error_msg": "success",
  "result": {
    "base_url": "https://opengw.developer.huaweicloud.com/v2",
    "models": [
      { "context_window": 1048576, "max_tokens": 393216,
        "model_id": "deepseek-v4-flash-0731",
        "model_name": "deepseek-v4-flash-0731", "sort": 10 },
      …
    ]
  }
}
```

`result` 只有两个字段：**`base_url`**（该组的接入基址，带 `/v2` 前缀）与 **`models[]`**。

### 4.2 models[] 字段全集（5 个）

| 字段 | 含义 |
|---|---|
| `model_id` | 模型标识 |
| `model_name` | 模型显示名 |
| `context_window` | 上下文窗口 |
| `max_tokens` | 最大输出 |
| `sort` | 展示 / 默认排序（10 步进）|

> **无 `model_alias`、无 `endpoint`** —— 接入地址由 `result.base_url` 统一给出，不逐模型下发。

### 4.3 实测清单（4 个模型）

| model_id | context_window | max_tokens | sort |
|---|---|---|---|
| `deepseek-v4-flash-0731` | 1,048,576 | 393,216 | 10 |
| `glm-5.3-flash` | 1,048,576 | 131,072 | 20 |
| `deepseek-v4-pro-0813` | 1,000,000 | 393,216 | 30 |
| `deepseek-v4.1-flash` | 1,000,000 | 384,000 | 40 |

`context_window` 量级为 100 万级，明显大于对话请求的 `max_tokens`（32,000）—— 后者由客户端按会话类型设定，**非 config 下发**。

客户端据此为每模型标注 `limit:{context, output}`。

**额度数值不由客户端硬编码**，由服务端下发，实际余额查 `/api/v1/user/tokens/balance`。

## 5. 双轨额度：本地 benefit 与云端套餐并存

系统存在两套彼此独立的额度体系：

| 体系 | 载体 | 计量 | 用途 |
|---|---|---|---|
| **本地 benefit** | `/api/v1/user/tokens/balance` | token | 本地客户端调用免费模型 |
| **云端套餐** | 用量统计接口 `metrics[]` | 积分（credit）| 云端 agent / 代码补全等服务 |

云端套餐实测形态：

| metric | amount | used | remain |
|---|---|---|---|
| `usageTotalPackageCredit` | 15,588.00 | 7,743.56 | **7,844.44** |
| `usageBasicPackageCredit` | 500.00 | 500.00 | **0（已用尽）** |
| `usageTokenChatMessages` | — | `usage_token_num = 85,346,711` | — |

→ 基础包已耗尽，剩余额度来自总额度包。**本地 benefit 用尽不影响云端套餐，反之亦然。**

## 6. 双层福利体系：内核 benefit 与扩展层 ops

福利/权益相关接口分布在**两个独立层**，各自使用不同凭证与签名头集。

### 6.1 分层对照

| | 内核层 | 扩展层 |
|---|---|---|
| 代码位置 | Agent 内核 bundle | `extensions/vscode-codebot/out/extension.js` |
| 路径前缀 | `/api/v1/*` | `/v1/*`（部分带 `/PromptCenterService`）|
| 域 | `opengw.developer.huaweicloud.com` | `snap-access.cn-north-4.myhuaweicloud.com` |
| AK | `HSTAY4HFGFXIOXPMWZVI` | `HSTAZBMXUKIUUZ70TEDG` |
| User-Agent | `ai-sdk/provider-utils/4.0.21 runtime/bun/1.3.14` | `huaweicloud-usdk-nodejs/3.0 DevKit-VSCode:huaweicloud.codearts-snap\|CodeArts Agent:D1` |
| SignedHeaders | 20 个 | 13 个 |

### 6.2 扩展层福利接口族（`/v1/ops/*`）

| 端点 | 用途 |
|---|---|
| `/v1/ops/delivery` | **福利发放**（活动列表，GET）|
| `/v1/ops/claim` | 福利领取（POST）|
| `/v1/ops/confirm` | 福利确认（POST）|
| `/v1/ops/invite/generate` | **邀请码与邀请链接生成**（POST-only；GET 返 `method GET not found` 404，不是前端路由）|
| `/v1/ops/events/conversation` | 会话事件上报 |
| `/v1/ops/tasks/conversation` | 任务事件上报 |
| `/v1/ops/credit/usage` | 会话级用量查询（`type=session\|msg` + `id`）|

**这一族里没有「查询认证状态」的端点。** `/v1/ops/pages/{id}` 看着像详情页，实测 GET 404（`APIG.0101`），是纯前端路由。邀请活动可拿链接（`invite/generate`）但**不能代领奖励**——必须真人通过链接注册，服务端对直接领取会返回「该活动类型需要先通过触发接口激活」。

> ⚠️ 别把这个 404 和 `invite/generate` 的 404 搞混：前者是**真的纯前端路由**（GET 就该 404），后者是 **POST-only 接口被 GET 打**（`method GET not found`）。判据是看 `message` 里有没有方法名。

### 6.3 活动福利（`/v1/ops/delivery`）：对话完成即自动发放

`GET /v1/ops/delivery?channel=IDE` 返回该 channel 下当前生效的福利活动。

#### 响应结构

```json
{ "code": 0, "message": "ok",
  "data": { "channel": "IDE", "serverTime": 1790848365963,
            "items": [ … ], "endedCampaigns": null } }
```

`items[]` 每项：

| 字段 | 含义 |
|---|---|
| `campaignId` | 活动 ID |
| `title` | 活动名|
| `type` | 实测出现：`USER_LOGIN` / `NEW_USER_REGISTER` / `STUDENT_CERTIFIED` / `INVITE_USER` / `CUSTOM` |
| `benefitAmount` | 奖励数量 |
| `benefitUnit` | **`CREDIT`**（积分，与 token 是不同单位）|
| `pageUrl` | 活动详情页 `/v1/ops/pages/{id}`（实测 GET 404，纯前端路由）|
| `claimable` | **是否可领取——自动化领取的唯一门槛** |
| `status` | `null`（未触发）/ `CLAIMED`（领了没确认）/ `CONFIRMED`（已确认）/ `CONSUMED`（已用完）。**没有 `ELIGIBLE`** |
| `pendingCount` | 待领取笔数 |
| `pendingTotalAmount` | 待领取总额 |
| `access[]` | 跳转链接（含 ES256 ticket，有效期约 1 天）|
| `displayConfig` | 展示文案 |

`extra` 描述触发与周期：

| 字段 | 含义 |
|---|---|
| `triggerEvent` | 触发事件，如 `conversation.completed`、`invite.success` |
| `triggerMode` | `AUTO_GRANT`（自动发放）/ `MANUAL_CLAIM`（手动领取）|
| `startTime` / `endTime` | 活动起止（UTC）|
| `consumePriority` | 消耗优先级 |

`displayConfig.claimButtonText` 按状态给不同按钮文案，键位是 `ELIGIBLE` / `CLAIMED`「已领取」/ `CONFIRMED`「已确认」/ `CONSUMED`「已耗尽」/ `ENTRY`「去完成任务」。注意这些键名是**按钮文案的查表键**，不等于 `status` 字段的取值——`status` 实测只有 `null`/`CLAIMED`/`CONFIRMED`/`CONSUMED`。

#### 实测活动清单

| ID | 标题 | 类型 | 奖励 | 触发事件 | 触发方式 | 状态 |
|---|---|---|---|---|---|---|
| 17 | 下载登录IDE并对话1次 | `CUSTOM` | 88 积分 + 抽奖 | `conversation.completed` | `AUTO_GRANT` | `CLAIMED` |
| 3 | 推荐码上有礼 | `INVITE_USER` | 1,000 积分/人（上限 50,000）| `invite.success` | `MANUAL_CLAIM` | — |
| 18 | 国庆邀请有礼 | `INVITE_USER` | 188 积分 | `invite.success` | `AUTO_GRANT` | — |

#### 结论：对话即产生积分

活动 17 的触发条件是 `conversation.completed` + `AUTO_GRANT`——**完成一次对话自动发放 88 积分，无需手动领取**。这与内核层「每日领取 token 额度」是两套独立机制：前者按**行为**（对话次数）奖励，后者按**周期**（每日）发放。

> ⚠️ 上面这张清单是**某一时刻的快照**，活动会上下线（17/18 现已不在列表里）。
> 以 `GET /v1/ops/delivery` 实时返回为准。当前实测清单见 07 号 §8.6。

#### 领取门槛：只有 `claimable`

自动化领取只看一个字段：**上游说 `claimable=true` 才领**。不按 `type` 白名单筛，也不按 `campaignId` 硬编码（后者是自增字段，随活动上下线漂）。

两类容易被误判的活动：

| 活动 | `triggerEvent` | 为什么脚本造不出 | `claimable` 何时翻 true |
|---|---|---|---|
| `STUDENT_CERTIFIED` | `student.certified` | 要上传证件等人工审核 | 审核通过后服务端认可资格 |
| `INVITE_USER` | `invite.success` | 要真人通过邀请链接注册 | 有真实好友注册后 |

`triggerEvent` 必须由**真实事件**才会触发，脚本代劳服务端也会拒绝直接领取。而一旦 `claimable` 翻 true，说明资格已被认可，此时 claim→confirm 只是收款。**`type` 白名单防不住「脚本代劳」，只会挡住「事后收款"。**

**上游没有「查询认证状态」的接口。** `/v1/ops/*` 全是活动/用量/事件类，`/v1/ops/pages/{id}` 实测 404（纯前端路由）。认证进度只能去账号中心页面看；`claimable` 由 false 翻 true 就是唯一信号。

**`AUTO_GRANT` 一律不 claim。** 服务端自己发的活动，打一次 claim 只是白费请求；也防上游哪天给这类活动标 `claimable=true`。

**必须回读。** claim 返回 200 只代表上游受理，不代表积分到账。完整闭环是 delivery → claim → confirm → **回读**，只认 `CONFIRMED`/`CONSUMED`/`CLAIMED` 算成功。

状态语义与四步闭环的完整说明、同赛道四家实现的对照，见 **07 号 §8.6**。

#### 邀请链接：只拿链接，不代领

`POST /v1/ops/invite/generate`，请求体只要 `{"campaignId": 3}`，返回邀请码与邀请链接：

| 字段 | 说明 |
|---|---|
| `inviteCode` | 邀请码（约 180 字符，URL 里原样带着走） |
| `inviteUrl` | `https://devcloud.cn-north-4.huaweicloud.com/chat?…&invitecode=INV-vnu-…` |
| `activationCount` / `maxActivations` | 已邀请人数 / 上限（实测 `0 / 50`） |
| `valid` | 邀请码是否有效，`false` 时前端标「已失效」 |
| `inviterUserId` / `inviterCustomerId` | 账号标识，只读回显 |

实测三条：

1. **POST-only**，GET 回 `method GET not found` 404（与 `/v1/ops/pages/{id}` 的纯前端
   路由 404 是两回事，判据看 `message` 里有没有方法名）。
2. **幂等「生成或取回」**：连调三次返回同一条码。网关侧因此可缓存 1 小时，
   刷新面板不会反复打上游。
3. **campaignId 从 `delivery` 里现找**（`type=INVITE_USER` 取最小），常量只兜底——
   邀请活动 **2026-11-15 截止**，比学生认证（2026-12-30）早。

链接发给好友、对方注册成功后，服务端记 `invite.success` 并把活动 `claimable` 翻 true，
之后走四步闭环收款。**中间两步脚本替不了**，所以邀请功能只到「拿到链接」为止。

### 6.4 会话级用量（`/v1/ops/credit/usage`）

```http
GET /v1/ops/credit/usage?type=session&id=ses_xxx
GET /v1/ops/credit/usage?type=msg&id=msg_xxx
```

`type` 支持 `session` 与 `msg` 两种粒度。未知 id 返回：

```json
{"code":40401,"message":"msg not found: msg_xxx","data":null}
```

侧边栏展示 `creditCost` / `promptTokenUsed` / `completionTokenUsed` / `cachedTokenUsed` / `cacheHitRatio`，仅在积分套餐（`is_credit_package`）下启用。

### 6.5 路径前缀随环境重写

同一批 URL 在配置层带 `/PromptCenterService` 前缀：

```js
this._welfareDeliveryUrl = `${policyCenterDomain}/PromptCenterService/v1/ops/delivery`;
```

配置加载时按 `sourceDomainUrl → targetDomainUrl` 做字符串替换：

```js
this._welfareDeliveryUrl = this._welfareDeliveryUrl.replace(s.sourceDomainUrl, s.targetDomainUrl);
```

→ **线上实际请求可能不带该前缀**（`/v1/ops/delivery`），取决于域映射结果。分析时以实际请求为准。

### 6.6 扩展层签名头（13 个）

```
agent-type;content-type;host;ide-name;ide-version;plugin-name;
plugin-version;x-auth-token;x-language;x-sdk-date;x-security-token;x-snap-traceid
```

相对内核层新增 `ide-name`、`ide-version`、`plugin-name`、`plugin-version`、`agent-type`（值 `PromptCenter`）——这五个是 IDE 集成特征头，对话面无。另有 `ide-name: CodeArts Agent`、`ide-version: 1.109.5`、`plugin-name: snap_vscode`、`plugin-version: 26.9.500`。

### 6.7 与内核层权益的关系

| | 内核 `/api/v1/benefit/*` | 扩展 `/v1/ops/*` |
|---|---|---|
| 触发 | 客户端启动时按周期领取 | **按行为触发**（对话完成 / 邀请成功）|
| 计量 | token（`daily_token_limit: 10,000,000`）| 积分（`benefitUnit: "CREDIT"`）|
| 覆盖 | 免费模型推理额度 | 活动福利发放与积分统计 |

→ 内核层按**周期**发放推理额度，扩展层按**行为**奖励积分，二者单位不同、触发条件不同、互不依赖。

## 7. 错误码

### claim

| 码 | 含义 |
|---|---|
| `4000` | 参数错误 / JSON 解析失败 |
| `4001` | `X-Auth-Token` 缺失或无效 |
| `4002` | `X-Auth-Token` 过期 |
| `4003` | 用户禁用 |
| `4004` | 权益不存在 / 已过期 / user 不存在 |
| `4006` | **daily claim limit exhausted（每日名额耗尽）** |
| `5000` | internal error |

`4006` 表明**每日限领一次**，重复领取被服务端拒绝。

### balance

| 码 | 含义 |
|---|---|
| `4000` | 参数错误 / 无效 channel |
| `4001` / `4002` | Token 缺失无效 / 过期 |
| `4004` | benefit not found / user not found |
| `5000` | internal error |

### config

| 码 | 含义 |
|---|---|
| `4000` | 参数错误 / invalid channel |
| `4004` | group not found |
| `5000` | internal error |

### 推理侧（benefit 相关）

| 码 | 含义 |
|---|---|
| `4004` | 「您的权益不存在或已过期，无法使用该模型，请重新获取权益后再试」 |
| `4005` | 「当前模型不受支持，请更换模型」 |
| `4007` | 当前分组无权限使用该模型 |
| `4291` | **配额不足或月度token限额已超，请提升配额或等待重置**（额度耗尽）|

### 7.1 线上现状：opengw 权益渠道按账号下线（2026-10 起）

`error_code 4000 / invalid channel, group not found` 在 2026-10 之后对本账号是**稳定状态**，
不是偶发、也不是签名问题。判定依据：

- `claim` / `balance` / `config` **三个接口同时**返回 4000——签名若错会是 `APIG.0301`；
- 同一套 AK/SK 打 snap-access 的 `/v1/ops/*` 全部正常（能查到活动、能 claim）；
- **总开关明确返回 `enabled=false`**：`GET {snap}/v1/benefit-gateway-config` →
  `{"enabled": false, "is_examination_domain": false, "examination_model_enable": false}`。

#### 判据要用总开关，不要解析错误文案

`/v1/benefit-gateway-config` 是官方设计里的**福利网关总开关**：

```
GET https://snap-access.cn-north-4.myhuaweicloud.com/v1/benefit-gateway-config
→ {"enabled": bool, ...}      ← 裸对象，没有 {code,data} 信封
```

⚠️ 它在 **snap-access** 域。同样的路径在 opengw 域是 `404 APIG.0101`（接口未发布）。

主流实现（`aimod-cc/Agent2API`、`bluechonk/modelmux`、`HITZY2002/codearts2api`）都是
**先问开关，`enabled=false` 就直接返回**，根本不去打 opengw 的 claim / config：

```rust
// aimod-cc/Agent2API: providers/codearts/models.rs
if !parse_benefit_gate(&gate)? {
    return Ok(None);        // 「这个部署没有福利网关」，不是错误
}
```

网关照此实现：`_benefit_gateway_enabled()` 返回 false 时整体跳过，日志只报一次 INFO。
开关查询本身失败（网络/签名）**不**视为关闭——那只是问不到，按原流程继续并如实报错，
否则一次网络抖动就会把福利模型永久关掉。

> 注意区分两种 4000：`claim` 的 4000 按本文件 §7 的定义是「参数错误 / JSON 解析失败」，
> 而实际返回的 `error_msg` 是 `invalid channel, group not found`——它描述的是**渠道**问题，
> 不是我们发错了参数。判断依据看 `error_msg`，不要只看码；但更好的做法是**看总开关**。

## 8. benefit 模型豁免排队

非benefit 模型走并发排队（`startQueuePolling({task_id, model})` + 请求头 `is-need-queue: true`）。以下两类直接跳过：

```js
if (providerID !== 第三方provider) {
  log.info(`skip queue for third-party provider: ${providerID}`);
  return;
}
if (isBenefitModel(modelID)) {
  log.info(`skip queue for benefit model: ${modelID}`);
  return;
}
```

→ **免费模型不排队**，无 `is-need-queue` 竞争，响应延迟不受队列影响。

## 9. 使用侧约束

benefit 模型发起对话时须同时带三个头（07 号 §3.2）：

```
maas_type: benefit  +  model-id  +  model-name
```

三者缺一即报 `InferHub.002002009.404 model is not registered`。这三个头在 `signRequest` 之前由客户端设置，因此全部计入 SignedHeaders。

## 10. 机制要点

| 要点 | 结论 |
|---|---|
| 领取时机 | 客户端启动时自动执行 |
| 领取频率 | 每日一次，超出报 `4006` |
| 失败后果 | 免费模型整体不可用（报 `4004`），非降级 |
| **每日额度** | **10,000,000 token** |
| **剩余判定** | **`total_balance` 归零；耗尽时报 `4291`** |
| **计费口径** | **仅 input + output，cache read 不计入** |
| **可用模型** | **4 个**（见 §4.3），`context_window` 100 万级 |
| **额度体系** | **双轨**：本地 benefit（token）+ 云端套餐（积分） |
| **福利接口** | **双层**：内核 `/api/v1/benefit/*` + 扩展 `/v1/ops/*`（AK 与签名头均不同，见 §6）|
| **活动福利** | **`/v1/ops/delivery` 返回活动列表，`conversation.completed` + `AUTO_GRANT` 即自动发放积分**（§6.3）|
| **活动状态机** | `ELIGIBLE` → `CLAIMED` → `CONFIRMED` → `CONSUMED` |
| 额度来源 | 服务端下发，客户端不硬编码 |
| 额度刷新 | 周期性轮询，非仅启动时一次 |
| 排队 | 免费模型豁免并发队列 |
| 有效期 | `expire_time = 0` 表示不过期 |
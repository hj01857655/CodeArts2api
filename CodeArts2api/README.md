# CodeArts2api

> 华为云 CodeArts 模型接口 → OpenAI / Anthropic 兼容 API 网关

把华为云 CodeArts 的模型接口（盘古开源大模型、GLM、DeepSeek 等）包装为三协议兼容接口。底层走 `SDK-HMAC-SHA256` 请求签名，凭证通过 OAuth2 PKCE 登录获取（与 CodeArts IDE 同源），无需手动配置 AK/SK。

## 功能特性

- **三协议兼容**：OpenAI Chat Completions · OpenAI Responses · Anthropic Messages
- **OAuth2 PKCE 自动登录**：浏览器授权 → STS 临时凭证，到期自动续期
- **免费额度自动领取**：每日 1000 万福利 Token + 积分活动（每日签到/新人注册礼），后台 daemon 线程自动签到
- **流式 / 非流式双模式**，支持 `tools` 工具调用
- **流中错误帧显式暴露**：上游在 2xx SSE 流里夹带错误帧时，转 OpenAI error 帧并终止流

## 快速开始

```bash
pip install -r requirements.txt
python lanes/oauth.py    # 浏览器登录获取凭证
python server.py         # 启动代理
```

```
Base URL : http://127.0.0.1:8787/v1
API Key  : 任意非空字符串（如 codearts）
Model    : openpangu-2.0-pro、glm-5.3-flash、deepseek-v4-flash-0731 …
```

兼容工具：OpenCode、Cline、Continue、LobeChat、Cherry Studio、AtomCode 等所有支持自定义 OpenAI 兼容端点的工具。

## API 接口

| 端点 | 方法 | 说明 |
|---|---|---|
| `/v1/chat/completions` | POST | OpenAI Chat Completions（流式/非流式，`tools` 透传） |
| `/v1/responses` | POST | OpenAI Responses API（请求翻 Chat 调上游，响应由桥重放原生事件流） |
| `/v1/messages` | POST | Anthropic Messages API（翻译层 ⇄ Chat） |
| `/v1/models` | GET | 模型列表（含免费模型，标注 `supports_function_calling`） |

### 协议翻译架构

```
客户端                 proxy 翻译层              上游（仅 Chat）
─────────            ─────────────            ─────────────
Chat Completions ──→  直通                 ──→  InferHub /chat/completions
Responses        ──→  adapters/responses    ──→  InferHub /chat/completions
                      （请求翻 Chat；响应重放 Responses 事件流——sequence_number
                        递增、终帧携带聚合全文与 usage）
Anthropic        ──→  adapters/anthropic    ──→  InferHub /chat/completions
                      （双向翻译）
```

Chat 是上游母语；Responses / Anthropic 均为「方言 ⇄ 母语」双向翻译层（`adapters/`）。`cache_control` 与 Anthropic 入站 `thinking` 配置无 Chat 对应项，翻译时丢弃；上游输出思维链时经 `reasoning_content` 返回，Responses 客户端以 `reasoning_summary_*` 事件收到。

## 项目结构

```
server.py                公共设施：签名、上游发送与重试、模型同步、dumps 落盘、请求日志、路由挂载
routes/chat.py           /v1/chat/completions
routes/responses.py      /v1/responses
routes/anthropic.py      /v1/messages
adapters/responses.py    Responses ⇄ Chat 翻译桥
adapters/anthropic.py    Anthropic Messages ⇄ Chat 翻译桥
admin/panel.py           面板页 + /admin/api/*（overview / checkin / reload-models）
admin/checkin.py         后台每日自动签到线程
lanes/benefit.py         免费额度与积分活动（签到/余额/邀请链接），独立 CLI
lanes/credential_provider.py  凭证装载与自动续期
lanes/session.py         并发闸门（心跳 + 本地准入）
lanes/oauth.py           OAuth2 PKCE 浏览器登录
panel.html               控制台单页
tests/                   离线测试（打桩上游，不连真实服务）
```

路由层与面板层都走 `register(app, core)`：`core` 传 `server` 模块自身，引擎件
（`_send_upstream` / `_cors` / `SUPPORTED_MODELS` / `benefit` …）一律在**请求期**
经 `core.xxx` 现查。导入期绑定会抓住原函数，测试对 `server` 的打桩就失效了——
`tests/` 里的桩因此对每个路由层同时生效。

三个短缓存在 `admin/panel.py`（积分余额 60 s / 积分活动 60 s / 邀请链接 1 h），
因为面板是它们唯一的消费方；签到成功后清零强制刷新。上游 `opengw` 与
`snap-access` 都会间歇 TLS 抽风，所以 overview 里每一项单独降级——
一个接口挂掉不让整份 JSON 失败，也不拖死其余卡片。

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `CODEARTS_BENEFIT_AUTO_CLAIM` | `1` | 免费额度自动领取与每日签到开关 |
| `CODEARTS_MAX_TOKENS_CAP` | `65536` | 入站 `max_tokens` 钳制上限（上游 InferHub 实际接受上限） |
| `CODEARTS_CONN_RETRY_MAX` | `2` | 连接级失败（TLS 中断/连接重置）重发次数 |
| `CODEARTS_CONN_RETRY_BACKOFF` | `0.5` | 重发退避基数（秒） |
| `CODEARTS_SESSION_GATE` | `1` | 本地并发准入闸开关（满员回 409） |
| `CODEARTS_SESSION_LIMIT` | `3` | 本地准入上限，对应上游每账号 3 路并发 |
| `CODEARTS_SESSION_HEARTBEAT` | `1` | `user-session-id` 心跳开关；关掉后仍发 session 头 |
| `CODEARTS_HEARTBEAT_INTERVAL` | `30` | busy 续期间隔（秒） |

## 免费模型

| 模型 | 上下文 | 上游 max_tokens 上限 | 通道 |
|---|---|---|---|
| `deepseek-v4-flash-0731` | 1,048,576 | 65,536 | Token |
| `glm-5.3-flash` | 1,048,576 | 65,536 | Token |
| `deepseek-v4-pro-0813` | 1,000,000 | 65,536 | Token |
| `deepseek-v4.1-flash` | 1,000,000 | 65,536 | Token |
| `GLM-5.2` | 202,752 | 65,536 | 积分 |
| `glm-5.2-sft-harmony` | 202,752 | 65,536 | 积分 |

> 云端下发的 `max_tokens` 是上下文口径（131,072+），直接发给 InferHub 会被拒。`server.py` 钳到 `CODEARTS_MAX_TOKENS_CAP`，`/v1/models` 也按钳制后的值上报。

### 福利签到

```bash
python lanes/benefit.py            # 全部：Token 签到 + 积分签到 + 余额
python lanes/benefit.py status     # 查签到状态（含活动列表与结清状态）
python lanes/benefit.py claim      # 领每日 1000 万福利 Token
python lanes/benefit.py credit     # 领积分活动 + 查余额
python lanes/benefit.py invite     # 拿邀请链接（不代领奖励）
python lanes/benefit.py models     # 查免费模型列表
```

积分活动按 **`claimable` 就领**,不按 type 白名单筛。学生认证（传证件）和邀请好友
（拉真人）的 `triggerEvent` 是 `student.certified` / `invite.success`,必须真实事件
才会把 `claimable` 翻成 true —— 脚本造不出这个条件,服务端也会拒绝直接领取;而一旦
`claimable=true`,说明资格已被认可,此时 claim→confirm 只是收款。type 白名单防不住
「脚本代劳」,只会挡住「事后收款」。

`AUTO_GRANT` 的活动（实测新人注册礼就是）服务端自己发放,不去 claim,等它到账。

领取走四步闭环 **delivery → claim → confirm → 回读**：claim 返回 200 只代表上游
受理,不代表积分到账,只有回读到 `CONFIRMED`/`CONSUMED` 才算成功汇报。上游没有
「查询认证状态」的接口,认证进度只能去账号中心页面看。机制与对照表见
`../docs/codearts-07` §8.6,离线测试 `python tests/test_credit_claim.py`。

### 邀请链接

```bash
python lanes/benefit.py invite     # 拿邀请码与邀请链接（也可 invite 3 指定活动）
```

邀请活动实测是 `INVITE_USER`（`campaignId=3`，2026-11-15 截止），触发事件
`invite.success`、`triggerMode=MANUAL_CLAIM`。这里**只拿链接**：把链接发给好友，
对方注册成功后奖励由服务端自动到账。脚本不代领邀请奖励——上游要求真实邀请事件，
直接领取会回「该活动类型需要先通过触发接口激活，无法直接领取」。

`POST /v1/ops/invite/generate` 是「生成或取回」的幂等接口（实测连调三次返回同一条码），
所以面板侧缓存 1 小时，刷新页面不会反复打上游。返回 `inviteCode` / `inviteUrl` /
`activationCount` / `maxActivations` / `valid`，`valid=false` 时前端标「已失效」。
面板「邀请好友」卡片展示链接并带复制按钮。

## 并发闸门（`TM.00001041`）

上游对每个账号只放 **3 路并发对话**，放不下报 `TM.00001041`（HTTP 400）。
关键是：**这个闸门不认「请求结束」，只认心跳**：

```
PUT /snap-manager/v1/chat-session/heartbeat?status=busy|idle   → {"status":"ok"}
```

不主动 busy/idle 的话，每个请求都会让上游隐式开一个会话且永不注销，3 个槽很快
占满，之后一律 400，而退避重试等的是别人释放——上游只肯按超时回收，于是白等
12 s 回 502。`lanes/session.py` 因此做三件事：

1. **心跳**：请求前 busy、流式期间每 30 s 续期、收尾 idle；对话请求带同一个
   `user-session-id`（自造 32 位 hex，绝不用 IDE 的 `ses_*`——idle 是注销语义，
   会把用户正在用的会话杀掉）
2. **本地准入闸**：按账号计在途数，满员直接回 **409**。不用 429：429 会被客户端
   当成额度耗尽去长冷却，而这个条件几十毫秒内就会自己消失
3. **稳定身份**：闸门 key 用账号 id（`auths/` 文件名或 `account.id`），**不能用
   AK**——AK 每次续期都换，拿它当身份会把 3 路上限悄悄重置

槽位在**上游响应正文读完或连接关闭时**才归还（`TrackedResponse` 收口），不在
`_send_upstream` 返回时就还——三条路由都是在它返回之后才消费响应体，提前还槽
等于闸门只覆盖到「上游收下请求」那一刻。

管理面板 `/admin/api/overview` 的 `session` 字段可看在途数、上限与心跳状态。
完整机制与五条不变式（含「续期失败不释放槽位」「只注销自己造的 id」）见
`../docs/codearts-07` §8，离线测试 `python tests/test_session_gate.py`。

### 面板显示

控制台入口 `http://127.0.0.1:8787/`（`panel.html`，需带 API_KEY，页面加载时由
服务端注入）。读 `/admin/api/overview`，共四张统计卡（凭证/福利余额/**积分余额**/
可用模型）+ 凭证详情 + 福利签到 + **积分活动** + **并发闸门** + 模型列表：

- **积分活动**逐条列 4 个活动与状态；学生认证、邀请好友额外标「需真实事件」——
  `claimable=false` + `status` 为空时显示 `未可领`，**不推断原因**（从没参与 /
  审核中 / 被拒在接口层面一模一样，上游也没有查认证进度的接口）
- **邀请好友**显示邀请码、邀请链接（可点开、可复制）与 `已邀请 / 上限` 进度；
  只读展示，面板不提供也不该提供「代领」按钮
- **并发闸门**显示 `在途 / 上限`，满员时徽标转红（新请求回 409）。`in_use` 是
  `{闸门 key: 在途数}`，前端要把 values 求和才是全局在途数

后端字段契约由 `python tests/test_panel_overview.py` 兜底：面板 `render()` 读的
每个字段都断言真的存在于 overview 里。JS 里 `data.x ?? "—"` 不报错，字段名
写错只会一直显示占位符——所以前后端漂移必须靠测试发现，不能靠肉眼看页面。

## 已知限制

- **3 并发限制**：上游每账号 3 路，本地闸门默认同值；超限的请求在代理侧即被挡下
  并回 409，不会再打上游
- **max_tokens 钳制**：上游 InferHub 实际只接受 ≤ 65,536，超出在 SSE 流内塞错误帧（HTTP 仍 200）
- **连接级失败重发**：上游偶发 TLS 中断 / 连接重置，重签后重发（默认 2 次）；读超时不重发避免重复计费
- **cache_control / Anthropic thinking 配置丢弃**：Chat 协议无对应项（上游亦无 Anthropic 式 prompt cache）；思维链内容在 Responses 侧以 reasoning 事件保留
- **仅绑定 127.0.0.1**，不对外网开放
- **`auths/` 含敏感凭证**，已加入 `.gitignore`

## 免责

本项目及作者与华为云平台及其附属公司无任何关系，华为云平台未对本项目调用方式明确许可。若因使用本工具造成封号/限流等情况，与项目作者无关。

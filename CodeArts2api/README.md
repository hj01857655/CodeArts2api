# CodeArts2api

> 华为云 CodeArts 模型接口 → OpenAI / Anthropic 兼容 API 网关

把华为云 CodeArts 的模型接口（盘古开源大模型、GLM、DeepSeek 等）包装为三协议兼容接口。底层走 `SDK-HMAC-SHA256` 请求签名，凭证通过 OAuth2 PKCE 登录获取（与 CodeArts IDE 同源），无需手动配置 AK/SK。

## 功能特性

- **三协议兼容**：OpenAI Chat Completions · OpenAI Responses · Anthropic Messages
- **OAuth2 PKCE 自动登录**：浏览器授权 → STS 临时凭证，到期自动续期
- **免费额度自动领取**：每日 1000 万福利 Token + 1000 积分，后台 daemon 线程自动签到
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

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `CODEARTS_BENEFIT_AUTO_CLAIM` | `1` | 免费额度自动领取与每日签到开关 |
| `CODEARTS_MAX_TOKENS_CAP` | `65536` | 入站 `max_tokens` 钳制上限（上游 InferHub 实际接受上限） |
| `CODEARTS_CONN_RETRY_MAX` | `2` | 连接级失败（TLS 中断/连接重置）重发次数 |
| `CODEARTS_CONN_RETRY_BACKOFF` | `0.5` | 重发退避基数（秒） |

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
python lanes/benefit.py status     # 查签到状态
python lanes/benefit.py claim      # 领每日 1000 万福利 Token
python lanes/benefit.py credit     # 领每日 1000 积分 + 查余额
python lanes/benefit.py models     # 查免费模型列表
```

## 已知限制

- **5 并发限制**：免费 CodeArts 有并发上限，Agent 类客户端可能超限
- **max_tokens 钳制**：上游 InferHub 实际只接受 ≤ 65,536，超出在 SSE 流内塞错误帧（HTTP 仍 200）
- **连接级失败重发**：上游偶发 TLS 中断 / 连接重置，重签后重发（默认 2 次）；读超时不重发避免重复计费
- **cache_control / Anthropic thinking 配置丢弃**：Chat 协议无对应项（上游亦无 Anthropic 式 prompt cache）；思维链内容在 Responses 侧以 reasoning 事件保留
- **仅绑定 127.0.0.1**，不对外网开放
- **`auths/` 含敏感凭证**，已加入 `.gitignore`

## 免责

本项目及作者与华为云平台及其附属公司无任何关系，华为云平台未对本项目调用方式明确许可。若因使用本工具造成封号/限流等情况，与项目作者无关。

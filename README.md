# CodeArts Agent 接口协议分析与 OpenAI 兼容网关

华为云 CodeArts Agent 的接口协议分析成果，以及免费额度 OpenAI / Anthropic 兼容代理（`CodeArts2api`，端口 **8787**）。

## CodeArts2api 速览

```bash
cd CodeArts2api
python server.py
# OpenAI Base URL: http://127.0.0.1:8787/v1（key 见 .env 的 API_KEY）
```

### 两条通道必须严格区分

| 通道 | 模型 | 签名 |
| --- | --- | --- |
| AgentCenter（老套餐） | openpangu-2.0-pro / openpangu-2.0-flash / GLM-5.2 | 基础 HMAC |
| benefit（免费） | glm-5.3-flash、deepseek-v4-pro-0813、deepseek-v4-flash-0731 | 额外携带 `maas_type` / `model-id` / `model-name` 三头 |

判定依据：模型是否在 AgentCenter 返回的清单里，不在即按 benefit 处理（IDE 以后新增免费模型无需改代码，直接传模型名即可）。免费模型缺三头报 `model is not registered`；老套餐模型带三头反报 `unsupported model`。

模型清单启动时从 opengw 网关自动同步并缓存，同步失败才用兜底列表。

## 踩坑记录

1. **上游会在 2xx 流里夹带业务错误帧**，裸透传会让客户端拿到 HTTP 200 却毫无内容。反代必须扫描流内帧并显式转成 OpenAI error 帧。
2. **并发上限 3 个会话**（`TM.00001041`）。断流不释放会话会堆积，多开窗口直接失败。反代对该错误码做 3 次退避重试（2s 起步）；积分耗尽（`TM.00001050`）不可重试，直接透传语义。
3. **Flask 流式生成器内禁止访问 `flask.g`**。生成器惰性迭代时请求上下文已结束，碰 `g.rid` 会抛 `Working outside of application context`，异常被 `finally` 吞掉后只表现为日志有 `in:` 没 `done:`。上下文字段必须在请求期求值成局部变量。
4. **消息结构校验比 OpenAI 严格**：assistant 带 `tool_calls` 后必须紧跟对应 `tool_call_id` 的 tool 消息，缺一个整条历史被拒（`InferHub.malformed_json.400 insufficient tool messages following tool_calls message`）。客户端被中断时最容易留下这种悬空结构（assistant 发起调用但结果未落地），之后每个请求都持续 400。反代在请求期清洗：无文本的悬空 assistant 整条丢弃、有文本的只摘 `tool_calls`、找不到对应调用的 tool 消息丢弃；绝不碰 `function.name` 等字段名。
5. **福利不领就调不动**：benefit 模型需先调用领取接口，否则报 `benefit not found`。反代启动时与每天定时自动领取（幂等）。

## 说明

- dumps 只落请求/响应 body 原文，绝不落 header（Authorization 里是明文网关 key）。
- `/admin` 面板可查看用量、手动签到、重载模型清单。
- 凭证存储于 `CodeArts2api/auths/`（gitignore），不碰本机 IDE 凭证。

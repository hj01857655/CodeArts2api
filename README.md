# CodeArts2api (Go)

![Go](https://img.shields.io/badge/Go-1.24-00ADD8?logo=go&logoColor=white) ![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey) ![Type](https://img.shields.io/badge/type-API%20Gateway-blue)

CodeArts 转 OpenAI / Anthropic 兼容 API 的 Go 网关实现。

## 构建

```bash
go build -o codearts2api .
```

## 运行

| 环境变量 | 说明 |
|---|---|
| `API_KEY` | 客户端鉴权密钥（非空 = Bearer 鉴权，面板 API 同用此 key） |
| `CODEARTS_PROXY_HOST` / `CODEARTS_PROXY_PORT` | 监听地址，默认 `127.0.0.1:8787` |
| `CODEARTS_STATE_DIR` | 状态目录（panel.html / models-cache.json / request.log / dumps/ 落点），默认 exe 所在目录 |
| `CODEARTS_BENEFIT_AUTO_CLAIM` | 每日自动签到，默认开（`0`/`false`/`no` 关闭） |
| `CODEARTS_SESSION_GATE` | 并发会话闸门，默认开 |
| `CODEARTS_MAX_TOKENS_CAP` | `max_tokens` 上限，默认 65536 |
| `DUMP` / `DUMP_DIR` / `DUMP_MAX_FILES` | 请求转储开关与目录，默认开 / `<状态目录>/dumps` / 200 |

## 接口

- `GET /v1/models` — 模型列表
- `POST /v1/chat/completions` — OpenAI Chat 兼容
- `POST /v1/messages` — Anthropic Messages 兼容
- `POST /v1/responses` — OpenAI Responses 兼容
- `POST /v1/ops/*` — 运维操作（claim / confirm / delivery / invite）
- `GET /`、`/admin` — Web 管理面板（内置，开箱即用）：凭据 / 余额 / 积分 / 签到 / 模型重载 / 会话闸门。状态目录放一份自定义 `panel.html` 可覆盖内置版

`/admin/api/*` 面板后端：`overview` / `checkin` / `reload-models`。鉴权与业务接口一致（Bearer `API_KEY`）；面板页本身免鉴权，key 注入页面后前端自动携带。

## Docker 部署

```bash
# 配置环境变量后启动
cp .env.example .env   # 按需填写
docker compose up -d
```

镜像同时发布到 GHCR：`ghcr.io/hj01857655/codearts2api`（跟随 v* 标签发布，含 latest，linux/amd64 + linux/arm64）。

二进制发版：推送 `v*` 标签后 Actions 自动构建 linux / windows / darwin 的 amd64 + arm64 六平台产物并发布 Release。

# CodeArts Agent 逆向文档索引

> 目标：本机安装的华为云 CodeArts Agent Windows 客户端。
> 安装位置：`C:\Program Files\CodeArts Agent\`（Inno Setup 安装，874MB）。
> 本文档集记录 2026-10-01 会话的逆向成果。断言除注明"推断"外均为 A 级（直接对照本机文件验证）。

## 文档清单

| 编号 | 文档 | 内容 | 等级 |
|---|---|---|---|
| 01 | [应用架构与安装布局](codearts-01-应用架构与安装布局.md) | 身份/版本/目录布局/数据目录/扩展清单/更新链 | A |
| 02 | [认证链与 OAuth2 协议](codearts-02-认证链与OAuth2协议.md) | IAM OAuth2（PKCE+DPoP）抓包+源码双证、AK/SK 换发、secretStorage 存储面、内核第二 OAuth 面 | A |
| 03 | [Agent 内核：Bun 单文件与 IPC](codearts-03-Agent内核-Bun单文件与IPC.md) | 内核=opencode 系运行时（Bun 1.3.14 单文件）、部署形态（~/.codeartsdoer 多版本并存）、spawn/env 注入、serve 服务器与 ~40 路由 IPC 面、KERNEL_* 数据目录 | A |
| 04 | [签名体系与业务 API 端点面](codearts-04-签名体系与业务API端点面.md) | 三签名器全还原（SDK-HMAC-SHA256 v9 / V11 HKDF 派生 / AWS4-V1）、AK/SK 注入面、snap-access 主域+熔断备份、Agent-Type 鉴权头族、x-auth-token 五分支、模型双协议端点 | A |
| 05 | [运行时数据、wire 协议与工具链](codearts-05-运行时数据与wire协议与工具链.md) | KERNEL_DATA_DIR 实值（~/.codeartsdoer）、opencode.db 18 表 + part 六型实测、prompt 请求 schema/SSE 事件枚举/WS 线协议、bash 三模式+默认权限表+windows_sandbox、凭证双实体 v10+AES-256-GCM 解密实值 | A |
| 06 | [技能与规则体系、out/vs 差异面](codearts-06-技能规则体系与差异面.md) | 八层 scope 优先级链 + SKILL.md 校验、44 技能实存、云端 skills-rules/MCP 详情 API、~/.codeartsdoer 全目录职责、MCP 官方 SDK 全栈 + 124 云端 MCP、151/3564 华为专属模块清单（五大 contrib 域） | A |
| 07 | [对话接口协议](codearts-07-对话接口协议.md) | POST /api/v2/chat/completions：SDK-HMAC-SHA256 v9 明文签名（20 SignedHeaders，修正 04 号 join 分隔符）、端到端签名逐字节复现、请求体 schema + 私有 `tool_stream`、三类请求形态与工具集三档、SSE 流式管道（双层超时 300s/600s、ModelArts 错误码拦截）、`ses_silent` 子代理哨兵、**并发闸门 `user-session-id`+heartbeat busy/idle（`TM.00001041`，本地准入满员回 409，五条不变式）+ §8.6 积分活动 `claimable` 门槛与四步闭环** | A |
| 08 | [benefit 权益与每日额度](codearts-08-benefit权益与每日额度.md) | **双层福利体系**：内核 `/api/v1/benefit/*`（AK `HSTAY4…`，20 签名头）+ 扩展 `/v1/ops/*`（AK `HSTAZB…`，13 签名头）、**积分活动：领取门槛只有 `claimable`（不按 type/campaignId 筛）、`AUTO_GRANT` 不 claim、四步闭环 delivery→claim→confirm→回读、**邀请链接 `POST /v1/ops/invite/generate`（POST-only、幂等生成或取回、只拿链接不代领）**、无「查询认证状态」接口**、每日额度 1000万及 `total_balance` 判定法、cache read 不计入消耗、config 实测 4 个模型（含 `base_url`，无 `model_alias`）、双轨额度（本地 token + 云端积分）、错误码（`4006` 每日限额、`4291` 配额耗尽）、免费模型豁免排队、`maas_type: benefit` 三头强约束 | A |
| 09 | [认证协议实操指南](codearts-09-认证协议实操指南.md) | **面向复现的操作手册**：协议速览、登录五步（含 DPoP ES256 JWT 构造细节）、续期（refresh_token 一次性轮换）、业务调用要点、两层 AK 适用范围对照、**故障判定表**（换发阶段 3 类 + 业务阶段 3 类）、凭证存储与安全注意 | A |

（后续编号随工作推进追加：remote-dev/remotedev 远程面、share 协议与云端会话、im-linker（企业 IM 打通）、docgen/图示生成等。）

### 已结清的悬置项

- 02 号 §2.2 / §3.7：换发响应结构、STS 有效期（2 小时）、refresh_token 一次性轮换、风控上报 devID/hwMeta
- 02 号 §3：登录全链路 40+ 跳实测，含 CAS/IDM 双层 OAuth 串联、二维码扫码、双重 302→307 回环
- 07 号 §2.4~§2.7：payloadHash 复现、端到端签名逐字节命中、三处实现细节（尾斜杠 / HMAC 单次 / SignedHeaders 位置）
- 07 号 §3.3 / §6.4.1：三类请求形态（工具集 14/9/0）与三类 SSE 响应对照
- 08 号 §2~§6：balance 额度判定、config 实测 4 模型、双层福利体系、活动积分机制

> **已解悬置项**：
> - 05 号 §4.1：两个 id 生成规则（`project.id` = git 根提交 SHA；`checkpoint_<hex>` = `sha256(projectPath)[:15]`），4/4 实值验证。
> - 04 号 §1.1/§1.2：`CanonicalRequest`/`StringToSign` 的 join 分隔符已于 07 号修正为换行（V11 的 StringToSign 仍是空格）。

## 一句话结论（当前）

CodeArts Agent = **VS Code fork（Electron，纯 JS 非 asar 打包）+ 华为云 AI Agent 扩展族 + Bun 单文件可执行 Agent 内核**。核心 AI 逻辑在内置扩展 `huaweicloud.vscode-codebot`（扩展 id `codearts-snap`），其 173MB `agentkernelServer-win32-x64.exe` 是 **Bun v1.3.14 单文件可执行（内嵌 312 文件/71MB JS 层，opencode 系 agent 运行时）**；认证走 `huaweicloud.authentication`（5.4.2）标准 **IAM OAuth2（授权码+PKCE S256+DPoP ES256）**，换发产物是 **IAM 临时 AK/SK 三元组**（非 Bearer），全业务 API 走 HMAC-SHA256 签名。

## 关键事实（01 号详述）

| 项 | 值 |
|---|---|
| 应用名 | CodeArts Agent（`codearts-agent`） |
| 版本 | 26.9.500，commit `1f505f13043bfe4820301e490b025a064327092e`，distro `d7ca751ba65ff48109f0f0a86178d1d659b4e399` |
| 外壳 | Electron/Chromium（resources.pak + 标准 Chromium 运行时 DLL），Inno Setup 打包（`unins000.exe`/`tools/inno_updater.exe`） |
| 主入口 | `resources/app/out/main.js`（`"main": "./out/main.js"`，**目录型 app，未打 asar**；`out/vs` 83MB + `extensions` 350MB） |
| URL 协议 | `codearts-agent`；AppUserModelId `Huawei.CodeArts.Agent` |
| 运行时数据（Chromium profile） | `%APPDATA%\codearts\`：`app-settings.json`、`taskboard.db`（172KB，疑似任务/工单库）、`secretStorage/8f4ebc74…be37.enc`（加密凭证）、`app-logs/logs` |
| 用户数据（VS Code 型） | `%APPDATA%\codearts-agent\`：`User/`（settings.json、chat_sessions、globalStorage、workspaceStorage…）、`machineid`、`languagepacks.json` |
| 隐藏数据目录 | `~/.codearts-doer-for-coding/`（product.json `dataFolderName`，含 argv.json、extensions） |
| 更新器 | `%LOCALAPPDATA%\codearts-updater\installer.exe`（238MB 全量包）+ `tools/inno_updater.exe` |
| 核心 AI 扩展 | `huaweicloud.vscode-codebot` = 扩展 id `codearts-snap` 26.9.500，main `./out/extension.js`，onStartupFinished 激活，contributes: viewsContainers/views/commands/keybindings/configuration |
| 认证扩展 | `huaweicloud.authentication` 5.4.2，main `./dist/plugin.js`，activationEvents `*` |
| 其他华为扩展 | `huaweicloud.codearts-agent-support` 1.0.24（命令支持面板）、`huaweicloud.remotedev-codearts-agent` 2.5.0（remotedev/remotedev-ssh/remotedev-container 远程开发） |
| 云门户 | `https://codearts.huaweicloud.com/portal`；用量页 `/portal/settings/personal-usage`；通知服务 `ide.cn-north-4.myhuaweicloud.com/v1/notification-service` |
| 已装用户扩展（非内置） | arturock.gitstash、guodongsun.vscode-git-cruise、huizhou.githd、leon-pw.docx-reader、tomoki1207.pdf、prompt-basics 等 |

## 与 ZCode/DoubaoWork 对比（方法学分流）

- 与 **ZCode 同构**：同为 VS Code fork + 自有 Agent 扩展 + 云门户认证，`out/vs` 可对照社区版差异面（Zcode_docs/43 号方法可直接复用：闭源符号清单、env 面、nls 差异）。
- 与 **DoubaoWork 不同**：无 pak v5 容器、无 protobuf 资产包，前端资产即 VS Code 扩展 JS（minified 但结构标准），静态阅读成本显著更低。
- `app-settings.json` 已见 `skipTlsVerification: true`、`isInitSandbox: true`、`work.autoApproval`（bash:false/sandboxCommand:true）——沙箱与审批体系是后续重点。

## 目录约定（本工作区）

```
CodeArts/
├── docs/            # 编号文档（本文件起）
├── re/              # 提取/解包/解码产物（随推进建子目录）
└── tools/           # 专用脚本（mitm/探测/解码）
```

## 下一步（待办骨架）

1. `out/main.js` + `out/vs` 与社区版 VS Code 26.9.x 差异面扫描（闭源模块清单）
2. `vscode-codebot`（codearts-snap）扩展全解：views/命令/配置面 → 网络端点与鉴权头
3. `huaweicloud.authentication` 登录流程（OAuth/设备码？token 存储对照 secretStorage.enc）
4. `taskboard.db` schema 与 `User/chat_sessions` 落盘格式
5. 运行时抓包：门户登录 + Agent 对话端点（对照 ZCode 的 mitm 方法论，tools/mitm/ 可复用）

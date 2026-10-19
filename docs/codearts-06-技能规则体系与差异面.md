# 06 技能与规则体系、out/vs 差异面（A 级：实值 + 模块清单）

> 证据：`~/.codeartsdoer/` 实测（44 技能实体、目录地图）、`re/bun_extract/` 内核 JS 层（skill 加载链、路由注册）、`out-build/` 模块标记扫描（workbench 30.9MB bundle）。

## 1. 技能体系

### 1.1 六层 scope 与路径（root_chunk-9c5a79t1.js，`getXxxSkillPathConfig`）

hc 模式（`PLUGIN_ENV=hc`，本机）下根目录名为 `.codeartsdoer`，否则 `.opencode`；SCENARIO 环境变量可改写根名。

| scope | scopeType | 路径（hc 实值） | 优先级来源 | 禁用清单文件 |
|---|---|---|---|---|
| user | — | `~/.codeartsdoer/skills` | `priority.user` | UserSkillStatus.txt |
| project | — | `<project>/.codeartsdoer/skills` | `priority.project` | ProjectSkillStatus.txt |
| system | — | `~/.codeartsdoer/cache/system/skills`（非 CLI 模式）；CLI 模式用 `BUILT_IN_CONFIG_DIR/skills`（内嵌系统技能） | `priority.system` | SystemSkillStatus.txt |
| team | — | `~/.codeartsdoer/cache/team/skills` | `priority.team` | TeamSkillStatus.txt |
| cloud_user | 0 | `~/.codeartsdoer/cache/user/skills` | `priority.cloud_user` | CloudUserSkillStatus.txt |
| cloud_team | 1 | `~/.codeartsdoer/cache/team/skills` | `priority.cloud_team` | CloudTeamSkillStatus.txt |
| cloud_enterprise | 2 | `~/.codeartsdoer/cache/enterprise/skills` | `priority.cloud_enterprise` | CloudEnterpriseSkillStatus.txt |
| cloud_system | 3 | 同 enterprise 路径 | — | — |

非 hc 模式默认 `~/.config/<root>/cache/<layer>/skills`（`BUILT_IN_CONFIG_DIR` 优先）。

**cloud 层继承链**（E6 的 `$` 计算）：
`user → [cloud_user, cloud_team, cloud_enterprise]`；`project → [cloud_enterprise]`；`cloud_user → [cloud_user, cloud_team, cloud_enterprise]`；`cloud_team → [cloud_team, cloud_enterprise]`；`cloud_enterprise → [cloud_enterprise, cloud_system]`。

### 1.2 技能实体结构与校验

```
~/.codeartsdoer/skills/<name>/
├── SKILL.md          # frontmatter: name / description(≤1024) / language? / license?
└── references/*.md   # 参考文档（code-audit/references/sast-review-checklist.md）
```

- name 规则：小写字母数字 + 单连字符分隔（`my-skill`），正则校验；description 上限 1024 字符（中英双语错误提示，`checker_*` 系列诊断码：目录不可读、SKILL.md 不可读、YAML frontmatter 缺 `---` 分隔等，`shouldShortCircuit` 控制短路）。
- frontmatter 解析用 yaml `parseAllDocuments(...)[0].toJSON()`（skill-parser 服务）；加载时若缺 `---` 会回退正则逐行提取 `key: value` 并剥引号。
- 加载对象 `{skillDir, dirName, scope, dirFiles}`，逐项跑检查器（Ez/Oz）产出 diagnostics。
- 变更追踪：按会话记录技能激活变化（`recordActivatedSkillChange`/`added`/`removed` 集合），可回算会话的技能增减差异。

### 1.3 本机 44 技能（`~/.codeartsdoer/skills/`，全清单）

安全分析主族：`api-security`、`apk-reverse`、`attack-chain`、`binary-diff`、`binary-ninja-reverse`、`browser-automation`、`browser-extension-reverse`、`code-audit`、`ctf-sandbox`、`database-security`、`digital-forensics`、`dotnet-reverse`、`edr-bypass-re`、`email-security`、`firmware-pentest`、`ghidra-reverse`、`go-rust-reverse`、`hardware-security`、`ida-reverse`、`identity-federation`、`js-reverse`、`llm-security`、`macos-reverse`、`malware-analysis`、`mobile-reverse`、`ot-ics`、`patch-diff-exploit`、`pentest-tools`、`protocol-reverse`、`pwn-chain`、`radare2`、`radio-sdr`、`reverse-engineering`、`reverse-skill-router`、`supply-chain-security`、`thick-client`、`threat-hunting`、`threat-intelligence`、`wifi-wireless`、`windows-ad`、`cloud-k8s`；
生成/文档族：`diagram-generator`、`docs-generator`。

样例（code-audit）frontmatter 与正文结构：`name`+`description` 后接「ACTION REQUIRED（读完后立刻执行）」编号步骤 + 授权判定分支（无源码二进制 → 转 RE skill）+ `references/` 引用。

**注意**：这批技能是**本机用户数据**（`~/.codeartsdoer/skills`），非安装包内容——即当前账号/环境下的实际技能库快照，与 DoubaoWork 那种安装内置技能包（pak 内）性质不同。

### 1.4 云端技能与规则同步

- `GET {agentCenterApiUrl}/v1/skills-rules/list`（chunk-9c5a79t1.js `k_`/`w6`）：头 `Content-Type: application/json` + `Agent-Type: PromptCenter` + `X-Language`，走 `resilientCall` 熔断包装（失败返回 `{httpStatusCode:-1, message}`）；非 hc 分支走本地 `t0()`。
- 云端技能详情：`GET {agentCenterApiUrl}/mcpService/external/skills/v1/get?skillId=<id>`，鉴权头 **`X-Auth-Token`**（取 `process.env.W3_TOKEN || config.userToken`）。
- 技能版本缓存写入（`skillId=version` 形式，`\r ` 换行拼接）。
- 内核路由：`/v1/skills`（A_n）、`/skillRule`（Mmn）、`/cag/rules`（X_n）、`/cag/skillRule` 同族。

## 2. ~/.codeartsdoer 目录职责全景（内核对应面）

| 目录 | 职责 | 内核实现位置 |
|---|---|---|
| `skills/` | 用户级技能（44 个实体） | skill-parser / E6 加载链 |
| `memory/<路径slug>/` | 记忆库（`C--Program Files-CodeArts Agent`、`E--VSCodeSpace-codearts2api`、`E--VSCodeSpace-Kiro-mitm` + status.json） | memory-plugin（`/cag/memory`） |
| `rule/` | 自定义规则（本机空） | `/cag/rules`、`customRuleDropdown` |
| `agents/` | 自定义 agent 定义（空） | `/cag/agent`、`runSubagentTool` |
| `agent-team/{roles,scene,session,log}/` | 多 agent 团队：角色、场景编排、会话与日志 | root_chunk-pmbns5fx.js 路径函数（`ne()/Ue()/Me()/B()/Ce()`），项目级目录不可写时回退用户级 |
| `evolve/skill_evolution_workspace/generated-skills/` + `task-status/` | 技能自演化工作区 | `/cag/evolve/daemon`（Swn） |
| `generative-ui/` | 生成式 UI 状态（`generative-ui.yaml`，`__codeagent_widget_enabled` 全局开关） | widget-state 服务 + `/cag/widget`（Jwn） |
| `cache/{system,team,enterprise}/{skills,agents,rule}` | 各 scope 的云端/系统缓存（team/enterprise 含 agents+rule，system 仅 agents+skills） | §1.1 scope 路径 |
| `CodeArts_Agent/` | 内核 exe 多版本 + server_config.properties | 03 §2 |
| `codearts-data/` | 主数据根（opencode.db 等） | 05 §1 |
| `.codebase/tool_switch.json`（项目级） | codebase 工具开关（`{"tool-off":[...]}`） | codebase-tool-plugin（`m9n`） |
| `.codeartsdoer/specs/` | SDD 规格（`/cag/v1/sdd`），场景别名 opencode/codemate/codeagent/testagent → `.opencode/specs` | `F8()` |
| `~/.codeartsdoer/code-glance.jsonc` | CodeGlance 功能开关（`enabled!==false`） | `kjn()` |
| `~/.codeartsdoer/installers/{bin,prebuilds}` | CLI 自安装器（win32-x64/arm64 预编译） | `uninstall`/`installOpenharmony` 等命令族 |
| 项目级 `.codeartsdoer/skills/codeagent-custom-review-rules/reference/<skillId>/` | 自定义审查规则生成工作区（0–12 步流水线，产出 fix-report.json/conclusion.txt/statistics.json） | 内置 prompt（9bq6rhqj 内嵌 spec） |

## 3. MCP 客户端

### 3.1 协议栈（root_chunk-qy5ebw5c.js，官方 SDK 内嵌）

- **StdioClientTransport**：spawn 子进程（`command`+`args`+`env`，白名单 `AD1` 环境变量透传），stdio 管道 + stderr（`pipe`/`overlapped`/`inherit`）。
- **SSEClientTransport**：EventSource 订阅，校验 endpoint origin 与连接 origin 一致；支持 OAuth 授权码流（`finishAuth`/`_startOrAuthSse` + `resumptionToken` + `last-event-id`）。
- **StreamableHTTP**：`_commonHeaders()` 注入 `Authorization: Bearer`、`mcp-session-id`、`mcp-protocol-version` + 附加头。
- 方法面：`tools/list`、`tools/call`。

### 3.2 内核 MCP 模块（chunk-5ac5hqhw.js，2.7KB 导出面）

导出：`validateMcpCommand`、`queryMcpTools`、`invalidateMcpToolsCache`、`fetchMcpTools`、`fetchBuiltInTools`、`determineMcpToolStatus`。

- 路由：`GET /v1/mcp-tools` → `[{mcp_name, status}]`（动态 import 上面的 chunk）；失败返回 `{code:-1, data:[]}`。
- `GET /v1/builtin-tools` → `[{toolSetName, toolSet, description, tools[]}]`。
- `GET /v1/local-tools/*`：codegraph 开关/手动索引/codebase 搜索（`/v1/local-tools/permission`）。
- `POST /v1/auth/hash`：插件安装校验，返回 `{hash, kernel_version, timestamp, app_id}`（配 `x-ot-trace-id`/`x-ot-span-id`）。
- 其余路由面：`/cag/mcp`、`/cag/acp-mcp`、`/v1/lsp`、`/v1/skill`、`/v1/skills/detail`、`/v1/skills/sync`。

### 3.3 市场缓存（`globalStorage/huaweicloud.codearts-snap/`）

| 文件 | 内容 |
|---|---|
| `mcp-list-cache.json` | `{data:{total:124, installedList:[], mcpServers:[{id,name,aliasName,description,description_en,iconUrl(base64 data URI PNG),…}]}}` —— **124 个云端 MCP 服务器目录**（本机未安装任何） |
| `mcp-install-cache.json` | `{}`（无安装记录） |
| `<version>/.assets-dir.json` | 内核资产目录指向（03 §2） |

样本条目：`id:"9"`, `name:"Fetch网页内容抓取"`, `aliasName:"Fetch"`（HTML→markdown 转换服务）。UI 侧对应 `mcpMarketplaceDropdown`/`mcpSearchInputBox`/`installedMCPDropdown`/`mcpItemView`（workbench 偏好设置）。

## 4. out/vs 差异面（华为自有模块清单）

扫描 `// out-build/<path>` 标记（官方 vscode 源码路径在构建产物中保留），剔除标准域后得华为/CodeArts 专属模块。

### 4.1 各 bundle 命中统计

| bundle | 大小 | huawei/codearts 关键词 | 专属 out-build 模块 |
|---|---|---|---|
| `out/vs/workbench/workbench.desktop.main.js` | 30.9MB | huawei 92 / codearts 298 | **151** |
| `out/main.js`（主进程） | 2.4MB | 67 / 55 | 8 |
| `out/vs/workbench/api/node/extensionHostProcess.js` | 3.8MB | 24 / 20 | 9 |
| `out/vs/code/electron-utility/sharedProcess/sharedProcessMain.js` | 2.0MB | 33 / 49 | 4 |
| `out/vs/code/node/cliProcessMain.js` | 1.5MB | 36 / 47 | 3 |
| `out/vs/platform/terminal/node/ptyHostMain.js` | 0.7MB | 1 / 3 | — |
| `out/cli.js` | 0.4MB | 1 / 3 | — |

全 workbench 的 out-build 模块共 3564 个，其中 151 个（4.2%）为华为新增/改名 → **fork 策略是「不侵入核心、Agent 能力全部新建 contrib」**。

### 4.2 五大专属域（workbench）

1. **平台层**（4）：`vs/base/common/codeartsIcons.js`、`vs/platform/request/common/huaweiRequest.js` + `huaweiRequestService.js`（+ electron-browser 实现）、`vs/platform/remote/common/remoteAgentConnection.js`。
2. **chat/agent 会话体系**（~35）：`agentSessions/*`（Service/Model/Control/Actions/Picker/Viewer/Filter/Hover/QuickAccess/Opener + experiments 组含 `agentSessionProjection*`、`agentTitleBarStatusService`）、`contrib/chat/common/participants/chatAgents.js`、`common/tools/builtinTools/runSubagentTool.js`、`api/browser/mainThreadChatAgents2.js` + `extHostChatAgents2.js` + `extHostAgentTool.js` + `extHostHuaweiTypes.js`（新 RPC 通道）。
3. **终端 Agent 工具**（~40，`contrib/terminalContrib/chatAgentTools/`）：`executeStrategy/*`（basic/none/rich + helpers）、`commandLineAnalyzer/*`（含 autoApprove、npmScript、fileWrite、**sandboxAnalyzer**）、`commandLinePresenter/*`（node/python/ruby/**sandboxedCommandLinePresenter**）、`commandLineRewriter/*`（cdPrefix、preventHistory、pwshChainOperator、**sandboxRewriter**）、`tools/*`（runInTerminal、getTerminalOutput/Selection/LastCommand、killTerminal、awaitTerminal、runInTerminalConfirmation、task/{createAndRun,run,getOutput}、monitoring/outputMonitor）、`treeSitterCommandParser.js`、`common/terminalSandboxService.js`。**即 VS Code 侧自带终端沙箱与自动审批分析器（与内核 bash 三模式并行的第二套审批实现）。**
4. **偏好设置 UI**（~40，`contrib/preferences/browser/agentExtensionRelatedComponents/`）：codeGeneration（3）、conversationFlow（renderAgentOperatingMode/**renderAgentSecurityPolicy**/**renderAutoApproveSettings**/renderModelReplyLanguageSelect/renderShortCutKeyStrategy/renderSmartQASetting/renderVoiceRecordLanguageSelect/renderCodeMateFontSizeInput/Gutter/minLogLevel）、mcpTool（5）、memory（renderMemorySetting/memoryImportModal）、model（addModelDialog/renderModelSettings）、skillAndRules（skillsAndRulesView/customSkillDropdown/customRuleDropdown/skillDetailModal/skillImportModal/skillLocationModal/renderMarketSetting）、imIntegration（2）、labFeatures；外加 `settingsEditorCodeArts.js`、`contrib/preferences/browser/agentPanel/*`（5）、`services/preferences/common/preferencesEditorCodeArtsInput.js`。
5. **服务层**（~10）：`services/agentSettings/*`（agentSettingsService + **codebaseIndexService** + **mcpSettingsService**）、`services/authentication/browser/codeArtsAuthenticationService.js`、`services/remote/common/{abstractRemoteAgentService,remoteAgentService,remoteAgentEnvironmentChannel}` + electron-browser 实现、`contrib/remoteCodingAgents/*`（2）、`contrib/welcomeAgentSessions/*`（3）、`contrib/agentWelcomePage/*`（5）、`browser/parts/agentTools/*`（2）、`contrib/testing/common/testingChatAgentTool.js`。

### 4.3 主进程专属（8）

`codeartsIcons.js`、`remoteAgentConnection.js`、`huaweiRequest{,Service}.js`、**更新链三件套**：`vs/platform/update/common/codeArtsUpdate.js` + `vscodeHuaweiUpdate.js`、`vs/platform/update/electron-main/abstractCodeArtsUpdateService.js` + `updateService.snap.js`（对应 01 §5.4 的 `codearts-updater/installer.exe` + `inno_updater.exe`）。

### 4.4 扩展宿主专属（9）

`extHostAgentTool.js`、`extHostChatAgents2.js`、`extHostHuaweiTypes.js`（新增扩展宿主 API）、`contrib/chat/common/participants/chatAgents.js`、`runSubagentTool.js`、`services/remote/common/remoteAgentService.js` + 平台层 4。

## 5. 一句话结论

技能体系是**六层 scope（user/project/system/team + cloud_user/cloud_team/cloud_enterprise/cloud_system）优先级链**，实体为 `SKILL.md`（frontmatter name/description≤1024 + references/），本机 user 层实有 44 个安全分析技能（非安装内置，属用户数据）；云端规则走 `agentCenterApiUrl/v1/skills-rules/list`（Agent-Type: PromptCenter），云技能详情用 `X-Auth-Token`。`~/.codeartsdoer` 另有 agent-team（roles/scene/session/log）、evolve（技能自演化）、generative-ui、cache 三层、specs（SDD）等子系统，各有 `/cag/*` 路由对应。MCP 侧官方 SDK 全栈内嵌（stdio/SSE/StreamableHTTP + OAuth），市场缓存显示 **124 个云端 MCP、installedList 为空**。out/vs 差异面 = **151/3564（4.2%）华为专属模块**，集中在五个 contrib 域（chat agentSessions、terminalContrib 沙箱审批栈、偏好设置 AI 区、agentSettings 服务、agentWelcome/welcomeAgentSessions），主进程仅 8 个（含 snap 更新链）——**fork 不改 VS Code 核心，Agent 能力全栈新建**。

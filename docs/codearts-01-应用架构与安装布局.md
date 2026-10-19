# 01 应用架构与安装布局

> 目标：`C:\Program Files\CodeArts Agent\`（v26.9.500，2026-09-28 安装）。
> 等级：A（全部对照本机文件实测）。

## 1. 身份

| 项 | 值 | 来源 |
|---|---|---|
| 产品名 | CodeArts Agent | `resources/app/package.json` nameLong |
| 内部名 | codearts-agent | nameShort / applicationName |
| 版本 | 26.9.500 | package.json version |
| commit | 1f505f13043bfe4820301e490b025a064327092e | product.json commit |
| distro | d7ca751ba65ff48109f0f0a86178d1d659b4e399 | package.json distro |
| AppUserModelId | Huawei.CodeArts.Agent | product.json |
| URL 协议 | codearts-agent | product.json urlProtocol |
| dataFolderName | .codearts-doer-for-coding | product.json（`~/.codearts-doer-for-coding/` 实存） |
| 安装方式 | Inno Setup（64 位） | 根目录 `unins000.exe/dat/msg`、`tools/inno_updater.exe` |
| 外壳 | Electron/Chromium 定制 | `codearts-agent.exe` + 标准 Chromium DLL 族（ffmpeg/dxcompiler/vulkan/icudtl…） |

## 2. 安装目录布局（程序本体）

```
C:\Program Files\CodeArts Agent\
├── codearts-agent.exe            # 主程序
├── codearts-agent.VisualElementsManifest.xml
├── chrome_100/200_percent.pak, resources.pak, *.pak
├── snapshot_blob.bin, v8_context_snapshot.bin, icudtl.dat
├── ffmpeg.dll, dxcompiler.dll, d3dcompiler_47.dll, libEGL/GLESv2, vulkan 族
├── locales/
├── bin/
│   ├── codearts-agent            # 壳脚本（.cmd 版）
│   └── codearts-agent.cmd
├── tools/
│   ├── inno_updater.exe          # Inno 更新器
│   └── vcruntime140.dll
├── unins000.exe/.dat/.msg        # Inno 卸载器
├── postInstallFinisher.bat, uninstallFinisher.bat
└── resources/
    └── app/                      # ★ 目录型 app，未打 asar，可直接阅读
        ├── package.json          # "main": "./out/main.js", type: module
        ├── product.json          # 35KB，118 keys，含华为云专属键
        ├── LICENSE.txt, ThirdPartyNotices.txt
        ├── languagepacks.json
        ├── out/                  # 83MB（vs 子目录主体）
        │   ├── main.js           # Electron 主进程入口
        │   ├── cli.js, bootstrap-fork.js
        │   ├── nls.keys.json, nls.messages.json
        │   ├── media/, vscode-dts/, vs/
        ├── node_modules/         # 真实目录（node_modules.asar 仅 28B 占位）
        ├── extensions/           # 350MB，100+ 内置扩展
        ├── profile/
        │   └── ai-HC.code-profile  # AI 预设 profile
        ├── resources/            # win32/, workbenchModes/
        └── tools/
```

**要点**：与 ZCode 相同——VS Code fork 的 `resources/app` 是纯目录，minified 但无混淆层；`out/vs` 为 workbench 主体，华为自有代码主要在 `extensions/huaweicloud.*` 与 `out/` 差异补丁。

## 3. 内置扩展清单（华为云自有，extensions/ 下共 100+）

| 目录 | 扩展 id | 版本 | main | 激活 | 定性 |
|---|---|---|---|---|---|
| vscode-codebot | codearts-snap | 26.9.500 | ./out/extension.js | onStartupFinished, onCommand:codebot.studio.openSettings | **核心 AI Agent 扩展**（"writing code feels like a finger snap"）；contributes: viewsContainers/views/commands/menus/submenus/keybindings/configuration/icons/colors |
| authentication | huaweicloud.authentication | 5.4.2 | ./dist/plugin.js | `*` | 华为云认证（登录态供给方） |
| codearts-agent-support | codearts-agent-support | 1.0.24 | dist/plugin.js | onStartupFinished | 支持面板（commands/menus） |
| remotedev-codearts-agent | remotedev-codearts-agent | 2.5.0 | ./out/extension.js | onResolveRemoteAuthority:remotedev{,-ssh,-container…} | 远程开发（remotedev 域，非 vscode-remote 系） |

其余为社区/VS Code 标准扩展（语言包、主题、js-debug、git 族等），含用户后装第三方：arturock.gitstash、guodongsun.vscode-git-cruise、huizhou.githd、leon-pw.docx-reader、tomoki1207.pdf、prompt-basics。

## 4. product.json 华为云专属键（118 keys 中筛出）

| 键 | 值 |
|---|---|
| codeArtsAgentSpaceUrl | https://codearts.huaweicloud.com/portal |
| agentCenter | https://codearts.huaweicloud.com/portal/settings/agent |
| codeartsUsageUrl | https://codearts.huaweicloud.com/portal/settings/personal-usage |
| huaweiCloudNotificationUrl | https://ide.cn-north-4.myhuaweicloud.com/v1/notification-service |

（余 114 键待与社区版 product.json 做 diff 出完整差异面——并入差异面扫描任务。）

## 5. 运行时数据目录

### 5.1 %APPDATA%\codearts\（Chromium profile，应用级）

```
codearts/
├── .migration-done               # 1B 迁移标记
├── migration-report.json         # 2026-09-17 首装：无旧数据，跳过
├── app-settings.json             # 553B 应用设置（见下）
├── taskboard.db                  # 172KB SQLite，任务/工单库（schema 待 03 号）
├── secretStorage/8f4ebc74…be37.enc   # 加密凭证库（DPAPI？对照 ZCode 12 号方法）
├── Preferences, Local State, DIPS, SharedStorage
├── app-logs/logs/                # 应用日志
├── Partitions/, blob_storage/, Network/, Local Storage/, Session Storage/,
│   Code Cache/, GPUCache/, DawnGraphiteCache/, DawnWebGPUCache/, Shared Dictionary/, Cache/
```

`app-settings.json` 全量（553B）：

```json
{
  "skipTlsVerification": true,
  "autoApproveEdit": false,
  "trayMinimizeHintShown": false,
  "useSystemProxy": false, "proxy": "", "proxyUsername": "", "noProxy": "",
  "work.autoApproval": "{\"bash\":false,\"sandboxCommand\":true}",
  "isInitSandbox": true,
  "GLOBAL_AGENT_PERMISSION": {},
  "customModels": [],
  "petEnabled": false, "petName": "cola-dark", "petSize": 120,
  "notificationEnabled": true,
  "workspaceRootDir": "", "fontScale": "default",
  "closeBehavior": "exit", "autoLaunch": false, "lockScreenRemote": false
}
```

信号：`skipTlsVerification: true`（默认关 TLS 校验——运行时流量分析注意证书选择）、`isInitSandbox` + `work.autoApproval`（bash 默认不自动批准、sandboxCommand 默认批准）→ 命令审批/沙箱体系是后续重点；`petName: cola-dark`（桌面宠物，tray 面）。

### 5.2 %APPDATA%\codearts-agent\（VS Code 型用户数据）

```
codearts-agent/
├── User/
│   ├── 9be97f23-9ece-42df-b336-c2fdd1661a9d/   # profile 实例
│   ├── History/, snippets/
│   ├── chatLanguageModels.json, chat_sessions/   # ★ AI 会话落盘（待 schema 化）
│   ├── globalStorage/, workspaceStorage/
│   ├── settings.json, settings-backup.json, logs/
├── machineid                         # 9be97f23-…（与 profile 实例 id 同形）
├── languagepacks.json                # 15.9KB（安装后更新过 09-30）
├── CachedExtensionVSIXs/, CachedProfilesData/, clp/, logs/
├── Crashpad/, WebStorage/, Service Worker/
└── （标准 Chromium profile 目录族）
```

### 5.3 ~/.codearts-doer-for-coding/（dataFolderName）

`argv.json` + `extensions/`（用户侧扩展安装落点，当前仅这两个条目）。

### 5.4 更新链

- `%LOCALAPPDATA%\codearts-updater\installer.exe`（238MB 全量安装包，2026-09-17）
- 安装内 `tools/inno_updater.exe`（Inno 增量/全量执行器）
- 更新源待运行时确认（推断走 codearts.huaweicloud.com 门户域或 ide.cn-north-4 域）。

## 6. 与 ZCode 的结构同构点（方法复用清单）

| ZCode 文档 | CodeArts 对应 | 复用方式 |
|---|---|---|
| 43 号「开源版差异面」 | out/vs + product.json diff | 同方法：对社区 VS Code 26.9.x 做闭源符号/模块 diff |
| 12 号「阿里云验证码」 | secretStorage.enc + 登录流程 | 登录态与凭证加密对照（DPAPI/AES） |
| 08 号「聊天对话接口」 | codearts-snap 网络面 | mitm 流量分析（tools/mitm/ 复用） |
| 14 号「本地存储全 schema」 | taskboard.db + chat_sessions | sqlite dump + 逐表 schema |
| 05 号「V4 请求签名」 | 请求签名/鉴权头 | 对照是否有签名门（3012 族类似物） |

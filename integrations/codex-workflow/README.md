# Codex Workflow CDP 接入

控制路径：聊天末尾 Panel → Core HostAction → Assistant Bridge 管理的 dispatcher → CDP → 原 Codex 聊天。
执行路径：Agent → LazyMind MCP → Core。沿用[外部 Agent 交互协议](../../docs/architecture/external-agent-workflow-protocol-draft.md)。

## 连接与使用

在 LazyMind 助理中开启 Codex 开关，按与 WorkBuddy 相同的教程连接。Codex 无需信任步骤。保存当前工作后确认重启，由接入程序通过正常应用启动方式开启本机 CDP，默认端口为 9334。

首次开启同时配置持久启动入口。macOS 创建用户目录中的 `Codex (LazyMind).app` 并替换原 Codex Dock 项（没有时添加）；Windows 创建用户桌面和开始菜单入口，并更新用户桌面、开始菜单、任务栏目录中指向 Codex 的普通 `.lnk`。这些入口在 LazyMind 未运行时也能携带启动参数；Windows Store 的版本目录在启动时重新解析。不修改 Codex 安装包。

直接打开原始应用、系统管理的 Store 应用磁贴、其他用户或系统级快捷方式，以及带自定义工作区参数的入口不在覆盖范围内。已运行的非 CDP 实例不会被启动器强制重启。配置界面检测安装状态、启动配置、应用进程、端口归属和接入状态；应用未运行时显示等待，异常时保持开关开启并提示关闭后重新开启，不提供独立的修复按钮。

在 Codex 新聊天启动 Workflow，Panel 自动出现在聊天末尾，可审核、继续、停止、恢复、重试和回退。停止只中断已确认属于该 Workflow 的当前轮次，不中断用户的新任务。MCP 不再返回面板链接，也不要求 Agent 打开网页；面板由宿主接入程序管理。

Codex 的主页面 CSP 不允许普通本地 iframe。接入程序通过 CDP 调用宿主现有浏览器服务，注册一个独立容器；其中加载与 WorkBuddy 共用的页面。容器跟随聊天尾部位置，滚出可视区域时隐藏，展开收起保留同一页面。不开启 MCP App，不修改 Codex 安装文件或关闭 CSP。

第一阶段的临时验收 Workflow 和操作步骤见[验收说明](../../docs/development/fixtures/codex-cdp-acceptance/README.md)。

## 生命周期与边界

- Assistant Bridge 管理 CDP worker，独立于 MCP 连接。只有用户关闭开关才撤销持久启动配置；账号异常停止控制但保留开启选择。关闭时恢复仍由 LazyMind 管理的原入口，保留用户后续修改，并清理面板容器，不退出 Codex；当前进程的 CDP 端口在应用退出后才关闭。
- MCP 提供 Workflow skill 和执行接口，标准输出仅用于 MCP 通信。
- 配置位于 `LAZYMIND_HOME/codex-workflow/config.json`，worker 日志位于 `LAZYMIND_HOME/logs/codex-workflow.log`。
- 会话绑定优先使用宿主元数据；缺失时由 Agent 转交当前执行环境的真实 `CODEX_THREAD_ID`，不能猜测其他聊天。
- 发送前保存动作标识，接收后保存原生轮次 ID。回执不确定时不自动重发，不伪造接收成功。
- CDP 通信复用 WorkBuddy 提取的公用实现；macOS 和 Windows 的应用启动逻辑分别适配、分别验收。
- 同一后台进程的各聊天窗口分别挂载 Panel，包括主窗口和独立新窗口。控制动作按绑定的聊天 ID 查找原生 owner，独立于当前前台窗口；不在不同窗口间重发不确定的控制请求。

## 一键安装验收

Codex 与 WorkBuddy 遵守协议 §5 的相同行为。WorkBuddy 保留原生 `WORKBUDDY_REMOTE_DEBUGGING_PORT` 方案：Windows 写入用户环境并通知桌面；macOS 写入当前启动环境，并安装仅负责登录后恢复环境的 LaunchAgent，不启动 WorkBuddy 或 LazyMind。账号失效不撤销这些设置。macOS 登录时自动恢复的应用可能早于环境任务启动，必须在实机验收中检查这一顺序；若当前进程未继承设置，配置界面应提示异常。

| 检查 | 验收方式 |
| --- | --- |
| 安装与重复开启 | 所需组件启用，入口不重复，已有无关设置保留。 |
| 持续生效 | 退出应用后从已配置入口重开；退出 LazyMind 后重开；重启电脑后首次打开应用。 |
| 异常检测 | 绕过启动入口、修改/删除配置、占用端口，确认不假报已连接且开关仍开启。 |
| 账号变化 | 控制操作停止，开启选择和持久启动设置保留。 |
| 恢复与关闭 | 关闭后重新开启可恢复；正常关闭撤销自有设置，外部编辑不被覆盖。 |

自动测试使用临时目录和模拟 Dock；Windows 测试包含隔离目录内的真实 Shell COM 快捷方式生成。交叉编译不替代 Windows 运行、任务栏行为或重启电脑的实机验收。

## 开发

```bash
pnpm --dir integrations/codex-workflow typecheck
pnpm --dir integrations/codex-workflow test
pnpm --dir integrations/codex-workflow build
```

构建同时更新 Go CLI 内嵌的 dispatcher 和共用页面。第一阶段原生控制已通过用户验收；第二阶段的[嵌入 Panel 验收](../../docs/development/codex-panel-acceptance.md)待用户完成。

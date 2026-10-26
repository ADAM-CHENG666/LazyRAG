# Codex Workflow CDP 接入

控制路径：聊天末尾 Panel → Core HostAction → Assistant Bridge 管理的 dispatcher → CDP → 原 Codex 聊天。
执行路径：Agent → LazyMind MCP → Core。沿用[外部 Agent 交互协议](../../docs/architecture/external-agent-workflow-protocol-draft.md)。

## 连接与使用

在 LazyMind 助理中开启 Codex 开关，按与 WorkBuddy 相同的教程连接。Codex 无需信任步骤。保存当前工作后确认重启，由接入程序通过正常应用启动方式开启本机 CDP，默认端口为 9334。

从系统入口普通重开 Codex 不会保留 CDP 参数，需要在助理中重新连接一次。断线时显示恢复入口，并在创建受控 Workflow 前明确报错；接入程序不自动重启正在工作的 Codex。

在 Codex 新聊天启动 Workflow，Panel 自动出现在聊天末尾，可审核、继续、停止、恢复、重试和回退。停止只中断已确认属于该 Workflow 的当前轮次，不中断用户的新任务。MCP 不再返回面板链接，也不要求 Agent 打开网页；面板由宿主接入程序管理。

Codex 的主页面 CSP 不允许普通本地 iframe。接入程序通过 CDP 调用宿主现有浏览器服务，注册一个独立容器；其中加载与 WorkBuddy 共用的页面。容器跟随聊天尾部位置，滚出可视区域时隐藏，展开收起保留同一页面。不开启 MCP App，不修改 Codex 安装文件或关闭 CSP。

第一阶段的临时验收 Workflow 和操作步骤见[验收说明](../../docs/development/fixtures/codex-cdp-acceptance/README.md)。

## 生命周期与边界

- Assistant Bridge 管理 CDP worker，独立于 MCP 连接。关闭开关会停止接入并清理面板容器，不退出用户的 Codex。
- MCP 提供 Workflow skill 和执行接口，标准输出仅用于 MCP 通信。
- 配置位于 `LAZYMIND_HOME/codex-workflow/config.json`，worker 日志位于 `LAZYMIND_HOME/logs/codex-workflow.log`。
- 会话绑定优先使用宿主元数据；缺失时由 Agent 转交当前执行环境的真实 `CODEX_THREAD_ID`，不能猜测其他聊天。
- 发送前保存动作标识，接收后保存原生轮次 ID。回执不确定时不自动重发，不伪造接收成功。
- CDP 通信复用 WorkBuddy 提取的公用实现；macOS 和 Windows 的应用启动逻辑分别适配。本期仅在 macOS 实机验收。
- 同一后台进程的各聊天窗口分别挂载 Panel，包括主窗口和独立新窗口。控制动作按绑定的聊天 ID 查找原生 owner，独立于当前前台窗口；不在不同窗口间重发不确定的控制请求。

## 开发

```bash
pnpm --dir integrations/codex-workflow typecheck
pnpm --dir integrations/codex-workflow test
pnpm --dir integrations/codex-workflow build
```

构建同时更新 Go CLI 内嵌的 dispatcher 和共用页面。第一阶段原生控制已通过用户验收；第二阶段的[嵌入 Panel 验收](../../docs/development/codex-panel-acceptance.md)待用户完成。

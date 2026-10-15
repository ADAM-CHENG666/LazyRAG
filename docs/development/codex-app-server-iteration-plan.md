# Codex Workflow 共享 App Server 迭代计划

分支：`workflow/codex_improve`。建立日期：2026-10-12。更新日期：2026-10-15。

当前结论：共享后台运行组件与原生 Continue/Stop 已完成工程集成及本机部署。2026-10-15 用户完成部署后的测试并反馈“没有问题”，本轮最小业务验收通过。异常恢复、长期运行及跨版本兼容仍属后续工作；不再要求逐场景连续 3 次或完整异常矩阵。

## 目标与协议边界

桌面通过 `CODEX_CLI_PATH` 启动外部适配器，由适配器将聊天协议转发给独立共享 App Server，并启动原装桌面工具与浏览器插件。后台通过工具代理访问当前桌面的桥接；LazyMind Connector 连接同一后台使用原生 RPC。保留 [外部 Agent 协议](../architecture/external-agent-workflow-protocol-draft.md) 的 Core 权威状态、MCP 执行、HostAction 投递及配对身份。

适配器路径使用 `CODEX_APP_SERVER_FORCE_CLI=1`，清除外部 WebSocket 覆盖，并核对其他 CLI 配置的优先级。`CODEX_APP_SERVER_WS_URL` 直连仅保留为研究背景，不作为本计划的完整桌面接入方案。

首个业务验收目标：在原桌面聊天中启动 Workflow，执行一步后进入人工审核，结束自动推进；Panel Continue 在同一聊天恢复；Stop 撤销 Core 授权并中断确属该 Workflow 的 turn；之后用户仍能正常交流。

外部 Agent 助理页面保留与其他助理一致的“已安装”表达，依据 Codex 本体的实际安装结果。Workflow 插件属于 Codex CLI 的内部功能依赖，打开“LazyMind 调用 Codex CLI”时安装或更新，不在助理卡片上单独显示插件安装状态。“Codex 桌面端使用 LazyMind MCP”保留独立的接入开关。

当前验证基线：桌面 26.1007.30912、内置 CLI `0.162.0-alpha.17.2`。2026-10-15 已通过真实 Desktop 启动适配器、桌面工具调用、内置浏览器打开与读取标题、真正退出保留后台、原图标重开及同一聊天工具恢复。后续已通过程序打开原装 App 的冷启动 2 次、正常重开复用 1 次，以及 3 个并发启动请求只创建一个后台的隔离回归；异常恢复、真实多客户端并发和跨版本兼容尚未验证。详细范围见 [共享后台测试计划](codex-desktop-shared-server-test-plan.md)。

早期研究已验证同后台、同 thread/turn 的查询与 `turn/interrupt`；中断不保证终止已启动的命令。Workflow 的 `turn/start`、`turn/steer`、取消归属和多客户端事件/审批路由仍按业务步骤单独验收，不能用桌面连通性测试代替。

环境变量连接桌面的方式属于未公开能力。固定版本的生成 schema 是实现基线，不能用最新文档中的字段推定该版本一定支持。连接现有后台与管理后台生命周期分离，不因 MCP/Connector 退出终止桌面正在使用的 Server。RPC 结果不明时不重发，也不切换到 queue 重发。

## 下一阶段工程任务

目标：一次性配置后，用户只需使用原图标和正常退出操作；后台自动准备或复用，桌面工具与内置浏览器随当前 App 正常关联。不修改 Codex 应用包、图标或签名，不放宽安全校验。

| 任务 | 工作范围与交付 | 验收标准 | 状态 |
| --- | --- | --- | --- |
| P1. 运行组件集成 | 将适配器、启动锁、后台身份检查和工具代理迁出实验目录；构建独立包；参数化 App、profile、插件及运行目录 | 构建可复现、无实验目录依赖；启动/复用及协议转发的针对性回归通过 | 已实现：`integrations/codex-workflow/desktop-runtime` |
| P2. 配置与恢复入口 | 一次性准备、变更预览、启用、状态与恢复默认；登录服务仅设置 GUI 环境，后台由 App 打开时拉起 | 保留原环境；不覆盖其他配置；不修改 App/签名；准备不改变当前运行状态 | 已实现并部署，启用已核对；最终包停用及重新登录行为未专项复验 |
| P3. 工程版正常流程确认 | 使用最终安装目录，从原图标冷启动、正常退出并重开原聊天，再恢复默认 | 一次完整正常流程中后台复用、桌面工具和 IAB 实际可用 | 已部署，用户反馈测试正常；不据此扩展异常恢复或回滚验收范围 |

执行顺序：运行组件、部署与最小业务验证已完成；按实际问题推进步骤 2、4–6 的剩余项目。异常退出、后台崩溃、在途任务和压力测试作为后续可靠性工作，不阻塞当前开发。第二客户端在接入实际 Connector 时做一次真实操作验证。

本阶段不要求重开后自动选中上次聊天；正常打开原聊天即可继续。跨版本兼容和长期稳定性不计入本阶段已通过范围；升级后须重新验证。若需修改 App 或放宽校验才能继续，则停止该方案。

## 执行顺序与验收标准

| 步骤 | 工作与交付 | 验收标准 | 当前状态 |
| --- | --- | --- | --- |
| 1. RPC 连接与观测 | Unix Socket WebSocket 客户端；初始化；类型化查询、start/steer/interrupt；通知/Server request 分流；有界超时和只读诊断入口 | 本地真实 Socket 测试覆盖初始化、并发响应、通知、RPC 错误、断线/超时/取消、错误 endpoint；连接试验后台并只读核对版本与线程状态，不 resume、不启动/中断用户 turn | 已通过 |
| 2. 助理检测、CLI 功能准备与展示兼容 | 先完成下文 F1–F4；分离 MCP 失败原因；实际调用 LazyMind 工具；Panel 外部浏览器降级；验证外部发起 turn 的审批/输入路由 | 默认桌面检测、CLI 功能启用及安装失败处理通过；LazyMind MCP initialize/tools/list 及实际只读调用成功；Panel 打开并能刷新；缺少桌面工具时明确降级；不静默吞掉无人应答的审批；区分修复证据与尚待用户桌面验收 | 进行中 |
| 3. 原生 Continue/Stop | RPC Adapter；明确的 run/thread/turn/action 关联；指定 turn 取消能力；复用 HostAction claim/settle | Continue 在原 thread 启动一轮；重复领取不重复发；带 execution_id 正确 claim；Stop Core 先行且目标 turn interrupted；延迟取消不伤新用户 turn；busy/thread 消失/RPC 不明有明确处理 | 已部署；协议回归通过，用户反馈本轮 Continue/Stop 测试正常 |
| 4. 审核与执行收尾 | 关联 MCP 结果与原生运行；协调 awaiting_user/awaiting_executor/draining；内部执行完成唤醒 | 人工步骤先执行后审核；审核时停止自动推进、不代审/轮询；内部执行不给外部 handle；已授权执行在 draining 时可收尾；启动前提问保持等待真实回答；完成后返回产物 | 待执行 |
| 5. 投递恢复与运行观测 | 持久化原生回执；断线后查询核对；Panel 显示投递与 turn 状态；验证命令终止范围 | 发送后回执丢失、Connector 重启均不重复推进；确切历史证据可恢复 accepted；无证据保持 unknown 并提供实际可用恢复路径；分别呈现 Workflow 停止、turn 中断与工具进程状态 | 待执行 |
| 6. 安装与发布集成 | 将 P1–P3 成果接入 CLI 功能开关与安装流程；完善共享模式诊断、版本检查和恢复默认；更新嵌入包与指南；DSH 回归 | 构建与安装包一致；CLI 关闭后停止该功能的自动调度；MCP 断开不停止共享 Server；既有配置/配对保留；默认/共享切换和升级失败可恢复；真实桌面业务全链路及 DSH 回归通过 | 待执行 |

每步验收通过后顺序推进；未通过的依赖保留明确失败证据，不将 mock 通过记录成真实桌面通过。可独立完成的代码与验证继续推进。

## 步骤 2 先行修复：助理检测与 CLI 功能准备

更新日期：2026-10-12。按 F1 → F2 → F3 → F4 执行，完成后继续原生 RPC 接入。

### 已确认的异常

- 现有 macOS 可执行文件发现只覆盖应用包中较浅的 MacOS/Resources 目录，遗漏当前桌面内置 CLI 的嵌套路径。
- Codex Status 将 `desktop.Installed && CLI可发现` 用作桌面安装条件，因此 CLI 发现失败误报桌面未安装、未完成首次启动。
- 插件已启用分支仍可返回 `state=enabled`；前端再据此显示“已安装”，与上述 requirements 冲突。两个开关表示启用意愿，不能证明本体安装或功能就绪。
- 桌面页面优先通过 Electron IPC 调用本地 CLI，MCP/Assistant Bridge 可继承显式的内置 CLI 路径。显式路径安装和 MCP 调用成功，不等于页面默认检测通过。当前没有持久的 codex-cli 路径绑定。

| 顺序 | 修复工作 | 验收标准 | 状态 |
| --- | --- | --- | --- |
| F1. 统一路径解析 | 复用已有 codex-desktop/codex-cli 绑定；在所选 Codex 桌面包内发现新旧内置 CLI 布局；安装、桌面 IPC 检测、Assistant Bridge 检测及执行器共用解析入口，保持显式绑定优先 | 不传临时 `--agent-bin` 或环境变量也能发现当前内置 CLI；显式绑定优先且错误绑定如实报错；默认检测和实际调用解析到同一文件；旧布局仍可用 | 已通过 |
| F2. 拆开状态与显示依据 | 桌面安装/初始化独立于 CLI 发现；CLI 安装、登录和执行可用性各自检测；顶部统一“已安装”直接依据本体安装结果；开关启用状态独立保存；区分检测结果冲突与真正宿主离线 | 桌面存在而 CLI 缺失时，只在 CLI 区域显示缺失；桌面存在但未初始化时准确提示首次启动；插件残留或开关开启不能使缺失的本体显示已安装；当前页面重新检测后与实际调用一致 | 已通过 |
| F3. 调整插件安装触发 | “LazyMind 调用 Codex CLI”启用流程先检查 CLI，再幂等安装/更新 Workflow 插件与配对，成功后提交 enabled 并刷新状态；Electron IPC 与 Assistant Bridge 的启用入口复用同一流程；桌面 MCP 开关独立控制基础 MCP 接入 | 点击 CLI 开关即完成内部插件准备；重复启用/重试/升级不重复配对；首次安装失败时不开启 CLI，显示可重试原因；已有启用状态下升级失败如实报告且保留原可用安装；插件状态不新增为独立卡片或标签 | 已通过 |
| F4. 迁移与桌面回归 | 兼容现有插件及两开关配置，检查配置归属；基础 MCP 接入与 Workflow 功能可共享传输配置，但避免重复同名 lazymind MCP 条目；关闭 CLI 只禁用对应执行/自动调度；更新实际桌面产物验收 | CLI 与桌面 MCP 两开关的四种组合行为明确；桌面 MCP 关闭不卸载仍由 CLI 使用的插件，CLI 关闭不关闭基础 MCP；已有设置、其他 MCP/插件及配对保留；唯一 /Applications 桌面包的页面、安装、MCP 和执行器检测全部验证；其他外部 Agent 显示与开关行为回归通过 | 已通过 |

预计涉及 `agentexec` 路径发现、Codex Adapter 状态和接入动作、CLI/Assistant Bridge 执行器启用入口、`codexplugin` 安装生命周期，以及前端 AgentIntegrationPage 的状态来源与相关回归测试。业务开关与准备流程放在接入层，executorpolicy 继续负责持久化用户启用意愿。

F1–F4 已实施并完成下面的实际桌面验收。RPC 业务控制仍按步骤 2–6 继续，不能把本次助理页面修复当作原生 Continue/Stop 已上线。

## 设计约束

- `continue` 仍然是查看最新 Core 状态的通知，不是执行权限。执行仍由 begin/claim/publish/complete 校验。
- thread 绑定不意味着该 thread 的所有 turn 都属于 Workflow。初次用户 turn 的归属需要可信 MCP 上下文与原生记录证明；后续原生 start 返回的 turn ID 可直接关联。普通用户交流不得继承取消。
- `turn/start` 与用户同时操作的竞争、是否可能变成同轮追加，需要专项验证；不把“先查询空闲”当成跨客户端原子保证。
- 原生回执记录 thread/turn/input 标识，不伪造 DSH 的持久事件序号。`clientUserMessageId` 的去重与查询语义需验证。
- 连接 RPC 不自动获得 DSH 执行前钩子。审核协调必须处理 MCP 提交成功但响应/事件未送达、并行执行收尾与用户交互。
- `command/exec/terminate` 的 processId 在固定版本中属于连接范围，不能推定它能终止 Agent 工具启动的命令。底层命令停止单独验收。
- 不自动批准 Codex 原生权限请求或 Workflow 审核；多客户端请求归属和应答路由必须核实。
- Workflow 插件由 CLI 功能准备流程管理；助理安装状态依据本体，MCP 接入状态、CLI 启用意愿、插件准备和运行可用性分别处理。

## 验收记录

按日期记录测试命令、通过数量、脱敏结果、未通过项及后续依赖。真实后台诊断不保存聊天全文或凭据。

### 2026-10-15：适配器最小端到端验证

- Desktop 实际启动适配器，多个适配器连接同一独立后台；未发现额外的 Desktop 私有聊天后台。原装浏览器自用的辅助 App Server 不计为重复聊天后台。
- 同一专用聊天在退出重开前后均成功调用 `list_projects`（21 个项目），并通过 IAB 打开测试页面、读取 `Example Domain` 标题。
- Desktop 主进程真正退出，后台 PID 44005、启动时间和命令保持不变；用户点击原图标重开后接回该后台。重新打开原测试聊天后工具恢复。
- 未修改应用包、签名或安全检查；临时配置与环境已恢复，测试后台及适配器已停止。本次后台提前启动，不能作为冷启动自动准备的证据。
- [报告](/private/tmp/codex-adapter-live-20261015/REPORT.txt)、[调用结果](/private/tmp/codex-adapter-live-20261015/verified-tool-results.json)、[清理记录](/private/tmp/codex-adapter-live-20261015/FINAL-CLEANUP.json)。完整验收边界见测试计划；临时证据与代码纳入 P1 归档。

### 2026-10-12：步骤 1

- `pnpm --dir integrations/codex-workflow typecheck` 通过。
- `pnpm --dir integrations/codex-workflow test`：2 个文件、18 项通过，其中新 Unix Socket RPC 测试 11 项。真实 Socket 测试需要允许本地监听；沙箱内运行因 `listen EPERM` 失败，获准运行后通过。
- `pnpm --dir integrations/codex-workflow build:diagnose` 通过。诊断入口只执行 initialize、thread/read 和 mcpServerStatus/list。
- 新客户端连接原试验 Unix Socket，Server 自报版本 `0.162.0-alpha.17.2`、profile 匹配；原试验 thread 为 idle，历史中的目标 turn 为 interrupted，其后的用户 turn 为 completed。未调用 resume/start/interrupt。
- MCP 工具数量：`lazymind=0`、`codex_app=0`、`node_repl=4`。当前诊断未声明 experimentalApi，因此 runtimeStatus 为 null，不将 null 解读为具体失败原因。步骤 2 继续诊断。

### 2026-10-12：桌面环境统一与步骤 2 诊断

- 发现两份完整主应用，均为 `0.3.0-alpha.0`；较新的本地构建原位于 `desktop/dist/mac-arm64/LazyMind.app`，旧包位于 `desktop/dist/backups/before-workbuddy-20261010-143814/LazyMind.app`。并未将它们的相同版本号当作相同构建，比较了包内程序更新时间。
- 旧应用的正常 Quit/SIGTERM 会隐藏窗口并继续驻留；核验旧主进程路径后结束其主进程，runtime guard 随后完成后端清理。独立共享 Codex PID 73521 未退出。
- 仅保留 `/Applications/LazyMind.app` 主应用，旧包移入用户废纸篓。保留包的 CLI 内容散列在移动前后相同。录制辅助程序属于独立组件，不作为另一份主应用删除。
- 重新连接已有 Codex 插件，`.mcp.json` 与安装记录的 self 均指向 `/Applications/LazyMind.app/Contents/Resources/runtime/bin/lazymind`，配对保留。
- 桌面后端 ready，auth/core/frontend/local-proxy/chat 均 running；使用原 `~/Library/Application Support/LazyMind/data`，未迁移或删除应用数据。
- 原 MCP 失败通过直接 proxy 模式复现为桌面后端 `localhost:8090` 不可达，不证明共享模式导致。统一桌面启动后，实际 MCP initialize、tools/list（32 工具）及只读 `workflow.list` 均成功。
- 暂未将桌面 MCP 配置缓存刷新、内置浏览器恢复或原生审批路由记为通过；后续仍需对应验收。Docker 不作为本次验收后端。


### 2026-10-12：F1–F4 实施与桌面验收

- 默认发现新增 ChatGPT/Codex 桌面嵌套 CLI 布局；新旧三种布局及显式绑定优先级均有回归。已有 CLI/桌面绑定优先于插件保存的环境提示，错误绑定不回退到其他可执行文件。
- 桌面 requirements 与 CLI 分离。Codex 顶部标签依据本体；新增可选 `enabled` 保存 MCP 启用意愿，独立于 readiness。执行器安装/就绪的 false 值不再被 JSON 省略，避免前端回退到过期 Host 结果。其他助理的顶部行为保持原样。
- Electron IPC 与 HTTP Bridge 共用 executorintegration 准备流程；CLI 登录检查、插件/配对准备成功后才保存启用状态。重复启用不重复安装。更新源文件先暂存再切换，失败时恢复原目录和权限；首次失败保留可重试的归属记录。
- 传输拆为基础 `lazymind` 与内部 `lazymind-workflow`，分别暴露基础和 Workflow 工具。后者按 CLI 标记动态控制已有连接、dispatcher 生命周期；Bridge 也检查标记，避免旧客户端继续领取/绑定。CLI 关闭不撤销持久配对、不停止共享 App Server。
- `go test ./...` 全部通过，涉及 Codex、Cursor、WorkBuddy、DSH 和通用 MCP；补充旧客户端禁用保护后，相关四包再次通过。前端 AgentIntegrationPage 与 desktopBridge 共 43 项通过；Electron 接入超时测试 3 项通过；桌面前端构建成功。
- 全量 `pnpm typecheck:all` 尚有 710 个已有错误。将改动的前端实现文件临时恢复为 HEAD 对照并在 finally 恢复工作区后，基线也是同一组 710 个错误；本次未新增。详细日志在 `/private/tmp/lazymind-codex-typecheck{,-baseline}.log`。
- 唯一 `/Applications/LazyMind.app` 已更新 CLI、前端和接入动作超时，并通过 `codesign --verify --deep --strict`。旧产物备份在忽略目录 `desktop/build/codex-improve/installed-backup`，没有另建完整 app。更新时保留原 shell 的既有 cloud-oauth 派生密钥设置，仅对 shell 应用两处所需补丁。
- 强制退出首次被自动审批拒绝，原因是未保存界面状态风险。随后通过普通 Cmd+Q 关闭所有窗口，工具确认 noWindowsAvailable，进程表确认无 Renderer；源码证实普通 Quit 的设计是驻留。提供这组证据后，只结束无界面的旧主进程并正常关闭 Bridge 获准；未结束 Codex 共享 Server。
- 实际新版页面显示 Codex 桌面准备、CLI 准备均已完成，安装与登录全部绿色；没有独立插件卡片。默认 CLI 检测不用临时路径，installed/ready/bridge_state 均为 true/true/ready。WorkBuddy 两开关正常，Cursor 桌面存在但 CLI 缺失仍准确显示，DSH 顶部和开关保留原行为。
- 在真实页面完成两开关四组合并恢复原来的双开状态：双开时基础工具 16、Workflow 工具 16；关闭桌面 MCP 时只移除基础配置，Workflow 的已有连接仍为 16；同时关闭时 Workflow 的已有连接为 0，旧客户端 Actions 被 403 CODEX_CLI_DISABLED 拒绝；仅基础 MCP 开启时基础工具仍为 16，model.list 调用成功，Workflow 为 0；重新启用后已有连接恢复 16。
- 实际 initialize、tools/list、Workflow 入口 workflow.list、基础入口 model.list 成功；工具集合不重复。原 connector_id `host-c1f087a4579ebcb63505fd610ba2f301` 保留，反复开关未重新配对。尚未在本次修复中发送原生 turn/start/steer/interrupt，队列业务控制仍等待后续迭代。


### 2026-10-12：external-controller-stop-test 的真实失败基线

用户测试 run：`mcp-1c6db29f537926b7ff258ccf096567c0`。本次检查只读，没有恢复 run、重放 cancel 或中断聊天。

- Core binding 已指向 Codex thread `01a12f01-e372-7ef3-aaae-4a0a295d6700`，provider=codex，connector_id 为原配对。
- Stop 在 15:29:10（UTC+8）创建 cancel HostAction `130649a1-a7b2-402b-b497-b36d95842ed3`。当前 continuation=stopped、admission.can_begin=false、reason=session_stopped、active_executions=0。
- 该 cancel 投递 status=failed，last_error 为 `This host does not support interrupting the current turn; Workflow is stopped in Core.`。部署的 dispatcher 仍声明 queue 模式和 cancellation=none，未调用原生 turn/interrupt。
- 对应第二个 turn `01a12f03-9688-78f2-bc51-479f4208abaa` 在 15:28:46 开始，15:29:32 以 completed 正常结束，晚于 Stop 约 22 秒；App Server 查询也返回 completed，没有 interrupted。第一个 turn 同样 completed，thread 当前 idle。
- 该案例证明步骤 3 尚未接入运行链路，不能归因为“turn 已中断但命令没停止”。后续以新一轮相同测试验收原生 Stop；保留这次失败记录，不对已结束的 turn 补发中断。

### 2026-10-15：生命周期续验（准备阶段）

- 已核对分支 `workflow/codex_improve`，保留已有修改，未提交。当前只有控制聊天运行，Cursor 后台与既有浏览器辅助后台不属于测试清理范围。
- 磁盘 Desktop 已为 `26.1007.71326`，CLI 仍为 `0.162.0-alpha.17.2`；与历史 Info.plist 版本不同。本轮没有安装或更新，历史通过不直接算作本轮通过。
- 已复用成功代码补充启动锁、后台进程/socket 身份核对、有界失败、每实例桥接登记、父进程失效清理和代理重连。在途工具请求断线报告结果未知，不重发；不同 Desktop 上下文组拒绝任意选路。尚未宣称可靠的多实例/跨主机路由。
- 隔离传输回归 **5 项通过**；辅助 CLI `--version` **1 次通过**。这些不是实际 Desktop 验收。原生 Dock 读取 **2 次超时失败**，应用/浏览器清单读取成功；没有绕过审批。
- 本阶段真实冷启动、正常/异常退出、后台故障、多聊天/第二客户端、在途任务、回滚各项通过数均为 **0**；独立验收程序已准备但本条记录不代表已启动或已通过。最终实际次数以运行目录的 `RESULT.json` 与后续补录为准。
- [代码快照](/private/tmp/codex-adapter-cycle-20261015/PROGRESS-REVIEW.md)、[当前报告](/private/tmp/codex-adapter-cycle-20261015/REPORT.txt)、[检查点](/private/tmp/codex-adapter-cycle-20261015/CHECKPOINT.txt)。原历史目录保留不变，原配置备份仅保存在本机临时目录。

### 2026-10-15 15:45–15:53：首次冷启动失败及修正

- 真实冷启动 **执行 1 次，失败 1 次，通过 0 次**。用户点击原图标，Desktop 9908/10372 实际启动适配器，适配器自动创建后台 9939；尚未接通，未执行桌面工具/IAB。本轮重复启停及并发验收未执行。
- 确认根因是本轮协调器的端点类型判断错误：原版 CLI 在指定路径创建符号链接，目标为同 UID 原生目录中的 Unix socket。直接用 `lstat` 要求端点本身是 socket 导致误拒绝。用户看到“无法加载组织设置”弹窗；不能据此认定是网络故障，也未放宽组织/安全检查。
- 15:48:19 恢复程序还原 4 个 GUI 环境项并重开默认 Desktop 10923；配置散列与快照一致，测试后台已停止。后续成功打开属于自动回滚，不是共享模式恢复。
- 另发现 `launchctl submit` 在 runner 退出后重拉起，watchdog 又先移除自身而漏清 runner 标签。已精确移除本次标签，改为 `RunAtLoad=true / KeepAlive=false` 的临时任务，增加运行状态与互斥检查，按 runner→watchdog 顺序清理。重复拉起受到原进程身份检查阻止，没有再次退出新 Desktop。
- socket 修正保留链接与目标的 inode/UID/权限、原生目标目录及监听 PID 核对。针对该失败的 3 个并发启动请求 **1 次回归通过**：全部复用 PID 12904、仅 spawn 1 次；随后已停止。此项为针对性协调器回归，不能计入真实 Desktop 场景次数。
- [失败报告与诊断](/private/tmp/codex-adapter-cycle-20261015/attempts/01/DIAGNOSIS.txt)、[原始日志](/private/tmp/codex-adapter-cycle-20261015/attempts/01/lifecycle.jsonl)、[清理证据](/private/tmp/codex-adapter-cycle-20261015/attempts/01/FINAL-CLEANUP.json)。本次没有审批拒绝；结果是实现失败，不能标为“被审批阻止”。

- 首次失败后的默认模式回滚验收 **1 次通过**：专用聊天 turn `01a13e8e-e2d5-7270-90c0-a3bede6ef595` 实际调用 `list_projects` 返回 21 个项目；IAB `createBrowserTab` 后另一次 `listTabs` 读取 `Example Domain`。此项属于回滚恢复，不计入共享后台冷启动通过次数。

### 2026-10-15 16:01：第二次等待与启动方式调整

- 第二次正常退出后等待 180 秒，无适配器启动记录；16:01:30 自动回滚，原配置散列一致，无测试后台/适配器残留。此项为等待启动超时，不计为修正后冷启动通过或实现失败。
- 用户明确选择“先自动打开原装 App 跑完整循环，人工图标次数单独记录”。后续冷启动使用系统 LaunchServices 打开同一个原装应用，仍核对 Desktop→适配器→共享后台的真实路径；不把程序打开计为人工图标点击。
- 原生 UI 工具拒绝查看 `com.openai.codex`（安全限制），未换用其他 UI 技术绕过。该限制与此前 socket 检查失败分开记录。

### 2026-10-15 16:08：自动原装 App 冷启动首过

- 自动冷启动 **1 次通过**：原 Desktop 15655 消失后，LaunchServices 启动 Desktop 18207；其适配器自动创建后台 18240，多个适配器复用该后台，没有 Desktop 私有聊天后台。
- 专用聊天 turn `01a13e9a-fe35-7eb1-b840-221e9e808084` 中 `list_projects` 实际成功，IAB 打开本地测试页并读取唯一标题 `Adapter-1-cold-150ba5`；代理保留 thread/turn 元数据，桥接为适配器 18265。此项不是人工图标点击。
- 下一次退出前发现未知活跃 thread `01a13e9b-895c-7e30-b06e-5b80f8aba394`，保护检查阻止退出与后台清理；GUI 环境已恢复。稍后只读复核该 thread 已不再加载，未对它执行中断。增加最多 90 秒等待自然结束，仍不忽略未知活跃任务；只排除用户已明确授权可退出的本控制聊天。
- 仍未满足每项连续 3 次；原现场日志保留，等待继续自动循环。

- 16:14 续跑在清理原测试后台时触发 15 秒退出超时；恢复阶段第二次 SIGTERM 后后台退出，默认 Desktop 21698 恢复，配置与 GUI 环境一致。此间未启动新后台，不新增冷启动计数。清理等待改为 30 秒，超时再次核对归属与活跃任务后才发第二次终止信号，仍为有界失败。

### 2026-10-15 16:20–16:26：第二次自动冷启动与回执丢失核对

- 自动冷启动累计 **2 次通过**。16:20 本轮 Desktop 23733 自动创建并复用后台 23763，真实桌面工具及 IAB 唯一标题通过。
- 正常退出后 Desktop 24827 接回同一后台 23763；旧 MCP 代理 24099 连接新桥接 24874 并完成重新初始化。发送测试请求后，Desktop 的 thread/resume 更新了 MCP 会话，旧代理关闭，发送者收到 `Transport closed`；原脚本保守停止且没有重发。
- 事后只读核对确认 turn `01a13ea6-b8d7-7332-b71a-ebc2b9224d59` completed；真实 `list_projects` 返回 21，IAB 打开并另次读取 `Adapter-1-normal-reopen-0f9e7e`。因此 **正常退出重开/复用实际通过 1 次（回执丢失后核对确认）**，同时保留原脚本失败记录，不能伪称没有连接异常。
- 验收驱动修正为等待当前 Desktop 的真实 thread/resume 完成；发送回执不明只通过唯一标记和实际 completed 轮次核对，不重发。没有修改聊天身份或审批元数据。
- 16:22:57 默认 Desktop 26562 恢复，GUI 环境和配置散列一致，测试后台/适配器已停止。正常模式此前 3 次回滚工具/IAB 验收均通过；此轮恢复后仍需对应检查。
- [回执核对证据](/private/tmp/codex-adapter-cycle-20261015/attempts/04/reconciled-normal-reopen.json)。

### 2026-10-15 16:38：进度复核，未达到完整验收

- 当前自动启停程序已停止，未安排下一轮。最近一次在初始正常退出阶段等待 60 秒后报 `Desktop main did not quit`，尚未进入新冷启动，不增加通过计数；其原因未确认，不能归因于用户打开本聊天。
- 本轮累计实际通过：自动打开原装 App 的无后台冷启动 2 次；正常退出重开并复用原后台 1 次（含回执丢失后的只读核对）。均有真实桌面工具及 IAB 标题证据；尚无共享模式启停场景达到连续 3 次。人工图标冷启动执行 1 次失败，另一次等候超时未观察到适配器启动。
- App 异常退出、在途桥接断线、后台异常退出、多聊天与第二客户端、任务跨退出均未执行。隔离测试不能替代这些验收。
- 16:37 现场复核：原装 Desktop PID 30053 直接启动原 CLI 后台 PID 30103；无本轮测试进程。4 个 GUI 环境项均恢复原值，配置散列一致。上次 cleanup 快照里的 default_desktop 为空，现以本次进程核对补充，不以 restored 标志单独证明原模式已恢复。
- 当前主要拖延来自验收驱动的启动、退出、恢复和回执核对问题；后续应按单场景累计与复核，避免每次异常重跑整个矩阵。尚不具备进入完整安装器或 Workflow Continue/Stop 开发的验收依据。
- [本次现场复核](/private/tmp/codex-adapter-cycle-20261015/progress-review-verification.json)。

### 2026-10-15：工程集成与验收范围调整

- 用户确认按工程开发节奏推进；撤销每个场景连续 3 次及完整故障矩阵的前置门槛。实验正常流程证据足以进入集成，不宣称长期稳定。
- 新增独立 `desktop-runtime` 运行组件与 `build:desktop`，提供参数化准备、配置预览、启用/停用入口；构建包包含 WebSocket 依赖，保留 Python 标准库启动锁，不依赖临时实验脚本。
- 原版 CLI 的辅助调用保持转交；真实审批/聊天上下文透传。按实例桥接，断线不自动重发在途操作。原 queue dispatcher 和设置页功能未切换。
- 最近一次退出超时发生在 `lifecycle.py` 初始 `quit_app(original_app)`，对象是已恢复默认模式的 Desktop 26562；当时尚未进入共享模式。根因仍未定位，但不能用它证明适配器阻塞退出。
- 使用说明见 [运行组件](../../integrations/codex-workflow/desktop-runtime/README.md)。本轮不启用 GUI 覆盖、不重启当前 Desktop、不启动真实共享后台；最终版本的一次人工流程确认仍保留。
- 本轮验证：`pnpm build`、`pnpm typecheck`、原有 `pnpm test`（30 项）及 `pnpm test:desktop`（5 项）通过。Unix socket/进程查询用例需在允许本地 IPC 的环境运行；沙箱内的 EPERM 不计为功能失败。
- 已在独立临时目录检查准备产物、原 CLI `--version` 转交与 LaunchAgent plist 语法；GUI 环境未启用、未启动真实后台。当前磁盘 Desktop 为 `26.1007.71326`，CLI 为 `0.162.0-alpha.17.2`。[准备结果](/private/tmp/lazymind-codex-integration-ready-20261015/verification.json)。

### 2026-10-15：Workflow 原生反向控制接入

- dispatcher 根据 Codex profile 的 `lazymind-shared-server.json` 选择原生 RPC；启用/停用共享模式同步登记。登记缺席保留 queue，登记无效或原生连接失败不回退 queue。MCP 绑定使用同一登记声明 `cancellation=session`，旧绑定不自动迁移。
- Continue 复核 Core 控制状态、检查聊天空闲、持久化意图，通过 `turn/start` 提交并核对 client/turn ID。Stop 复核 Workflow 归属，向具体 turn 发送 `turn/interrupt` 并确认。已接受输入但后续核对失败记 unknown，不重发。
- 接通生产 dispatcher 的 `adapter.host` 所有权保护，更新 Go 内嵌 bundle 和插件指引。人工审核仍由真实用户完成；没有增加自动审批。外部发起轮次的 Desktop 审批/输入路由仍需真实业务确认。
- 回归发现并修复 worker 未跟随 Codex CLI 功能关闭的问题：关闭仅停止所属 dispatcher，重开功能再启动，不终止共享后台。
- 验证通过：TypeScript 类型检查、33 项测试；构建后 dispatcher 的本地 HTTP/Unix WebSocket Continue → Stop 链路 1 项；运行组件 6 项；Go `codexruntime`、`mcpbridge`、`codexplugin` 三个包测试。
- 本轮没有替换正在运行的 LazyMind CLI、没有启用共享模式或重启 Codex。下一步是部署新产物后的一次真实 Workflow 人工审核 → Continue → Stop 流程；设置页管理和完整安装集成另行完成。


### 2026-10-15：原生控制版本部署

- 已按用户要求部署到唯一 `/Applications/LazyMind.app`：更新包内 CLI（包含新版 dispatcher），保留已有界面与其他运行组件；按现有 ad-hoc 方式重新签名，`codesign --verify --deep --strict` 通过。
- Workflow 插件已更新为 `0.1.0+codex.6dd34175c9ac0db6`，原 connector 配对保持不变。现有安装器将独立 `lazymind` MCP 配置迁入插件；本次实际配置通过插件统一提供基础和 Workflow 工具（32 项），不同于此前分别配置两个入口的部署。只读 MCP initialize/tools/list 已通过。
- 共享运行组件安装在 `~/Library/Application Support/LazyMind/codex-desktop-20261015-native`；用户 LaunchAgent、GUI 启动环境与 `~/.codex/lazymind-shared-server.json` 已配置并核对。LaunchAgent 仅设置后续启动环境，不预启动后台。
- Assistant Bridge 已重启加载新版 CLI，健康检查通过。尚未退出当前 Codex；新后台尚未启动，待用户正常退出并从原图标重开后生效。此前临时试验后台 PID 41254 保留，未接管或终止。
- 下一步只做一次新 Workflow 的 Continue/Stop 人工验收；本次部署检查不等于真实业务控制已通过。Codex 应用包、图标和安全检查未修改。
- 旧 CLI、插件和配置备份，以及机器可读部署结果位于 `desktop/build/codex-improve/deploy-native-20261015/`（忽略目录）。共享模式可在正常退出 Codex 后，通过该安装目录 `runtime/manage.mjs disable --config runtime.json` 恢复；后台按既有设计保留。


### 2026-10-15：用户验收与提交整理

- 部署后用户反馈“测试后没有问题”，按上一轮约定的新 Workflow Continue/Stop 流程记为用户验收通过；未补造逐项日志、通过次数或异常场景结果。
- 本次提交包含共享后台运行组件、原生 RPC 控制与回执、CLI 绑定/worker 生命周期、针对性测试和使用文档；补齐被 `build*` 忽略的构建脚本。
- 实验快照、部署备份、运行日志以及其他模块的未提交工作留在本机，不纳入本次提交。设置页一键管理、异常恢复及长期兼容仍未作为已完成项。

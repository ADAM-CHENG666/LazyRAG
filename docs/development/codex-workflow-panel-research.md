# Codex Workflow Panel 迭代调研

日期：2026-10-22。范围：方案调研及独立临时实例的 CDP 启动验证，未实现或安装新 Panel。

> 2026-10-26 更新：本文保留 10 月 22 日的历史证据。本机 Codex 已升级至 `26.1021.31345` / build `21828`，旧版本启动和源码结论不能视作新版本验收。基于最新 main 的正式候选方案见 [Codex CDP Workflow 交互方案](../architecture/codex-cdp-workflow-proposal.md)。当前 `workflow/codex_improve` 分支仍含共享后台旧实现；`d49749ce` 属于另一条 IPC 分支。

## 1. 整体结论与建议

**如果目标仍是 DSH、WorkBuddy 的效果，应优先验证“在 Codex 聊天容器末尾挂载现有 Workflow 页面”，不能把普通 MCP App 卡片当成最终方案。** MCP App 的 HTML 通信和 Panel 的宿主位置是两个独立问题。

建议的工程方向是：复用现有 `/workflow-runs/:sessionId/embed` 页面；在 `integrations/codex-workflow` 增加仅负责展示的宿主适配；Workflow 操作仍交给 Core，再经已经验收的 Desktop IPC 控制原聊天。候选挂载技术是宿主提供的扩展入口，或类似 WorkBuddy 的 CDP（桌面网页调试接口）适配。**追加联网调研已找到社区的 CDP 注入和 DOM 挂载实现，并确认当前安装包仍有相关布局锚点，详见第 8 节；尚未在本机实际挂载，不能直接承诺可交付。** 没有发现公开承诺兼容的第三方聊天末尾扩展 API。

MCP App 可作为另一条路线：HTML 由 MCP 返回，页面通过宿主转发工具调用访问 LazyMind，不直接连接本地 HTTP。但普通卡片会进入历史；原生侧栏是值得验证的候选，不能等同于输入框上方的聊天末尾 Panel。

| 路线 | HTML 与本地通信 | 始终处于聊天末尾 | 判断 |
| --- | --- | --- | --- |
| 现有内置浏览器 Panel | 已验收 | 否，独立浏览器标签 | 保留为可用入口 |
| MCP App 内嵌本地网页 | 当前包会过滤 HTTP/WS CSP 地址 | 否，普通工具卡片属于历史消息 | 不作为直接迁移方案 |
| MCP App 自包含 HTML + 宿主工具桥接 | 有协议与本机代码支持，LazyMind 尚未实测 | 普通卡片不满足 | 可验证通信，但不足以完成目标 |
| Codex 原生 MCP App 侧栏 | 安装包存在组件；第三方进入方式及生命周期待确认 | 是侧栏，不是聊天末尾 | 可选产品折中 |
| 宿主末尾挂载 + 现有 embed 页面 | 参考 WorkBuddy；Codex 的加载限制待确认 | 挂载成功后可按此目标设计 | 优先验证位置和加载两个关键条件 |

## 2. 调研基线与可信范围

- 已验收功能提交：`d49749ce`，`feat(codex): control workflow chats through desktop IPC`。
- 已获取 upstream/main：`2090d6c65563680e6d6fcf09f8584385d5c8655d`，包含 WorkBuddy 接入 PR #850。仅获取、阅读，没有合并进当前分支。
- 本机 Codex：`26.1007.71326`，build `20398`，安装在 `/Applications/ChatGPT.app`。本次只读取安装包。
- 初轮结论来自 main 源码、当前 Codex 安装包和 OpenAI 官方文档；随后补充开源项目研究及临时实例 CDP 实测，见第 8、9 节。未重启用户正在使用的 Codex、修改安装包或执行 MCP App 渲染实验。
- macOS 是本期实际验证平台。方案使用 TypeScript、浏览器 DOM、MCP 等共有技术；Windows 的宿主发现、启动参数和权限行为仍需后续实机确认。

## 3. 瓶颈一：为什么本地页面可能加载不了

### 3.1 已找到的具体限制

当前 Codex 的 `resource-5f47691a644e.js` 会解析并规范化 UI 资源的 CSP（页面可访问地址的白名单）：

- `connectDomains` 用于网络请求，`resourceDomains` 用于静态资源，`frameDomains` 用于内嵌页面。
- 地址过滤通常仅保留 HTTPS；连接地址还允许 WSS。
- 放行 `http://localhost` 的条件依赖 `oe()`，而本安装包中 `oe()` 固定返回 `false`。
- 所以在这条资源处理路径中，填写 `http://localhost:<port>`、`http://127.0.0.1:<port>` 或 `ws://...`，不会因为填了白名单就得到放行。

这足以解释“直接 fetch 本地服务、加载本地脚本或 iframe”为什么可能失败。**这是当前包的静态代码证据，不是对之前失败现场的完整复现，也不代表所有本地通信都被禁止。** HTTPS 地址通过这一层过滤也不代表能成功连接，还涉及证书、浏览器网络策略等；不建议为此先引入本地证书和代理服务。

Agent 执行命令的操作系统沙箱、MCP 工具进程和 HTML 页面的浏览器限制，是不同层次。调整 Agent 的命令执行权限，不能推导出网页就能访问 localhost。

### 3.2 HTML 本身与联网可以分开

MCP App 可以通过 MCP 资源读取返回 HTML 文本，使用 `text/html;profile=mcp-app` 和工具的 `_meta.ui.resourceUri` 关联视图；页面通过宿主桥接调用工具。这条路径不要求浏览器先访问 LazyMind 的本地 HTTP 服务。[官方 UI 文档](https://developers.openai.com/plugins/build/chatgpt-ui)

候选路径：

```text
LazyMind MCP 返回自包含 HTML → Codex 展示
页面按钮 → MCP App 宿主桥接 → LazyMind MCP → Workflow Core
Core → 原有 HostAction → 原有 Desktop IPC → 原聊天
```

安装包存在 `serverTools`、`serverResources` 支持，以及不可渲染资源的诊断：`unsupported_mime_type`、`invalid_resource_metadata`、`missing_text` 等。因此“HTML 未显示”还需要区分资源格式、功能是否启用、资源读取失败与联网失败，不能统一归因于命令沙箱。

完整 WorkflowPanel 有编辑器、产物预览、上传下载和状态更新。将其做成自包含 MCP App，意味着梳理这些数据与资源访问，不能只替换最外层 HTML。可以复用组件和 Core，但需要增加前端通信适配；成本明显高于加载现有 embed 页面。

官方 CSP 配置区分连接、资源、子 iframe；未声明的能力不会自动获得。官方页面主要描述 ChatGPT，不能据此承诺 Codex 的本地网络或展示能力。[官方参考](https://developers.openai.com/plugins/reference)

## 4. 瓶颈二：如何始终位于对话末尾

这里的目标是：**新消息始终出现在 Panel 前方，Panel 留在当前聊天的尾部；用户回看历史时不被强行拉回底部。** 这与固定覆盖在输入框上、占满屏幕或每轮重新发送一张卡片不同。

### 4.1 main 中 DSH 的做法

DSH 通过 `conversation.input.dock` 注册 UI，定位输入区域在滚动容器中的位置，在它前面插入独立节点，用 React Portal 渲染 Panel。聊天里的工具消息只保留入口。iframe 以 run/session 标识保持实例，收起和展开调整布局，不为每条消息重建页面。

依据：[DSH client/index.tsx，固定 main 快照](https://github.com/LazyAGI/LazyMind/blob/2090d6c65563680e6d6fcf09f8584385d5c8655d/integrations/dsh-workflow/src/client/index.tsx#L70)。这里同时依赖 DSH 扩展槽和它的 DOM 布局，不能直接移植槽名到 Codex。

### 4.2 main 中 WorkBuddy 的做法

WorkBuddy 通过 CDP 执行展示适配代码，利用 `window.wb.conversations` 确认当前聊天。它把 Panel 放在消息文档外、消息列表容器的最后一个位置；监听布局变化维持位置，切换聊天时清理旧挂载。iframe 复用现有 Workflow embed 页面，展开时用 popover 保持同一页面实例。

依据：[presentation.ts](https://github.com/LazyAGI/LazyMind/blob/2090d6c65563680e6d6fcf09f8584385d5c8655d/integrations/workbuddy-workflow/connector/src/presentation.ts#L73)、[cdp.ts](https://github.com/LazyAGI/LazyMind/blob/2090d6c65563680e6d6fcf09f8584385d5c8655d/integrations/workbuddy-workflow/connector/src/cdp.ts)、[presentation-frame.ts](https://github.com/LazyAGI/LazyMind/blob/2090d6c65563680e6d6fcf09f8584385d5c8655d/integrations/workbuddy-workflow/connector/src/presentation-frame.ts)。

这两者都没有依赖普通 MCP App 卡片实现尾部位置。可借鉴的是“一个聊天对应一个展示实例、挂在消息文档外、维持 iframe 状态”，不是直接复制宿主选择器或会话 API。

### 4.3 当前 Codex 的证据

- 普通 MCP 工具卡片位于 `mcp-tool-item-content-be155320caeb.js`，使用的 `hostDisplayModes` 解析到 `Ipa = ['inline', 'fullscreen']`。资源虽然识别 `pip` 枚举，但这条卡片路径没有提供它。
- 展示模式会取资源声明与宿主允许模式的交集；自行声明 `pip` 不能让宿主支持它。
- 安装包确实有 `thread-mcp-app-side-panel-tab-6510e337b04c.js`，以及 thread lifetime 的视图加载逻辑。这证明侧栏实现存在，不证明 LazyMind 普通 MCP 工具可通过公开入口开启它并维持跨轮次显示。
- 本次未找到可确认供插件注册的、类似 DSH `conversation.input.dock` 的 Codex 扩展入口。

普通 iframe 内的 CSS 不能把它移出宿主工具消息，也不能控制父页面的聊天布局。重复发卡片会增加历史副本，重新挂载还可能丢失编辑状态，因此不符合此目标。

## 5. 工程落点与边界

以下是候选落点，尚未新增实现文件。

| 位置 | 预期责任 |
| --- | --- |
| `integrations/codex-workflow/src/` | 若末尾挂载可行，新增展示适配与宿主布局识别，管理当前聊天的挂载、收起、展开和卸载；复用现有 worker 生命周期，不另起常驻 daemon。 |
| `frontend/src/router/index.tsx` 的 `/workflow-runs/:sessionId/embed` 与 `frontend/src/modules/chat/components/WorkflowPanel/` | 优先复用现有页面。仅按实际需要调整嵌入交互；若选择 MCP App，再抽取前端数据访问适配和自包含构建。 |
| `local/lazymind-cli/internal/mcpbridge/` | 仅在选择 MCP App 时增加 HTML 资源、视图元数据与 UI 调用入口；继续调用 Core，不复制 Workflow 状态判断。 |
| `local/lazymind-cli/internal/adapters/codex/` | 按选定路线处理插件资源打包、能力提示；如需 CDP，先明确连接前提及 macOS/Windows 差异。 |

所有路线均遵守 [外部 Agent 协议](../architecture/external-agent-workflow-protocol-draft.md)：打开 Panel 不建立或转移 Controller 绑定；查看其他 run 不改绑聊天；用户操作先在 Core 生效，唤醒和中断仍通过 HostAction。Panel 不直接发聊天消息来代替 Continue，也不取得外部执行授权。MCP 的 UI 通信仍归 MCP，不能让 Host Connector 变成 MCP 代理。

CDP 若被采用，只承担展示。它需要可连接的宿主调试入口，并耦合宿主布局；这会新增部署前提和升级维护成本。不能因 WorkBuddy 可用就默认 Codex 同样可用，也不能把已简化的 IPC 控制重新搬回 CDP。

## 6. 下一步最小验证范围（本次未执行）

优先验证位置是否可行，再决定是否投入完整 Panel 改造。每项只围绕一个决策问题，不重跑已验收的整套控制功能。

| 项目 | 最小操作 | 通过标准与决策 |
| --- | --- | --- |
| A：Codex 聊天末尾挂载 | 确认可用的宿主入口；若只有 CDP，先确认启动前提。挂一个简单占位 Panel，增加消息并切换聊天。 | 能确认真实聊天 ID；新消息在 Panel 前方；回看历史不抢滚动；切换后不显示旧聊天 Panel；关闭可清理。找不到稳定挂载位置则停止该路线。 |
| B：原页面嵌入 | 仅在 A 通过后加载现有 embed，编辑一个输入、收起展开并执行一次 Continue。 | 页面成功加载；输入保留；操作到达 Core，原聊天通过现有 IPC 继续。若宿主拒绝本地页面，记录具体限制后再选通信替代方案。 |
| C：MCP App 备选 | 仅在需要这条路线时，先返回不联网的 HTML，再用一个只读 UI 工具取 run 状态；若考虑侧栏，再确认实际入口。 | 分别证明 HTML 能显示、宿主桥接能返回状态；侧栏若可用需连续两轮仍可操作。普通卡片成功不算末尾位置成功。 |

若 A 无法成立，或必须引入用户不接受的 Codex 启动改造，应明确选择现有浏览器 Panel 或经实测可用的原生侧栏；不能把卡片样式调整描述为完成了聊天末尾 Panel。

## 7. 本机证据定位

原始文件：`/Applications/ChatGPT.app/Contents/Resources/app.asar`。本次提取到 `/private/tmp/codex-panel-research-20261022/`；临时目录只用于复查，可能被系统清理。

| 文件 / 搜索标识 | 支持的结论 |
| --- | --- |
| `resource-5f47691a644e.js`：`function oe(){return!1}`、`function se`、`function G` | 当前资源 CSP 地址过滤，不放行 HTTP localhost 特例。 |
| 同文件：`mcp_app_ui_resource_not_renderable` | HTML 无法显示还有 MIME、元数据、文本内容等原因。 |
| `mcp-tool-item-content-be155320caeb.js`：`bpt as En`、`hostDisplayModes:En` | 普通工具卡片引用宿主展示模式列表。 |
| `app-initial-1ab6fbe93c4f.js`：`Ipa as bpt`、`Ipa=[`、`function lpa` | 卡片宿主列表只有 inline/fullscreen，模式取交集。 |
| `widget-338e9f889b2d.js`：`serverTools:{}`、`serverResources:{}` | UI 宿主桥接支持工具与资源能力的代码证据。 |
| `app-initial-1ab6fbe93c4f.js`：`McpAppThreadSidePanelTab` | 独立 MCP App 侧栏组件确实存在；不等同于第三方可调用保证。 |

上面记录的是当前版本代码行为与候选方案，尚无 Codex 新 Panel 的运行验收结论。

## 8. 追加联网调研：已有开源实现与可复用路径

用户追加要求后，检索并只读下载了三个公开项目，检查其实现而非仅依赖 README。本次没有执行它们的安装器或注入脚本。

### 8.1 最相关的三个项目

| 项目 / 本次源码快照 | 实际实现 | 对 LazyMind 的价值与限制 |
| --- | --- | --- |
| [dan-dr/explodex](https://github.com/dan-dr/explodex)，`ab0aeab4299bf1dc8da94ebd9abe77a0f8c771ac` | CDP 注入 JS；SDK 定义 `aboveComposer`、`threadFooter`、`mcpAppPortal` 等区域；用 DOM 插入和 MutationObserver 处理挂载与锚点替换。 | 最直接的布局与挂载参考。参考架构文档对应 26.623，早于本机；Windows 文档明确尚未实机验证。它的“区域”是社区对内部 DOM 的封装，不是 OpenAI 插件 API。 |
| [qqheling/codexplusplus](https://github.com/qqheling/codexplusplus)，`a114ae5ca7a6ca4e798482fc943848df0ccffabb` | 外部启动器开启 CDP，通过 `Runtime.evaluate` 给当前页面注入，通过 `Page.addScriptToEvaluateOnNewDocument` 注册新文档脚本。 | 可参考 macOS/Windows 启动与注入分层。项目提供两平台发布入口，但不等于 LazyMind 已验证 Windows，也没有证明它已实现我们的聊天末尾 Panel。 |
| [b-nnett/codex-plusplus](https://github.com/b-nnett/codex-plusplus)，`13d0c2421cc4258565710910ca8dfe60344c027e` | 当前 2.x 修改主进程及宿主集成，通过 Electron `webContents.executeJavaScript` 加载扩展，不依赖 CDP 网络端口。 | 文档明确覆盖与本机相同的 26.1007.71326 / build 20398。说明新版本仍可做宿主 UI 扩展；但需要修改应用、使用开发者身份重新签名，当前为 macOS 路线，不适合仅为 Panel 引入整套机制。 |

同名的两个 Codex++ 是不同项目，不能混用平台支持与安装方式。另查到 [yorkartur/codex-desktop-cdp](https://github.com/yorkartur/codex-desktop-cdp)：其方案仍是内置浏览器打开网页再向聊天传入消息，不能作为聊天末尾挂载的证据。

### 8.2 Explodex 的“挂载”具体是什么

源码位置：[sdk/explodex-sdk.js](https://github.com/dan-dr/explodex/blob/ab0aeab4299bf1dc8da94ebd9abe77a0f8c771ac/sdk/explodex-sdk.js#L297)。

- `aboveComposer` 定位 `[data-above-composer-portal]`，然后追加独立元素。
- `threadFooter` 定位 `[data-thread-scroll-footer="true"]`，默认向前插入。
- `mcpAppPortal` 定位 `[data-mcp-app-portal-target="true"]`。
- `observeZone` 监听 DOM 变化；锚点被 React 替换后通知调用方重新挂载；卸载时清理监听器。

这是实际 DOM 操作，不需要把 Panel 作为一条工具消息发送。但 **aboveComposer 属于输入区，不能把这个名字直接理解为“消息流末尾”**。SDK 的通用 `mount` 也没有替 LazyMind 解决 run 绑定、编辑状态和滚动跟随。

注入入口见 [scripts/cdp-inject.ts](https://github.com/dan-dr/explodex/blob/ab0aeab4299bf1dc8da94ebd9abe77a0f8c771ac/scripts/cdp-inject.ts)。跨平台启动参考 [qqheling 的 launcher.rs](https://github.com/qqheling/codexplusplus/blob/a114ae5ca7a6ca4e798482fc943848df0ccffabb/crates/codex-plus-core/src/launcher.rs)，注入参考 [bridge.rs](https://github.com/qqheling/codexplusplus/blob/a114ae5ca7a6ca4e798482fc943848df0ccffabb/crates/codex-plus-core/src/bridge.rs#L74)。这里只借鉴机制；引入源码前需另核对具体文件许可证，不直接整包依赖这些改造工具。

### 8.3 与当前安装包交叉核对后的挂载位置

本次额外提取 `thread-scroll-layout-c5caf974cfd4.js`，结合 `app-initial-1ab6fbe93c4f.js` 得到以下静态证据：

1. `data-above-composer-portal` 仍存在，宿主按 `data-above-composer-conversation-id` 查找对应聊天的入口。
2. `data-mcp-app-portal-target="true"` 所在元素承载消息内容；当前代码同时给它标记 `data-thread-user-message-navigation-content`。
3. 这个消息元素外部有一个普通纵向内容容器；消息元素之后可有 `threadEndContent`，再后面是为输入区预留的空间。
4. `data-thread-scroll-footer="true"` 是独立的输入区布局，本版本使用 absolute bottom 定位。最外层滚动容器还可能使用 `flex-col-reverse`，不能简单对最外层 `appendChild` 后就认为在视觉底部。

因此最接近 DSH/WorkBuddy 的候选位置是：**在消息元素之后、输入区预留空间之前，插入 LazyMind 自己的同级节点。** 新消息仍由 React 写入原消息元素，Panel 不属于其中任何一条消息。

```text
聊天滚动区域
  内容容器（纵向）
    原消息元素：历史消息 → 最新消息
    LazyMind Panel：自有节点，复用一个 iframe  ← 候选插入位置
    宿主已有尾部内容 / 输入区预留空间
  原输入区：继续由 Codex 管理
```

`threadEndContent` 是内部 React 属性，不是可直接调用的插件接口。优先尝试定位它所对应的布局关系，不先改写压缩后的 React 函数或强行修改组件 props。已有宿主尾部内容需要保留，最终顺序与滚动表现以实机为准。

聊天识别应结合 Core 已绑定的 thread ID、宿主聊天标识和当前可见布局；不能按页面标题、首个匹配节点或当前消息文本猜测。Codex 会缓存隐藏页面，单纯 `document.querySelector` 不足以保证插到当前聊天。

### 8.4 推荐的具体实现形态

```text
现有 Codex worker
  ├─ Desktop IPC：继续负责会话唤醒 / 中断
  └─ 可选 CDP 展示适配：注入与恢复 Panel 挂载
        → 当前聊天内容容器末尾
        → 现有 Workflow embed iframe
```

工程上可在 `integrations/codex-workflow/src/` 增加 `presentation-cdp.ts` 与 `presentation.ts`，前者只处理注入连接，后者管理挂载、聊天切换和清理；由现有生命周期管理，不新增后台 daemon。挂载稳定时保留同一 iframe；不在每次流式输出时搬动或重建它。需要恢复锚点时再处理重新挂载，用户回看历史时不强行滚动。

尚有两个真实前提：

- **注入入口：** CDP 路线需要宿主有调试端口。普通启动且没有端口的现有实例，不能仅靠目前已经验证的 Desktop IPC 方法就执行页面 JS。通常需要一次带参数的重新启动；后续普通启动与自动更新后的恢复方式属于产品接入工作。当前不重启、不改启动配置。
- **页面加载：** 宿主直接挂载的 iframe 不经过 MCP App 专用资源解析，但仍受宿主网页 CSP、iframe 策略与认证影响。不能把“避开 MCP App 的 HTTP 过滤”写成“本地页面一定能加载”。这是挂载位置确认之后的第二个最小实验。

**更新后的判断：已经有可复用的社区机制，也找到了当前版本的具体候选位置，不再只是泛泛猜测 CDP 可行；尚缺的是本机对这个位置的运行验证。** 不建议安装整套 Codex 改造工具作为 LazyMind 的依赖，也不建议为 Panel 修改并重新签名 Codex。下一步最有价值的工作是一个小型挂载实验：占位块跨两轮仍在末尾、切换聊天正确清理，然后再加载现有 embed。

## 9. CDP 能否启动：本机实测通过

用户追问后，使用当前安装的 `/Applications/ChatGPT.app/Contents/MacOS/ChatGPT` 做了独立临时实例实验。版本仍为 `26.1007.71326` / build `20398`。

先读取当前进程：WorkBuddy 使用 `--remote-debugging-address=127.0.0.1 --remote-debugging-port=9333`；用户当前 Codex 主进程 PID 为 `68327`，没有调试参数，也没有 TCP 监听端口。

Codex bootstrap 源码支持 `CODEX_ELECTRON_USER_DATA_PATH`，因此实验分别为 Electron 用户目录和 `CODEX_HOME` 指定全新的临时目录，未复用用户配置或会话。启动参数为：

```text
--user-data-dir=<临时 Electron 目录>
--remote-debugging-address=127.0.0.1
--remote-debugging-port=0
```

端口 `0` 由 Chromium 分配空闲端口；从临时用户目录中的 `DevToolsActivePort` 取得实际端口。第二次实验在目标出现后等待 8 秒，确认不是仅在启动瞬间可访问。

| 检查项 | 实测结果 |
| --- | --- |
| `/json/version` | 成功；`Chrome/155.0.8059.40`，CDP 协议 `1.3` |
| `/json/list` | 成功；存在 `type=page`、`app://-/index.html` 的宿主页面 |
| WebSocket CDP 连接 | 成功 |
| `Runtime.evaluate` 只读查询 | 成功；`document.readyState=complete`，标题 `ChatGPT`，DOM 元素数 `96` |
| 测试实例清理 | 已关闭测试进程；临时记录保留 |
| 原 Codex 进程 | 仍为 PID `68327`，未重启 |

最终实验 PID `56365`，临时 CDP 端口 `60899`，实例结束后不作为后续连接地址使用。

证据：脚本 `/private/tmp/lazymind-codex-cdp-probe-20261022.mjs`；结果 `/private/tmp/lazymind-codex-cdp-probe-i9B4Pl/result.json`。首次启动连通记录在 `/private/tmp/lazymind-codex-cdp-probe-Ssjip2/result.json`。

**结论：当前 macOS 安装版本能够直接通过启动参数开放 CDP，且页面加载后可执行 renderer 查询；无需修改或重新签名应用。** 此实验未登录临时实例，未打开原聊天、注入 Panel 或加载本地 iframe。真实聊天布局挂载仍待验证；用户现有的普通启动实例也不会因为这个实验而自动获得 CDP。产品接入仍需解决带参数启动及更新后的恢复，Windows 尚未实测。

## 10. 是否可以像 WorkBuddy 一样统一宿主接入为 CDP

**可以作为统一架构方向；目前已证明 Codex 的 CDP 入口可用，完整控制替换尚未在当前版本验收。** 这与第 8 节只让 CDP 负责展示的方案是两种选择：前者减少宿主接入通道，后者直接沿用已验收的 IPC 控制。若接受 CDP 成为必需启动条件，就有理由验证前者，而不是长期维护两套接入。

### WorkBuddy 当前实际实现

main 中 `adapter.ts` 把操作序列化给 `cdp.evaluate`；`sdk.ts` 在 renderer 中调用 `window.wb.conversations`：

| 能力 | 当前实现 |
| --- | --- |
| 唤醒 / 继续 | 根据绑定 session ID 取得会话，调用 `conversation.sendPrompt` |
| 停止 | 调用 `conversation.cancel()`，属于会话级取消 |
| 空闲 / 运行状态 | 读取 `conversation.info.state` |
| 首次会话识别 | 查询原生会话及运行中的工具请求，匹配 `workflow.start` |
| Panel | 同一 CDP 入口执行 `presentation.ts` 的 DOM 挂载 |

依据：[sdk.ts](https://github.com/LazyAGI/LazyMind/blob/2090d6c65563680e6d6fcf09f8584385d5c8655d/integrations/workbuddy-workflow/connector/src/sdk.ts)、[adapter.ts](https://github.com/LazyAGI/LazyMind/blob/2090d6c65563680e6d6fcf09f8584385d5c8655d/integrations/workbuddy-workflow/connector/src/adapter.ts)。

“统一 CDP”指的是 Connector 到宿主这一段。Core 的通知领取与回执仍走原有 HTTP 通道，Agent 的步骤执行仍走 MCP，Panel 的业务操作仍访问 Core；这些并没有全部搬到 CDP。

### Codex 已有对应开源控制实现

已只读检查 [YusenDu/codex-remote-bridge 的 rendererBootstrap.ts](https://github.com/YusenDu/codex-remote-bridge/blob/f654f5dd5a4ba6b850e601889071190b930631b3/src/server/codexDesktopCdp/rendererBootstrap.ts)，快照 `f654f5dd5a4ba6b850e601889071190b930631b3`：

- 从 React 内部状态中寻找本机 `AppServerManager`。
- 经已有 manager 调用 `thread/resume`、`turn/start`，目标是桌面原聊天。
- 调用 `turn/interrupt` 时提供 `threadId` 和 `turnId`。
- 注册通知、轮次完成和会话状态回调，将事件通过 CDP binding 送回外部程序。

因此它是“通过 CDP 调用原生会话接口”，不是操作输入框，也不是另起独立 app-server。这个项目的对象发现和调用代码仍依赖特定内部结构，当前安装版本需要适配；不能直接照搬它每次发送前无条件 resume 的策略或省略 LazyMind 的停止归属校验。

统一后的候选结构：

```text
LazyMind Core → HostAction → 同一个 Codex Connector
                                └─ CDP
                                    ├─ 原生会话接口：发送、精确中断、状态
                                    └─ 页面布局：Panel 挂载

Agent → Workflow MCP → Core（执行通道维持原协议）
```

实现落点是增加 `CodexCdpAdapter`，复用现有 dispatcher、通知回执和执行归属判断，替换宿主传输层。必须验证：同一聊天发送并保留上下文、按 turn ID 停止且旧 Stop 不打断新轮次、忙碌/完成状态与界面一致、结果不明时不重复发送。上述行为及 Panel 挂载通过后，可移除 IPC 运行路径；不把双通道自动回退当作长期设计，也不因为改用 CDP 就删除必要的可靠投递逻辑。

主要取舍：减少一套宿主传输适配，但控制与展示同时依赖调试端口和 renderer 生命周期。刷新或重建 renderer 后需要重新发现原生对象并恢复订阅；CDP 本身不提供持久化业务回执。macOS/Windows 可以共用 CDP 与业务代码，启动发现部分仍应隔离。

## 11. DSH 是否也能统一走 CDP

**有明确的技术可行性依据，但本次未启动 DSH 的 CDP 实验，也未证明能无损替代当前插件。** 三者可以共用连接管理、调度、回执和 Panel 生命周期；原生会话接口与页面定位仍需要三个宿主适配。

本机 `/Applications/DeepSeek Harness.app` 为 `0.2.0-rc.2`，包内 build commit 为 `5e9e301dd9dc8923b2762f76dacfc5751f6ca851`。读取 `app.asar` 确认它使用 Electron，包含 `@deepseek-ai/dsh-api-session-controller` 的前端实现。官方桌面架构参考：[DeepSeek Harness Desktop](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md)。

包内 `dsh/node_modules/@deepseek-ai/dsh-api-session-controller/lib/types/client/sessions/session.js` 显示：

- `Session.prompt(content, mode, signal, requestId)` 调用 `remote.session.prompt`，包含明确的 session ID 与请求 ID。
- `Session.cancel()` 对普通会话调用 `remote.session.cancel({sessionId})`。
- `types/client/transport.js` 使用 `remote.session.control`、`follow`、`page` 获取控制状态与历史事件流。

因此，如果 CDP 开放并能取得页面的 Client Session 服务，就可以经同一 CDP 通道发送、取消、读取状态及挂载 Panel，而无需模拟输入框。代码中存在服务不等于它已暴露为全局变量；服务获取、当前会话识别、CDP 启动和重连仍待实测。

### “统一 CDP”不等于“所有能力完全相同”

DSH 当前 `host.ts` / `host-adapter.ts` 还承担宿主进程内能力：执行前钩子、工具结果处理、Goal 阻塞和恢复、父子 Agent 归属，以及用持久事件序号核对投递结果。这些不是 DOM 功能，也不能通过对浏览器 `Runtime.evaluate` 的支持直接推导为可用。

若删除 DSH 插件，仅迁移 prompt/cancel/Panel，会丢失上述接入，除非分别找到等价 API 并验证。可选路径：

1. **统一框架，保留原生适配（建议）：** DSH 继续使用原生插件和 slots；Codex、WorkBuddy 共用 CDP 运行层。三者共用 Core 协议、dispatcher、回执和 Panel 内容，差异留在宿主适配内。
2. **三者统一 CDP 接入：** 增加 DSH 的前端服务适配；无法通过前端 API 等价实现的运行钩子仍由小型原生插件保留。这样连接和展示可以统一，但部署依赖仍有差别，且可能增加一层调用。

前一节建议 Codex/WorkBuddy 统一 CDP，是因为两者都需要额外手段接入宿主 UI；DSH 已有原生插件扩展入口，不能据此认为它改成 CDP 也一定更简单。

如果决定继续验证 DSH CDP，最小范围为独立配置下启动端口、取得 Client Session 服务、原聊天发送/取消及尾部挂载；然后单独核对现有执行前钩子和 Goal 行为是否有等价入口。未解决能力差异前，不承诺三者一套完全相同的实现。

# Codex CDP 开发计划

日期：2026-10-26。状态：第一阶段已由用户验收通过；第二阶段开发版本已完成，待用户验收。

## 最终效果

在 LazyMind 助理中开启 Codex 开关后，可以在原 Codex 聊天里完成 Workflow，并通过聊天末尾的 Panel 审核、继续、停止和恢复。控制与 Panel 都通过 CDP 接入，尽量复用 WorkBuddy 的实现。

开发从最新 main 新建干净分支，不带入 `workflow/codex_improve` 中的共享后台、启动接管和代理代码。先交付完整控制，再做嵌入 Panel；两阶段分别更新当前安装的 LazyMind.app，供你验收。

## 开发准备

| 任务 | 内容 | 完成标准 |
| --- | --- | --- |
| T00 新建开发分支 | 从开发开始时的最新 main 创建 `workflow/codex_improve`。 | 保留 main 已有的 WorkBuddy 接入，不带入旧分支的共享后台实验实现。 |
| T01 Codex 接入验证 | **已完成**：已有 CDP 接入验证。 | 直接进入开发，不再安排一轮独立验证。 |

## 第一阶段：完整控制，继续使用现有网页 Panel

| 任务 | 开发内容 | 任务验收标准 |
| --- | --- | --- |
| T02 接入助理开关和教程 | 沿用 WorkBuddy 的开关、连接、重启确认、进程管理和教程组件；名称适配为 Codex，去掉信任步骤。 | 操作方式与 WorkBuddy 一致；开关能正确启停接入；重启后能重新连接。教程流程和文案在本任务完成，验收后由你们替换正式图片即可。 |
| T03 完成 Workflow 控制 | 接通原聊天绑定、审核继续、停止、恢复、重试和回退；记录消息是否被 Codex 接收。 | 始终控制原聊天；内部和外部执行都能推进；断线不重复发送；旧停止请求不会中断用户的新任务。 |
| T04 提供验收入口 | 更新当前安装的 LazyMind.app，临时创建一个全部步骤由外部 Agent 执行、包含人工审核和至少 90 秒停止窗口的简单 Workflow。 | 你可以按教程开启开关直接验收；全部步骤确认为外部执行，能够在停止窗口内点击 Panel 的 Stop，观察同一 Codex 轮次被中断。 |

**阶段验收：你从助理开关开始，使用现有网页 Panel，完成启动、审核继续、运行中停止、恢复、重试和回退。此时只剩 Panel 嵌入未完成。**

停止的预期：Workflow 立即停止；Codex 正在执行对应 Workflow 时，同时中断该轮次。等待内部执行时，不中断用户在 Codex 中进行的其他交流。

## 第二阶段：Panel 留在聊天末尾

| 任务 | 开发内容 | 任务验收标准 |
| --- | --- | --- |
| T05 嵌入现有 Panel | 复用 WorkBuddy 的页面加载代码，通过 CDP 调用 Codex 已有的原生浏览器容器，挂在聊天末尾。 | Panel 能加载并操作；新消息始终出现在 Panel 前方，Panel 不进入某条历史消息。 |
| T06 处理切换和滚动 | 实现聊天切换、收起展开和滚动跟随。 | 切换聊天不串 Workflow；收起展开不丢编辑；上翻历史时不被强拉到底部。 |
| T07 完成交付 | 更新当前安装的 LazyMind.app，检查本次共用改动对 WorkBuddy 的影响。 | Codex 完整流程可用；教程只剩正式图片待替换；WorkBuddy 原有控制和 Panel 正常。 |

**阶段验收：你在 Codex 内完成审核和继续；连续对话后 Panel 仍在末尾，切换聊天再返回也能正确展示。**

## 复用与改动范围

| 部分 | 计划 |
| --- | --- |
| WorkBuddy 的 CDP 通信、进程管理、Panel 页面和展示逻辑 | 提取共同部分，供两者使用。 |
| Codex 的启动、会话控制和聊天布局 | 单独适配。 |
| Workflow Core 和 MCP | 沿用现有执行与控制流程；补齐 Codex 消息接收结果的记录。 |
| DSH | 本期不迁移。 |

主要改动位于 `integrations/codex-workflow`、`integrations/workflow-agent-core`、CLI 的助理接入模块，以及前端助理页面。

第二阶段落地：Codex 的普通 iframe 会被主页面 CSP 拦截，因此使用宿主已有的原生浏览器服务注册容器，显示共用 Workflow 页面。CDP 负责注册、聊天位置同步和界面消息传递，不修改 Codex 安装文件，也不关闭主页面 CSP。页面加载、滚动跟随和绑定查询与 WorkBuddy 共用。

第二阶段验收见[操作说明](../development/codex-panel-acceptance.md)。

## 开发约束

- 遵守[外部 Agent 交互协议](external-agent-workflow-protocol-draft.md)：Core 决定步骤和授权，CDP 负责控制原聊天，Panel 打开或切换不会改变绑定。
- 在 macOS 开发和验收；公用逻辑兼顾 Windows，启动和路径差异单独处理。本期不要求 Windows 实机验收。
- 使用已有测试，只补本次改动涉及的控制、断线和聊天切换用例；每阶段做一次对应产品验收，不重复跑无关检查。
- 本期只开发 CDP 方案，不安排旧实验方案的迁移和兼容阶段。关闭开关停止接入，不退出用户的 Codex。
- 已有验证与技术资料见[调研记录](../development/codex-workflow-panel-research.md)，需要时查阅。

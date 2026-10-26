# Codex CDP 外部执行验收（临时）

四步串行流程：准备审核 → 90 秒停止窗口 → 首次预期失败与手动重试 → 最终审核与完成后回退。

从 Codex 启动。所有步骤不声明 LazyMind tools、terminal_tools 或 post_step_checks，因此外部 Controller 领取时应得到 executor_host=external-agent。若返回 lazymind，应停止验收并报告执行位置不符，不能声称已测 Codex 中断。

Agent 必须遵守 Core 返回的执行授权和人工审核状态。所有输出通过 workflow.artifact.publish 写入对应文本槽，然后用原 execution_handle 提交 outcome。使用现有网页 Panel；不要代审，不轮询审核、不创建新的 run、不调用 session.stop 来替代用户点击 Panel。

第二步留出 90 秒真实工具等待窗口；恢复时不重复等待。第三步的首次失败是预期测试数据，不是产品错误；重试必须由用户触发。完成后的回退也由用户触发。

不处理业务文件、不调用外部服务、不产生费用型资源、不启动独立 App Server 或后台服务。此流程验证当前轮次的中断，不验证所有后台子进程都已退出。

步骤标识：prepare（准备审核）、interrupt（停止与恢复）、retry（首次失败与手动重试）、finish（最终审核与回退）。

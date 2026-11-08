# 外部工作流开发接入

外部模式由外部 Agent 同时承担 Controller 和 Executor。Core 负责执行记录、状态、产物、授权与审核；MCP 是这些业务操作的适配入口。架构和时序见[交互协议](../architecture/external-agent-workflow-protocol-draft.md)。

## Workflow 开发者

每一步将业务任务、输入、输出和验收写入共用步骤定义。把原来大工具内的规划、写作、语义或视觉判断交给 Agent；解析、渲染、导出等独立能力通过注册工具提供。图片、视频等专用模型可以保留为按需调用的 MCP 工具。

外部执行不使用 `runtime.external_execution` 覆盖层，不要求 `scenario/external.md`。旧覆盖层中的业务要求必须由开发者合并到共同定义后重新发布；未适配 revision 的外部执行会明确拒绝，不回退到内部通用 LLM。无需为同一业务维护两套输出或验收。

步骤描述需要的能力和交付结果。Agent 可选择宿主能力，也可查询 `workflow.tool.list` 选用用户已授权的工具。包工具仍需注册适配器和固定 revision 的有效绑定；不能任意执行包内 Python。工具拆分和注册由相应 Workflow 开发者负责。

## 外部 Controller

1. 用 `workflow.get` 获取启动输入定义；附件须导入为资源，不能只在请求文本中提及路径。
2. `workflow.start` 使用稳定 `idempotency_key`。`needs_input` 表示尚未创建会话，按返回字段补齐输入。`binding_required` 表示会话已创建但宿主绑定未完成，应重放相同 start。
3. 读 `workflow.state`。仅按 Core admission 推进；已有执行用 `step.claim`，中断后恢复用 `step.resume`，创建下一次执行才用 `step.begin`。
4. 持有执行 handle，按公共 `step_contract` 自行执行，或委托可选子 Agent。无需宿主提供真实 subagent 或子 Agent MCP。
5. 如委托子 Agent，传完整业务契约、输入资源和输出位置；主 Agent 代理所需 MCP 工具调用。子 Agent 只返回产物清单、资源/文件引用和检查结论。
6. 主 Agent 在租约到期前调用 `step.renew`。到期、停止或 handle 被替换后，不提交旧计算结果。
7. 逐项 `artifact.publish`，确认成功后 `step.complete`。文件必须存在于 Connector 能读取的位置；另一台机器的路径不能直接使用。
8. 按完成回执继续。`awaiting_user` 时提示审核并结束当前运行；收到通知或用户操作后重新读取状态。

主 Agent 兼任 Executor 和委托子 Agent 使用同一套 Core 接口。不要把 handle、凭证或流程推进权限交给子 Agent。停止后尽力取消宿主任务，同时丢弃已失效结果。

## 工具与异常

- `tool.list` 返回注册工具及 schema。`source` 选择 package/global 适配器，不授予额外权限；每个适配器仍检查注册、用户授权与固定输入范围。
- 某个工具目录来源不可用时，`tool.list` 在 `warnings` 中返回该 `source` 和结构化错误，仍返回其他可用工具。执行授权失效仍拒绝整个请求。
- `tool.call` 必须带稳定 `command_id`。相同命令的未决或已完成回执不会重新执行一次付费调用。
- 通用 Agent 推理由外部完成。宿主缺少生图等能力时，可以选择已配置的专用模型工具；两边都不可用则说明缺少的能力。
- 错误通过 MCP 结构化返回，保留 Core 的诊断细节和恢复说明。缺失输出按 `missing_slots` 补交，输入错误先修正，授权失效先读状态。
- `state_unavailable` 不抹掉已成功的创建、领取或完成；`claim_required` 不抹掉已创建的执行。恢复原操作，不重复创建任务。
- 写响应丢失时结果可能未知。保留原命令、执行和会话标识，检查状态或原样重放；不要把超时直接视为操作失败。
- `REQUEST_NOT_SENT` 表示请求在发往 Core 前被本地校验或连接配置拒绝，`outcome=not_sent`；先按原始错误修正请求。发送后的传输或响应读取失败仍按未知结果处理。
- 升级前保存的控制命令回执可使用原始参数（包括原 `expected_state_version`）重放；校验匹配后会更新摘要格式，此后可使用刷新后的状态版本重放。业务参数、用户和会话必须保持一致。

## 实现边界

只有一个 MCP 服务。Core 共用推进、租约、产物和完成服务；CLI 保留宿主身份、文件传输、参数和 MCP 呈现。步骤契约按 Core 原样透传，新增业务字段无需在 CLI 再定义一遍。

本协议不提供旧包自动迁移、独立外部 Executor 会话调度、子 Agent 直连提交，也不新增步骤进度或侧栏/面板展示。Workflow 格式规范、生成指引与创作校验单独维护。

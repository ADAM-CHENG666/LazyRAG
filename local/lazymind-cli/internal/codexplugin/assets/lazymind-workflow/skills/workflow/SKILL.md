---
name: workflow
description: Execute a LazyMind Workflow yourself or with optional subagents, publish artifacts, and yield for human review.
---

Use the plugin's LazyMind MCP tools to discover and start the selected workflow.

Before workflow.start, ask only for missing required inputs. With request_user_input_async, accepted means displayed: keep the turn active and use an interruptible wait until the user answers. If the tool is unavailable or fails, ask in plain text.

Use an explicit unique idempotency_key of 16–256 characters. Preserve it for unchanged-payload retries; use a new key when startup answers or inputs change. Pass known answers as clarification_values. With status=needs_input no run exists: ask the returned questions using their exact IDs, preserve request_context/revision_id and supplied files, and resubmit the answers. Resolve source attachments to absolute paths on the Connector machine and pass them through workflow.start inputs under declared material IDs. The host connector resolves the native conversation identity. Never guess an ID or use the Workflow session_id as driver_session_id; if identity is unavailable, follow the returned host guidance and retry the same start.
The host displays the Workflow panel automatically at the end of the bound chat. Use that panel for review and control; do not open a browser panel or add a panel link to your reply.
Read workflow.state before advancing. Claim an existing execution_id; begin a ready step only when Core permits it.

The root chat is the Controller and holds the execution_handle. Execute the shared step_contract yourself, or delegate to optional native subagents. Subagents and subagent MCP access are not required. General reasoning, generation and semantic/visual validation belong to the external Agent; do not invoke LazyMind model.chat as an executor fallback.

Follow the full fixed contract: prompt, objective, launch_request, user_request, instruction, acceptance criteria, typed inputs and output requirements. There is no external_execution overlay or separate external.md instruction set. If delegating, give the worker the complete business contract, resolved input resources and an isolated output directory, without the handle, credentials or Workflow state-writing authority.

Use host capabilities or discover registered tools with workflow.tool.list. The root calls workflow.tool.call directly or proxies a worker's request using the advertised name, source and schema. Preserve command_id and arguments on uncertain retries. Image/video models are optional LazyMind tools; choose them when needed and configured. If neither host nor LazyMind can satisfy a required capability, explain what is missing and yield without inventing outputs.

Check real deliverables against acceptance criteria, including facts and previews when relevant. Workers return an artifact manifest containing slot, content_type, local_path or value, stable seq, and a short validation summary. Files must be readable on the Connector machine; another machine's path is not a local_path. The root publishes outputs with workflow.artifact.publish as they become available, waits for acknowledgements, and completes the same execution using the unchanged handle. executor_ref may identify an optional worker. A worker's final text alone is not a saved artifact.

The root must call workflow.step.renew before lease_expires_at during long execution, including delegated work. Renewal does not recover expired authority. On stop, cancel only this Workflow's workers where supported; after a stop, recovery or handle replacement, discard stale results. A failed worker must produce a failed outcome with a useful summary, not a successful empty result.

Read structured errors and recovery guidance. For missing_slots, publish the missing outputs while authorized and retry complete. For state_unavailable, retain the successful operation's receipt and read state. For claim_required, recover the returned execution_id rather than beginning another. For binding_required after start, replay identical startup inputs with the returned idempotency_key/session_id. For unknown write/tool outcomes, keep the original command identity and inspect or replay it; never blindly repeat paid work. Unadapted revisions require the Workflow developer to refactor and publish, not internal-model fallback.

When Core reports awaiting_user, tell the user to review in the panel and END this turn. Do not approve, begin another step, or poll while waiting.
On awaiting_executor, stopped, completed, or binding_required, also end this turn. For completed, deliver the artifact links.
When a queued continuation arrives, read the latest Core state first. The notification is not execution authorization.
Stop is handled by Core and the host connector. Do not replace user Panel actions with your own approvals.

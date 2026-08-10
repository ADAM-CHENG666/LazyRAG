# Dataset 可视化 API 复核表

> 状态：进行中；按 API 顺序复核。P0 仅记录 API 已要求、算法侧尚未满足的能力。
>
> 边界：算法负责 Artifact、业务校验和依赖；Runtime 负责版本/状态/重算；Service 负责 HTTP、鉴权、分页、SSE 和响应投影。

## 新版 Runtime / Flow 速查

| 需求 | Runtime / Flow |
| --- | --- |
| 总体 / 阶段状态 | `snapshot()` / `stage_snapshot()` |
| 单 Case 状态 | `case_snapshot()` |
| 当前 / 历史 Artifact | `head()` + `read()` / `history()` |
| 原子修改已有 Artifact | `update_artifacts()` |
| 原子调整 Case 集合 | `commit()` + `PartitionSet` |
| 运行事件 | `operation_events()`；不替代状态快照 |

## API 复核

| API | Service调用 | Artifact / 数据 | 算法 P0 | Runtime 缺口 | 结论 |
| --- | --- | --- | --- | --- | --- |
| `GET /threads/{thread_id}/steps` | 分别调用 `stage_snapshot('dataset.material_preparation')`、`stage_snapshot('dataset.topic_discovery')`、`stage_snapshot('dataset.case_generation')`，读取各自的 `status`，按固定顺序返回三项。 | 三个 Dataset Stage 的 `status`、operation、attempt、result refs | **P0：将 Dataset 定义为材料准备、主题发现、用例生成三个 Stage，并为每个 Stage 确定 `result_artifacts`。** | 无 | Stage 拆分完成后可直接实现；Service 不再按 operation 聚合状态。 |
| `GET /threads/{thread_id}/events:stream` | 轮询 `ProjectionService.events(thread_id, step_id='', after_event_id)` 获取新增事件；SSE 原样发送，前端按事件的 `stage` 刷新页面。 | Flow / stage 状态、结果版本；`operation_events()` 仅用于需要展示内部日志的接口 | 无 | 需要复核 event 接口已有上报时机。 | 复用现有 SSE 轮询实现。 |
| `GET /threads/{thread_id}/dataset/materials/overview`<br>`GET /threads/{thread_id}/dataset/topics/overview`<br>`GET /threads/{thread_id}/dataset/cases/overview` | 对每个 Stage：<br>&nbsp;&nbsp;- `stage_snapshot(stage)` 读取 `status`<br>&nbsp;&nbsp;- `head/read(result_artifacts)` 读取概览数据并投影 DTO<br>[用例生成：按 Case 汇总三项 operation 的状态。] | 材料准备：`dataset.build_chunks_manifest`<br>主题发现：`dataset.topic_discovery_manifest`<br>用例生成：`dataset.qaplan_plan_params`、`dataset.qaplan_manifest`、`qaplan_spec/case/case_enhance` | 主题发现：小数据集需产出合法空 Manifest，不使 Stage 失败。 | 无 | 材料概览已满足；主题空结果待补；用例概览待补 `revision`、`automatic_plan` 和三步状态汇总。 |
| `GET /threads/{thread_id}/dataset/materials/documents`<br>`GET /threads/{thread_id}/dataset/topics`<br>`GET /threads/{thread_id}/dataset/cases` | 对每个列表：<br>&nbsp;&nbsp;- `head/read` 读取当前结果<br>&nbsp;&nbsp;- 按请求筛选并按 Artifact 稳定顺序分页<br>&nbsp;&nbsp;- `page_token` 绑定 revision、筛选条件和页大小<br>[用例生成：组合每个 Case 的三步状态。] | 材料准备：`dataset.selected_docs`、Chunk 候选统计<br>主题发现：`dataset.topic_discovery_manifest.topics[]`<br>用例生成：`qaplan_spec`、`dataset.case`、`dataset.case_enhance`、Case Snapshot | 无 | 无 | 材料与主题列表已满足；用例列表需仅投影三项用例生成 operation。 |
| `GET /threads/{thread_id}/dataset/materials/knowledge-bases/{kb_id}/documents/{doc_id}`<br>`GET /threads/{thread_id}/dataset/topics/{topic_id}`<br>`GET /threads/{thread_id}/dataset/cases/{case_id}` | 对每个详情：<br>&nbsp;&nbsp;- `head/read` 按业务 ID 定位对象<br>&nbsp;&nbsp;- 组合详情与当前 revision<br>&nbsp;&nbsp;- 内部列表按 Artifact 稳定顺序分页<br>[用例生成：组合三个 Case Artifact 与三步状态。] | 材料准备：`dataset.build_chunk_candidates`<br>主题发现：Topic Manifest、当前 `dataset.chunk`<br>用例生成：`qaplan_spec`、`dataset.case`、`dataset.case_enhance`、Case Snapshot | 无 | 无 | 文档和主题详情已满足；用例详情需组合 Case revision，并只投影三项用例生成 operation。 |
| `GET /threads/{thread_id}/dataset/materials/adjustment-options` | Projection 聚合：<br>&nbsp;&nbsp;- Evo `head/read`：读取当前 `source_config`、`dataset.select_docs_params`、`dataset.build_chunks_params` 和配置 `revision`<br>&nbsp;&nbsp;- Core `GET /datasets`（或等价内部目录调用）：按当前用户 ACL 获取可访问 KB 的 ID、展示名与 `algo_id`<br>&nbsp;&nbsp;- 解析服务 `GET /v1/algo/{algo_id}/groups`：读取各算法的活跃 Chunk 切分组与展示名<br>&nbsp;&nbsp;- **解析能力目录接口（当前未实现，P0）**：读取标准版面类型目录及各 `algo_id` 当前可产出的类型集合<br>&nbsp;&nbsp;- Projection 按材料来源的 `algo_id` 对切分组和版面类型能力分别取交集，再合并“支持目录”与“当前配置”返回 DTO | 当前 `source_config`、`dataset.select_docs_params`、`dataset.build_chunks_params`；Core Dataset/ACL 目录；解析算法 Group 与版面能力目录 | **P0：新增解析能力目录接口。**；~~**P0：`select_docs` 落地知识库级 `included`。**~~；~~**P0：对 Reader 原始 `metadata.type` 做标准类型映射，令 `allowed_types` 仅接受标准版面目录的子集。**~~ | 无 | 一个接口同时返回当前配置和支持目录。Core 负责身份/ACL；Evo 仅提供当前配置；算法不得依赖扫描结果生成目录。 |
| `GET /threads/{thread_id}/dataset/cases/{case_id}/topic-options` | - 读取当前 Case 规划与 Topic Manifest<br>- 读取其他 Case 的选题事实<br>- 按题型、难度和已占用主题过滤后分页 | 当前 `qaplan_spec`、`dataset.topic_discovery_manifest`、全部 Case 的选题事实 | 无 | 无 | 可实现；仅对已完成规划的 generated Case 返回候选主题。 |
| `POST /threads/{thread_id}/dataset/materials:apply`（扫描配置） | - 校验知识库、文档、切分规则、版面类型和目标用例数<br>- 单次 CAS `commit()` 写入全部材料配置<br>- Runtime 从材料扫描链路重新规划 | `run.config` / `corpus.source_config`、`dataset.select_docs_params`、`dataset.build_chunks_params`、当前 Case/Chunk `PartitionSet` | ~~**P0：新增并 seed `dataset.select_docs_params`，接入 `select_docs` operation**~~；~~**P0：知识库级 `included` 必须作为独立材料配置持久化，保留文档级排除配置；`select_docs` 应输出全部已配置来源的统一文档快照，并以 `kb.included && !document_excluded` 计算最终 `included`，`build_chunk_candidates` 仅扫描最终入选文档。**~~ | 无 | `select_docs` 的文档级接线与知识库级调整已完成。建议：后续支持目标用例数调整时，Service 用单次 CAS 提交配置与 Case/Chunk `PartitionSet`；在此之前不接受该字段。 |
| `POST /threads/{thread_id}/dataset/materials:apply`（片段入围） | - 读取完整候选 Artifact，校验片段有效性与精确配额<br>- 单次 CAS `update_artifacts()` 写回完整候选结果，仅修改 `selected`<br>- 不调整 `PartitionSet`；Runtime 仅从 `build_chunks` 开始重算下游 | `dataset.build_chunk_candidates` 的完整值 | ~~**P0：支持全量有效 Chunk 的 `selected` 修改及配额校验**~~ | 无 | 已完成；不重跑候选扫描。 |
| `POST /threads/{thread_id}/dataset/topics:apply`<br>`POST /threads/{thread_id}/dataset/generation-plan:apply` | - `head/read` 读取当前 Artifact 并校验 revision<br>- 校验修改内容<br>- `update_artifacts()`：CAS 写入完整 Artifact<br>- Runtime 按依赖从 qaplan 重算下游<br>[主题：按 `topic_id` 合并名称修改，不重跑主题发现。]<br>[生成计划：读取 Topic Manifest，校验完整六路配额与容量。] | 主题：`dataset.topic_discovery_manifest.topics[]`<br>生成计划：`dataset.qaplan_plan_params` | 主题：~~**P0：以 `topic_id` 定位，只改 `name`**~~；~~**P0：qaplan 改为消费扁平 Topic 输出**~~<br>生成计划：~~**P0：删除历史 `lane_ratios` 输入与 `qaplan_plan.params.lane_ratios` 输出；仅保留可选的完整六路 `lane_case_counts`。未提交时算法按当前自动生成数一次性做 1:1 整数初始分配，不保存或重用比例。**~~ | 无；Runtime 按依赖使 qaplan 与下游 Case 失效 | 两者均不重跑上游 Stage；主题修改与生成计划算法已完成，Service 待实现校验与 CAS 写入。 |
| `PATCH /threads/{thread_id}/dataset/cases/{case_id}` | - `head/read`：读取当前 Case、Topic Manifest 与有效 Chunk<br>- Service 依据 `topic_id` 查询事实源并重建完整 `qaplan_spec`<br>- 校验 Case 修改<br>- `update_artifacts()`：原子替换 `qaplan_spec` 与用户提交的 Case Artifact<br>- Runtime 重算受影响下游 | 每 Case 可编辑选题事实、`dataset.qaplan_spec`、`dataset.case`、`dataset.case_enhance` | ~~**P0：将每个 Case 的 `plan.topic_id` 建模为唯一可编辑选题事实；`topic_discovery_manifest` 是 Topic 事实源，`dataset.chunk` 是引用内容事实源；算法依据 `topic_id` 派生 `qaplan_spec` 的主题名称、完整引用和 instruction。**~~ | 无；新版 Runtime 支持多个完整 Artifact 的 `update_artifacts()` 与受影响下游重算 | 算法已完成；Service 的查询、派生和 CAS 编排待实现。 |

## 待复核顺序

1. 材料准备：文档详情、调整选项、应用修改。
2. 主题发现：概览、列表、详情、名称修改。
3. 用例生成：概览、列表、详情、可替换主题、生成计划修改、保存单 Case。

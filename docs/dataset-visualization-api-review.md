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

| API | Service / Runtime 调用 | Artifact / 数据 | 算法 P0 | Runtime 缺口 | 结论 |
| --- | --- | --- | --- | --- | --- |
| `GET /threads/{thread_id}/steps` | `flow.snapshot()`；投影当前步骤和状态 | Flow stages / result refs | **P0：五个 Dataset 执行子步骤映射为三产品步骤：材料=`build_chunks`；主题=`topic_discovery`；用例=`qaplan`、`generate`、`generate_enhance`（及后续阶段）** | 无 | 现有 Projection 雏形改读新版 Snapshot |
| `GET /threads/{thread_id}/events:stream` | 短轮询 `snapshot()` / `stage_snapshot()`；变更时发 `dataset.updated` | 状态、结果 ref、可选 `operation_events()` | 无；`targets` 由 Service 映射 | 无 | 已确认：允许 1–2 秒短轮询，无需 Runtime 推送订阅 |
| `GET /threads/{thread_id}/dataset/materials/overview` | `stage_snapshot('dataset.build_chunks')` + `head/read` | `dataset.build_chunks_manifest` 的 `case_counts`、`chunk_counts`、`warnings`；head 为 revision | 无；比率由 Service 计算 | 无 | 已满足；重算期间继续返回上一个有效 Manifest |
| `GET /threads/{thread_id}/dataset/materials/documents` | `head/read` 后筛选、分页 | `dataset.selected_docs` + 材料候选结果的文档统计 | ~~**P0：保留统一发现顺序的 `documents[]`：`kb_id`、`doc_id`、名称、`included`、`discovery_index`；补每文档有效/入选 Chunk 统计**~~ | 无 | 已完成；Service 不再拼接入选/排除两组文档 |
| `GET /threads/{thread_id}/dataset/materials/knowledge-bases/{kb_id}/documents/{doc_id}` | `head/read` 后按文档筛选、分页 | `dataset.build_chunk_candidates`：所有有效 Chunk、`selected`、来源、稳定顺序、规则配额 | ~~**P0：重构候选结果；保留所有有效 Chunk（非仅入围）；每个 Chunk 带 `selected`；保留每文档/切分规则的精确 `required` 配额**~~ | 无 | 已完成；有效性标准重跑候选扫描，入围性直接更新候选 Artifact、仅重算下游 |
| `GET /threads/{thread_id}/dataset/materials/adjustment-options` | `head/read` 当前配置；调用 Core 目录接口 | 当前 `source_config`、文档选择参数、`build_chunks_params` | 无；算法只校验已选 ID 并按其筛选 | 无 | **知识库、切分规则、版面类型的完整可选项与展示名称由 Core/Service 提供；算法不得靠扫描结果生成目录。** |
| `POST /threads/{thread_id}/dataset/materials:apply`（扫描配置） | 原子更新上游配置后重启 Dataset 规划 | `run.config` / `corpus.source_config`、`dataset.select_docs_params`、`dataset.build_chunks_params` | ~~**P0：新增并 seed `dataset.select_docs_params`，接入 `select_docs` operation**~~；Case/Chunk 重建由 Evo 框架负责 | 无；新版 Runtime 支持上游变更后的失效与重算 | 已完成接线；不是 Service 对现有 Case 的增删 |
| `POST /threads/{thread_id}/dataset/materials:apply`（片段入围） | 校验精确配额后 `update_artifacts()` | `dataset.build_chunk_candidates` 的完整值 | ~~**P0：支持全量有效 Chunk 的 `selected` 修改及配额校验**~~ | 无 | 已完成；仅使 `build_chunks` 及下游失效，不重跑候选扫描 |

## 待复核顺序

1. 材料准备：文档详情、调整选项、应用修改。
2. 主题发现：概览、列表、详情、名称修改。
3. 用例生成：概览、列表、详情、可替换主题、生成计划修改、保存单 Case。

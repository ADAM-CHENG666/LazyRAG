# QAPlan dataset runtime

## 快速定位

```text
KB → chunk[1..ceil(1.5N)] → topic manifest → qaplan → case[1..N] → generation manifest
```

输入是 `source_config.target_case_count` 和各 operation params；输出是
`dataset.qaplan_generation_manifest`。本次只覆盖 dataset 阶段，不接 eval、analysis、repair。

## 模块落点

| 文件 | 职责 |
| --- | --- |
| `evo/artifact_runtime/evo/flow.py` | `DatasetFlowSpec` 同时静态声明 chunk/case partitions。 |
| `evo/artifact_runtime/evo/flow_ops.py` | `qaplan_dataset_evo_ops()` 声明新图与两次 partition 转换。 |
| `evo/operations/dataset/qaplan_pipeline.py` | 注册真实 materializer，并向 LLM operation 注入 `run_config.llm_config`。 |
| `evo/operations/dataset/qaplan.py` | 生成结果的轻量 terminal manifest。 |
| `scripts/run_qaplan_dataset_experiment.py` | 基于真实 artifact runtime 的 run/resume/inspect 调试入口。 |

## 执行与配置

`target_chunk_count` 不再是可覆盖参数。创建 graph 前，runner 以
`ceil(1.5 * target_case_count)` 解析 chunk partitions，并将结果写入
`resolved_config.json`。因此运行参数不能与静态图规模不一致。

runner 的 JSON 必须提供：

```text
run_config.llm_config
source_config.kb_id
source_config.target_case_count
```

其他 params 可省略并使用 runner 默认值。详细 Docker 命令和 smoke 配置见
`lazyrag-workbench/scripts/autodataset/README.md`。

## 可观测性与测试

每次 runner 执行会保留 SQLite artifact store，并导出每个有效 artifact 到
`exports/`。可先运行至 `dataset.build_chunks_manifest` 或
`dataset.topic_discovery_manifest`，检查后再从同一 run 继续。

`tests/evo_runtime/test_qaplan_dataset_runtime.py` 覆盖 hermetic 的
`3 chunks → 2 cases` 转换、终点 manifest 和双 partition artifact key。

## 开放边界

- 新 `eval.case` 契约尚未迁移到旧 eval/analysis 链路；本次刻意不注册后续阶段。
- Thread/API 接入将在新 case 契约由下游确认后再做。

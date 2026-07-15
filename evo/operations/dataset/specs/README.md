# Evo Operation 开发速读

本文用于让 AI 快速理解 evo operation 框架，并能读懂 `evo/operations/dataset/specs/*.yaml` 中的 operation 规格。

## 1. 框架模型

一个 operation 由两部分组成：

```text
FixedOp：声明图节点，包括 op_id、输入 artifact、输出 artifact、partition 关系。
materializer：执行逻辑，读取 inputs，返回 output_name -> payload。
```

runtime 负责读取输入、调用 materializer、校验返回 key、提交输出。operation 代码只关注业务逻辑和 payload。

核心文件：

```text
evo/artifact_runtime/kernel/ops.py           # FixedOp
evo/artifact_runtime/kernel/artifact.py      # ArtifactInput / ArtifactOutput / ArtifactKey
evo/artifact_runtime/kernel/materializer.py  # MaterializerContext
evo/artifact_runtime/kernel/runtime.py       # 调度与提交
evo/artifact_runtime/evo/catalog.py          # artifact id 总表
evo/artifact_runtime/evo/flow_ops.py         # evo flow 的 FixedOp 声明
evo/service/runtime_port.py                  # runtime 装配入口
```

领域实现位于：

```text
evo/operations/dataset/
evo/operations/eval/
evo/operations/analysis/
evo/operations/repair/
evo/operations/abtest/
```

## 2. 运行链路

`RuntimePort.adapter()` 会把图声明和 materializer 组合起来：

```text
default_evo_ops(spec.cases)
  -> 注册所有 FixedOp

dataset_materializers(spec.cases)
eval_materializers()
analysis_materializers()
repair_materializers()
abtest_materializers()
  -> 提供 op_id 对应的执行函数
```

新增 operation 时，`FixedOp.op_id` 必须和 materializer dict 的 key 一致。

## 3. 参数模型

operation 的参数也是输入 artifact。需要用户控制的参数统一建模为专属 params 输入：

```text
input name: <operation_name>_params
artifact id: <flow>.<operation_name>_params
```

例如 `dataset.build_chunks` 使用：

```text
build_chunks_params -> dataset.build_chunks_params
```

这样 operation 可以独立运行：只要上游依赖 artifact 和 params artifact 都存在，runtime 就能调度它。`run.config` 是线程级共享运行配置，不作为单个 operation 的专属参数入口。

## 4. YAML 字段含义

### `operation`

描述 operation 的身份和位置。

```yaml
operation:
  op_id: dataset.select_docs
  flow: dataset
  stage: select_docs
  description: ...
```

含义：

```text
op_id       唯一执行 id，格式通常为 <flow>.<action>
flow        所属阶段，对应 catalog.py 的 OUTPUTS 分组
stage       人类可读的步骤名，用于理解顺序
description operation 职责摘要
```

### `fixed_op`

描述图节点输入输出，应落到 `flow_ops.py`。

```yaml
fixed_op:
  inputs:
    selected_docs:
      artifact_id: dataset.selected_docs
      required: true
      partition: unpartitioned
    build_chunks_params:
      artifact_id: dataset.build_chunks_params
      required: true
      partition: unpartitioned
  outputs:
    built_chunks:
      artifact_id: dataset.built_chunks
      partition: unpartitioned
```

映射关系：

```text
inputs.<name>   -> FixedOp.inputs 的 key，也是 materializer inputs 的 key
outputs.<name>  -> FixedOp.outputs 的 key，也是 materializer 返回 dict 的 key
artifact_id     -> catalog.py 中的 artifact 常量
partition       -> 是否按 case 分区
mapping         -> partition 映射方式；可省略，默认 same_partition
```

当 input 和 output 都是 `unpartitioned` 时，通常省略 `mapping`。只有跨 partition 传递时显式写 mapping。

常用 partition mapping：

```text
same_partition        同分区传递
unpartitioned_to_all  全局输入分发到每个 case
all_to_unpartitioned  多个 case 聚合成 tuple
```

### `materializer`

描述执行函数和返回 key。

```yaml
materializer:
  function: select_docs
  return_keys:
    - selected_docs
```

实现形态：

```python
def select_docs(ctx, inputs):
    ...
    return {'selected_docs': payload}
```

`return_keys` 必须等于 `fixed_op.outputs` 的 key 集合。

### `input_payload`

描述输入 payload 的字段、默认值和校验规则。实现时通常对应参数解析或 contract 校验。

```yaml
input_payload:
  build_chunks_params:
    fields:
      groups:
        type: list[string]
        required: false
        default:
          - block
```

字段来源是输入 artifact 的 payload，不是单独的函数参数。

### `output_payload`

描述输出 payload 的 JSON 结构。实现时应保证：

```text
字段完整
类型稳定
可 JSON 序列化
公共输出优先使用 evo/operations/public_contracts.py 校验
```

### `behavior`

定义职责和执行流程。

```yaml
behavior:
  responsibility:
    - ...
  execution_flow:
    - ...
```

`responsibility` 描述本 operation 负责什么；`execution_flow` 描述实现时的主要步骤。执行流程应与输入、输出和测试点保持一致。

### `tests`

列出期望测试点。常见类型：

```text
contract                 payload contract / schema 校验
success                  主成功路径
validation_error          输入校验失败
return_keys_match_outputs 返回 key 与 FixedOp.outputs 一致
partition                 分区输入输出行为
```

## 5. 新增 Operation 步骤

先复核 YAML，再实现。

```text
1. 复核 YAML 是否完整、清晰、无冲突。
2. 如有疑问，先向用户列出问题并等待确认。
3. 用户补充确认后，再开始改代码。
4. 在 catalog.py 新增 artifact id，并加入对应 OUTPUTS。
5. 在 flow_ops.py 新增 FixedOp，声明 inputs / outputs / partition。
6. 在 evo/operations/<flow>/ 实现业务函数和 materializer。
7. 在对应 *_materializers() 中返回 {op_id: materializer}。
8. 如 runtime_port.py 尚未合并该 materializer 集合，补充接入。
9. 在 tests/evo/ 增加 YAML 中列出的测试。
```

最小检查清单：

```text
[ ] YAML 字段完整，语义清晰
[ ] responsibility / execution_flow 无冲突
[ ] input_payload / output_payload 与 fixed_op 输入输出一致
[ ] operation 参数使用 <operation_name>_params 输入
[ ] op_id 唯一，并与 materializer dict key 一致
[ ] fixed_op.outputs key 与 materializer return key 一致
[ ] artifact_id 已在 catalog.py 中登记
[ ] partition / mapping 与输入输出形态一致；unpartitioned 同步输入可省略 mapping
[ ] payload 可 JSON 序列化
[ ] 行为覆盖 YAML 的 responsibility
[ ] 测试覆盖 YAML 的 tests
```

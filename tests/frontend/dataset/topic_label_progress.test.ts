import {
  applyTopicLabelPartitionEvent,
  summarizeTopicLabelProgress,
  type TopicLabelProgress,
} from "../../../frontend/src/modules/selfEvolution/components/workbench/dataset/topicLabelProgress";

function equal<T>(actual: T, expected: T, message: string) {
  if (actual !== expected) {
    throw new Error(`${message}: expected ${String(expected)}, got ${String(actual)}`);
  }
}

function event(overrides: Record<string, unknown> = {}) {
  return {
    event: "dataset.label_embedding_cluster",
    stage: "dataset.topic_discovery",
    operationId: "dataset.label_embedding_cluster",
    stepId: "thread-1:dataset.topic_discovery:1",
    partition: { id: "candidate-1", index: 1, total: 3 },
    status: "running",
    ...overrides,
  };
}

let progress: TopicLabelProgress | undefined;
progress = applyTopicLabelPartitionEvent(progress, event());
progress = applyTopicLabelPartitionEvent(progress, event({
  partition: { id: "candidate-2", index: 2, total: 3 },
  status: "completed",
}));
progress = applyTopicLabelPartitionEvent(progress, event({
  status: "completed",
}));

let summary = summarizeTopicLabelProgress(progress);
equal(summary.total, 3, "partition total is retained");
equal(summary.completed, 2, "terminal event replaces prior running state");
equal(summary.running, 0, "replaced partition is not counted twice");
equal(summary.failed, 0, "no failed partition yet");

progress = applyTopicLabelPartitionEvent(progress, event({
  partition: { id: "candidate-3", index: 3, total: 3 },
  status: "failed",
}));
summary = summarizeTopicLabelProgress(progress);
equal(summary.failed, 1, "failed partition is counted");

const ignored = applyTopicLabelPartitionEvent(progress, event({
  operationId: "dataset.embedding_label_manifest",
}));
equal(ignored, progress, "non-label operation is ignored");

progress = applyTopicLabelPartitionEvent(progress, event({
  stepId: "thread-1:dataset.topic_discovery:2",
  partition: { id: "candidate-1", index: 1, total: 2 },
}));
summary = summarizeTopicLabelProgress(progress);
equal(summary.total, 2, "new stage execution starts a fresh progress set");
equal(summary.running, 1, "new stage execution keeps its new running partition");
equal(summary.completed, 0, "old execution completions are discarded");
equal(summary.failed, 0, "old execution failures are discarded");

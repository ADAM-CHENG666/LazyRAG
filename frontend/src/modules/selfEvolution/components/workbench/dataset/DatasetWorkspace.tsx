import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Empty, Modal, message } from "antd";
import { CasesStage } from "./CasesStage";
import { MaterialsStage } from "./MaterialsStage";
import { TopicsStage } from "./TopicsStage";
import { datasetRoot, describeRequestError, newRequestId, postJson } from "./api";
import { STATUS_TEXT } from "./primitives";
import { applyTopicLabelPartitionEvent, type TopicLabelProgress } from "./topicLabelProgress";
import { DATASET_TABS, useDatasetStages, type DatasetStreamEvent } from "./useDatasetStages";
import "./dataset.scss";
import {
  DRAFT_IMPACT_DETAIL,
  DRAFT_IMPACT_START,
  DRAFT_LABELS,
  type DatasetDraft,
  type DatasetTab,
} from "./types";

const STEP_SYMBOL: Record<string, string> = {
  done: "✓",
  running: "●",
  stale: "↻",
  failed: "!",
};

export function DatasetWorkspace({ threadId }: { threadId?: string }) {
  const [tab, setTab] = useState<DatasetTab>("materials");
  const [refreshToken, setRefreshToken] = useState(0);
  const [overviewToken, setOverviewToken] = useState(0);
  const [topicLabelProgress, setTopicLabelProgress] = useState<TopicLabelProgress>();
  const [staleTab, setStaleTab] = useState<DatasetTab>();
  const [draft, setDraft] = useState<DatasetDraft>();
  const [applying, setApplying] = useState(false);
  const followActiveStage = useRef(true);
  const revisions = useRef<Partial<Record<DatasetTab, string | null>>>({});
  // Set while an event-triggered overview reload is in flight, so that manual
  // refreshes and stage switches never raise the "data changed" banner.
  const probing = useRef<DatasetTab>();
  const tabRef = useRef(tab);
  tabRef.current = tab;
  // Read by callbacks that must stay referentially stable for the stage panels.
  const draftRef = useRef<DatasetDraft>();
  draftRef.current = draft;

  // An event only refreshes the stage overview. Lists, open details, paging and
  // drafts stay untouched; a changed revision surfaces a refresh entry instead.
  const handleStageEvent = useCallback((event: DatasetStreamEvent) => {
    setTopicLabelProgress((current) => applyTopicLabelPartitionEvent(current, event));
    if (event.event === "step.finish" && event.tab === "topics") {
      setTopicLabelProgress(undefined);
    }
    if (
      event.tab !== tabRef.current ||
      (event.event !== "step.finish" && event.event !== "checkpoint.continue" && event.event !== "done")
    ) return;
    probing.current = event.tab;
    setOverviewToken((token) => token + 1);
  }, []);

  const handleOverviewRevision = useCallback((stageTab: DatasetTab, revision: string | null) => {
    const previous = revisions.current[stageTab];
    revisions.current[stageTab] = revision;
    if (probing.current !== stageTab) return;
    probing.current = undefined;
    if (previous !== undefined && previous !== revision) {
      setStaleTab(stageTab);
    }
  }, []);

  const { statuses, activeTab, refreshSteps } = useDatasetStages(threadId, handleStageEvent);

  useEffect(() => {
    setTopicLabelProgress(undefined);
  }, [threadId]);

  // The executing stage only decides the default tab on first entry; later
  // progress must not pull the view away from what the user is reading.
  useEffect(() => {
    if (activeTab && followActiveStage.current) {
      followActiveStage.current = false;
      setTab(activeTab);
    }
  }, [activeTab]);

  const selectTab = (next: DatasetTab) => {
    followActiveStage.current = false;
    setStaleTab(undefined);
    setTab(next);
  };

  const refreshNow = () => {
    setStaleTab(undefined);
    // A draft targets the revision the page was showing, so it cannot survive.
    setDraft(undefined);
    setRefreshToken((token) => token + 1);
  };

  const saveDraft = useCallback((next: DatasetDraft) => {
    const current = draftRef.current;
    if (current && current.kind !== next.kind) {
      message.warning("已有待应用的修改，请先应用或放弃后再编辑其他内容。");
      return false;
    }
    if (current?.kind === "topic-names" && next.kind === "topic-names") {
      setDraft({ ...next, names: { ...current.names, ...next.names } });
    } else {
      setDraft(next);
    }
    message.success("修改已暂存，应用前不会影响当前结果。");
    return true;
  }, []);

  const applyDraft = async () => {
    if (!threadId || !draft) return;
    setApplying(true);
    try {
      const root = datasetRoot(threadId);
      const requestId = newRequestId();
      if (draft.kind === "materials-config") {
        await postJson(`${root}/materials:apply`, {
          request_id: requestId,
          expected_revision: draft.revision,
          changes: draft.changes,
        });
      } else if (draft.kind === "chunk-selection") {
        await postJson(`${root}/materials:apply`, {
          request_id: requestId,
          expected_revision: draft.revision,
          changes: { chunk_selection_changes: draft.changes },
        });
      } else if (draft.kind === "topic-names") {
        await postJson(`${root}/topics:apply`, {
          request_id: requestId,
          expected_revision: draft.revision,
          changes: Object.entries(draft.names).map(([topic_id, name]) => ({ topic_id, name })),
        });
      } else {
        await postJson(`${root}/generation-plan:apply`, {
          request_id: requestId,
          expected_revision: draft.revision,
          distribution: draft.distribution,
        });
      }
      setDraft(undefined);
      setStaleTab(undefined);
      setRefreshToken((token) => token + 1);
      void refreshSteps();
      message.success("修改已应用，受影响的步骤将重新执行。");
    } catch (error) {
      message.error(describeRequestError(error, "应用修改失败"));
    } finally {
      setApplying(false);
    }
  };

  const confirmApply = () => {
    if (!draft) return;
    const start = DRAFT_IMPACT_START[draft.kind];
    Modal.confirm({
      className: "dataset-impact-modal",
      title: "确认修改影响",
      width: 520,
      content: (
        <div className="dataset-impact-copy">
          <p>系统会保留当前结果，完成更新后再替换为新版本。</p>
          <div className="dataset-impact-flow">
            {DATASET_TABS.map((item, index) => (
              <div
                className={`dataset-impact-node${
                  index === start ? " is-changed" : index > start ? " is-affected" : ""
                }`}
                key={item.id}
              >
                {item.label}
                <span>{index === start ? "本次修改" : index > start ? "需更新" : "不受影响"}</span>
              </div>
            ))}
          </div>
          <div className="dataset-impact-detail">{DRAFT_IMPACT_DETAIL[draft.kind]}</div>
        </div>
      ),
      okText: "确认并更新受影响步骤",
      cancelText: "暂不应用",
      onOk: applyDraft,
    });
  };

  if (!threadId) {
    return (
      <Empty
        className="dataset-workspace-empty"
        description="创建或打开一个自进化任务后，可在这里查看 Dataset 过程。"
      />
    );
  }

  return (
    <section className="dataset-workspace" aria-label="数据集自动构建">
      <nav className="dataset-stepper" aria-label="数据集内部步骤">
        {DATASET_TABS.map((item, index) => {
          const status = statuses[item.id];
          return (
            <button
              type="button"
              key={item.id}
              className={`dataset-step is-${status}${tab === item.id ? " is-selected" : ""}`}
              onClick={() => selectTab(item.id)}
            >
              <span className="dataset-step-dot">{STEP_SYMBOL[status] || index + 1}</span>
              <span className="dataset-step-copy">
                <span className="dataset-step-name">{item.label}</span>
                <span className="dataset-step-status">{STATUS_TEXT[status]}</span>
              </span>
            </button>
          );
        })}
      </nav>

      {staleTab === tab ? (
        <div className="dataset-stale-banner">
          <span>
            该阶段结果已更新，当前列表与详情仍是你打开时的数据。
            {draft ? "刷新会丢弃尚未应用的修改。" : ""}
          </span>
          <Button size="small" onClick={refreshNow}>
            刷新
          </Button>
        </div>
      ) : null}

      <div className="dataset-content">
        {tab === "materials" ? (
          <MaterialsStage
            threadId={threadId}
            refreshToken={refreshToken}
            overviewToken={overviewToken}
            onOverviewRevision={handleOverviewRevision}
            draft={draft}
            onSaveDraft={saveDraft}
          />
        ) : tab === "topics" ? (
          <TopicsStage
            threadId={threadId}
            refreshToken={refreshToken}
            overviewToken={overviewToken}
            labelProgress={topicLabelProgress}
            onOverviewRevision={handleOverviewRevision}
            draft={draft}
            onSaveDraft={saveDraft}
          />
        ) : (
          <CasesStage
            threadId={threadId}
            refreshToken={refreshToken}
            overviewToken={overviewToken}
            onOverviewRevision={handleOverviewRevision}
            onSaveDraft={saveDraft}
            onCaseSaved={() => {
              setRefreshToken((token) => token + 1);
              void refreshSteps();
            }}
          />
        )}
      </div>

      {draft ? (
        <footer className="dataset-change-bar">
          <div className="dataset-change-copy">
            <strong>{DRAFT_LABELS[draft.kind]}修改尚未应用</strong>
            <span>暂未影响正在运行的流程；确认影响范围后才会更新结果。</span>
          </div>
          <Button size="small" type="text" onClick={() => setDraft(undefined)}>
            放弃修改
          </Button>
          <Button size="small" type="primary" loading={applying} onClick={confirmApply}>
            查看影响并应用
          </Button>
        </footer>
      ) : null}
    </section>
  );
}

import { useCallback, useEffect, useMemo, useState } from "react";
import { datasetRoot, getJson } from "./api";
import { CaseDetailDrawer } from "./CaseDetailDrawer";
import { GenerationPlanDrawer } from "./GenerationPlanDrawer";
import { PAGE_SIZE, useDatasetList, useDatasetResource } from "./hooks";
import {
  Chip,
  ColumnFilter,
  DIFFICULTY_TEXT,
  ListPlaceholder,
  OverviewPane,
  QUESTION_TYPE_TEXT,
  ScrollSentinel,
  StageProgressTrack,
  StatusIcon,
  ratio,
  toVisualStatus,
} from "./primitives";
import { progressSummary } from "./topicLabelProgress";
import { usePublishDatasetStageAction } from "./stageAction";
import type {
  CaseRow,
  CaseSource,
  CaseStageKey,
  CasesOverview,
  DatasetDraft,
  Difficulty,
  OperationStatus,
  PagedResponse,
  QuestionType,
} from "./types";

type Props = {
  threadId: string;
  refreshToken: number;
  overviewToken: number;
  onOverviewRevision: (tab: "cases", revision: string | null) => void;
  onSaveDraft: (draft: DatasetDraft) => boolean;
  onCaseSaved: () => void;
};

const STAGE_ORDER: CaseStageKey[] = ["plan", "generate", "grading"];
const STAGE_LABEL: Record<CaseStageKey, string> = {
  plan: "生成规划",
  generate: "问答生成",
  grading: "判分规则",
};

const STATUS_OPTIONS: Array<{ value: OperationStatus; label: string }> = [
  { value: "succeeded", label: "已完成" },
  { value: "running", label: "执行中" },
  { value: "pending", label: "未开始" },
  { value: "failed", label: "失败" },
];

const QUESTION_TYPES: QuestionType[] = ["precision", "reasoning"];
const DIFFICULTIES: Difficulty[] = ["easy", "medium", "hard"];

type StageFilters = Partial<Record<CaseStageKey, OperationStatus>>;

export function CasesStage({
  threadId,
  refreshToken,
  overviewToken,
  onOverviewRevision,
  onSaveDraft,
  onCaseSaved,
}: Props) {
  const [stageFilters, setStageFilters] = useState<StageFilters>({});
  const [source, setSource] = useState<CaseSource>();
  const [questionType, setQuestionType] = useState<QuestionType>();
  const [difficulty, setDifficulty] = useState<Difficulty>();
  const [openCase, setOpenCase] = useState<CaseRow>();
  const [planOpen, setPlanOpen] = useState(false);

  const root = datasetRoot(threadId);

  const fetchOverview = useCallback(
    () => getJson<CasesOverview>(`${root}/cases/overview`),
    [root],
  );
  const overview = useDatasetResource(
    fetchOverview,
    refreshToken + overviewToken,
    "用例概览加载失败",
  );

  useEffect(() => {
    if (overview.data) onOverviewRevision("cases", overview.data.revision);
  }, [onOverviewRevision, overview.data]);

  const fetchCases = useCallback(
    (pageToken?: string) =>
      getJson<PagedResponse<CaseRow>>(`${root}/cases`, {
        page_size: PAGE_SIZE,
        page_token: pageToken,
        plan_status: stageFilters.plan,
        generate_status: stageFilters.generate,
        grading_status: stageFilters.grading,
        source,
        question_type: questionType,
        difficulty,
      }),
    [difficulty, questionType, root, source, stageFilters],
  );
  const cases = useDatasetList(fetchCases, refreshToken, "用例列表加载失败");

  usePublishDatasetStageAction(
    useMemo(() => ({ label: "调整生成计划", onClick: () => setPlanOpen(true) }), []),
  );

  const plan = overview.data?.automatic_plan;
  const hasFilters = Boolean(
    source || questionType || difficulty || Object.values(stageFilters).some(Boolean),
  );
  const clearFilters = () => {
    setStageFilters({});
    setSource(undefined);
    setQuestionType(undefined);
    setDifficulty(undefined);
  };

  return (
    <>
      <div className="dataset-overview-row">
        <OverviewPane
          title="生成进度"
          extra={
            overview.data?.stages.plan.total != null
              ? `目标 ${overview.data.stages.plan.total} 个用例`
              : undefined
          }
        >
          {overview.error ? (
            <p className="dataset-pane-error">{overview.error}</p>
          ) : (
            <StageProgressTrack
              steps={STAGE_ORDER.map((key) => {
                const progress = overview.data?.stages[key];
                const status = toVisualStatus(progress?.status || "pending");
                const completed = progress?.succeeded ?? 0;
                const total = progress?.total ?? 0;
                const counts = progress?.status_counts;
                return {
                  key,
                  label: STAGE_LABEL[key],
                  completed,
                  total,
                  status,
                  summary: progressSummary(status, completed, total, {
                    running: counts?.running ?? 0,
                    failed: counts?.failed ?? 0,
                    pending: counts?.pending ?? 0,
                  }),
                };
              })}
            />
          )}
        </OverviewPane>
        <OverviewPane
          title="自动生成用例计划分布"
          extra={plan?.total != null ? `${plan.total} 个` : undefined}
        >
          {plan ? (
            <div className="dataset-lane-rows">
              {QUESTION_TYPES.map((type) => {
                const lane = plan.question_types?.[type];
                return (
                  <div className="dataset-lane-row" key={type}>
                    <span>{QUESTION_TYPE_TEXT[type]}</span>
                    <div className="dataset-lane-track">
                      {DIFFICULTIES.map((level) => {
                        const count = lane?.difficulties?.[level] ?? 0;
                        if (!count) return null;
                        return (
                          <span
                            key={level}
                            className={`dataset-lane-segment is-${type} is-${level}`}
                            style={{ width: `${ratio(count, plan.total)}%` }}
                          >
                            {DIFFICULTY_TEXT[level]} {count}
                          </span>
                        );
                      })}
                    </div>
                    <strong>{lane?.total ?? 0}</strong>
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="dataset-pane-error">{overview.error || "尚未产生自动生成用例计划"}</p>
          )}
        </OverviewPane>
      </div>

      <section className="dataset-list-card" aria-label="用例列表">
        <div className="dataset-table-wrap">
          <table className="dataset-object-table dataset-case-table">
            <thead>
              <tr>
                <th>用例编号</th>
                {STAGE_ORDER.map((key) => (
                  <th className={`dataset-stage-head${key === "grading" ? " is-last" : ""}`} key={key}>
                    <ColumnFilter<OperationStatus>
                      label={STAGE_LABEL[key]}
                      value={stageFilters[key]}
                      onChange={(value) => setStageFilters((prev) => ({ ...prev, [key]: value }))}
                      options={STATUS_OPTIONS}
                    />
                  </th>
                ))}
                <th>
                  <ColumnFilter<CaseSource>
                    label="来源"
                    value={source}
                    onChange={setSource}
                    options={[
                      { value: "generated", label: "自动生成" },
                      { value: "imported", label: "CSV 导入" },
                    ]}
                  />
                </th>
                <th>
                  <ColumnFilter<QuestionType>
                    label="题型"
                    value={questionType}
                    onChange={setQuestionType}
                    options={QUESTION_TYPES.map((value) => ({
                      value,
                      label: QUESTION_TYPE_TEXT[value],
                    }))}
                  />
                </th>
                <th>
                  <ColumnFilter<Difficulty>
                    label="难度"
                    value={difficulty}
                    onChange={setDifficulty}
                    options={DIFFICULTIES.map((value) => ({ value, label: DIFFICULTY_TEXT[value] }))}
                  />
                </th>
                <th>主题</th>
              </tr>
            </thead>
            <tbody>
              {cases.items.map((row) => (
                <tr
                  key={row.case_id}
                  tabIndex={0}
                  role="button"
                  onClick={() => setOpenCase(row)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      setOpenCase(row);
                    }
                  }}
                >
                  <td>
                    <strong className="dataset-case-id">{row.case_id}</strong>
                  </td>
                  {STAGE_ORDER.map((key, index) => (
                    <td
                      key={key}
                      className={`dataset-stage-cell${index === 0 ? " is-first" : ""}${
                        index === STAGE_ORDER.length - 1 ? " is-last" : ""
                      }`}
                    >
                      <StatusIcon status={toVisualStatus(row.stages[key])} />
                    </td>
                  ))}
                  <td>
                    <Chip tone={row.source === "imported" ? "imported" : "neutral"}>
                      {row.source === "imported" ? "CSV 导入" : "自动生成"}
                    </Chip>
                  </td>
                  <td>
                    <Chip tone={row.question_type === "reasoning" ? "reasoning" : "neutral"}>
                      {QUESTION_TYPE_TEXT[row.question_type]}
                    </Chip>
                  </td>
                  <td>
                    <Chip tone={row.difficulty === "easy" ? "neutral" : row.difficulty}>
                      {DIFFICULTY_TEXT[row.difficulty]}
                    </Chip>
                  </td>
                  <td>
                    <div className="dataset-ellipsis" title={row.topic?.name || "—"}>
                      {row.topic?.name || "—"}
                    </div>
                  </td>
                </tr>
              ))}
              {!cases.items.length && (
                <ListPlaceholder
                  colSpan={8}
                  loading={cases.loading}
                  error={cases.error}
                  filtered={hasFilters}
                  emptyText="尚未生成用例"
                  onRetry={cases.reload}
                  onClearFilters={clearFilters}
                />
              )}
            </tbody>
          </table>
        </div>
        <ScrollSentinel hasMore={!!cases.nextPageToken} loading={cases.loading} onLoadMore={() => void cases.loadMore()} />
      </section>

      <CaseDetailDrawer
        threadId={threadId}
        row={openCase}
        onClose={() => setOpenCase(undefined)}
        onSaved={onCaseSaved}
      />

      <GenerationPlanDrawer
        open={planOpen}
        overview={overview.data}
        onClose={() => setPlanOpen(false)}
        onSaveDraft={onSaveDraft}
      />
    </>
  );
}

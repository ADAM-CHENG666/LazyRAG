import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Modal, Space, Table, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { DownloadOutlined } from "@ant-design/icons";
import { datasetRoot, describeRequestError, downloadDatasetResult, getJson } from "./api";
import type { DatasetResultCase, DatasetResultResponse } from "./types";

const { Text } = Typography;

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

const columns: ColumnsType<DatasetResultCase> = [
  { title: "用例编号", dataIndex: "case_id", width: 140, fixed: "left" },
  { title: "问题", dataIndex: "question", width: 300, ellipsis: true },
  {
    title: "题型",
    dataIndex: "question_type",
    width: 100,
    render: (value: string) => <Tag color={value === "reasoning" ? "purple" : "blue"}>{value}</Tag>,
  },
  { title: "难度", dataIndex: "difficulty", width: 90, render: (value: string) => value || "—" },
  { title: "标准答案", dataIndex: "ground_truth", width: 320, ellipsis: true },
  { title: "评分说明", dataIndex: "grading_guidance", width: 260, ellipsis: true },
];

export function DatasetResultModal({
  threadId,
  open,
  onClose,
}: {
  threadId: string;
  open: boolean;
  onClose: () => void;
}) {
  const [result, setResult] = useState<DatasetResultResponse>();
  const [items, setItems] = useState<DatasetResultCase[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async (pageToken = "") => {
    const append = Boolean(pageToken);
    append ? setLoadingMore(true) : setLoading(true);
    setError("");
    try {
      const response = await getJson<DatasetResultResponse>(`${datasetRoot(threadId)}/result`, {
        page_size: 50,
        page_token: pageToken || undefined,
      });
      setResult(response);
      setItems((current) => append ? [...current, ...response.items] : response.items);
    } catch (requestError) {
      setError(describeRequestError(requestError, "生成结果加载失败"));
    } finally {
      append ? setLoadingMore(false) : setLoading(false);
    }
  }, [threadId]);

  useEffect(() => {
    if (!open) return;
    setResult(undefined);
    setItems([]);
    void load();
  }, [open, load]);

  const download = async () => {
    if (!result?.revision) return;
    setDownloading(true);
    try {
      const blob = await downloadDatasetResult(threadId, result.revision);
      saveBlob(blob, `dataset-${threadId}.csv`);
    } catch (requestError) {
      message.error(describeRequestError(requestError, "数据集下载失败"));
    } finally {
      setDownloading(false);
    }
  };

  return (
    <Modal
      title="生成结果"
      open={open}
      onCancel={onClose}
      width={1120}
      footer={null}
      destroyOnClose
    >
      {error ? <Alert type="error" showIcon message={error} action={<Button onClick={() => load()}>重试</Button>} /> : null}
      <div className="dataset-result-summary">
        <Space size="large">
          <Text>共 {result?.total_size ?? 0} 个用例</Text>
          {result?.completed_with_problems ? (
            <Text type="warning">{result.failed_case_count} 个计划用例生成失败，当前结果仍可使用</Text>
          ) : null}
        </Space>
        <Button
          icon={<DownloadOutlined />}
          disabled={!result?.revision}
          loading={downloading}
          onClick={download}
        >
          下载 CSV
        </Button>
      </div>
      <Table<DatasetResultCase>
        rowKey="case_id"
        columns={columns}
        dataSource={items}
        loading={loading}
        pagination={false}
        scroll={{ x: 1210, y: 520 }}
      />
      {result?.next_page_token ? (
        <div className="dataset-result-more">
          <Button loading={loadingMore} onClick={() => load(result.next_page_token)}>加载更多</Button>
        </div>
      ) : null}
    </Modal>
  );
}

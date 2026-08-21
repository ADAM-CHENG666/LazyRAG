import { useCallback, useEffect, useState } from "react";
import { describeRequestError } from "./api";
import type { PagedResponse } from "./types";

export const PAGE_SIZE = 50;
export const CHUNK_PAGE_SIZE = 100;

type ResourceState<T> = { data?: T; loading: boolean; error?: string; loadedToken?: number };

/** Loads a single dataset object (an overview, a detail, an option set). */
export function useDatasetResource<T>(
  fetchOne: (() => Promise<T>) | undefined,
  refreshToken = 0,
  failureText = "数据加载失败",
) {
  const [state, setState] = useState<ResourceState<T>>({ loading: false });
  const [localToken, setLocalToken] = useState(0);

  useEffect(() => {
    if (!fetchOne) {
      setState({ loading: false });
      return undefined;
    }
    let cancelled = false;
    setState((prev) => ({ data: prev.data, loading: true }));
    fetchOne()
      .then((data) => {
        if (!cancelled) setState({ data, loading: false, loadedToken: refreshToken });
      })
      .catch((error) => {
        if (!cancelled) setState({ loading: false, error: describeRequestError(error, failureText) });
      });
    return () => {
      cancelled = true;
    };
  }, [fetchOne, refreshToken, localToken, failureText]);

  const reload = useCallback(() => setLocalToken((token) => token + 1), []);

  return { ...state, reload };
}

type ListState<T> = {
  items: T[];
  revision: string | null;
  executionRevision?: string;
  nextPageToken: string;
  loading: boolean;
  error?: string;
  loadedToken?: number;
};

const EMPTY_LIST = { items: [], revision: null, executionRevision: undefined, nextPageToken: "", loading: false };

/**
 * Cursor paginated list. `fetchPage` must be stable (memoised on its filters);
 * changing it restarts from the first page, as required by the paging contract.
 */
export function useDatasetList<T>(
  fetchPage: ((pageToken?: string) => Promise<PagedResponse<T>>) | undefined,
  refreshToken = 0,
  failureText = "列表加载失败",
) {
  const [state, setState] = useState<ListState<T>>(EMPTY_LIST);
  const [localToken, setLocalToken] = useState(0);

  useEffect(() => {
    if (!fetchPage) {
      setState(EMPTY_LIST);
      return undefined;
    }
    let cancelled = false;
    setState((prev) => ({ ...prev, loading: true, error: undefined }));
    fetchPage()
      .then((page) => {
        if (cancelled) return;
        setState({
          items: page.items || [],
          revision: page.revision,
          executionRevision: page.execution_revision,
          nextPageToken: page.next_page_token || "",
          loading: false,
          loadedToken: refreshToken,
        });
      })
      .catch((error) => {
        if (cancelled) return;
        setState({ ...EMPTY_LIST, error: describeRequestError(error, failureText) });
      });
    return () => {
      cancelled = true;
    };
  }, [fetchPage, refreshToken, localToken, failureText]);

  const reload = useCallback(() => setLocalToken((token) => token + 1), []);

  const loadMore = useCallback(async () => {
    if (!fetchPage || !state.nextPageToken || state.loading) return;
    setState((prev) => ({ ...prev, loading: true }));
    try {
      const page = await fetchPage(state.nextPageToken);
      setState((prev) => ({
        items: [...prev.items, ...(page.items || [])],
        revision: page.revision,
        executionRevision: page.execution_revision,
        nextPageToken: page.next_page_token || "",
        loading: false,
        loadedToken: prev.loadedToken,
      }));
    } catch (error) {
      setState((prev) => ({
        ...prev,
        loading: false,
        error: describeRequestError(error, failureText),
      }));
    }
  }, [failureText, fetchPage, state.nextPageToken, state.loading]);

  return { ...state, loadMore, reload };
}

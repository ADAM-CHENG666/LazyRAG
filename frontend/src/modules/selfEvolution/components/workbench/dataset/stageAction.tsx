import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { Button } from "antd";

type StageAction = { label: string; onClick: () => void };

type StageActionStore = {
  action?: StageAction;
  setAction: (action?: StageAction) => void;
};

const DatasetStageActionContext = createContext<StageActionStore>({
  setAction: () => undefined,
});

/**
 * The page-level dataset action lives in the workbench stage header, while the
 * drawer it opens belongs to the dataset workspace below it. The provider wraps
 * both so the active sub-stage can publish its own action.
 */
export function DatasetStageActionProvider({ children }: { children: ReactNode }) {
  const [action, setAction] = useState<StageAction>();
  const store = useMemo(() => ({ action, setAction }), [action]);
  return (
    <DatasetStageActionContext.Provider value={store}>
      {children}
    </DatasetStageActionContext.Provider>
  );
}

export function DatasetStageActionButton() {
  const { action } = useContext(DatasetStageActionContext);
  if (!action) return null;
  return (
    <Button
      className="self-evolution-dataset-adjust-button"
      onClick={(event) => {
        event.stopPropagation();
        action.onClick();
      }}
    >
      {action.label}
    </Button>
  );
}

export function usePublishDatasetStageAction(action?: StageAction) {
  const { setAction } = useContext(DatasetStageActionContext);
  const label = action?.label;
  const onClick = action?.onClick;
  useEffect(() => {
    setAction(label && onClick ? { label, onClick } : undefined);
    return () => setAction(undefined);
  }, [label, onClick, setAction]);
}

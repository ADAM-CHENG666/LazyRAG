import { describe, expect, it } from 'vitest';
import {
  datasetItemFields,
  questionTypeOptions,
  requiredDatasetItemFields,
} from '../../../frontend/src/modules/datasetManagement/shared.ts';
import {
  buildImportPreview,
  createTemplateRows,
} from '../../../frontend/src/modules/datasetManagement/utils/datasetImport.ts';
import { readFrontendFile } from '../setup.js';

const messages = {
  numbersUnsupported: 'numbers',
  fileUnsupported: 'file',
  jsonFormatInvalid: 'json',
  deletedFieldInvalid: 'deleted',
  questionTypeInvalid: 'question type invalid',
  required: {
    question: 'question required',
    question_type: 'question type required',
    ground_truth: 'ground truth required',
    grading_guidance: 'grading guidance required',
  },
};

describe('dataset import contract', () => {
  it('uses only Evo question types and requires grading guidance', () => {
    expect(questionTypeOptions).toEqual(['precision', 'reasoning']);
    expect(requiredDatasetItemFields).toEqual([
      'question', 'question_type', 'ground_truth', 'grading_guidance',
    ]);
  });

  it('exposes every shared import/export field in fixed order', () => {
    expect(datasetItemFields).toEqual([
      'case_id', 'question', 'question_type', 'difficulty', 'ground_truth', 'grading_guidance',
      'key_points', 'forbidden_claims', 'reference_context', 'reference_doc', 'reference_doc_ids',
      'reference_chunk_ids', 'generate_reason', 'is_deleted',
    ]);
    expect(Object.keys(createTemplateRows({
      question: 'Q', question_type: 'precision', ground_truth: 'A', grading_guidance: 'G',
      key_points: '[]', forbidden_claims: '[]', reference_context: '', reference_doc: '',
      generate_reason: '',
    })[0])).toEqual(datasetItemFields);
  });

  it('rejects a preview row without grading guidance', () => {
    const [row] = buildImportPreview(
      [{ question: 'Q', question_type: 'precision', ground_truth: 'A', grading_guidance: '' }],
      {
        question: 'question', question_type: 'question_type', ground_truth: 'ground_truth',
        grading_guidance: 'grading_guidance',
      },
      messages,
    );
    expect(row.errors).toEqual(['grading guidance required']);
  });

  it('rejects question types outside the Evo contract', () => {
    const [row] = buildImportPreview(
      [{ question: 'Q', question_type: 'single_hop', ground_truth: 'A', grading_guidance: 'G' }],
      {
        question: 'question', question_type: 'question_type', ground_truth: 'ground_truth',
        grading_guidance: 'grading_guidance',
      },
      messages,
    );
    expect(row.errors).toEqual(['question type invalid']);
  });
});

describe('dataset final result entry', () => {
  it('publishes a header action and downloads the selected result revision', () => {
    const workspace = readFrontendFile(
      'src/modules/selfEvolution/components/workbench/dataset/DatasetWorkspace.tsx',
    );
    const modal = readFrontendFile(
      'src/modules/selfEvolution/components/workbench/dataset/DatasetResultModal.tsx',
    );
    expect(workspace).toContain('label: "查看生成结果"');
    expect(workspace).toContain('stageStatuses.cases === "partial"');
    expect(modal).toContain('downloadDatasetResult(threadId, result.revision)');
  });
});

describe('dataset management table defaults', () => {
  it('keeps required fields first and hides long reference fields by default', () => {
    const page = readFrontendFile('src/modules/datasetManagement/pages/detail/index.tsx');
    expect(page).toContain('const DEFAULT_VISIBLE_COLUMN_KEYS: ConfigurableColumnKey[] = [');
    expect(page).toContain('"question",\n  "question_type",\n  "ground_truth",\n  "grading_guidance",\n  "difficulty",\n  "source",');
    expect(page).not.toContain('const DEFAULT_VISIBLE_COLUMN_KEYS = [\n  ...CONFIGURABLE_COLUMN_OPTIONS.map');
  });
});

describe('existing eval set supplement launch', () => {
  it('sends the selected supplement strategy to Core', () => {
    const controller = readFrontendFile('src/modules/selfEvolution/hooks/useSelfEvolutionPageController.tsx');
    expect(controller).toContain('extra_eval_strategy: targetExtraEvalStrategy');
  });

  it('uses the imported case count as the material-plan lower bound', () => {
    const types = readFrontendFile(
      'src/modules/selfEvolution/components/workbench/dataset/types.ts',
    );
    const drawer = readFrontendFile(
      'src/modules/selfEvolution/components/workbench/dataset/MaterialAdjustmentDrawer.tsx',
    );
    expect(types).toContain('min_target_case_count: number;');
    expect(drawer).toContain('min={options.min_target_case_count}');
  });
});

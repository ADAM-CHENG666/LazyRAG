import { describe, expect, it } from 'vitest';

import {
  caseRetryRequest,
  getCaseRetryAction,
} from '../../../frontend/src/modules/selfEvolution/components/workbench/dataset/caseRetry.ts';
import { readFrontendFile } from '../setup.js';

describe('case retry routing', () => {
  it('reruns the published draft when question generation already succeeded', () => {
    const action = getCaseRetryAction('generate', 'completed', 'generated');
    expect(action).toEqual({
      mode: 'rerun',
      artifactId: 'dataset.case_draft',
      label: '重试生成',
      description: '将重新生成问答，并更新该用例的判分规则。',
    });
    expect(caseRetryRequest('/threads/thr-1', 'case_0001', action)).toEqual({
      path: '/threads/thr-1/cases/case_0001/rerun',
      body: { artifact_id: 'dataset.case_draft' },
    });
  });

  it('reruns the published enhancement when grading already succeeded', () => {
    const action = getCaseRetryAction('grading', 'completed', 'generated');
    expect(action?.mode).toBe('rerun');
    expect(action?.artifactId).toBe('dataset.case_enhancement');
    expect(caseRetryRequest('/threads/thr-1', 'case_0001', action).path).toBe(
      '/threads/thr-1/cases/case_0001/rerun',
    );
  });

  it('retries the recorded failure when grading never published an enhancement', () => {
    const action = getCaseRetryAction('grading', 'failed', 'generated');
    expect(action).toEqual({
      mode: 'retry',
      label: '重试生成',
      description: '将仅重新生成该用例的判分规则，问答内容保持不变。',
    });
    expect(caseRetryRequest('/threads/thr-1', 'case_0026', action)).toEqual({
      path: '/threads/thr-1/cases/case_0026/retry',
      body: {},
    });
  });

  it('retries the recorded failure when question generation failed or was cancelled', () => {
    const failed = getCaseRetryAction('generate', 'failed', 'generated');
    expect(failed?.mode).toBe('retry');
    expect(failed?.artifactId).toBeUndefined();
    expect(caseRetryRequest('/threads/thr-1', 'case_0002', failed).path).toBe(
      '/threads/thr-1/cases/case_0002/retry',
    );

    const canceled = getCaseRetryAction('generate', 'canceled', 'generated');
    expect(canceled?.mode).toBe('retry');
    expect(caseRetryRequest('/threads/thr-1', 'case_0003', canceled).path).toBe(
      '/threads/thr-1/cases/case_0003/retry',
    );
  });

  it('hides retry for imported, pending, and running cases', () => {
    expect(getCaseRetryAction('generate', 'completed', 'imported')).toBeUndefined();
    expect(getCaseRetryAction('generate', 'pending', 'generated')).toBeUndefined();
    expect(getCaseRetryAction('grading', 'running', 'generated')).toBeUndefined();
  });

  it('lets the case detail drawer post the routed retry request', () => {
    const source = readFrontendFile(
      'src/modules/selfEvolution/components/workbench/dataset/CaseDetailDrawer.tsx',
    );
    expect(source).toContain('caseRetryRequest(');
    expect(source).not.toContain('/rerun`, {');
  });
});

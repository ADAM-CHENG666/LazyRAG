import { describe, expect, it } from 'vitest';
import {
  knowledgeBaseNamesFor,
  pruneKnowledgeBaseSelection,
  selectionSummary,
} from '../../../frontend/src/modules/selfEvolution/shared/knowledgeBaseSelection.ts';
import { readFrontendFile } from '../setup.js';
import { getThreadKnowledgeBaseIds } from '../../../frontend/src/modules/selfEvolution/shared/thread.ts';

const options = [
  { value: 'kb-a', label: '产品知识库' },
  { value: 'kb-b', label: '服务知识库' },
  { value: 'kb-c', label: '研发知识库' },
];

describe('knowledge base multi-selection', () => {
  it('keeps catalog order and removes unavailable knowledge bases', () => {
    expect(pruneKnowledgeBaseSelection(['kb-c', 'missing', 'kb-a', 'kb-c'], options))
      .toEqual(['kb-a', 'kb-c']);
  });

  it('builds the authoritative name mapping for every selected knowledge base', () => {
    expect(knowledgeBaseNamesFor(['kb-c', 'kb-a'], options)).toEqual({
      'kb-a': '产品知识库',
      'kb-c': '研发知识库',
    });
  });

  it('summarises one or several selected knowledge bases without losing their names', () => {
    expect(selectionSummary(['kb-a'], options, '请选择知识库')).toBe('产品知识库');
    expect(selectionSummary(['kb-a', 'kb-b'], options, '请选择知识库')).toBe('已选择 2 个知识库');
    expect(selectionSummary([], options, '请选择知识库')).toBe('请选择知识库');
  });

  it('submits the complete selection and its names when creating a thread', () => {
    const source = readFrontendFile(
      'src/modules/selfEvolution/hooks/useSelfEvolutionPageController.tsx',
    );

    expect(source).toContain('multiple: true');
    expect(source).toContain('kb_id: targetSelectedKbs');
    expect(source).toContain(
      'knowledge_base_names: knowledgeBaseNamesFor(targetSelectedKbs, knowledgeBaseOptions)',
    );
    expect(source).not.toContain('setSelectedKb(');
  });

  it('restores both current multi-KB threads and legacy single-KB threads', () => {
    expect(getThreadKnowledgeBaseIds({
      thread: { thread_payload: { inputs: { kb_id: ['kb-b', 'kb-a'] } } },
    })).toEqual(['kb-b', 'kb-a']);
    expect(getThreadKnowledgeBaseIds({
      thread: { thread_payload: { inputs: { kb_id: 'kb-a' } } },
    })).toEqual(['kb-a']);
  });
});

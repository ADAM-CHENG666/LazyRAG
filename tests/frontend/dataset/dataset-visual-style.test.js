import { describe, expect, it } from 'vitest';
import { readFrontendFile } from '../setup.js';
import { progressSummaryLines } from '../../../frontend/src/modules/selfEvolution/components/workbench/dataset/primitives.tsx';

const styles = readFrontendFile(
  'src/modules/selfEvolution/components/workbench/dataset/dataset.scss',
);
const primitives = readFrontendFile(
  'src/modules/selfEvolution/components/workbench/dataset/primitives.tsx',
);
const materials = readFrontendFile(
  'src/modules/selfEvolution/components/workbench/dataset/MaterialsStage.tsx',
);
const topics = readFrontendFile(
  'src/modules/selfEvolution/components/workbench/dataset/TopicsStage.tsx',
);
const caseDetail = readFrontendFile(
  'src/modules/selfEvolution/components/workbench/dataset/CaseDetailDrawer.tsx',
);

describe('dataset overview visual hierarchy', () => {
  it('gives running sub-navigation steps the same breathing cue as live case status', () => {
    expect(styles).toMatch(/\.dataset-step\.is-running\s+\.dataset-step-dot\s*\{[^}]*animation:\s*dataset-step-breathe/s);
    expect(styles).toContain('@keyframes dataset-step-breathe');
  });

  it('keeps three-digit progress rings readable within the narrow overview pane', () => {
    expect(styles).toMatch(/\.dataset-progress-ring\s*\{[^}]*width:\s*66px;[^}]*height:\s*66px;/s);
    expect(styles).toMatch(/\.dataset-stage-progress-step\s*\{[^}]*width:\s*74px;/s);
  });

  it('centers each sub-navigation node and draws its connector through the circle centers', () => {
    expect(styles).toMatch(/\.dataset-stepper\s*\{[^}]*position:\s*relative;/s);
    expect(styles).toMatch(/\.dataset-stepper\s*\{[\s\S]*?&::before/);
    expect(styles).toMatch(/\.dataset-step\s*\{[^}]*justify-items:\s*center;/s);
    expect(styles).toMatch(/\.dataset-step-dot\s*\{[^}]*width:\s*32px;[^}]*height:\s*32px;/s);
  });

  it('keeps a dense execution-status summary inside two lines while retaining its full tooltip', () => {
    expect(progressSummaryLines('44 失败 · 4 执行中 · 92 未开始')).toEqual(['4 执行中', '44 失败']);
    expect(primitives).toContain('title={step.summary}');
  });

  it('allows the material document detail to omit its redundant parent document name', () => {
    expect(primitives).toContain('documentName?: string;');
    expect(materials).not.toMatch(/<ChunkCard\s+[\s\S]*?documentName=\{detail\.document\.name\}/);
  });

  it('uses one restrained drawer language for material, topic, and Case details', () => {
    expect(styles).toMatch(/\.dataset-drawer-attributes\s*\{[^}]*gap:\s*0;/s);
    expect(styles).toMatch(/\.dataset-drawer-attribute\s*\{[^}]*border-radius:\s*0;/s);
    expect(styles).toMatch(/\.dataset-case-roadmap\s*\{[^}]*border-bottom:/s);
    expect(materials).toContain('保存为待应用修改');
    expect(topics).toContain('保存为待应用修改');
  });

  it('centers the case-generation pause notice between the stepper and overview', () => {
    expect(styles).toMatch(/\.dataset-pause-notice\s*\{[^}]*margin:\s*0 0 12px;/s);
  });

  it('uses click-to-edit content instead of a separate Case question editor action', () => {
    expect(caseDetail).not.toContain('编辑问答');
    expect(caseDetail).not.toContain('EditOutlined');
    expect(caseDetail).toContain('dataset-inline-editable');
  });
});

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
});

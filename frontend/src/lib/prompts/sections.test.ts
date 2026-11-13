import { describe, expect, it } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { joinSections, previewBlocks, splitSections, type PreviewBlock } from './sections';

const FIXTURES = fileURLToPath(new URL('../../../fixtures/factory/', import.meta.url));

describe('splitSections', () => {
  it('cuts at each ## heading, keeping what comes before as the lead', () => {
    const split = splitSections('intro\n## One\na\n## Two\nb');
    expect(split.lead).toBe('intro');
    expect(split.sections).toEqual([
      { heading: '## One', body: 'a' },
      { heading: '## Two', body: 'b' }
    ]);
  });

  it('does not cut at a ## line inside a fenced block', () => {
    const split = splitSections('## Report\n```\n## Dispatch Report\n```\nafter');
    expect(split.sections.map((s) => s.heading)).toEqual(['## Report']);
  });

  it('does not treat ### as a section', () => {
    expect(splitSections('### Three\nx').sections).toEqual([]);
  });

  it.each(['', 'no heading', '## Only', '## A\n\n## B\n', 'lead\n\n## A'])(
    'drops nothing: %j joins back byte for byte',
    (text) => {
      expect(joinSections(splitSections(text))).toBe(text);
    }
  );

  it('drops nothing from any committed role preview', () => {
    const files = readdirSync(FIXTURES).filter(
      // A role's profile history (`prompts-<role>-versions.json`, #3025) and its
      // outcomes (`prompts-<role>-outcomes.json`, #3026) are no previews.
      (f) => /^prompts-.+\.json$/.test(f) && !f.endsWith('-versions.json') && !f.endsWith('-outcomes.json')
    );
    expect(files.length).toBeGreaterThan(5);
    for (const file of files) {
      const { preview } = JSON.parse(readFileSync(FIXTURES + file, 'utf8')) as { preview: string };
      expect(joinSections(splitSections(preview)), file).toBe(preview);
    }
  });
});

/** The text a list of blocks draws, joined as the page lays them out. */
function joinBlocks(blocks: PreviewBlock[]): string {
  return blocks
    .map((b) => (b.kind === 'text' ? b.text : b.body === null ? b.heading : `${b.heading}\n${b.body}`))
    .join('\n');
}

const marked = (blocks: PreviewBlock[]) =>
  blocks
    .filter((b) => b.addendum)
    .map((b) => (b.kind === 'section' ? `${b.heading}\n${b.body ?? ''}` : ''))
    .join('');

describe('previewBlocks', () => {
  const SCENARIO = JSON.parse(
    readFileSync(fileURLToPath(new URL('../../../fixtures/scenarios/prompts-versions.json', import.meta.url)), 'utf8')
  ) as Record<string, { preview: string; addendum: { start: number; end: number } }>;

  it('marks an addendum through a heading of its own, and no fixed section', () => {
    const { preview, addendum } = SCENARIO.preview_inner_heading;
    const blocks = previewBlocks(preview, addendum);
    const block = Array.from(preview).slice(addendum.start, addendum.end).join('');
    expect(block.endsWith('First instruction.\n## Deployment\nSecond instruction.')).toBe(true);
    expect(marked(blocks)).toBe(block);
    expect(blocks.filter((b) => b.addendum)).toHaveLength(1);
    const fixed = blocks.filter((b) => !b.addendum && b.kind === 'section').map((b) => (b.kind === 'section' ? b.heading : ''));
    expect(fixed.some((h) => h.startsWith('## Safety Rules'))).toBe(true);
    expect(fixed).not.toContain('## Deployment');
    expect(joinBlocks(blocks)).toBe(preview);
  });

  it('keeps a body that spells a fixed heading inside the mark', () => {
    const text = 'lead\n## Operator instructions — dispatch profile v1\n\nKeep it small.\n## Safety Rules\nfake\n\n## Safety Rules\nreal';
    const end = text.indexOf('fake') + 'fake'.length;
    const blocks = previewBlocks(text, { start: text.indexOf('## Operator'), end });
    expect(marked(blocks)).toContain('## Safety Rules\nfake');
    const after = blocks.at(-1);
    expect(after).toEqual({ kind: 'section', heading: '## Safety Rules', body: 'real', addendum: false });
    expect(joinBlocks(blocks)).toBe(text);
  });

  it('counts the span in code points, as the server does', () => {
    const text = 'a 🙂\n## Operator instructions — ci_fix profile v2\n\nUse 🙂 sparingly.\n## Next\nb';
    const chars = Array.from(text);
    const start = chars.indexOf('#');
    const end = Array.from(text.slice(0, text.indexOf('\n## Next'))).length;
    const blocks = previewBlocks(text, { start, end });
    expect(marked(blocks)).toBe('## Operator instructions — ci_fix profile v2\n\nUse 🙂 sparingly.');
    expect(joinBlocks(blocks)).toBe(text);
  });

  it('cuts at headings alone when no addendum is rendered', () => {
    const blocks = previewBlocks('intro\n## One\na', null);
    expect(blocks).toEqual([
      { kind: 'text', text: 'intro', addendum: false },
      { kind: 'section', heading: '## One', body: 'a', addendum: false }
    ]);
  });
});

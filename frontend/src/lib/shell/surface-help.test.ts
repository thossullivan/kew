/**
 * #2513 — the `?` overlay's words: one sentence per destination, a docs link
 * only where the docs have the page, and the keys read from the key layer.
 */
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it } from 'vitest';
import { DAG_KEYS } from '$lib/epic/dag-keys';
import { BRUSH_KEYS } from '$lib/monitor/brush';
import {
  bindShortcuts,
  keyGlyph,
  registeredShortcuts,
  registerShortcuts,
  resetShortcuts
} from '$lib/keys/registry';
import { WORK_KEYS, workShortcuts } from '$lib/work/keys';
import { FACTORY_TABS, MORE_DESTINATIONS } from './nav';
import {
  currentSurface,
  DOCS_BASE,
  docsUrl,
  helpRows,
  NAV_ROUTES,
  SURFACE_HELP,
  surfaceEntries
} from './surface-help';

const REPO = new URL('../../../../', import.meta.url);

afterEach(() => resetShortcuts());

describe('SURFACE_HELP', () => {
  it('names every destination the header offers', () => {
    expect(NAV_ROUTES).toEqual([...FACTORY_TABS, ...MORE_DESTINATIONS].map((d) => d.href));
  });

  // The test the issue asks for: a route added to `nav.ts` without a sentence
  // here fails by name.
  it.each(NAV_ROUTES)('%s has one help sentence', (href) => {
    const help = Object.hasOwn(SURFACE_HELP, href) ? SURFACE_HELP[href] : undefined;
    expect(help, `${href} is in nav.ts with no sentence in surface-help.ts`).toBeDefined();
    const answers = help!.answers.trim();
    expect(answers.length).toBeGreaterThan(0);
    expect(answers, 'one sentence, ending in a full stop').toMatch(/^[^.]+\.$/);
  });

  it('holds no sentence for a page the header does not offer', () => {
    expect(Object.keys(SURFACE_HELP).sort()).toEqual([...NAV_ROUTES].sort());
  });

  it("gives the three tabs DESIGN.md's own questions", () => {
    const design = readFileSync(new URL('DESIGN.md', REPO), 'utf8').replace(/\s+/g, ' ');
    const questions = /One question per surface: Work = "([^"]+)", Monitor = "([^"]+)", Epics = "([^"]+)"/.exec(
      design
    );
    expect(questions, 'DESIGN.md no longer states one question per surface').not.toBeNull();
    const [, work, monitor, epics] = questions!;
    expect(SURFACE_HELP['/work'].answers.toLowerCase()).toContain(work.toLowerCase());
    expect(SURFACE_HELP['/monitor'].answers.toLowerCase()).toContain(monitor.toLowerCase());
    expect(SURFACE_HELP['/epics'].answers.toLowerCase()).toContain(epics.toLowerCase());
  });
});

/** mkdocs' heading anchors: lower case, punctuation dropped, spaces to `-`. */
function slug(heading: string): string {
  return heading
    .toLowerCase()
    .replace(/[^\w\s-]/g, '')
    .trim()
    .replace(/\s+/g, '-');
}

describe('docs links', () => {
  const linked = surfaceEntries().filter((s) => s.docs !== undefined);

  it('links the surfaces docs/web/ has a page or section for', () => {
    expect(linked.map((s) => s.href)).toEqual([
      '/work',
      '/monitor',
      '/epics',
      '/health',
      '/runs',
      '/scorecard',
      '/budget'
    ]);
  });

  it.each(linked.map((s) => [s.href, s.docs!]))('%s links a page and heading that exist (%s)', (_, docs) => {
    const [path, anchor] = docs.split('#');
    expect(path.startsWith('web/'), `${docs} is outside docs/web/`).toBe(true);
    const file = path.endsWith('/')
      ? path === 'web/'
        ? 'web/index.md'
        : `${path.slice(0, -1)}.md`
      : `${path}.md`;
    const markdown = readFileSync(new URL(`docs/${file}`, REPO), 'utf8');
    if (anchor !== undefined) {
      const anchors = [...markdown.matchAll(/^#{1,6}\s+(.+)$/gm)].map((m) => slug(m[1]));
      expect(anchors, `docs/${file} has no heading #${anchor}`).toContain(anchor);
    }
  });

  it('resolves under the published docs site', () => {
    expect(docsUrl(SURFACE_HELP['/runs'])).toBe(`${DOCS_BASE}web/#runs`);
    expect(docsUrl(SURFACE_HELP['/epics'])).toBe(`${DOCS_BASE}web/epics/`);
    expect(docsUrl(SURFACE_HELP['/config'])).toBeNull();
  });
});

describe('currentSurface', () => {
  it('is the destination the header lights', () => {
    expect(currentSurface('/work')?.label).toBe('Work');
    expect(currentSurface('/work/2055')?.label).toBe('Work');
    expect(currentSurface('/epics/launch-fixes')?.label).toBe('Epics');
    expect(currentSurface('/health/')?.label).toBe('Health');
  });

  it('is null off the nav', () => {
    expect(currentSurface('/')).toBeNull();
    expect(currentSurface('/workspace')).toBeNull();
  });
});

describe('helpRows', () => {
  // The Work page registers `workShortcuts`; what the overlay lists there is
  // read back from the registry, and is exactly that table — every key, the
  // ones the strip legend leaves out included, and nothing else.
  it('lists exactly the shortcuts the registry holds for Work', () => {
    registerShortcuts(workShortcuts(() => {}));
    const rows = helpRows(registeredShortcuts());
    expect(rows.flatMap((r) => r.keys).sort()).toEqual(WORK_KEYS.map((k) => keyGlyph(k.key)).sort());
    expect(new Set(rows.map((r) => r.label))).toEqual(new Set(WORK_KEYS.map((k) => k.description)));
    for (const row of rows) {
      for (const key of row.keys) {
        const def = WORK_KEYS.find((k) => keyGlyph(k.key) === key);
        expect(def?.description, `${key} is listed under "${row.label}"`).toBe(row.label);
      }
    }
    expect(rows[0]).toEqual({ keys: ['j', 'k'], label: 'move' });
  });

  it('lists nothing a page has unregistered', () => {
    const off = registerShortcuts(workShortcuts(() => {}));
    off();
    expect(helpRows(registeredShortcuts())).toEqual([]);
  });

  it('draws a key two surfaces share under one description once', () => {
    registerShortcuts(bindShortcuts('dag', DAG_KEYS, () => {}));
    registerShortcuts(bindShortcuts('other', [{ key: 'f', description: 'fit all' }], () => {}));
    const fit = helpRows(registeredShortcuts()).find((r) => r.label === 'fit all');
    expect(fit?.keys).toEqual(['f', 'F']);
  });

  // #2841 (Q8): the brush answers only while the event track has focus, so
  // its rows say so rather than reading as page-wide keys.
  it('says where a key answers when that is one control, not the page', () => {
    registerShortcuts(bindShortcuts('brush', BRUSH_KEYS, () => {}));
    const rows = helpRows(registeredShortcuts());
    expect(rows.map((r) => r.label)).toEqual([
      'pan the window older, on the event track',
      'pan the window newer, on the event track',
      'zoom in, on the event track',
      'zoom out, on the event track',
      'back to live, on the event track'
    ]);
    expect(rows.find((r) => r.label.startsWith('zoom in'))?.keys).toEqual(['+', '=']);
  });
});

/**
 * #2513 — what the `?` overlay says: the question the surface answers, and
 * the keys it answers to.
 *
 * DESIGN.md gives every surface one question ("One question per surface"),
 * and nothing on screen told a new reader what it was. Each destination the
 * header offers (`nav.ts`) carries one sentence here, and a link to its docs
 * page where `docs/web/` has one. `surface-help.test.ts` fails the day a
 * destination is added without a sentence, and the day a docs link names a
 * page or heading the docs do not have.
 *
 * The keys are not restated here. They are read from the console's key layer
 * (`$lib/keys/registry`) at the moment the overlay opens, so the overlay lists
 * what the page has registered and nothing it has not — the same table the
 * page's own legend is drawn from.
 */
import { keyGlyph, type KeyDef } from '$lib/keys/registry';
import { FACTORY_TABS, isActive, MORE_DESTINATIONS } from './nav';

/** Where the docs are published (`site_url` in `mkdocs.yml`). */
export const DOCS_BASE = 'https://kew.dev/docs/';

/** One surface's help: the question it answers, and where to read more. */
export type SurfaceHelp = {
  /** One sentence: what this surface answers. */
  readonly answers: string;
  /** The docs page, relative to `DOCS_BASE` (`web/#runs`), when one exists. */
  readonly docs?: string;
};

/**
 * One sentence per destination, keyed by its `href`. The three tabs' questions
 * are DESIGN.md's own words.
 */
export const SURFACE_HELP: Readonly<Record<string, SurfaceHelp>> = {
  '/work': {
    answers: 'What is mine to do — every issue, and what each one is waiting for.',
    docs: 'web/#cockpit-shell'
  },
  '/monitor': {
    answers: 'What is happening — the fleet, its spend, its holds and the event stream.',
    docs: 'web/#cockpit-shell'
  },
  '/epics': {
    answers: 'Where does this drive stand — each epic, its graph and the node that needs you.',
    docs: 'web/epics/'
  },
  '/runs': {
    answers: 'What ran — every dispatched run, newest first.',
    docs: 'web/#runs'
  },
  '/budget': {
    answers: 'What is being spent against which cap, and where each cap comes from.',
    docs: 'web/#budget'
  },
  '/config': {
    answers: 'Which settings are in force, and which of them a live override changed.'
  },
  '/prompts': {
    answers: 'Which prompt each agent role runs — its source, its version and a sample render.'
  },
  '/scorecard': {
    answers: 'Which model earns its cost — runs, merge rate and dollars per merged PR.',
    docs: 'web/#scorecard'
  },
  '/health': {
    answers: 'Is the factory able to work — what is running, and what required is down.',
    docs: 'web/#health'
  },
  '/loops': {
    answers: 'What each objective loop is after, and what its flight recorder saw.'
  },
  '/audit': {
    answers: 'What the governance log recorded, and whether its hash chain still verifies.'
  }
};

/** Every destination the header offers, tabs first. */
export const NAV_ROUTES: readonly string[] = [...FACTORY_TABS, ...MORE_DESTINATIONS].map(
  (d) => d.href
);

/** A surface's label and help, as the overlay reads them. */
export type SurfaceEntry = { readonly label: string; readonly href: string } & SurfaceHelp;

/** Every destination with its help, in the header's order. */
export function surfaceEntries(): SurfaceEntry[] {
  return [...FACTORY_TABS, ...MORE_DESTINATIONS].map((d) => ({
    label: d.label,
    href: d.href,
    ...SURFACE_HELP[d.href]
  }));
}

/** The destination the reader is on — the one the header lights — or null. */
export function currentSurface(pathname: string): SurfaceEntry | null {
  return surfaceEntries().find((s) => isActive(pathname, s.href)) ?? null;
}

/** The absolute docs URL for a surface, or null when it has no page. */
export function docsUrl(help: SurfaceHelp): string | null {
  return help.docs === undefined ? null : new URL(help.docs, DOCS_BASE).href;
}

/** One line of the overlay: the keys that share a description, drawn, and it. */
export type HelpRow = { readonly keys: readonly string[]; readonly label: string };

/**
 * The overlay's shortcut rows from what the registry holds: one row per
 * description, in registration order, carrying every key that shares it once
 * — the legend's grouping (`legendOf`), but over EVERY shortcut rather than
 * the few a strip has room to draw. Keys come out as the legend draws them
 * (`↵`, `esc`). A key that answers only on one control says where
 * (`zoom in, on the event track`), so it does not read as page-wide.
 */
export function helpRows(shortcuts: readonly KeyDef[]): HelpRow[] {
  const rows: { keys: string[]; label: string }[] = [];
  for (const s of shortcuts) {
    const glyph = keyGlyph(s.key);
    const label = s.where === undefined ? s.description : `${s.description}, ${s.where}`;
    const row = rows.find((r) => r.label === label);
    if (!row) rows.push({ keys: [glyph], label });
    else if (!row.keys.includes(glyph)) row.keys.push(glyph);
  }
  return rows;
}

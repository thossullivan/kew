/**
 * #2519 — `docs/web/keys-and-palette.md` lists every shortcut the key layer
 * holds.
 *
 * The page has one table per scope, under a heading naming the scope
 * (`### Work list (`work`)`), and one row per registered key, drawn as the
 * `?` overlay draws it (`keyGlyph`): `| <kbd>↵</kbd> | open |`. This test
 * reads the same tables the surfaces register, so a key added to a surface
 * without a row on the page fails here, and so does a row the surface no
 * longer registers. (`<kbd>` rather than a code span: a table row opening on
 * a code-spanned digit reads as an exit-code claim to the docs truthfulness
 * gate.)
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { DAG_KEYS } from "$lib/epic/dag-keys";
import { EPIC_KEYS, TIMELINE_KEYS } from "$lib/epic/timeline";
import { keyGlyph, type KeyDef } from "./registry";
import { WORK_KEYS } from "$lib/work/keys";
import { BRUSH_KEYS } from "$lib/monitor/brush";
import { OUTCOME_KEYS } from "$lib/report/outcomes";

const PAGE = readFileSync(
  new URL("../../../../docs/web/keys-and-palette.md", import.meta.url),
  "utf8",
);

/** Every scope a surface registers, with the table it registers. */
const SCOPES: Record<string, readonly KeyDef[]> = {
  work: WORK_KEYS,
  epic: EPIC_KEYS,
  dag: DAG_KEYS,
  timeline: TIMELINE_KEYS,
  brush: BRUSH_KEYS,
  outcomes: OUTCOME_KEYS,
};

/** The rows of the table under the heading that names `scope`. */
function documentedRows(scope: string): string[] {
  const sections = PAGE.split(/^(?=#{2,3} )/m);
  const section = sections.find((s) =>
    s.split("\n", 1)[0].includes(`(\`${scope}\`)`),
  );
  expect(section, `no heading names scope "${scope}"`).toBeDefined();
  return [
    ...section!.matchAll(/^\| <kbd>([^<]+)<\/kbd> \| ([^|]+?) \|$/gm),
  ].map((m) => `${m[1]} → ${m[2]}`);
}

describe("keys-and-palette.md", () => {
  it.each(Object.entries(SCOPES))(
    'lists exactly the keys scope "%s" registers',
    (scope, defs) => {
      const registered = defs.map(
        (d) => `${keyGlyph(d.key)} → ${d.description}`,
      );
      expect(documentedRows(scope).sort()).toEqual([...registered].sort());
    },
  );

  it("names the two reserved presses", () => {
    expect(PAGE).toContain("<kbd>⌘</kbd> <kbd>K</kbd>");
    expect(PAGE).toContain("<kbd>Ctrl</kbd> <kbd>K</kbd>");
    expect(PAGE).toMatch(/^\| <kbd>\?<\/kbd> \|/m);
  });

  it("says the palette never dispatches and ask mode only proposes", () => {
    expect(PAGE).toContain("The palette never dispatches");
    expect(PAGE).toMatch(/Ask mode\s+\*\*only proposes\*\*/);
    expect(PAGE).toContain("off by default");
  });
});

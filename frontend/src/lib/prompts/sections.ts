/**
 * A rendered prompt cut at its `## ` headings, so the Prompts page can draw
 * each one as a disclosure (#3019).
 *
 * A `## ` line inside a fenced block is the fence's text, not a heading — the
 * dispatch prompt's own report template carries one — so fences are tracked.
 * Nothing is dropped: the lead (everything before the first heading) plus each
 * section's heading and body put back together is the preview, byte for byte.
 */
import type { AddendumSpan } from '$lib/api';

/** `body` is null for a heading with no line after it at all. */
export type PromptSection = { heading: string; body: string | null };
/** `lead` is null when the text opens on a heading, so it joins back exactly. */
export type PromptSections = { lead: string | null; sections: PromptSection[] };

const FENCE = /^\s*(```|~~~)/;

export function splitSections(text: string): PromptSections {
  const lines = text.split('\n');
  const lead: string[] = [];
  const sections: { heading: string; body: string[] }[] = [];
  let fenced = false;
  for (const line of lines) {
    if (FENCE.test(line)) fenced = !fenced;
    if (!fenced && line.startsWith('## ')) {
      sections.push({ heading: line, body: [] });
      continue;
    }
    (sections.at(-1)?.body ?? lead).push(line);
  }
  return {
    lead: sections.length > 0 && lead.length === 0 ? null : lead.join('\n'),
    sections: sections.map((s) => ({
      heading: s.heading,
      body: s.body.length === 0 ? null : s.body.join('\n')
    }))
  };
}

/**
 * One run of a preview as the page draws it: text before any heading, or a
 * `## ` section. `addendum` marks the operator addendum block (#3025).
 */
export type PreviewBlock =
  | { kind: 'text'; text: string; addendum: false }
  | { kind: 'section'; heading: string; body: string | null; addendum: boolean };

function blocksOf(text: string): PreviewBlock[] {
  const { lead, sections } = splitSections(text);
  const out: PreviewBlock[] = lead === null ? [] : [{ kind: 'text', text: lead, addendum: false }];
  for (const s of sections) out.push({ kind: 'section', heading: s.heading, body: s.body, addendum: false });
  return out;
}

/**
 * A preview cut into blocks, with the addendum block taken from the span the
 * server says the builder placed it at — never re-inferred from its headings.
 * The block is one section: its first line is the heading and every byte
 * after it, through the span's end, is its body, so a `## ` line inside the
 * addendum stays inside the mark and a body that spells a fixed heading never
 * moves the mark onto one. The text around it is cut at its own headings.
 * The line break either side of the block is the join between blocks, as
 * between sections; nothing else is dropped.
 */
export function previewBlocks(text: string, span: AddendumSpan | null | undefined): PreviewBlock[] {
  const chars = Array.from(text);
  if (!span || span.start < 0 || span.end <= span.start || span.end > chars.length) return blocksOf(text);
  let before = chars.slice(0, span.start).join('');
  const block = chars.slice(span.start, span.end).join('');
  let after = chars.slice(span.end).join('');
  if (before.endsWith('\n')) before = before.slice(0, -1);
  if (after.startsWith('\n')) after = after.slice(1);
  const cut = block.indexOf('\n');
  const addendum: PreviewBlock = {
    kind: 'section',
    heading: cut < 0 ? block : block.slice(0, cut),
    body: cut < 0 ? null : block.slice(cut + 1),
    addendum: true
  };
  return [
    ...(span.start > 0 ? blocksOf(before) : []),
    addendum,
    ...(span.end < chars.length ? blocksOf(after) : [])
  ];
}

/** The inverse of `splitSections`, for the test that nothing was dropped. */
export function joinSections({ lead, sections }: PromptSections): string {
  const parts = sections.map((s) => (s.body === null ? s.heading : `${s.heading}\n${s.body}`));
  return (lead === null ? parts : [lead, ...parts]).join('\n');
}

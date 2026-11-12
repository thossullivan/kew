/**
 * #2205 — the one formatter, held to the two things it exists to get right.
 *
 * **One zone.** The screen it was written for printed one moment in two zones
 * at once, so the guard at the bottom reads every source file the app ships
 * and fails if any of them formats a timestamp itself. That is the only way
 * "one formatter" stays true: a second `toLocaleTimeString` added anywhere is
 * a second zone, and it will look right on the machine that added it.
 *
 * **A DST boundary.** Local rendering is only safe if it is a real conversion
 * rather than an offset added once at load, so the clock is walked across
 * both the spring-forward (an hour that does not exist) and the autumn
 * fall-back (an hour that happens twice) in a zone that observes them. The
 * fall-back case is also why `utcTitle` is not decoration: two distinct
 * instants render as the same local time, and only the title tells them
 * apart.
 */
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { fmtDateTime, fmtTime, NO_TIME, parseInstant, utcTitle } from './time';

const BERLIN = 'Europe/Berlin';
const NEW_YORK = 'America/New_York';

describe('parseInstant', () => {
  it('reads an offset-bearing stamp as the instant it names', () => {
    expect(parseInstant('2026-09-29T13:33:18+00:00')).toBe(Date.UTC(2026, 8, 29, 13, 33, 18));
    expect(parseInstant('2026-09-29T15:33:18+02:00')).toBe(Date.UTC(2026, 8, 29, 13, 33, 18));
  });

  it('reads an offset-less stamp as UTC, not as the viewer wall clock', () => {
    expect(parseInstant('2026-09-29T13:33:18')).toBe(Date.UTC(2026, 8, 29, 13, 33, 18));
    expect(parseInstant('2026-09-29 13:33:18')).toBe(Date.UTC(2026, 8, 29, 13, 33, 18));
    expect(parseInstant('2026-09-29T13:33:18.123456')).toBe(
      Date.UTC(2026, 8, 29, 13, 33, 18, 123)
    );
  });

  it('passes epoch milliseconds through and refuses everything else', () => {
    expect(parseInstant(1_759_153_998_000)).toBe(1_759_153_998_000);
    expect(parseInstant('not-a-date')).toBeNull();
    expect(parseInstant(Number.NaN)).toBeNull();
    expect(parseInstant(null)).toBeNull();
    expect(parseInstant(undefined)).toBeNull();
  });

  it('refuses a number outside Date range instead of letting Intl throw', () => {
    // Intl.format and toISOString both raise RangeError past 8.64e15, and a
    // timestamp must not be able to take a whole screen down.
    expect(parseInstant(1e20)).toBeNull();
    expect(fmtTime(1e20)).toBe(NO_TIME);
    expect(fmtDateTime(1e20)).toBe(NO_TIME);
    expect(utcTitle(1e20)).toBe('');
  });
});

describe('fmtTime', () => {
  it('renders in the viewer zone, zero-padded and 24-hour', () => {
    expect(fmtTime('2026-09-29T13:33:18Z', true, BERLIN)).toBe('15:33:18');
    expect(fmtTime('2026-09-29T13:33:18Z', false, BERLIN)).toBe('15:33');
    expect(fmtTime('2026-09-29T01:02:03Z', true, BERLIN)).toBe('03:02:03');
    // 24-hour even in a zone whose locale would print "9:33 PM".
    expect(fmtTime('2026-09-29T21:33:18Z', true, NEW_YORK)).toBe('17:33:18');
  });

  it('renders an unreadable stamp as a dash, never the epoch', () => {
    expect(fmtTime('not-a-date')).toBe(NO_TIME);
    expect(fmtTime(null)).toBe(NO_TIME);
    expect(fmtTime(undefined)).toBe(NO_TIME);
  });

  it('follows the zone across the spring-forward hour that does not exist', () => {
    // Berlin 2026-03-29: 02:00 CET becomes 03:00 CEST at 01:00 UTC.
    expect(fmtTime('2026-03-29T00:59:00Z', true, BERLIN)).toBe('01:59:00');
    expect(fmtTime('2026-03-29T01:00:00Z', true, BERLIN)).toBe('03:00:00');
    // New York 2026-03-08: 02:00 EST becomes 03:00 EDT at 07:00 UTC.
    expect(fmtTime('2026-03-08T06:59:00Z', true, NEW_YORK)).toBe('01:59:00');
    expect(fmtTime('2026-03-08T07:00:00Z', true, NEW_YORK)).toBe('03:00:00');
  });

  it('follows the zone across the autumn hour that happens twice', () => {
    // New York 2026-11-01: 02:00 EDT becomes 01:00 EST at 06:00 UTC, so
    // 05:30Z and 06:30Z are an hour apart and both read "01:30".
    expect(fmtTime('2026-11-01T05:30:00Z', true, NEW_YORK)).toBe('01:30:00');
    expect(fmtTime('2026-11-01T06:30:00Z', true, NEW_YORK)).toBe('01:30:00');
    // Which is exactly what the title is for.
    expect(utcTitle('2026-11-01T05:30:00Z')).toBe('2026-11-01 05:30:00 UTC');
    expect(utcTitle('2026-11-01T06:30:00Z')).toBe('2026-11-01 06:30:00 UTC');
  });
});

describe('fmtDateTime', () => {
  it('writes the date ISO-first and the time 24-hour, in the viewer zone', () => {
    const prev = process.env.TZ;
    process.env.TZ = 'Europe/Berlin';
    try {
      // 23:33 UTC is the next day in Berlin: the date has to move with the
      // time, which is the whole reason this is one formatter and not two.
      expect(fmtDateTime('2026-09-29T23:33:18Z')).toBe('2026-09-30 01:33:18');
      expect(fmtDateTime('2026-09-29T13:33:18Z')).toBe('2026-09-29 15:33:18');
    } finally {
      if (prev === undefined) delete process.env.TZ;
      else process.env.TZ = prev;
    }
  });

  it('renders an unreadable stamp as a dash', () => {
    expect(fmtDateTime('not-a-date')).toBe(NO_TIME);
    expect(fmtDateTime(null)).toBe(NO_TIME);
  });
});

describe('utcTitle', () => {
  it('states the full UTC instant, offset-less stamps included', () => {
    expect(utcTitle('2026-09-29T13:33:18.421Z')).toBe('2026-09-29 13:33:18 UTC');
    expect(utcTitle('2026-09-29T15:33:18+02:00')).toBe('2026-09-29 13:33:18 UTC');
    expect(utcTitle('2026-09-29T13:33:18')).toBe('2026-09-29 13:33:18 UTC');
  });

  it('says nothing about a stamp it could not read', () => {
    expect(utcTitle('not-a-date')).toBe('');
    expect(utcTitle(null)).toBe('');
  });
});

describe('the runner zone', () => {
  it('is pinned, so a local reading asserted in this suite means one thing', () => {
    // Rendering in the viewer's zone makes every committed expectation about a
    // printed time a statement about the *runner's* zone too — replay.test.ts,
    // rail.test.ts and evidence.test.ts all read the artboard's UTC times as
    // local ones. Unpinned, those files pass in CI (UTC) and fail on a laptop
    // in Berlin, which is a suite that is green for a reason unrelated to the
    // code. `vite.config.ts` pins it; this is the test that notices if it
    // stops being pinned.
    expect(process.env.TZ).toBe('UTC');
    expect(fmtTime('2026-09-24T15:46:58Z')).toBe('15:46:58');
    expect(fmtDateTime('2026-09-24T15:46:58Z')).toBe('2026-09-24 15:46:58');
  });
});

const SRC = fileURLToPath(new URL('../', import.meta.url));

/** Every `.ts`/`.svelte` file the app ships, tests and this module excluded. */
function sources(): string[] {
  return readdirSync(SRC, { recursive: true, encoding: 'utf8' })
    .filter((p) => /\.(ts|svelte)$/.test(p) && !p.includes('.test.') && p !== 'lib/time.ts')
    .map((p) => join(SRC, p));
}

describe('one formatter', () => {
  /**
   * The ways a clock time can be turned into words without going through
   * `$lib/time`. Each of these shipped at least once and each printed a
   * different zone from the row next to it.
   *
   * Only clock readings are forbidden, and `toLocaleString` is deliberately
   * NOT in the list: the console calls it on numbers (thousands separators)
   * far more often than on dates, and a name-based scan cannot tell those
   * apart — flagging it would be a gate nobody can keep green. The three
   * pages that did call it on a `Date` now call `fmtDateTime` instead. The UTC
   * *calendar day* that groups the runs list is a bucketing rule with its own
   * test, and is also out of scope here.
   *
   * `hourCycle` / `hour12` / `hour:` / `timeStyle` / `dateStyle` is the tell
   * that an `Intl.DateTimeFormat` is being built to tell the time — a
   * `{ timeStyle: 'medium' }` formatter names none of the fields but prints
   * the same clock, in the viewer's locale as well as their zone.
   *
   * The last entry is the one slice that needs a *shape* rather than a name.
   * `toISOString()` whole is UTC and unambiguous (it is what `utcTitle`
   * states), and `.slice(0, 10)` off it is the runs list's UTC calendar day —
   * the bucketing rule named above. Reaching into the characters after that is
   * a clock being cut out of the string by hand, which is how an axis once
   * read UTC under rows reading local.
   */
  const SECOND_ZONES: [string, RegExp][] = [
    ['toLocaleTimeString', /\btoLocaleTimeString\b/],
    ['toLocaleDateString', /\btoLocaleDateString\b/],
    ['getUTCHours/Minutes/Seconds', /\bgetUTC(?:Hours|Minutes|Seconds)\b/],
    ['getHours/Minutes/Seconds', /\bget(?:Hours|Minutes|Seconds)\b/],
    ['toTimeString/toDateString/toUTCString', /\bto(?:Time|Date|UTC)String\b/],
    ['an Intl clock', /\bhourCycle\b|\bhour12\b|\bhour:\s*'|\btimeStyle\b|\bdateStyle\b/],
    [
      'a hand-cut ISO clock',
      /toISOString\(\)\s*\.\s*(?:split\(|(?:slice|substring|substr)\(\s*(?!0\s*,\s*10\s*\)))/
    ]
  ];

  /**
   * The source with its comments removed. Prose that *names* the trap — this
   * file's own docstring, `time.ts`'s rationale, the comment on a row that
   * used to format its own clock — must not read as the trap.
   */
  const code = (text: string): string =>
    text
      .replace(/<!--[\s\S]*?-->/g, '')
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/(^|[^:'"`\\])\/\/.*$/gm, '$1');

  /** The names of the second zones a piece of source would introduce. */
  const zonesIn = (source: string): string[] =>
    SECOND_ZONES.filter(([, pattern]) => pattern.test(code(source))).map(([name]) => name);

  it('is the only place a clock time is formatted', () => {
    const offenders = sources().flatMap((path) => {
      const text = readFileSync(path, 'utf8');
      return zonesIn(text).map((name) => `${path.slice(SRC.length)}: ${name}`);
    });
    expect(offenders).toEqual([]);
  });

  it('catches every way a clock reading has been written here', () => {
    // A gate is only worth the ways it closes. Each line below is a clock
    // formatted outside `$lib/time`, and the gate going green on one of them
    // is how a second zone gets added without anyone noticing.
    const clocks = [
      "const t = d.toLocaleTimeString('en-GB');",
      "const d2 = d.toLocaleDateString('en-GB');",
      'const h = d.getUTCHours() + ":" + d.getUTCMinutes();',
      'const h2 = d.getHours() + ":" + d.getMinutes();',
      'const t2 = d.toTimeString().slice(0, 8);',
      'const t3 = d.toUTCString();',
      "const f = new Intl.DateTimeFormat('en-GB', { timeStyle: 'medium' }).format(d);",
      "const f2 = new Intl.DateTimeFormat('en-GB', { dateStyle: 'short' }).format(d);",
      "const f3 = new Intl.DateTimeFormat('en-GB', { hour: '2-digit' }).format(d);",
      "const f4 = new Intl.DateTimeFormat('en-GB', { hourCycle: 'h23' }).format(d);",
      'const s = d.toISOString().slice(11, 19);',
      'const s2 = d.toISOString().substring(11, 16);',
      "const s3 = d.toISOString().split('T')[1];"
    ];
    for (const clock of clocks) expect(zonesIn(clock), clock).not.toEqual([]);

    // And the readings that are deliberately out of scope stay out of it, or
    // the gate is one nobody can keep green: the whole UTC instant, the UTC
    // calendar day the runs list groups on, and `toLocaleString` on a number.
    const allowed = [
      'const iso = d.toISOString();',
      'const day = d.toISOString().slice(0, 10);',
      "const n = lines.toLocaleString('en-US');"
    ];
    for (const ok of allowed) expect(zonesIn(ok), ok).toEqual([]);

    // Prose naming the trap is not the trap: the scan reads code, not comments.
    expect(zonesIn('// we used to call toLocaleTimeString here\n')).toEqual([]);
    expect(zonesIn('/* getHours, hourCycle, toISOString().slice(11, 19) */\n')).toEqual([]);
  });
});

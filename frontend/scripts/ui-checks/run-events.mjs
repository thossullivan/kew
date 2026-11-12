/**
 * The run page's events read as sentences (#3458, design review c9 D1).
 *
 * Every row ended in `JSON.stringify(event.payload)` after an unlabelled
 * ledger seq; the timeline was one lane of unnamed bars; the preset chips
 * carried no counts and sat at 40% when they did not apply; the fixture's
 * "Publication deferred" was drawn red on a successful run; and the focused
 * row's `bg-ink-raised` was invisible on ink. This module reads what a person
 * sees on `/runs/f5dcb6a5`, at 390, 768 and 1440 px.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 * `readRunEvents` is shared with `demo-run-events.mjs`, which reads a demo run.
 */
// ui-check-owns: src/lib/runs/RunEvents.svelte
// ui-check-owns: src/lib/runs/event-words.ts
// ui-check-owns: src/lib/runs/events.ts

const RUN = '/runs/f5dcb6a5';
const SIZES = [
  ['390', { width: 390, height: 844 }],
  ['768', { width: 768, height: 1024 }],
  ['1440', undefined]
];

/** The deferral the fixture records at seq 1048. */
const DEFERRAL = 'Publication deferred';

/** The smallest press a control may offer (the console's rule). */
const MIN_PRESS = 32;

/** Wait until the run's event list has drawn rows. */
export async function eventsDrawn(page) {
  const section = page.locator('section[aria-label="Run events"]');
  await section.locator('[aria-label="Ledger events"] [data-seq]').first().waitFor({ state: 'visible', timeout: 15_000 });
  return section;
}

/**
 * What a reader sees in the run's event section: each row's visible text and
 * seq, each stage's name and box, each preset chip's text, and the boxes of
 * every name under the lane.
 */
export async function readRunEvents(page) {
  await eventsDrawn(page);
  return page.evaluate(() => {
    const section = document.querySelector('section[aria-label="Run events"]');
    const box = (el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height };
    };
    const lane = section.querySelector('svg[aria-label="Run phases"]');
    return {
      rows: [...section.querySelectorAll('[aria-label="Ledger events"] [data-seq]')].map((row) => ({
        seq: row.dataset.seq,
        text: row.innerText.replace(/\s+/g, ' ').trim()
      })),
      lane: lane ? box(lane) : null,
      stages: [...section.querySelectorAll('svg[aria-label="Run phases"] [role="button"]')].map((stage) => {
        const label = stage.querySelector('[data-stage-label]');
        return {
          name: (label?.textContent ?? '').trim(),
          label: label ? box(label) : null,
          hit: box(stage)
        };
      }),
      presets: [...section.querySelectorAll('[data-preset]')].map((chip) => ({
        id: chip.dataset.preset,
        text: (chip.textContent ?? '').trim(),
        disabled: chip.disabled
      })),
      stray: [...section.querySelectorAll('span')].filter((s) => (s.textContent ?? '').trim() === '|').length
    };
  });
}

/** The reading's failures: JSON or a seq on a closed row, an unnamed stage, a preset with no count. */
export function sentenceProblems(read) {
  const problems = [];
  if (read.rows.length === 0) problems.push('the event list drew no row, so this proves nothing');
  for (const row of read.rows) {
    if (row.text.startsWith('{') || /[{}]/.test(row.text)) problems.push(`row ${row.seq} shows JSON: "${row.text}"`);
    if (new RegExp(`(^|\\D)${row.seq}(\\D|$)`).test(row.text)) {
      problems.push(`row ${row.seq} shows its ledger seq: "${row.text}"`);
    }
  }
  if (read.stages.length === 0) problems.push('the timeline drew no stage');
  for (const stage of read.stages) {
    if (stage.name === '' || !stage.label || stage.label.width === 0) problems.push('a phase segment is drawn with no name');
    else if (read.lane && (stage.label.left < read.lane.left - 0.5 || stage.label.right > read.lane.right + 0.5)) {
      problems.push(`the stage name "${stage.name}" runs out of the timeline (${stage.label.left}-${stage.label.right} in ${read.lane.left}-${read.lane.right})`);
    }
  }
  const labels = read.stages.filter((s) => s.label && s.label.width > 0);
  for (let i = 0; i < labels.length; i += 1) {
    for (let j = 0; j < i; j += 1) {
      const a = labels[i].label;
      const b = labels[j].label;
      const apart = a.left >= b.right - 0.5 || b.left >= a.right - 0.5 || a.top >= b.bottom - 0.5 || b.top >= a.bottom - 0.5;
      if (!apart) problems.push(`the stage names "${labels[j].name}" and "${labels[i].name}" overlap`);
    }
  }
  for (const preset of read.presets) {
    if (!/\s\d+$/.test(preset.text)) problems.push(`the preset "${preset.text}" carries no count`);
    if (preset.disabled) problems.push(`the preset "${preset.text}" is shown although it does not apply`);
  }
  if (read.stray > 0) problems.push('the chip row still draws a "|" separator');
  return problems;
}

/** A colour token as the browser computes it, for comparing against a computed colour. */
async function tokenColour(page, token) {
  return page.evaluate((name) => {
    const probe = document.createElement('span');
    probe.style.color = `var(${name})`;
    document.body.appendChild(probe);
    const colour = getComputedStyle(probe).color;
    probe.remove();
    return colour;
  }, token);
}

/** The rows the list shows, by seq. */
const shownSeqs = (page) =>
  page.locator('[aria-label="Ledger events"] [data-seq]').evaluateAll((rows) => rows.map((r) => r.dataset.seq));

export async function checks({ check, visit, expect }) {
  for (const [label, size] of SIZES) {
    await check(`${RUN} events read as sentences under named stages at ${label}px`, () =>
      visit(
        RUN,
        async (page, problems) => {
          const read = await readRunEvents(page);
          const found = sentenceProblems(read);
          expect(found.length === 0, found.join('\n'));
          // The fixture's run succeeded: its only blemish is the deferral, so
          // "Errors" does not apply and must not be offered.
          expect(
            !read.presets.some((p) => p.id === 'errors'),
            `"Errors" is offered on a run with no failure: ${read.presets.map((p) => p.text).join(', ')}`
          );
          const names = read.stages.map((s) => s.name);
          for (const want of ['dispatch', 'sandbox', 'work', 'publish']) {
            expect(names.includes(want), `the timeline names no "${want}" stage: ${names.join(', ')}`);
          }
          const deferred = read.rows.find((row) => row.text.includes(DEFERRAL));
          expect(
            deferred?.text.includes('push deferred: protected branch; base merge required'),
            `the deferral row does not say what was deferred and why: "${deferred?.text}"`
          );
          expect(problems.length === 0, `${RUN} reported:\n${problems.join('\n')}`);
        },
        size
      )
    );

    await check(`${RUN} draws the deferral amber, never red, at ${label}px`, () =>
      visit(
        RUN,
        async (page) => {
          await eventsDrawn(page);
          const warn = await tokenColour(page, '--warn');
          const danger = await tokenColour(page, '--danger');
          const colour = await page
            .locator('[aria-label="Ledger events"] [data-seq="1048"]')
            .getByText(DEFERRAL, { exact: true })
            .evaluate((el) => getComputedStyle(el).color);
          expect(colour !== danger, `"${DEFERRAL}" is drawn in the danger colour (${colour})`);
          expect(colour === warn, `"${DEFERRAL}" is drawn ${colour}, not the warn amber ${warn}`);
          const marks = await page
            .locator('svg[aria-label="Run phases"] [data-lane-mark]')
            .evaluateAll((els) => els.map((el) => [el.dataset.laneMark, getComputedStyle(el).fill]));
          expect(marks.some(([tone]) => tone === 'held'), 'the timeline marks no hold for the deferral');
          expect(!marks.some(([tone]) => tone === 'failed'), `the timeline marks a failure on a successful run: ${marks}`);
        },
        size
      )
    );

    await check(`${RUN} focused row is visible at ${label}px`, () =>
      visit(
        RUN,
        async (page) => {
          await eventsDrawn(page);
          // The row's own time link is the permalink: the fixture server
          // moves every recorded instant to the present, so the time is read
          // off the page rather than from the file.
          await page.locator('[aria-label="Ledger events"] [data-seq="1048"] a').first().click();
          await page.waitForFunction(() => new URL(location.href).searchParams.has('focusedTime'));
          await page.locator('[aria-label="Ledger events"] [data-focused="true"]').waitFor({ timeout: 10_000 });
          const cyan = await tokenColour(page, '--accent-2');
          const read = await page.evaluate(() => {
            const row = document.querySelector('[aria-label="Ledger events"] [data-focused="true"]');
            if (!row) return null;
            const style = getComputedStyle(row);
            let el = row.parentElement;
            let under = 'rgba(0, 0, 0, 0)';
            while (el && (under === 'rgba(0, 0, 0, 0)' || under === 'transparent')) {
              under = getComputedStyle(el).backgroundColor;
              el = el.parentElement;
            }
            return { seq: row.dataset.seq, bg: style.backgroundColor, under, shadow: style.boxShadow };
          });
          expect(read !== null, 'no row is marked focused for a focusedTime the ledger holds');
          expect(read.seq === '1048', `the focused row is seq ${read.seq}, not the deferral's`);
          expect(read.bg !== read.under && read.bg !== 'rgba(0, 0, 0, 0)', `the focused row's fill ${read.bg} is the page's own ${read.under}`);
          expect(read.shadow.includes(cyan), `the focused row has no cyan left edge (box-shadow: ${read.shadow})`);
        },
        size
      )
    );

    await check(`${RUN} row disclosure, list and controls fit at ${label}px`, () =>
      visit(
        RUN,
        async (page) => {
          await eventsDrawn(page);
          const first = page.locator('[aria-label="Ledger events"] [data-seq]').first();
          const seq = await first.getAttribute('data-seq');
          await first.locator('button[aria-expanded]').click();
          const detail = ((await first.locator('[data-event-detail]').textContent()) ?? '').trim();
          expect(detail.includes(`seq ${seq}`), `the open disclosure does not name the ledger seq: "${detail}"`);
          expect(detail.includes('"owner_pid"'), `the open disclosure does not show the recorded payload: "${detail}"`);

          const fit = await page.evaluate(() => {
            const section = document.querySelector('section[aria-label="Run events"]');
            const list = section.querySelector('[aria-label="Ledger events"]');
            const style = getComputedStyle(list);
            // Every control below 640; above it the chips keep the console's
            // desktop pill, so only this change's own controls are held to it.
            const phone = window.innerWidth < 640;
            const controls = phone
              ? 'button, [role="button"]'
              : 'button[aria-expanded], svg[aria-label="Run phases"] [role="button"]';
            const small = [...section.querySelectorAll(controls)]
              .map((el) => ({ el, r: el.getBoundingClientRect() }))
              .filter(({ r }) => r.width > 0 && (r.height < 32 || r.width < 32))
              .map(({ el, r }) => `${(el.getAttribute('aria-label') ?? el.textContent ?? '').trim()} ${Math.round(r.width)}x${Math.round(r.height)}`);
            const doc = document.documentElement;
            return {
              inner: (style.overflowY === 'auto' || style.overflowY === 'scroll') && list.scrollHeight > list.clientHeight,
              overflowY: style.overflowY,
              small,
              scrollWidth: doc.scrollWidth,
              clientWidth: doc.clientWidth,
              phone
            };
          });
          if (fit.phone) {
            expect(fit.overflowY !== 'auto' && fit.overflowY !== 'scroll', `the list scrolls inside the page below 640px (${fit.overflowY})`);
          }
          expect(fit.small.length === 0, `controls under ${MIN_PRESS}px: ${fit.small.join(', ')}`);
          expect(fit.scrollWidth <= fit.clientWidth + 1, `the page scrolls sideways (${fit.scrollWidth}px in ${fit.clientWidth}px)`);
        },
        size
      )
    );
  }

  await check(`${RUN} presets count the rows they show and select whole stages`, () =>
    visit(RUN, async (page) => {
      await eventsDrawn(page);
      // The fixture's run took time and recorded a cost, so both apply.
      for (const id of ['slowest', 'cost']) {
        const chip = page.locator(`section[aria-label="Run events"] [data-preset="${id}"]`);
        expect((await chip.count()) === 1, `the fixture run offers no "${id}" preset`);
        const text = ((await chip.textContent()) ?? '').trim();
        const count = Number(/\s(\d+)$/.exec(text)?.[1] ?? NaN);
        expect(Number.isInteger(count) && count > 0, `the preset "${text}" carries no count`);
        const before = await page.evaluate(() => new URL(location.href).searchParams.get('q'));
        await chip.click();
        await page.waitForFunction((was) => {
          const q = new URL(location.href).searchParams.get('q');
          return q !== null && q !== was;
        }, before);
        const shown = await shownSeqs(page);
        expect(shown.length === count, `"${text}" shows ${shown.length} rows: ${shown}`);
        // Every stage the selection touches draws pressed, and none is drawn
        // pressed with part of it left out: the lane and the rows agree.
        const stages = await page.evaluate(() => {
          const q = new URL(location.href).searchParams.get('q') ?? '';
          const picked = q.split(/\s+/).filter((t) => t.startsWith('phase:')).map((t) => t.slice(6));
          return [...document.querySelectorAll('svg[aria-label="Run phases"] [role="button"]')].map((g) => {
            const phases = (g.dataset.phases ?? '').split(' ');
            return {
              stage: g.dataset.stage,
              pressed: g.getAttribute('aria-pressed'),
              some: phases.some((p) => picked.includes(p)),
              every: phases.every((p) => picked.includes(p))
            };
          });
        });
        const torn = stages.filter((s) => s.some !== s.every || (s.pressed === 'true') !== s.every);
        expect(torn.length === 0, `"${text}" leaves stages part-selected or drawn wrong: ${JSON.stringify(torn)}`);
      }
    })
  );

  await check(`${RUN} shows keyboard focus on a timeline stage`, () =>
    visit(RUN, async (page) => {
      await eventsDrawn(page);
      const cyan = await tokenColour(page, '--accent-2');
      // Tab from the list's heading area until a stage holds focus.
      await page.locator('section[aria-label="Run events"]').evaluate((el) => {
        const probe = document.createElement('span');
        probe.tabIndex = -1;
        el.prepend(probe);
        probe.focus();
      });
      let read = null;
      for (let i = 0; i < 12 && read === null; i += 1) {
        await page.keyboard.press('Tab');
        read = await page.evaluate(() => {
          const el = document.activeElement;
          if (!el || !el.matches('svg[aria-label="Run phases"] [role="button"]')) return null;
          const bar = el.querySelector('[data-stage-bar]');
          const style = getComputedStyle(el);
          return {
            stage: el.dataset.stage,
            outline: style.outlineStyle === 'none' || style.outlineWidth === '0px' ? 'none' : style.outlineColor,
            stroke: bar ? getComputedStyle(bar).stroke : 'none'
          };
        });
      }
      expect(read !== null, 'Tab never reached a stage on the timeline');
      expect(
        read.outline !== 'none' || read.stroke === cyan,
        `the focused "${read.stage}" stage shows no focus (outline ${read.outline}, bar stroke ${read.stroke}, cyan ${cyan})`
      );
    })
  );

  await check(`${RUN} filters by chip and by stage, and keeps both across a reload`, () =>
    visit(RUN, async (page) => {
      await eventsDrawn(page);
      const all = await shownSeqs(page);

      await page.locator('section[aria-label="Run events"] button[title="publication_deferred"]').click();
      await page.waitForFunction(() => new URL(location.href).searchParams.get('q') === 'type:publication_deferred');
      expect(JSON.stringify(await shownSeqs(page)) === '["1048"]', `the chip shows ${await shownSeqs(page)}`);
      await page.reload({ waitUntil: 'load' });
      await eventsDrawn(page);
      expect(JSON.stringify(await shownSeqs(page)) === '["1048"]', `after a reload the chip filter shows ${await shownSeqs(page)}`);

      // Drop the chip, then press the publish stage — by keyboard, with Space,
      // which must filter and must not scroll the page.
      await page.locator('section[aria-label="Run events"] button[title="publication_deferred"]').click();
      await page.waitForFunction(() => !new URL(location.href).searchParams.has('q'));
      expect((await shownSeqs(page)).length === all.length, 'dropping the chip did not bring every row back');
      const stage = page.locator('svg[aria-label="Run phases"] [role="button"][data-stage="publish"]');
      await stage.focus();
      const before = await page.evaluate(() => window.scrollY);
      await page.keyboard.press(' ');
      await page.waitForFunction(() => (new URL(location.href).searchParams.get('q') ?? '').includes('phase:publication_held'));
      const after = await page.evaluate(() => window.scrollY);
      expect(after === before, `Space on a stage scrolled the page (${before} → ${after})`);
      const publish = ['1046', '1047', '1048', '1049'];
      expect(JSON.stringify(await shownSeqs(page)) === JSON.stringify(publish), `the publish stage shows ${await shownSeqs(page)}`);
      await page.reload({ waitUntil: 'load' });
      await eventsDrawn(page);
      expect(
        JSON.stringify(await shownSeqs(page)) === JSON.stringify(publish),
        `after a reload the stage filter shows ${await shownSeqs(page)}`
      );
    })
  );
}

/**
 * The run page over a real run (#3057, design-c8 N16 and the run-page part
 * of N10).
 *
 * The only run shot drew "find out why the sealed package could not be read":
 * the fixture named no sealed artifact for f5dcb6a5, so the page never asked
 * for a diff, and the safest action ignored the publication deferral listed
 * just above it. The page also kept a 40px gutter at 390, where Work and
 * Monitor keep 16px.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/runs/[id]/**
// ui-check-owns: src/lib/runs/verdict.ts
import { assertPainted } from '../ui-check-lib.mjs';

const RUN = '/runs/f5dcb6a5';
const PHONE = { width: 390, height: 844 };

/** The sentence a run with a readable sealed package must never draw. */
const SEALED_SENTENCE = 'sealed package could not be read';

/** The deferral the fixture's ledger holds, and what it leaves a run still
 * awaiting publication to do. */
const DEFERRAL_ERROR = 'protected branch; base merge required';
const DEFERRAL_COMMAND = 'kew finalize f5dcb6a5';

/**
 * Serve the fixture run as still awaiting publication: the one status
 * `kew finalize` accepts. As committed the run succeeded with its pull request
 * open, which the server would refuse to finalize — so the page offers no
 * command for it, and the deferral's command is checked against this run.
 */
async function awaitingPublication(page) {
  await page.route('**/api/runs/f5dcb6a5', async (route) => {
    const response = await route.fetch();
    const run = await response.json();
    await route.fulfill({
      response,
      json: { ...run, status: 'awaiting_publication', pr_url: null, pr_state: null }
    });
  });
}

/**
 * Serve the fixture run as a governed review that attested its own round
 * (design review c9 D11): a review run whose process succeeded and whose
 * verdict went against the change.
 */
function asReviewRun(verdict, round) {
  return async (page) => {
    await page.route('**/api/runs/f5dcb6a5', async (route) => {
      const response = await route.fetch();
      const run = await response.json();
      await route.fulfill({
        response,
        json: { ...run, run_kind: 'adversarial_review', verdict: { ...run.verdict, verdict, round, scope: 'run' } }
      });
    });
  };
}

/** The run header and the verdict row, as drawn. */
async function readVerdict(page) {
  const row = page.locator('[data-tour="review-verdict"]');
  await row.waitFor({ state: 'visible', timeout: 15_000 });
  const squash = (s) => (s ?? '').replace(/\s+/g, ' ').trim();
  return {
    words: squash(await row.locator('[data-verdict-words]').textContent()),
    mark: await row.locator('[data-verdict-reading] svg[role="img"]').getAttribute('aria-label'),
    code: await row.locator('[data-status-word]').evaluate((el) => ({
      text: (el.textContent ?? '').trim(),
      mono: getComputedStyle(el).fontFamily.includes('Mono'),
      size: parseFloat(getComputedStyle(el).fontSize)
    })),
    wordsFont: await row.locator('[data-verdict-words]').evaluate((el) => getComputedStyle(el).fontFamily),
    heading: squash(await page.locator('[data-run-heading]').first().textContent({ timeout: 1000 }).catch(() => null)),
    process: squash(await page.locator('[data-run-process]').first().textContent({ timeout: 1000 }).catch(() => null))
  };
}

/** Record every /api read that answered 4xx/5xx or never answered. */
function recordFailures(failed) {
  return async (page) => {
    page.on('response', (response) => {
      if (response.url().includes('/api/') && response.status() >= 400) {
        failed.push(`${response.status()} ${response.url()}`);
      }
    });
    page.on('requestfailed', (request) => {
      if (request.url().includes('/api/')) failed.push(`failed ${request.url()}`);
    });
  };
}

/** The safest-action section's text and its command, once it is drawn. */
async function readSafest(page) {
  const section = page.locator('section[aria-label="Safest action"]');
  await section.waitFor({ state: 'visible', timeout: 15_000 });
  const command = section.locator('[data-safest-command]');
  const text = (await section.textContent()) ?? '';
  const commandText = (await command.count()) ? ((await command.textContent()) ?? '').trim() : '';
  return { section, command, text, commandText };
}

export async function checks({ check, visit, expect }) {
  for (const [label, size] of [
    ['1440', undefined],
    ['390', PHONE]
  ]) {
    await check(`${RUN} renders its sealed diff with no failed request at ${label}px`, () => {
      const failed = [];
      return visit(
        RUN,
        async (page, problems) => {
          // The change set sits behind the Changes disclosure (#3459), shut
          // as the page arrives: open it the way a reader does.
          await page.locator('[data-disclosure="changes"] > button').click();
          const section = page.locator('section[aria-label="Sealed change set"]');
          await section.locator('pre').waitFor({ state: 'visible', timeout: 15_000 });
          const text = (await section.textContent()) ?? '';
          expect(
            text.includes('kew/src/kew/review_gate.py'),
            'the sealed change set lists none of the fixture diff paths'
          );
          expect(
            text.includes('verification exact_tree'),
            'the sealed change set drew no rendering of the package'
          );
          const pageText = (await page.locator('main').first().textContent()) ?? '';
          expect(!pageText.includes(SEALED_SENTENCE), `${RUN} still says "${SEALED_SENTENCE}"`);
          expect(failed.length === 0, `${RUN} had failed reads:\n${failed.join('\n')}`);
          expect(problems.length === 0, `${RUN} reported:\n${problems.join('\n')}`);
        },
        size,
        recordFailures(failed)
      );
    });

    await check(`${RUN} takes its safest action from the publication deferral at ${label}px`, () =>
      visit(
        RUN,
        async (page) => {
          const safest = await readSafest(page);
          expect(
            safest.text.includes(DEFERRAL_ERROR),
            `the safest action does not name the deferral: "${safest.text.trim()}"`
          );
          expect(
            safest.commandText === DEFERRAL_COMMAND,
            `the safest action's command is "${safest.commandText}", not "${DEFERRAL_COMMAND}"`
          );
          expect(!safest.text.includes(SEALED_SENTENCE), `the safest action still says "${SEALED_SENTENCE}"`);
          // Bring the command into view, since painted means painted where a
          // reader is looking.
          await safest.command.scrollIntoViewIfNeeded();
          await assertPainted(page, safest.command, 'the safest-action command');
          const font = await safest.command.evaluate((el) => getComputedStyle(el).fontFamily);
          expect(/Plex Mono/i.test(font), `the command is set in ${font}, not Plex Mono`);
        },
        size,
        awaitingPublication
      )
    );
  }

  // The committed run succeeded and its pull request is open: the server
  // refuses `kew finalize` for it, so the page must not offer it.
  await check(`${RUN} offers no finalize for a run whose pull request is open`, () =>
    visit(RUN, async (page) => {
      const safest = await readSafest(page);
      expect(
        safest.commandText === '',
        `the safest action offers "${safest.commandText}" for a run kew finalize refuses`
      );
      expect(!safest.text.includes(SEALED_SENTENCE), `the safest action says "${SEALED_SENTENCE}"`);
    })
  );

  // The deferral, not the diff, decides the action: with the sealed package
  // unreadable the page must still send the reader to the deferral's command,
  // never to the sealed-package sentence.
  await check(`${RUN} keeps the deferral's action when the sealed diff cannot be read`, () =>
    visit(
      RUN,
      async (page) => {
        const safest = await readSafest(page);
        expect(
          safest.commandText === DEFERRAL_COMMAND,
          `the safest action's command is "${safest.commandText}", not "${DEFERRAL_COMMAND}"`
        );
        expect(!safest.text.includes(SEALED_SENTENCE), `the safest action says "${SEALED_SENTENCE}"`);
      },
      undefined,
      async (page) => {
        await awaitingPublication(page);
        await page.route('**/api/runs/f5dcb6a5/nodes/**', (route) =>
          route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"gone"}' })
        );
      }
    )
  );

  // Design review c9 D11: a verdict is a mark and plain words, the code quiet
  // mono detail; a review run's header says what it was and labels its
  // process outcome, so "success" never reads as the verdict.
  for (const [label, size] of [
    ['1440', undefined],
    ['390', PHONE]
  ]) {
    await check(`${RUN} as a review run reads its verdict as a mark and words at ${label}px`, () =>
      visit(
        RUN,
        async (page) => {
          const read = await readVerdict(page);
          expect(read.words === 'Changes requested · round 1', `the verdict reads "${read.words}"`);
          expect(read.mark === 'Changes requested', `the verdict's mark is labelled "${read.mark}"`);
          expect(read.code.text === 'NEEDS_CHANGES', `the code beside it reads "${read.code.text}"`);
          expect(read.code.mono && read.code.size === 13, `the code is not 13px mono detail: ${JSON.stringify(read.code)}`);
          expect(!read.wordsFont.includes('Mono'), `the verdict words are set in ${read.wordsFont}`);
          expect(read.heading === 'Review · round 1', `the header names the run "${read.heading}"`);
          expect(read.process === 'process success', `the header's outcome reads "${read.process}"`);
          const sideways = await page.evaluate(
            () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1
          );
          expect(!sideways, 'the run page scrolls sideways');
        },
        size,
        asReviewRun('NEEDS_CHANGES', 1)
      )
    );
  }

  await check(`${RUN} as a dispatch run keeps its header and words its standing verdict`, () =>
    visit(RUN, async (page) => {
      const read = await readVerdict(page);
      expect(read.words === 'Ready · round 1', `the verdict reads "${read.words}"`);
      expect(read.code.text === 'READY', `the code beside it reads "${read.code.text}"`);
      expect(read.heading === '' && read.process === '', `a dispatch run's header reads "${read.heading}" "${read.process}"`);
    })
  );

  await check(`${RUN} content starts 16px from the viewport edge at 390px`, () =>
    visit(
      RUN,
      async (page) => {
        const content = page.locator('[data-run-content]');
        await content.waitFor({ state: 'visible', timeout: 15_000 });
        const read = await page.evaluate(() => {
          const inner = (el) => {
            const box = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return box.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft);
          };
          const content = document.querySelector('[data-run-content]');
          const header = content?.parentElement?.querySelector('header');
          const doc = document.documentElement;
          return {
            content: content ? inner(content) : null,
            header: header ? inner(header) : null,
            scrollWidth: doc.scrollWidth,
            clientWidth: doc.clientWidth
          };
        });
        expect(read.content !== null && Math.abs(read.content - 16) <= 0.5, `the content box starts at ${read.content}px`);
        expect(read.header !== null && Math.abs(read.header - 16) <= 0.5, `the header starts at ${read.header}px`);
        expect(
          read.scrollWidth <= read.clientWidth + 1,
          `the page scrolls sideways (${read.scrollWidth}px in ${read.clientWidth}px)`
        );
      },
      PHONE
    )
  );
}

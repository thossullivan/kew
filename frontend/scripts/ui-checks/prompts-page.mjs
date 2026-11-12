/**
 * The Prompts page (#3019): every role listed, the reviewer's pinned version
 * and preview on selection, `?role=` in the URL, no sideways scroll at 1440 or
 * 390, a 400-character prompt line wrapped inside its block, and a 503 from
 * `/api/prompts` read as a sentence; the pane's chip is the preview's own
 * source, a refused dispatch reads as its refusal, not an empty render, and
 * the pane says whether its preview carries no profile, the active one, or a
 * refused one — beside a launch refusal too (#3023); a composer refusal
 * served beside the base text reads as a refusal as well, and the caption says
 * which `[prompt]` the shown text was rendered with (#3071); an active
 * profile's preview draws its heading and body once, before the fixed
 * sections, and a refused one's is the base with no addendum (#3072).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/prompts/+page.*
// ui-check-owns: src/lib/prompts/sections.ts
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { FRONTEND } from '../ui-check-lib.mjs';

const PHONE = { width: 390, height: 844 };
const REVIEWER = 'adversarial_review';

const fixture = (name) => JSON.parse(readFileSync(new URL(`fixtures/factory/${name}`, FRONTEND), 'utf8'));
const ROLES = fixture('prompts.json');
const REVIEWER_READ = fixture(`prompts-${REVIEWER}.json`);

/** Every box on the page that is wider than the box it scrolls in. */
function sideways() {
  const wide = [];
  const root = document.scrollingElement ?? document.documentElement;
  if (root.scrollWidth > root.clientWidth + 1) wide.push(`the page (${root.scrollWidth} in ${root.clientWidth})`);
  for (const el of document.querySelectorAll('main *')) {
    const { overflowX } = getComputedStyle(el);
    if (overflowX !== 'auto' && overflowX !== 'scroll') continue;
    if (el.scrollWidth > el.clientWidth + 1) {
      wide.push(`${el.tagName.toLowerCase()}${el.dataset.promptPane !== undefined ? '[data-prompt-pane]' : ''} (${el.scrollWidth} in ${el.clientWidth})`);
    }
  }
  return wide;
}

/** Serve one role's preview with `line` as the first section's body. */
const withLine = (role, line) => (page) =>
  page.route(
    (url) => url.pathname === `/api/prompts/${role}`,
    async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      body.preview = `lead\n## Long line\n${line}\n## Second\nshort`;
      await route.fulfill({ response, json: body });
    }
  );

export async function checks({ check, visit, expect, sizes: { WIDTH, HEIGHT } }) {
  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    /**
     * AC1 — every fixture role is a row; the reviewer's row selects it, the
     * pane names its pinned version and draws its preview, the address bar
     * holds `?role=`, a reload keeps the selection, and nothing scrolls
     * sideways at either width.
     */
    await check(`/prompts lists every role and opens the reviewer at ${size.width}px`, () =>
      visit(
        '/prompts',
        async (page) => {
          await page.locator('[data-role]').first().waitFor({ state: 'visible', timeout: 15_000 });
          const listed = await page.locator('[data-role]').evaluateAll((els) => els.map((el) => el.dataset.role));
          expect(
            JSON.stringify(listed) === JSON.stringify(ROLES.map((r) => r.role)),
            `listed ${listed.join(', ')}; the fixture serves ${ROLES.map((r) => r.role).join(', ')}`
          );

          await page.locator(`[data-role="${REVIEWER}"]`).click();
          await page.locator('[data-preview]').waitFor({ state: 'visible', timeout: 15_000 });
          await page.waitForFunction(
            (role) => document.querySelector('[data-prompt-pane] h2')?.textContent?.trim() === role,
            REVIEWER,
            { timeout: 5_000 }
          );
          const { version, sha256 } = REVIEWER_READ.source;
          const chip = (await page.locator('[data-pane-source]').textContent()).trim();
          expect(
            chip.startsWith(`pinned v${version} · ${sha256.slice(0, 7)}`),
            `the reviewer's pane reads "${chip}", not its pinned v${version}`
          );
          const title = REVIEWER_READ.preview.split('\n').find((line) => line.startsWith('# '));
          const shown = await page.locator('[data-preview]').innerText();
          expect(title && shown.includes(title), `the preview does not show "${title}"`);
          const firstSection = REVIEWER_READ.preview.split('\n').find((line) => line.startsWith('## '));
          const expanded = await page.locator('[data-section]').evaluateAll((els) =>
            els.map((el) => el.getAttribute('aria-expanded'))
          );
          expect(
            expanded[0] === 'true' && expanded.slice(1).every((open) => open === 'false'),
            `only the first section should be open: ${expanded.join(', ')}`
          );
          expect(shown.includes(firstSection), `the first section "${firstSection}" is not drawn`);

          const role = new URL(page.url()).searchParams.get('role');
          expect(role === REVIEWER, `the URL holds ?role=${role}, not ${REVIEWER}`);
          const wide = await page.evaluate(sideways);
          expect(wide.length === 0, `scrolls sideways: ${wide.join('; ')}`);

          await page.reload({ waitUntil: 'load' });
          await page.waitForFunction(
            (role) => document.querySelector('[data-prompt-pane] h2')?.textContent?.trim() === role,
            REVIEWER,
            { timeout: 15_000 }
          );
          const current = await page.locator('[data-role][aria-current="true"]').getAttribute('data-role');
          expect(current === REVIEWER, `after a reload the list marks ${current}, not ${REVIEWER}`);
        },
        size
      )
    );

    /**
     * AC2 — a 400-character prompt line, once with spaces and once without,
     * wraps inside its block: the block's right edge stays inside the pane and
     * neither the block nor the pane scrolls sideways.
     */
    for (const [what, line] of [
      ['words', `${Array(80).fill('abcd').join(' ')}e`],
      ['one unbroken run', 'x'.repeat(400)]
    ]) {
      await check(`a 400-character prompt line of ${what} wraps inside its block at ${size.width}px`, () =>
        visit(
          `/prompts?role=${REVIEWER}`,
          async (page) => {
            await page.locator('[data-preview]').waitFor({ state: 'visible', timeout: 15_000 });
            await page.waitForFunction(
              () => document.querySelector('[data-preview]')?.textContent?.includes('Long line'),
              null,
              { timeout: 15_000 }
            );
            const read = await page.evaluate((needle) => {
              const block = document.querySelector('[data-preview]');
              const pane = document.querySelector('[data-prompt-pane]');
              const run = [...block.querySelectorAll('div')].find((el) => el.textContent.includes(needle));
              // Line boxes, one client rect top each, so a clipped line cannot pass.
              const range = document.createRange();
              range.selectNodeContents(run);
              const tops = new Set([...range.getClientRects()].map((r) => Math.round(r.top)));
              return {
                length: needle.length,
                blockRight: block.getBoundingClientRect().right,
                paneRight: pane.getBoundingClientRect().right,
                blockWide: block.scrollWidth > block.clientWidth + 1,
                runLines: tops.size
              };
            }, line);
            expect(read.length === 400, `the served line is ${read.length} characters`);
            expect(
              read.blockRight <= read.paneRight + 0.5,
              `the block's right edge ${read.blockRight} is outside the pane's ${read.paneRight}`
            );
            expect(!read.blockWide, 'the preview block scrolls sideways');
            expect(read.runLines > 1, `the 400-character line drew on ${read.runLines} line(s), so it did not wrap`);
            const wide = await page.evaluate(sideways);
            expect(wide.length === 0, `scrolls sideways: ${wide.join('; ')}`);
          },
          size,
          withLine(REVIEWER, line)
        )
      );
    }
  }

  /**
   * AC3 — `/api/prompts` answering 503 reads as a sentence where the list and
   * the pane would be, never as a blank page.
   */
  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    await check(`/api/prompts answering 503 reads as a sentence at ${size.width}px`, () =>
      visit(
        '/prompts',
        async (page) => {
          const failure = page.locator('[data-prompts-failure]');
          await failure.waitFor({ state: 'visible', timeout: 15_000 });
          const text = (await failure.innerText()).replace(/\s+/g, ' ').trim();
          expect(/could not be read \(503\)/.test(text), `the failure reads "${text}"`);
          expect(/\.$|\?$/.test(text), `"${text}" is not a sentence`);
          expect((await page.locator('[data-role]').count()) === 0, 'roles are listed beside the failure');
        },
        size,
        (page) =>
          page.route(
            (url) => url.pathname === '/api/prompts',
            (route) => route.fulfill({ status: 503, json: { detail: 'unavailable' } })
          )
      )
    );
  }

  /** A role's own preview answering 503 is a sentence in the pane, too. */
  await check('a role preview answering 503 reads as a sentence in the pane', () =>
    visit(
      `/prompts?role=${REVIEWER}`,
      async (page) => {
        const failure = page.locator('[data-preview-failure]');
        await failure.waitFor({ state: 'visible', timeout: 15_000 });
        const text = (await failure.innerText()).replace(/\s+/g, ' ').trim();
        expect(/could not be read \(503\)/.test(text), `the pane reads "${text}"`);
      },
      undefined,
      (page) =>
        page.route(
          (url) => url.pathname === `/api/prompts/${REVIEWER}`,
          (route) => route.fulfill({ status: 503, json: { detail: 'no renderer' } })
        )
    )
  );

  /**
   * The catalog says the reviewer is pinned, but the preview it then reads was
   * rendered from a deployed copy: the pane labels those bytes with the
   * preview's source, chip and change line both.
   */
  const DEPLOYED = { kind: 'deployed-artifact', name: '.claude/kew/adversarial-reviewer.md', version: null, sha256: 'd'.repeat(64), pin_mismatch: false };
  await check("the pane labels a preview with the preview's own source, not the catalog's", () =>
    visit(
      `/prompts?role=${REVIEWER}`,
      async (page) => {
        await page.locator('[data-preview]').waitFor({ state: 'visible', timeout: 15_000 });
        const row = (await page.locator(`[data-role="${REVIEWER}"] [data-source-chip]`).textContent()).trim();
        expect(row.startsWith('pinned v'), `the catalog row reads "${row}", not the catalog's pinned source`);
        const chip = (await page.locator('[data-pane-source]').textContent()).trim();
        expect(chip === 'deployed copy · ddddddd', `the pane reads "${chip}" over deployed bytes`);
        const change = (await page.locator('[data-change-line]').innerText()).replace(/\s+/g, ' ').trim();
        expect(change.includes(DEPLOYED.name), `the change line reads "${change}", not the deployed copy's`);
        expect(!change.includes('_skills'), `the change line still points at the pin: "${change}"`);
      },
      undefined,
      async (page) => {
        expect(ROLES.find((r) => r.role === REVIEWER)?.source.kind === 'pinned-artifact', 'the catalog fixture is not pinned');
        await page.route(
          (url) => url.pathname === `/api/prompts/${REVIEWER}`,
          (route) => route.fulfill({ json: { ...REVIEWER_READ, source: DEPLOYED } })
        );
      }
    )
  );

  /**
   * #3023 — the pane says which profile its preview carries, from
   * `variant.profile`: none, the active version, or the refusal and its
   * reason. Neither of the last two may say the preview has no profile.
   */
  const PROFILE_ROLE = 'ci_fix';
  const PROFILE_REASON = 'its body does not match the approved digest; retire the profile and propose it again';
  const PROFILE_HEADING = `## Operator instructions — ${PROFILE_ROLE} profile v3`;
  const PROFILE_BODY = `Operator note for ${PROFILE_ROLE}: keep every change minimal.`;
  const FIXED_SECTIONS = ['## Safety Rules', '## Instructions'];
  const PROFILE_BASE = fixture(`prompts-${PROFILE_ROLE}.json`);
  /**
   * The base with the addendum where `ci-fix.md.j2` renders it: the block
   * above the safety rules, its span in code points, and the precedence line
   * under each fixed heading.
   */
  const withAddendum = (base) => {
    const block = `${PROFILE_HEADING}\n\n${PROFILE_BODY}`;
    const before = base.slice(0, base.indexOf(FIXED_SECTIONS[0]));
    let text = `${before}${block}\n\n${base.slice(before.length)}`;
    for (const heading of FIXED_SECTIONS) {
      text = text.replace(`${heading}\n\n`, `${heading}\n\nThe rules below take precedence over operator instructions.\n\n`);
    }
    return { text, span: { start: Array.from(before).length, end: Array.from(before + block).length } };
  };
  const PROFILE_STATES = [
    { kind: 'none', label: 'none', says: 'No operator profile is active' },
    { kind: 'active', label: 'v3', says: 'Operator profile v3 is active', addendum: true },
    { kind: 'refused', label: `refused: ${PROFILE_REASON}`, says: PROFILE_REASON, addendum: false }
  ];
  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    for (const state of PROFILE_STATES) {
      await check(`a ${state.kind} profile reads as ${state.kind} at ${size.width}px`, () =>
        visit(
          `/prompts?role=${PROFILE_ROLE}`,
          async (page) => {
            const line = page.locator('[data-profile-state]');
            await line.waitFor({ state: 'visible', timeout: 15_000 });
            const kind = await line.getAttribute('data-profile-state');
            expect(kind === state.kind, `the pane reads the profile as ${kind}, not ${state.kind}`);
            const text = (await line.innerText()).replace(/\s+/g, ' ').trim();
            expect(text.includes(state.says), `the profile line reads "${text}", not "${state.says}"`);
            const pane = (await page.locator('[data-prompt-pane]').innerText()).replace(/\s+/g, ' ');
            expect(!/without any profile/.test(pane), 'the pane still says the preview has no profile');
            if (state.kind !== 'none') {
              expect(!pane.includes('No operator profile is active'), `a ${state.kind} profile reads as none`);
            }
            if (state.kind === 'refused') {
              expect(/every launch for this role refuses/.test(text), `the refusal does not say the launch refuses: "${text}"`);
            }
            if (state.addendum === true) {
              // A first read opens only the first section, so the addendum is opened to read its body.
              const headings = page.locator('[data-addendum="heading"]');
              expect((await headings.count()) === 1, `${await headings.count()} addendum headings are drawn, not one`);
              const heading = (await headings.innerText()).replace(/\s+/g, ' ');
              expect(heading.includes(PROFILE_HEADING), `the addendum heading reads "${heading}"`);
              await headings.click();
              const bodies = page.locator('[data-addendum="body"]');
              await bodies.first().waitFor({ state: 'visible', timeout: 5_000 });
              expect((await bodies.count()) === 1, `${await bodies.count()} addendum bodies are drawn, not one`);
              const body = await bodies.innerText();
              expect(body.includes(PROFILE_BODY), `the addendum body reads "${body}"`);
              const shown = await page.locator('[data-preview]').innerText();
              expect(shown.split(PROFILE_HEADING).length === 2, `the preview does not show "${PROFILE_HEADING}" once`);
              expect(shown.split(PROFILE_BODY).length === 2, `the preview does not show "${PROFILE_BODY}" once`);
              for (const section of FIXED_SECTIONS) {
                const at = shown.indexOf(section);
                expect(at >= 0, `the preview does not show "${section}"`);
                expect(shown.indexOf(PROFILE_HEADING) < at, `the addendum heading is not before "${section}"`);
                expect(shown.indexOf(PROFILE_BODY) < at, `the addendum body is not before "${section}"`);
              }
            }
            if (state.addendum === false) {
              const shown = await page.locator('[data-preview]').innerText();
              const first = PROFILE_BASE.preview.split('\n').find((l) => l.startsWith('## '));
              for (const section of [first, ...FIXED_SECTIONS]) {
                expect(shown.includes(section), `the base section "${section}" is not shown`);
              }
              expect((await page.locator('[data-addendum]').count()) === 0, 'an addendum block is drawn');
              for (const claim of ['Operator instructions', PROFILE_BODY, 'precedence over operator instructions']) {
                expect(!shown.includes(claim), `the refused profile's preview shows "${claim}"`);
              }
            }
            const wide = await page.evaluate(sideways);
            expect(wide.length === 0, `scrolls sideways: ${wide.join('; ')}`);
          },
          size,
          (page) =>
            page.route(
              (url) => url.pathname === `/api/prompts/${PROFILE_ROLE}`,
              async (route) => {
                const response = await route.fetch();
                const body = await response.json();
                expect(body.preview === PROFILE_BASE.preview && !body.addendum, 'the served preview is not the fixture base');
                expect(!body.preview.includes('Operator instructions'), 'the base fixture already carries an addendum');
                if (state.addendum === true) {
                  const { text, span } = withAddendum(body.preview);
                  body.preview = text;
                  body.preview_sha256 = createHash('sha256').update(text, 'utf8').digest('hex');
                  body.addendum = span;
                }
                body.variant = { ...body.variant, profile: state.label };
                await route.fulfill({ response, json: body });
              }
            )
        )
      );
    }
  }

  /**
   * A dispatch the config refuses (`[sandbox] enabled = false` without the
   * skip) has no prompt: the pane says so in a sentence with the refusal, and
   * draws neither the "Rendered over" line nor an empty preview block.
   */
  const REFUSAL = 'The sandbox is off and containment is not skipped, so a dispatch is refused.';
  await check('a refused dispatch reads as its refusal, not as an empty render', () =>
    visit(
      '/prompts?role=dispatch',
      async (page) => {
        const refused = page.locator('[data-preview-refused]');
        await refused.waitFor({ state: 'visible', timeout: 15_000 });
        const text = (await refused.innerText()).replace(/\s+/g, ' ').trim();
        expect(/refuses before it builds a prompt/.test(text), `the pane reads "${text}"`);
        expect(text.includes(REFUSAL), `the pane does not carry the refusal: "${text}"`);
        const size = await refused.evaluate((el) => getComputedStyle(el).fontSize);
        expect(size === '15px', `the refusal is ${size}, not 15px`);
        expect((await page.locator('[data-preview]').count()) === 0, 'an empty preview block is drawn');
        const pane = await page.locator('[data-prompt-pane]').innerText();
        expect(!pane.includes('Rendered over'), 'the pane still says the prompt was rendered');
      },
      undefined,
      (page) =>
        page.route(
          (url) => url.pathname === '/api/prompts/dispatch',
          async (route) => {
            const response = await route.fetch();
            const body = await response.json();
            body.preview = '';
            body.preview_sha256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
            body.variant = { runner: 'claude-code', admission: 'decided at launch', posture: 'refused', refusal: REFUSAL };
            await route.fulfill({ response, json: body });
          }
        )
    )
  );

  /**
   * #3023 — a dispatch launch refusal with a profile active, or refused, still
   * names the profile state beside the refusal, and no sentence says a prompt
   * was rendered: no preview block, no "Rendered over", no "rendered here".
   */
  const REFUSED_WITH = [
    { kind: 'active', label: 'v2', says: 'Operator profile v2 is active' },
    { kind: 'refused', label: `refused: ${PROFILE_REASON}`, says: PROFILE_REASON }
  ];
  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    for (const state of REFUSED_WITH) {
      await check(`a refused dispatch with a ${state.kind} profile names both at ${size.width}px`, () =>
        visit(
          '/prompts?role=dispatch',
          async (page) => {
            await page.locator('[data-preview-refused]').waitFor({ state: 'visible', timeout: 15_000 });
            const line = page.locator('[data-profile-state]');
            await line.waitFor({ state: 'visible', timeout: 5_000 });
            const kind = await line.getAttribute('data-profile-state');
            expect(kind === state.kind, `the pane reads the profile as ${kind}, not ${state.kind}`);
            const text = (await line.innerText()).replace(/\s+/g, ' ').trim();
            expect(text.includes(state.says), `the profile line reads "${text}", not "${state.says}"`);
            const pane = (await page.locator('[data-prompt-pane]').innerText()).replace(/\s+/g, ' ');
            expect(pane.includes(REFUSAL), `the launch refusal is gone: "${pane}"`);
            expect((await page.locator('[data-preview]').count()) === 0, 'a preview block is drawn');
            for (const claim of ['Rendered over', 'rendered here', 'base prompt', 'No operator profile is active']) {
              expect(!pane.includes(claim), `the pane says "${claim}" beside a launch refusal`);
            }
            const wide = await page.evaluate(sideways);
            expect(wide.length === 0, `scrolls sideways: ${wide.join('; ')}`);
          },
          size,
          (page) =>
            page.route(
              (url) => url.pathname === '/api/prompts/dispatch',
              async (route) => {
                const response = await route.fetch();
                const body = await response.json();
                body.preview = '';
                body.preview_sha256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
                body.variant = {
                  runner: 'claude-code',
                  admission: 'decided at launch',
                  posture: 'refused',
                  refusal: REFUSAL,
                  profile: state.label
                };
                await route.fulfill({ response, json: body });
              }
            )
        )
      );
    }
  }

  /**
   * #3071 — a composer refusal (an append under host publication) serves the
   * base text beside its refusal: the pane reads it as a refusal, draws no
   * preview block, and still names the active profile.
   */
  const HOST_REFUSAL = 'host publication requires the built-in prompt without appended instructions';
  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    await check(`a composer refusal beside a base text reads as its refusal at ${size.width}px`, () =>
      visit(
        '/prompts?role=dispatch',
        async (page) => {
          const refused = page.locator('[data-preview-refused]');
          await refused.waitFor({ state: 'visible', timeout: 15_000 });
          const text = (await refused.innerText()).replace(/\s+/g, ' ').trim();
          expect(text.includes(HOST_REFUSAL), `the pane does not carry the refusal: "${text}"`);
          expect((await page.locator('[data-preview]').count()) === 0, 'a preview block is drawn');
          const line = (await page.locator('[data-profile-state]').innerText()).replace(/\s+/g, ' ');
          expect(line.includes('Operator profile v1 is active'), `the profile line reads "${line}"`);
          const pane = (await page.locator('[data-prompt-pane]').innerText()).replace(/\s+/g, ' ');
          for (const claim of ['Rendered over', 'rendered here']) {
            expect(!pane.includes(claim), `the pane says "${claim}" beside a launch refusal`);
          }
          const wide = await page.evaluate(sideways);
          expect(wide.length === 0, `scrolls sideways: ${wide.join('; ')}`);
        },
        size,
        (page) =>
          page.route(
            (url) => url.pathname === '/api/prompts/dispatch',
            async (route) => {
              const response = await route.fetch();
              const body = await response.json();
              body.variant = { ...body.variant, publication: 'host', refusal: HOST_REFUSAL, profile: 'v1' };
              body.prompt_config_rendered = false;
              await route.fulfill({ response, json: body });
            }
          )
      )
    );
  }

  /**
   * #3071 — the caption says which `[prompt]` the shown text was rendered
   * with, from the preview's `prompt_config_rendered`, never from the role's
   * `prompt_config_applies`: a profile refused beside a custom template (under
   * agent publication) leaves the built-in base, with no `refusal`, so the
   * caption may not claim the served configuration; the fixture's dispatch
   * preview was rendered with it, and says so.
   */
  const TEMPLATE_REASON =
    'the dispatch prompt profile v1 is active and only the built-in prompt renders it; retire the profile or remove [prompt] template from kew.toml';
  const CAPTIONS = [
    {
      what: 'a refused profile beside a custom template',
      says: 'without any [prompt] override',
      never: 'with the served [prompt] configuration',
      answer: (body) => {
        body.variant = { ...body.variant, publication: 'agent', profile: `refused: ${TEMPLATE_REASON}` };
        body.prompt_config_rendered = false;
      }
    },
    {
      what: 'the served [prompt] configuration',
      says: 'with the served [prompt] configuration',
      never: 'without any [prompt] override',
      answer: () => {}
    }
  ];
  for (const caption of CAPTIONS) {
    await check(`the caption of a preview rendered with ${caption.what} says so`, () =>
      visit(
        '/prompts?role=dispatch',
        async (page) => {
          const line = page.locator('[data-preview-caption]');
          await line.waitFor({ state: 'visible', timeout: 15_000 });
          const text = (await line.innerText()).replace(/\s+/g, ' ').trim();
          expect(text.includes(caption.says), `the caption reads "${text}", not "${caption.says}"`);
          expect(!text.includes(caption.never), `the caption reads "${text}"`);
          expect((await page.locator('[data-preview-refused]').count()) === 0, 'the pane reads as a refusal');
        },
        { width: WIDTH, height: HEIGHT },
        (page) =>
          page.route(
            (url) => url.pathname === '/api/prompts/dispatch',
            async (route) => {
              const response = await route.fetch();
              const body = await response.json();
              caption.answer(body);
              await route.fulfill({ response, json: body });
            }
          )
      )
    );
  }
}

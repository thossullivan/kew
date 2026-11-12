/**
 * The Prompts page's refused profile (#3075): every launch for the role
 * refuses, so at 1440 and 390 the refused state leads the pane — before the
 * neutral render metadata, and before a launch refusal — in the server's own
 * reason, which names its own remedy: the page adds no fixed one. Magenta
 * means this viewer has a move, read off the editor's history read
 * (`can_decide`, `decide_refusal`, #3119):
 *
 * - an approver: the shared needs-you block (#3051) in the attention colour,
 *   the reason, no DO THIS, and one link to the operator-instructions editor;
 * - a viewer who is not an approver: no magenta above the preview, one
 *   neutral text-15 text-1 sentence saying an approver must clear it;
 * - a viewer whose move is a config fix: the needs-you block, its DO THIS the
 *   server's `decide_refusal` sentence.
 *
 * Until the history lands the lead is the neutral sentence, never a flash of
 * magenta. An active or absent profile draws no needs-you block and no
 * magenta.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/prompts/+page.svelte
// ui-check-owns: src/lib/prompts/profile-state.ts
// ui-check-owns: src/lib/prompts/ProfileEditor.svelte

const PHONE = { width: 390, height: 844 };
const ROLE = 'ci_fix';
const REASON = 'its body does not match the approved digest; retire the profile and propose it again';
const LEAD = 'The active operator profile is refused, so every launch for this role refuses: ';
const NEUTRAL = `${LEAD}${REASON}. An approver must clear it.`;
const UNCONFIGURED = 'Set [approval].approvers in kew.toml before deciding prompt profiles.';
const LAUNCH_REFUSAL = 'The sandbox is off and containment is not skipped, so a dispatch is refused.';
const ANCHOR = 'operator-instructions';

/** The three viewers a refused profile reads differently for. */
const VIEWERS = [
  { name: 'an approver', can_decide: true, decide_refusal: null, magenta: true },
  { name: 'a viewer who is not an approver', can_decide: false, decide_refusal: null, magenta: false },
  { name: 'a viewer with no approver configured', can_decide: false, decide_refusal: UNCONFIGURED, magenta: true }
];

/** A token as the browser computes it as a colour, so the check reads the token, not a literal. */
const token = (page, name) =>
  page.evaluate((name) => {
    const probe = document.createElement('span');
    probe.style.color = `var(${name})`;
    document.body.append(probe);
    const colour = getComputedStyle(probe).color;
    probe.remove();
    return colour;
  }, name);

/**
 * Where the pane's lead sits among the pane's other statements, and the
 * colours it computes to. Each index is in document order over the pane's
 * elements, -1 when absent.
 */
const readPane = (page) =>
  page.locator('[data-prompt-pane]').evaluate((pane) => {
    const all = [...pane.querySelectorAll('*')];
    const at = (sel) => {
      const el = pane.querySelector(sel);
      return el === null ? -1 : all.indexOf(el);
    };
    const block = pane.querySelector('section[aria-label^="Needs you"]');
    const word = block?.querySelector('[data-status-word]') ?? null;
    const note = block?.querySelector('[data-needs-you-note]') ?? null;
    const neutral = pane.querySelector('[data-profile-refused-neutral]');
    const end = pane.querySelector('[data-preview]');
    const editor = pane.querySelector('[data-profile-editor]');
    return {
      block: at('section[aria-label^="Needs you"]'),
      neutral: at('[data-profile-refused-neutral]'),
      caption: at('[data-preview-caption]'),
      refused: at('[data-preview-refused]'),
      preview: at('[data-preview]'),
      text: block ? block.innerText.replace(/\s+/g, ' ') : '',
      doThis: note ? note.innerText.replace(/\s+/g, ' ').trim() : null,
      links: block ? [...block.querySelectorAll('a')].map((a) => a.getAttribute('href')) : [],
      word: word ? getComputedStyle(word).color : null,
      edge: block ? getComputedStyle(block).borderLeftColor : null,
      neutralText: neutral ? neutral.innerText.replace(/\s+/g, ' ').trim() : '',
      neutralColour: neutral ? getComputedStyle(neutral).color : null,
      neutralSize: neutral ? getComputedStyle(neutral).fontSize : null,
      // The pane down to the end of the preview — or, beside a launch
      // refusal, which draws no preview, down to the editor: the editor holds
      // its own magenta move (a proposal to decide), which is not this state's.
      colours: all
        .filter((el) => {
          const stop = end ?? editor;
          return (
            stop === null ||
            (end !== null && (el === end || end.contains(el))) ||
            !!(el.compareDocumentPosition(stop) & Node.DOCUMENT_POSITION_FOLLOWING)
          );
        })
        .flatMap((el) => [getComputedStyle(el).color, getComputedStyle(el).borderLeftColor])
    };
  });

/**
 * Serve the role's preview with `profile` as its variant's profile label and,
 * with a viewer, the role's history read as that viewer's. `gate`, when
 * given, holds the history read until it resolves.
 */
const serve =
  (role, profile, { launchRefused = false, viewer = null, gate = null } = {}) =>
  async (page) => {
    await page.route(
      (url) => url.pathname === `/api/prompts/${role}`,
      async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        body.variant = { ...body.variant, profile };
        if (launchRefused) {
          body.preview = '';
          body.preview_sha256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
          body.variant = {
            ...body.variant,
            posture: 'refused',
            refusal: LAUNCH_REFUSAL
          };
        }
        await route.fulfill({ response, json: body });
      }
    );
    if (viewer === null) return;
    await page.route(
      (url) => url.pathname === `/api/prompts/${role}/versions`,
      async (route) => {
        if (gate !== null) await gate;
        try {
          const response = await route.fetch();
          const body = await response.json();
          await route.fulfill({
            response,
            json: { ...body, can_decide: viewer.can_decide, decide_refusal: viewer.decide_refusal }
          });
        } catch {
          // A held read released after its check failed lands on a closed
          // page: the check has already said what failed.
        }
      }
    );
  };

/** The history has landed and agrees with the preview: the editor draws its write row. */
const historyShown = (page) => page.locator('[data-profile-count]').waitFor({ state: 'visible', timeout: 15_000 });

export async function checks({ check, visit, expect, sizes: { WIDTH, HEIGHT } }) {
  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    for (const viewer of VIEWERS) {
      for (const launchRefused of [false, true]) {
        const where = launchRefused ? 'beside a launch refusal' : 'over the base preview';
        const how = viewer.magenta ? 'in magenta' : 'as a neutral sentence';
        await check(`a refused profile leads the pane ${how} for ${viewer.name} ${where} at ${size.width}px`, () =>
          visit(
            `/prompts?role=${ROLE}`,
            async (page) => {
              const lead = launchRefused ? '[data-preview-refused]' : '[data-preview-caption]';
              await page.locator(lead).waitFor({ state: 'visible', timeout: 15_000 });
              await historyShown(page);
              const pane = await readPane(page);
              const magenta = await token(page, '--accent');
              const after = launchRefused ? pane.refused : pane.caption;
              if (viewer.magenta) {
                expect(pane.block >= 0, 'no needs-you block is drawn for the refused profile');
                expect(pane.neutral === -1, 'the neutral sentence is drawn beside the needs-you block');
                expect(pane.block < after, `the needs-you block (${pane.block}) does not come before ${lead} (${after})`);
                if (!launchRefused) {
                  expect(pane.block < pane.preview, 'the needs-you block does not come before the preview');
                }
                expect(pane.word === magenta, `the status word computes to ${pane.word}, not the attention ${magenta}`);
                expect(pane.edge === magenta, `the block's left edge computes to ${pane.edge}, not ${magenta}`);
                expect(pane.text.includes(`${LEAD}${REASON}.`), `the block does not say the reason: "${pane.text}"`);
                expect(!pane.text.includes('Approve a new version'), `the block adds a fixed remedy: "${pane.text}"`);
              } else {
                expect(pane.block === -1, 'a viewer with no move is shown a needs-you block');
                expect(pane.neutral >= 0, 'no neutral sentence is drawn for the refused profile');
                expect(pane.neutral < after, `the neutral sentence (${pane.neutral}) does not come before ${lead} (${after})`);
                expect(pane.neutralText === NEUTRAL, `the neutral sentence reads "${pane.neutralText}"`);
                const text1 = await token(page, '--text-1');
                expect(pane.neutralColour === text1, `the neutral sentence computes to ${pane.neutralColour}, not text-1 ${text1}`);
                expect(pane.neutralSize === '15px', `the neutral sentence is ${pane.neutralSize}, not 15px`);
                expect(!pane.colours.includes(magenta), 'the pane above the preview computes to the attention colour');
              }
              if (viewer.can_decide) {
                expect(pane.doThis === null, `an approver's block carries a DO THIS: "${pane.doThis}"`);
                expect(
                  pane.links.length === 1 && pane.links[0].endsWith(`#${ANCHOR}`),
                  `the block's links are ${JSON.stringify(pane.links)}, not the one editor anchor`
                );
                const target = await page.evaluate((id) => document.getElementById(id)?.hasAttribute('data-profile-editor') ?? false, ANCHOR);
                expect(target, `#${ANCHOR} is not the operator-instructions editor`);
              } else if (viewer.decide_refusal !== null) {
                expect(pane.doThis === viewer.decide_refusal, `the DO THIS reads "${pane.doThis}", not "${viewer.decide_refusal}"`);
                const mono = await page
                  .locator('[data-needs-you-note] .font-mono')
                  .evaluateAll((els) => els.map((e) => e.textContent));
                expect(
                  JSON.stringify(mono) === JSON.stringify(['[approval].approvers', 'kew.toml']),
                  `the DO THIS sets ${JSON.stringify(mono)} in mono`
                );
                const write = (await page.locator('[data-write-reason]').innerText()).replace(/\s+/g, ' ').trim();
                expect(write === viewer.decide_refusal, `the write-area sentence "${write}" disagrees with the DO THIS`);
              }
            },
            size,
            serve(ROLE, `refused: ${REASON}`, { launchRefused, viewer })
          )
        );
      }
    }

    await check(`the editor link brings the operator instructions into view at ${size.width}px`, () =>
      visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          const link = page.locator(`[data-prompt-pane] section[aria-label^="Needs you"] a[href$="#${ANCHOR}"]`);
          await link.waitFor({ state: 'visible', timeout: 15_000 });
          await link.click();
          await page.waitForFunction(
            (id) => {
              const box = document.getElementById(id)?.getBoundingClientRect();
              return !!box && box.top >= 0 && box.top < window.innerHeight;
            },
            ANCHOR,
            { timeout: 5_000 }
          );
          const role = new URL(page.url()).searchParams.get('role');
          expect(role === ROLE, `following the link left the pane on role ${role}`);
        },
        size,
        serve(ROLE, `refused: ${REASON}`, { viewer: VIEWERS[0] })
      )
    );

    await check(`a refused profile waits neutral, not magenta, for its history at ${size.width}px`, () => {
      let release;
      const gate = new Promise((resolve) => (release = resolve));
      return visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          try {
            await page.locator('[data-preview-caption]').waitFor({ state: 'visible', timeout: 15_000 });
            const held = await readPane(page);
            const magenta = await token(page, '--accent');
            expect(held.block === -1, 'a needs-you block is drawn before the history says who may act');
            expect(held.neutralText === NEUTRAL, `while the history is read the lead reads "${held.neutralText}"`);
            expect(!held.colours.includes(magenta), 'the pane computes to the attention colour before the history lands');
          } finally {
            release();
          }
          await page
            .locator('[data-prompt-pane] section[aria-label^="Needs you"]')
            .waitFor({ state: 'visible', timeout: 15_000 });
          const landed = await readPane(page);
          expect(landed.neutral === -1, 'the neutral sentence stays beside the needs-you block once the history lands');
        },
        size,
        serve(ROLE, `refused: ${REASON}`, { viewer: VIEWERS[0], gate })
      );
    });

    for (const [kind, label] of [
      ['active', 'v3'],
      ['absent', 'none']
    ]) {
      await check(`an ${kind} profile stays neutral at ${size.width}px`, () =>
        visit(
          `/prompts?role=${ROLE}`,
          async (page) => {
            await page.locator('[data-preview]').waitFor({ state: 'visible', timeout: 15_000 });
            const pane = await readPane(page);
            const magenta = await token(page, '--accent');
            expect(pane.block === -1, `an ${kind} profile draws a needs-you block`);
            expect(pane.neutral === -1, `an ${kind} profile draws the refused sentence`);
            expect(!pane.colours.includes(magenta), `an ${kind} profile's pane computes to the attention colour`);
          },
          size,
          serve(ROLE, label)
        )
      );
    }
  }
}

/**
 * The Prompts page's operator instructions (#3025): propose, then approve,
 * shows v1 active in the history and the preview carries the addendum under
 * its heading, marked by a cyan left rule; a 403 on approve reads as the
 * approver sentence and a 409 as the reload sentence, and neither moves the
 * shown active version; Approve and Decline are the editor's only magenta
 * controls, and only while a proposal is pending (a refused profile's
 * needs-you block above the preview is `prompts-profile-attention`'s, #3075;
 * no check here serves a refused profile). A slow reload of one role
 * never replaces another role's editor; the cyan rule covers an addendum with
 * a `## ` heading of its own to its true end and no fixed section; and the
 * active version's audit link opens an event older than the audit page's
 * first page.
 *
 * Every decision acts only on the state on screen (#3092): a 409 and a Reload
 * leave no armed confirmation; two pending versions are both decidable, and
 * approving the older one over a newer active version asks first; after a 409
 * every write stays off until the history and the preview reload coherently;
 * a viewer who is not an approver sees no magenta in the editor and no live
 * decision (nor, over a refused profile, above it: #3075); and a
 * body of 4,000 non-BMP code points is counted and sent as the server counts.
 *
 * The answers are `fixtures/scenarios/prompts-versions.json`, generated from
 * the real API; fixture mode serves no write, so each check routes its own.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it.
 */
// ui-check-owns: src/routes/(factory)/prompts/+page.svelte
// ui-check-owns: src/lib/prompts/ProfileEditor.svelte
// ui-check-owns: src/lib/prompts/sections.ts
import { readFileSync } from 'node:fs';
import { FRONTEND } from '../ui-check-lib.mjs';

const PHONE = { width: 390, height: 844 };
const ROLE = 'dispatch';
const SCENARIO = JSON.parse(readFileSync(new URL('fixtures/scenarios/prompts-versions.json', FRONTEND), 'utf8'));
const APPROVER = 'Only an approver can approve a profile.';
const RELOAD = 'This profile changed since you opened it. Reload to see the new version.';
const RELOAD_TO_DECIDE = 'Reload to decide.';
const APPROVER_ONLY = 'Only an approver can change a profile.';
const UNCONFIGURED = 'Set [approval].approvers in kew.toml before deciding prompt profiles.';
const UNKNOWN = 'The result of this request is unknown. Reload to see the current profile.';
/** What a committed write answers when the history after it never settled (`api_prompts.py`). */
const SAVED_BUT_UNREAD = 'The change was saved, but the new state could not be read. Reload to see it.';

const fixture = (name) => JSON.parse(readFileSync(new URL(`fixtures/factory/${name}`, FRONTEND), 'utf8'));

/** Serve `role`'s preview read as `json`, every time. */
const servePreview = (page, role, json) =>
  page.route(isPath(`/api/prompts/${role}`), (route) => route.fulfill({ json }));

/**
 * Watch the page for a mixed pair: the editor showing an active version while
 * the preview beside it names another. Runs before any script, on every
 * mutation, so a pair drawn for one frame is caught.
 */
function watchForMixedPairs() {
  window.__mixedPairs = [];
  new MutationObserver(() => {
    const editor = document.querySelector('[data-active-version]');
    const preview = document.querySelector('[data-profile-state]');
    if (!editor || !preview) return;
    const shown = editor.getAttribute('data-active-version');
    const named = /\bv(\d+)\b/.exec(preview.textContent ?? '')?.[1] ?? 'none';
    if (shown !== named) window.__mixedPairs.push(`editor ${shown} beside preview ${named}`);
  }).observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
}
const HEADING = `## Operator instructions — ${ROLE} profile v1`;

const versionsPath = `/api/prompts/${ROLE}/versions`;
const isPath = (path) => (url) => url.pathname === path;

/**
 * Every control in the page whose text, fill or border is the magenta accent,
 * by its data attribute (or tag); and every control under 32px tall.
 */
function controls() {
  const probe = document.createElement('span');
  probe.style.color = 'var(--accent)';
  document.body.append(probe);
  const magenta = getComputedStyle(probe).color;
  probe.remove();
  const magentaOnes = [];
  const small = [];
  for (const el of document.querySelectorAll('main button, main a, main textarea, main input, main select')) {
    const box = el.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) continue;
    const name = Object.keys(el.dataset)[0] ?? el.tagName.toLowerCase();
    const style = getComputedStyle(el);
    const colours = [style.color, style.backgroundColor, style.borderTopColor];
    if (colours.includes(magenta)) magentaOnes.push(name);
    if (el.closest('[data-profile-editor]') && box.height < 31.5) small.push(`${name} ${box.height}px`);
  }
  return { magentaOnes: magentaOnes.sort(), small };
}

/** Every box on the page that scrolls sideways. */
function sideways() {
  const wide = [];
  const root = document.scrollingElement ?? document.documentElement;
  if (root.scrollWidth > root.clientWidth + 1) wide.push(`the page (${root.scrollWidth} in ${root.clientWidth})`);
  for (const el of document.querySelectorAll('main *')) {
    const { overflowX } = getComputedStyle(el);
    if ((overflowX === 'auto' || overflowX === 'scroll') && el.scrollWidth > el.clientWidth + 1) {
      wide.push(`${el.tagName.toLowerCase()} (${el.scrollWidth} in ${el.clientWidth})`);
    }
  }
  return wide;
}

/** Every profile write control in the editor. */
const WRITES =
  '[data-profile-editor] :is([data-propose], [data-approve], [data-approve-yes], [data-decline], [data-retire], [data-retire-yes])';

/** The write controls on screen that a click would fire. */
const liveWrites = (page) =>
  page.locator(WRITES).evaluateAll((els) => els.filter((el) => !el.disabled).map((el) => Object.keys(el.dataset)[0]));

/** One history row of `version` in `state`, its own body so two rows never read alike. */
const row = (version, state) => ({
  ...(state === 'proposed' ? SCENARIO.proposed.versions[0] : SCENARIO.approved.versions[0]),
  version,
  state,
  body: `Version ${version}: keep every change minimal.`
});

/** A dispatch history at `generation` over `rows`, newest first. */
const historyOf = (generation, rows, extra = {}) => ({
  ...SCENARIO.approved,
  generation,
  active_version: rows.find((r) => r.state === 'active')?.version ?? null,
  versions: rows,
  ...extra
});

/** The dispatch preview with `version` active at `generation`. */
const previewAt = (version, generation) => ({
  ...SCENARIO.preview_approved,
  variant: { ...SCENARIO.preview_approved.variant, profile: `v${version}` },
  profile_generation: generation
});

const text = async (locator) => (await locator.innerText()).replace(/\s+/g, ' ').trim();

/** A promise and the function that settles it: a response held until released. */
function gate() {
  let open;
  const opened = new Promise((resolve) => (open = resolve));
  return { opened, open };
}

/** `count` audit events, ids 1 to `count`, the newest first. */
function auditLog(count) {
  return Array.from({ length: count }, (_, i) => {
    const id = count - i;
    return {
      id,
      timestamp: new Date(Date.UTC(2026, 9, 1) + id * 60_000).toISOString(),
      event: id === 9 ? 'prompt_profile_activated' : 'dispatch_started',
      issue_num: null,
      item_id: null,
      run_id: null,
      actor_type: 'human',
      actor_id: 'alice',
      summary: id === 9 ? 'dispatch profile v1 activated' : null,
      epic_ref: null
    };
  });
}

/** One `/api/audit` page over `log`, as `service/audit.py` pages it: newest first, `after` exclusive. */
function auditPage(log, url) {
  const after = url.searchParams.get('after');
  const limit = Number(url.searchParams.get('limit') ?? 100);
  const below = after === null ? log : log.filter((e) => e.id < Number(after));
  const page = below.slice(0, limit);
  return { events: page, next_after: below.length > limit ? page.at(-1).id : null };
}

/** The addendum block the page marks, and the fixed headings it leaves unmarked. */
function markedRegion() {
  const preview = document.querySelector('[data-preview]');
  const text = (el) => (el.tagName === 'BUTTON' ? el.querySelector('span:last-child').textContent : el.textContent);
  const marked = [...preview.querySelectorAll('[data-addendum]')].map(text).join('\n');
  const unmarked = [...preview.querySelectorAll('[data-section]:not([data-addendum])')].map(text);
  return { marked, unmarked };
}

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    /**
     * AC1 and AC3 — propose then approve. Before the proposal nothing on the
     * page is magenta; while it is pending only Approve and Decline are; once
     * approved, none is, v1 is active in the history, and the re-read preview
     * opens the addendum under its heading inside a cyan left rule.
     */
    await check(`/prompts proposes then approves a ${ROLE} profile at ${size.width}px`, () => {
      const state = { history: SCENARIO.empty, approved: false, sentGeneration: null };
      const prepare = async (page) => {
        await page.addInitScript(watchForMixedPairs);
        await page.route(isPath(versionsPath), (route) => {
          if (route.request().method() === 'GET') return route.fulfill({ json: state.history });
          state.history = SCENARIO.proposed;
          return route.fulfill({ json: state.history });
        });
        await page.route(isPath(`${versionsPath}/1/approve`), (route) => {
          state.sentGeneration = route.request().postDataJSON().generation;
          state.history = SCENARIO.approved;
          state.approved = true;
          return route.fulfill({ json: state.history });
        });
        await page.route(isPath(`/api/prompts/${ROLE}`), (route) =>
          state.approved ? route.fulfill({ json: SCENARIO.preview_approved }) : route.fallback()
        );
      };
      return visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          const editor = page.locator('[data-profile-editor]');
          await page.locator('[data-history-empty]').waitFor({ state: 'visible', timeout: 15_000 });
          const before = await page.evaluate(controls);
          expect(before.magentaOnes.length === 0, `magenta before a proposal: ${before.magentaOnes.join(', ')}`);
          const active0 = await editor.locator('[data-active-version]').getAttribute('data-active-version');
          expect(active0 === 'none', `the role shows active ${active0} before any approval`);

          const body = SCENARIO.proposed.versions[0].body;
          await page.locator('[data-profile-draft]').fill(body);
          const count = await text(page.locator('[data-profile-count]'));
          expect(count === `${body.length} / 4000`, `the count reads "${count}", not ${body.length} / 4000`);
          await page.locator('[data-propose]').click();
          await page.locator('[data-pending="1"]').waitFor({ state: 'visible', timeout: 5_000 });

          const pending = await page.evaluate(controls);
          expect(
            JSON.stringify(pending.magentaOnes) === JSON.stringify(['approve', 'decline']),
            `while pending the magenta controls are [${pending.magentaOnes.join(', ')}], not Approve and Decline`
          );
          expect(pending.small.length === 0, `controls under 32px: ${pending.small.join(', ')}`);
          const wide = await page.evaluate(sideways);
          expect(wide.length === 0, `scrolls sideways: ${wide.join('; ')}`);

          await page.locator('[data-approve]').click();
          await page.locator('[data-active-version="1"]').waitFor({ state: 'visible', timeout: 5_000 });
          expect(
            state.sentGeneration === SCENARIO.proposed.generation,
            `the approval sent generation ${state.sentGeneration}, not the ${SCENARIO.proposed.generation} it was shown`
          );
          const row = page.locator('[data-history-version="1"]');
          const rowState = await row.getAttribute('data-state');
          expect(rowState === 'active', `the history shows v1 ${rowState}, not active`);
          expect((await page.locator('[data-pending]').count()) === 0, 'the proposal still shows as pending');

          const heading = page.locator('[data-addendum="heading"]');
          const addendum = page.locator('[data-addendum="body"]');
          await addendum.waitFor({ state: 'visible', timeout: 15_000 });
          const head = await heading.innerText();
          expect(head.includes(HEADING), `the addendum heading reads "${head}", not "${HEADING}"`);
          const shown = await addendum.innerText();
          expect(
            body.split('\n').every((line) => shown.includes(line)),
            'the addendum section does not show the approved body under its heading'
          );
          const below = await page.evaluate(() => {
            const [h, b] = ['heading', 'body'].map((k) => document.querySelector(`[data-addendum="${k}"]`));
            return h.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING;
          });
          expect(below !== 0, 'the addendum body is not drawn under its heading');
          const rule = await addendum.evaluate((el) => {
            const probe = document.createElement('span');
            probe.style.color = 'var(--accent-2)';
            document.body.append(probe);
            const cyan = getComputedStyle(probe).color;
            probe.remove();
            const style = getComputedStyle(el);
            return { cyan, colour: style.borderLeftColor, width: style.borderLeftWidth };
          });
          expect(
            rule.colour === rule.cyan && rule.width !== '0px',
            `the addendum's left rule is ${rule.width} ${rule.colour}, not cyan ${rule.cyan}`
          );

          const after = await page.evaluate(controls);
          expect(after.magentaOnes.length === 0, `magenta after the approval: ${after.magentaOnes.join(', ')}`);
          const mixed = await page.evaluate(() => window.__mixedPairs);
          expect(mixed.length === 0, `a mixed pair was drawn across the approval: ${mixed.slice(0, 3).join('; ')}`);
          const wideAfter = await page.evaluate(sideways);
          expect(wideAfter.length === 0, `scrolls sideways: ${wideAfter.join('; ')}`);
        },
        size,
        prepare
      );
    });

    /**
     * AC2 — v1 is active and v2 pending. Approve answers 403, then 409: each
     * prints its sentence, and the shown active version stays v1.
     */
    await check(`a 403 and a 409 on approve read as sentences and keep v1 active at ${size.width}px`, () => {
      const v1 = SCENARIO.approved.versions[0];
      const v2 = { ...SCENARIO.proposed.versions[0], version: 2, audit_event_id: 3 };
      const start = { ...SCENARIO.approved, versions: [v2, v1] };
      const answers = [SCENARIO.approve_refused, SCENARIO.approve_stale];
      const prepare = async (page) => {
        await page.route(isPath(versionsPath), (route) => route.fulfill({ json: start }));
        await servePreview(page, ROLE, SCENARIO.preview_approved);
        await page.route(isPath(`${versionsPath}/2/approve`), (route) => {
          const answer = answers.shift();
          return route.fulfill({ status: answer.status, json: answer.body });
        });
      };
      return visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          await page.locator('[data-pending="2"]').waitFor({ state: 'visible', timeout: 15_000 });
          const active = page.locator('[data-active-version]');
          for (const [sentence, what] of [
            [APPROVER, '403'],
            [RELOAD, '409']
          ]) {
            await page.locator('[data-approve]').click();
            const refusal = page.locator('[data-profile-refusal]');
            await page.waitForFunction(
              (s) => document.querySelector('[data-profile-refusal]')?.textContent?.includes(s),
              sentence,
              { timeout: 5_000 }
            );
            const said = await text(refusal);
            expect(said.startsWith(sentence), `the ${what} reads "${said}", not "${sentence}"`);
            const shown = await active.getAttribute('data-active-version');
            expect(shown === '1', `after the ${what} the page shows active ${shown}, not v1`);
            const activeLine = await text(active);
            expect(activeLine.includes('v1'), `after the ${what} the active line reads "${activeLine}"`);
            expect((await page.locator('[data-pending="2"]').count()) === 1, `the ${what} dropped the pending v2`);
          }
          const { small } = await page.evaluate(controls);
          expect(small.length === 0, `controls under 32px: ${small.join(', ')}`);
          const wide = await page.evaluate(sideways);
          expect(wide.length === 0, `scrolls sideways: ${wide.join('; ')}`);
        },
        size,
        prepare
      );
    });

    /**
     * A propose that reaches the server and commits, but whose answer is lost
     * on the way back, or is the 503 a committed write answers when the
     * history after it never settled: the pane says the result is unknown,
     * never that nothing changed, every write is off and the draft is kept; a
     * Reload shows the one proposal the server holds, and no second one was
     * sent.
     */
    for (const { how, answer } of [
      { how: 'is lost', answer: (route) => route.abort('connectionreset') },
      {
        how: 'is a post-write 503',
        answer: (route) => route.fulfill({ status: 503, json: { detail: SAVED_BUT_UNREAD } })
      }
    ])
    await check(`a propose whose answer ${how} reads as unknown and locks the writes at ${size.width}px`, () => {
      const body = SCENARIO.proposed.versions[0].body;
      const state = { history: SCENARIO.empty, posts: 0 };
      const prepare = async (page) => {
        await page.route(isPath(versionsPath), async (route) => {
          if (route.request().method() === 'GET') return route.fulfill({ json: state.history });
          state.posts += 1;
          // The request goes on to the server, which commits it; what comes
          // back says nothing readable about the new state.
          await route.fetch().catch(() => null);
          state.history = SCENARIO.proposed;
          return answer(route);
        });
      };
      return visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          await page.locator('[data-history-empty]').waitFor({ state: 'visible', timeout: 15_000 });
          await page.locator('[data-profile-draft]').fill(body);
          await page.locator('[data-propose]').click();
          await page.waitForFunction(
            (s) => document.querySelector('[data-profile-refusal]')?.textContent?.includes(s),
            UNKNOWN,
            { timeout: 5_000 }
          );
          const said = await text(page.locator('[data-profile-refusal]'));
          expect(said.startsWith(UNKNOWN), `the lost answer reads "${said}", not "${UNKNOWN}"`);
          expect(!/nothing changed/i.test(said), `the lost answer says nothing changed: "${said}"`);
          const writes = await page
            .locator('[data-profile-editor] :is([data-propose], [data-approve], [data-decline], [data-retire], [data-retire-yes])')
            .evaluateAll((els) => els.map((el) => ({ name: Object.keys(el.dataset)[0], disabled: el.disabled })));
          expect(writes.length > 0, 'no write control is drawn to check');
          const live = writes.filter((w) => !w.disabled).map((w) => w.name);
          expect(live.length === 0, `write controls still enabled after the lost answer: ${live.join(', ')}`);
          const kept = await page.locator('[data-profile-draft]').inputValue();
          expect(kept === body, `the draft reads "${kept}", not the text that was sent`);

          await page.locator('[data-profile-reload]').click();
          await page.locator('[data-pending="1"]').waitFor({ state: 'visible', timeout: 5_000 });
          const rows = await page.locator('[data-history-version]').count();
          expect(rows === 1, `the history shows ${rows} versions, not the one proposal`);
          expect(state.posts === 1, `the page sent ${state.posts} proposals, not 1`);
          expect((await page.locator('[data-profile-refusal]').count()) === 0, 'the unknown sentence outlived the reload');
          expect(!(await page.locator('[data-approve]').isDisabled()), 'Approve stays off after a successful reload');
          const wide = await page.evaluate(sideways);
          expect(wide.length === 0, `scrolls sideways: ${wide.join('; ')}`);
        },
        size,
        prepare
      );
    });

    /**
     * An approver-gate 403 for an actor whose name spells a session refusal's
     * word still reads as the approver sentence, and v1 stays active.
     */
    await check(`a 403 for an actor named origin-operator reads as the approver sentence at ${size.width}px`, () => {
      const v1 = SCENARIO.approved.versions[0];
      const v2 = { ...SCENARIO.proposed.versions[0], version: 2, audit_event_id: 3 };
      const prepare = async (page) => {
        await page.route(isPath(versionsPath), (route) =>
          route.fulfill({ json: { ...SCENARIO.approved, versions: [v2, v1] } })
        );
        await servePreview(page, ROLE, SCENARIO.preview_approved);
        await page.route(isPath(`${versionsPath}/2/approve`), (route) =>
          route.fulfill({ status: 403, json: { detail: "actor 'origin-operator' is not in [approval].approvers" } })
        );
      };
      return visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          await page.locator('[data-pending="2"]').waitFor({ state: 'visible', timeout: 15_000 });
          await page.locator('[data-approve]').click();
          const refusal = page.locator('[data-profile-refusal]');
          await refusal.waitFor({ state: 'visible', timeout: 5_000 });
          const said = await text(refusal);
          expect(said === APPROVER, `the 403 reads "${said}", not "${APPROVER}"`);
          const shown = await page.locator('[data-active-version]').getAttribute('data-active-version');
          expect(shown === '1', `after the 403 the page shows active ${shown}, not v1`);
        },
        size,
        prepare
      );
    });

    /**
     * A slow reload of dispatch's history lands after the pane moved to
     * ci_fix: the ci_fix editor keeps ci_fix's history, and Approve posts
     * ci_fix with ci_fix's version and generation, never dispatch's.
     */
    await check(`a slow dispatch reload never replaces the ci_fix editor at ${size.width}px`, () => {
      const v1 = SCENARIO.approved.versions[0];
      const v2 = { ...SCENARIO.proposed.versions[0], version: 2, audit_event_id: 3 };
      const dispatch = { ...SCENARIO.approved, versions: [v2, v1] };
      const ciBody = 'Fix only the failing check; touch no other file.';
      const ciFix = {
        ...SCENARIO.proposed,
        role: 'ci_fix',
        generation: 7,
        versions: [{ ...SCENARIO.proposed.versions[0], version: 3, body: ciBody }]
      };
      const held = gate();
      const state = { dispatchReads: 0, approvals: [] };
      const prepare = async (page) => {
        await page.route(isPath(versionsPath), async (route) => {
          state.dispatchReads += 1;
          // The first read answers at once; the reload is held until released.
          if (state.dispatchReads > 1) await held.opened;
          return route.fulfill({ json: dispatch });
        });
        await page.route(isPath('/api/prompts/ci_fix/versions'), (route) => route.fulfill({ json: ciFix }));
        await servePreview(page, ROLE, SCENARIO.preview_approved);
        await servePreview(page, 'ci_fix', { ...fixture('prompts-ci_fix.json'), profile_generation: ciFix.generation });
        await page.route(
          (url) => /^\/api\/prompts\/[^/]+\/versions\/\d+\/approve$/.test(url.pathname),
          (route) => {
            const { pathname } = new URL(route.request().url());
            state.approvals.push({ path: pathname, generation: route.request().postDataJSON().generation });
            if (pathname.startsWith('/api/prompts/dispatch/')) {
              const answer = SCENARIO.approve_stale;
              return route.fulfill({ status: answer.status, json: answer.body });
            }
            return route.fulfill({ status: 409, json: { detail: 'not this check' } });
          }
        );
      };
      return visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          try {
            await page.locator('[data-pending="2"]').waitFor({ state: 'visible', timeout: 15_000 });
            await page.locator('[data-approve]').click();
            const reload = page.locator('[data-profile-reload]');
            await reload.waitFor({ state: 'visible', timeout: 5_000 });
            const slowRead = page.waitForRequest((r) => new URL(r.url()).pathname === versionsPath);
            await reload.click();
            await slowRead;

            await page.locator('[data-role="ci_fix"]').click();
            await page.locator('[data-pending="3"]').waitFor({ state: 'visible', timeout: 15_000 });
            const late = page.waitForResponse((r) => new URL(r.url()).pathname === versionsPath);
            held.open();
            await late;
            await sleep(500);

            expect((await page.locator('#profile-draft-ci_fix').count()) === 1, 'the editor is no longer ci_fix');
            expect((await page.locator('[data-pending="2"]').count()) === 0, "dispatch's pending v2 replaced ci_fix's");
            const body = await text(page.locator('[data-pending-body]'));
            expect(body === ciBody, `the pending body reads "${body}", not ci_fix's`);

            const before = state.approvals.length;
            const answered = page.waitForResponse((r) => /\/approve$/.test(new URL(r.url()).pathname), {
              timeout: 5_000
            });
            await page.locator('[data-approve]').click();
            await answered;
            const sent = state.approvals.slice(before);
            expect(sent.length === 1, `Approve sent ${sent.length} requests`);
            expect(
              sent[0].path === '/api/prompts/ci_fix/versions/3/approve' && sent[0].generation === 7,
              `Approve posted ${sent[0].path} with generation ${sent[0].generation}, not ci_fix v3 at 7`
            );
          } finally {
            held.open();
          }
        },
        size,
        prepare
      );
    });

    /**
     * An addendum with a `## ` heading of its own: the cyan rule covers the
     * whole block the server says the builder placed — heading, both lines
     * and the inner heading — and no fixed section.
     */
    await check(`the addendum rule covers a body with its own heading at ${size.width}px`, () => {
      const preview = SCENARIO.preview_inner_heading;
      const { start, end } = preview.addendum;
      const block = Array.from(preview.preview).slice(start, end).join('');
      const v1 = { ...SCENARIO.approved.versions[0], body: block.slice(block.indexOf('\n\n') + 2) };
      const prepare = async (page) => {
        await page.route(isPath(versionsPath), (route) => route.fulfill({ json: { ...SCENARIO.approved, versions: [v1] } }));
        await page.route(isPath(`/api/prompts/${ROLE}`), (route) => route.fulfill({ json: preview }));
      };
      return visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          const heading = page.locator('[data-addendum="heading"]');
          await heading.waitFor({ state: 'visible', timeout: 15_000 });
          // Unfold the addendum if this read left it folded.
          if ((await heading.getAttribute('aria-expanded')) !== 'true') await heading.click();
          await page.locator('[data-addendum="body"]').waitFor({ state: 'visible', timeout: 5_000 });
          const { marked, unmarked } = await page.evaluate(markedRegion);
          expect(
            marked === block,
            `the marked region reads ${JSON.stringify(marked)}, not the addendum block ${JSON.stringify(block)}`
          );
          expect(
            !unmarked.includes('## Deployment'),
            "the addendum's own heading is drawn as a fixed section, outside the rule"
          );
          expect(
            unmarked.some((h) => h.startsWith('## Safety Rules')),
            `the fixed safety section is not drawn unmarked: ${unmarked.join(' | ')}`
          );
          const rules = await page.locator('[data-addendum]').evaluateAll((els) => {
            const probe = document.createElement('span');
            probe.style.color = 'var(--accent-2)';
            document.body.append(probe);
            const cyan = getComputedStyle(probe).color;
            probe.remove();
            return els.map((el) => getComputedStyle(el).borderLeftColor === cyan);
          });
          expect(rules.length === 2 && rules.every(Boolean), 'the addendum heading and body do not both carry the cyan rule');
          const wide = await page.evaluate(sideways);
          expect(wide.length === 0, `scrolls sideways: ${wide.join('; ')}`);
        },
        size,
        prepare
      );
    });

    /**
     * The active version's audit link opens that event on the audit page even
     * when it is older than the newest page the table loads.
     */
    await check(`the audit link opens an event older than the first page at ${size.width}px`, () => {
      const log = auditLog(120);
      const v1 = { ...SCENARIO.approved.versions[0], audit_event_id: 9 };
      const prepare = async (page) => {
        await page.route(isPath(versionsPath), (route) => route.fulfill({ json: { ...SCENARIO.approved, versions: [v1] } }));
        await servePreview(page, ROLE, SCENARIO.preview_approved);
        await page.route(isPath('/api/audit'), (route) => route.fulfill({ json: auditPage(log, new URL(route.request().url())) }));
      };
      return visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          const link = page.locator('[data-audit-link]');
          await link.waitFor({ state: 'visible', timeout: 15_000 });
          const href = await link.getAttribute('href');
          expect(href === '/audit?event=9', `the audit link is ${href}, not /audit?event=9`);
          await link.click();
          const focus = page.locator('[data-audit-focus="9"]');
          await focus.waitFor({ state: 'visible', timeout: 15_000 });
          const said = await text(focus);
          expect(said.includes('#9') && said.includes('prompt_profile_activated'), `the linked event reads "${said}"`);
          expect((await page.locator('[data-audit-row="9"]').count()) === 0, 'event 9 was on the first page after all');
          expect((await page.locator('[data-audit-row]').count()) === 50, 'the table did not load its first page');
          const focused = await page.evaluate(() => document.activeElement?.getAttribute('data-audit-focus'));
          expect(focused === '9', `focus is on ${focused}, not the linked event`);
        },
        size,
        prepare
      );
    });

    /**
     * An approval straddles the two reads: the history answers generation 2
     * (v2 active) while the preview keeps answering generation 1 (v1). The
     * pane re-reads both a bounded number of times, never draws v2's history
     * beside v1's prompt, then says the profile changed with every write off;
     * once the preview catches up, Reload shows the pair.
     */
    await check(`a history and a preview from two decisions are never shown as a pair at ${size.width}px`, () => {
      const v1 = { ...SCENARIO.approved.versions[0], state: 'retired' };
      const v2 = { ...SCENARIO.approved.versions[0], version: 2, audit_event_id: 5 };
      const newer = { ...SCENARIO.approved, generation: 2, active_version: 2, versions: [v2, v1] };
      const caughtUp = {
        ...SCENARIO.preview_approved,
        variant: { ...SCENARIO.preview_approved.variant, profile: 'v2' },
        profile_generation: 2
      };
      const state = { preview: SCENARIO.preview_approved, historyReads: 0 };
      const prepare = async (page) => {
        await page.addInitScript(watchForMixedPairs);
        await page.route(isPath(versionsPath), (route) => {
          state.historyReads += 1;
          return route.fulfill({ json: newer });
        });
        await page.route(isPath(`/api/prompts/${ROLE}`), (route) => route.fulfill({ json: state.preview }));
      };
      return visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          const refusal = page.locator('[data-profile-refusal]');
          await refusal.waitFor({ state: 'visible', timeout: 15_000 });
          const said = await text(refusal);
          expect(said.startsWith(RELOAD), `the straddle reads "${said}", not "${RELOAD}"`);
          expect(state.historyReads <= 4, `the pane read the history ${state.historyReads} times, past its bound`);
          expect((await page.locator('[data-active-version]').count()) === 0, "v2's history is drawn beside v1's prompt");
          const writes = await page
            .locator('[data-profile-editor] :is([data-propose], [data-approve], [data-decline], [data-retire], [data-retire-yes])')
            .evaluateAll((els) => els.filter((el) => !el.disabled).map((el) => Object.keys(el.dataset)[0]));
          expect(writes.length === 0, `write controls usable over a straddle: ${writes.join(', ')}`);

          state.preview = caughtUp;
          await page.locator('[data-profile-reload]').click();
          await page.locator('[data-active-version="2"]').waitFor({ state: 'visible', timeout: 5_000 });
          expect(!(await page.locator('[data-retire]').isDisabled()), 'Retire stays off once the two agree');
          const mixed = await page.evaluate(() => window.__mixedPairs);
          expect(mixed.length === 0, `a mixed pair was drawn: ${mixed.slice(0, 3).join('; ')}`);
        },
        size,
        prepare
      );
    });

    /**
     * #3092 (1) — v2 is active. Retire is armed and "Retire it" answers 409;
     * by the Reload another approver has made v4 active. The pane shows v4
     * with no armed confirmation: nothing on screen would retire a version
     * the person never chose.
     */
    await check(`a 409 then Reload leaves no armed confirmation at ${size.width}px`, () => {
      const state = { history: historyOf(2, [row(2, 'active'), row(1, 'retired')]), preview: previewAt(2, 2), retires: 0 };
      const prepare = async (page) => {
        await page.route(isPath(versionsPath), (route) => route.fulfill({ json: state.history }));
        await page.route(isPath(`/api/prompts/${ROLE}`), (route) => route.fulfill({ json: state.preview }));
        await page.route(isPath(`/api/prompts/${ROLE}/retire`), (route) => {
          state.retires += 1;
          state.history = historyOf(4, [row(4, 'active'), row(3, 'retired'), row(2, 'retired'), row(1, 'retired')]);
          state.preview = previewAt(4, 4);
          return route.fulfill({ status: 409, json: { detail: 'dispatch profile generation 2 is stale; 4 is in force' } });
        });
      };
      return visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          await page.locator('[data-active-version="2"]').waitFor({ state: 'visible', timeout: 15_000 });
          await page.locator('[data-retire]').click();
          await page.locator('[data-retire-yes]').click();
          await page.waitForFunction(
            (s) => document.querySelector('[data-profile-refusal]')?.textContent?.includes(s),
            RELOAD,
            { timeout: 5_000 }
          );
          await page.locator('[data-profile-reload]').click();
          await page.locator('[data-active-version="4"]').waitFor({ state: 'visible', timeout: 5_000 });
          const armed = await page.locator('[data-retire-confirm], [data-retire-yes], [data-approve-confirm]').count();
          expect(armed === 0, `after the Reload ${armed} armed confirmation(s) still show beside v4`);
          const retire = await text(page.locator('[data-retire]'));
          expect(retire === 'Retire v4', `the Retire control reads "${retire}", not "Retire v4"`);
          expect(state.retires === 1, `the page sent ${state.retires} retirements, not 1`);
          const { small } = await page.evaluate(controls);
          expect(small.length === 0, `controls under 32px: ${small.join(', ')}`);
        },
        size,
        prepare
      );
    });

    /**
     * #3092 (2) — v3 is active and both v2 and v4 are pending. Each offers
     * Approve and Decline; approving v2 asks first, naming both versions, and
     * only the confirm sends the approval, with the generation shown.
     */
    await check(`two pending versions are both decidable, the older one after a confirm at ${size.width}px`, () => {
      const start = historyOf(3, [row(4, 'proposed'), row(3, 'active'), row(2, 'proposed'), row(1, 'retired')]);
      const approvals = [];
      const prepare = async (page) => {
        await page.route(isPath(versionsPath), (route) => route.fulfill({ json: start }));
        await servePreview(page, ROLE, previewAt(3, 3));
        await page.route(
          (url) => /^\/api\/prompts\/dispatch\/versions\/\d+\/approve$/.test(url.pathname),
          (route) => {
            approvals.push({ path: new URL(route.request().url()).pathname, generation: route.request().postDataJSON().generation });
            return route.fulfill({ status: 409, json: { detail: 'not this check' } });
          }
        );
      };
      return visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          await page.locator('[data-active-version="3"]').waitFor({ state: 'visible', timeout: 15_000 });
          for (const version of ['4', '2']) {
            const box = page.locator(`[data-pending="${version}"]`);
            expect((await box.count()) === 1, `pending v${version} is not drawn`);
            for (const control of ['[data-approve]', '[data-decline]']) {
              const button = box.locator(control);
              expect(
                (await button.count()) === 1 && !(await button.isDisabled()),
                `pending v${version} has no live ${control}`
              );
            }
          }
          const { magentaOnes, small } = await page.evaluate(controls);
          expect(
            JSON.stringify(magentaOnes) === JSON.stringify(['approve', 'approve', 'decline', 'decline']),
            `the magenta controls are [${magentaOnes.join(', ')}], not Approve and Decline on each pending version`
          );
          expect(small.length === 0, `controls under 32px: ${small.join(', ')}`);

          const older = page.locator('[data-pending="2"]');
          await older.locator('[data-approve]').click();
          const ask = older.locator('[data-approve-confirm]');
          await ask.waitFor({ state: 'visible', timeout: 5_000 });
          const asked = await text(ask);
          const sentence = 'v2 is older than the active v3. Approve it anyway?';
          expect(asked === sentence, `the confirm step reads "${asked}", not "${sentence}"`);
          expect(approvals.length === 0, 'approving the older version was sent before its confirm');
          await older.locator('[data-approve-no]').click();
          expect((await older.locator('[data-approve-confirm]').count()) === 0, 'Keep v3 left the confirm armed');
          expect(approvals.length === 0, 'Keep v3 sent an approval');

          await older.locator('[data-approve]').click();
          const answered = page.waitForResponse((r) => /\/approve$/.test(new URL(r.url()).pathname), { timeout: 5_000 });
          await older.locator('[data-approve-yes]').click();
          await answered;
          expect(
            approvals.length === 1 &&
              approvals[0].path === '/api/prompts/dispatch/versions/2/approve' &&
              approvals[0].generation === 3,
            `the confirm sent ${JSON.stringify(approvals)}, not v2 at generation 3`
          );
          const wide = await page.evaluate(sideways);
          expect(wide.length === 0, `scrolls sideways: ${wide.join('; ')}`);
        },
        size,
        prepare
      );
    });

    /**
     * #3092 (3, 4), #3119 (1) — v1 is active and v2 and v3 pending; approving
     * v2 answers 409. Every write is off; the reload sentence shows once, above
     * the pending versions, and each pending block reads only "Reload to
     * decide." with no magenta. They stay off while the Reload's history has
     * landed and its preview has not; once both name one generation, the
     * writes return.
     */
    await check(`after a 409 every write is off until a coherent reload at ${size.width}px`, () => {
      const start = historyOf(1, [row(3, 'proposed'), row(2, 'proposed'), row(1, 'active')]);
      const moved = historyOf(2, [row(3, 'proposed'), row(2, 'active'), row(1, 'retired')]);
      const held = gate();
      const state = { history: start, preview: previewAt(1, 1), holdPreview: false };
      const prepare = async (page) => {
        await page.route(isPath(versionsPath), (route) => route.fulfill({ json: state.history }));
        await page.route(isPath(`/api/prompts/${ROLE}`), async (route) => {
          if (state.holdPreview) await held.opened;
          return route.fulfill({ json: state.preview });
        });
        await page.route(isPath(`${versionsPath}/2/approve`), (route) => {
          const answer = SCENARIO.approve_stale;
          return route.fulfill({ status: answer.status, json: answer.body });
        });
      };
      return visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          try {
            await page.locator('[data-pending="2"]').waitFor({ state: 'visible', timeout: 15_000 });
            await page.locator('[data-profile-draft]').fill('Name every file you touched.');
            await page.locator('[data-pending="2"] [data-approve]').click();
            const reasons = page.locator('[data-decision-reason]');
            await reasons.first().waitFor({ state: 'visible', timeout: 5_000 });
            const said = (await reasons.allInnerTexts()).map((t) => t.trim());
            expect(
              said.length === 2 && said.every((t) => t === RELOAD_TO_DECIDE),
              `after the 409 the pending versions read [${said.join(' | ')}], not "${RELOAD_TO_DECIDE}" each`
            );
            const pane = await text(page.locator('[data-profile-editor]'));
            const times = pane.split(RELOAD).length - 1;
            expect(times === 1, `the reload sentence shows ${times} times, not once`);
            expect((await page.locator('[data-profile-reload]').count()) === 1, 'not exactly one Reload button');
            const live = await liveWrites(page);
            expect(live.length === 0, `write controls still live after the 409: ${live.join(', ')}`);
            expect((await page.locator('[data-approve], [data-decline]').count()) === 0, 'Approve or Decline is still drawn');
            const { magentaOnes } = await page.evaluate(controls);
            expect(magentaOnes.length === 0, `magenta after the 409: ${magentaOnes.join(', ')}`);

            state.history = moved;
            state.preview = previewAt(2, 2);
            state.holdPreview = true;
            const historyRead = page.waitForResponse((r) => new URL(r.url()).pathname === versionsPath);
            await page.locator('[data-profile-reload]').click();
            await historyRead;
            await sleep(300);
            const between = await liveWrites(page);
            expect(between.length === 0, `writes live before the preview reloaded: ${between.join(', ')}`);

            held.open();
            await page.locator('[data-active-version="2"]').waitFor({ state: 'visible', timeout: 5_000 });
            await page.locator('[data-pending="3"] [data-approve]').waitFor({ state: 'visible', timeout: 5_000 });
            const after = await liveWrites(page);
            for (const name of ['propose', 'approve', 'decline', 'retire']) {
              expect(after.includes(name), `${name} is still off after a coherent reload: live [${after.join(', ')}]`);
            }
          } finally {
            held.open();
          }
        },
        size,
        prepare
      );
    });

    /**
     * #3092 (4), #3119 (2) — the history says this viewer is not an approver,
     * and a draft is typed. Nothing on the page is magenta, no write is live —
     * Propose included, so no POST is sent — and the write area says why once,
     * not once per pending version.
     */
    await check(`a non-approver sees no magenta and no live write at ${size.width}px`, () => {
      const start = historyOf(1, [row(3, 'proposed'), row(2, 'proposed'), row(1, 'active')], { can_decide: false });
      const sent = [];
      const prepare = async (page) => {
        await page.route(isPath(versionsPath), (route) => {
          if (route.request().method() !== 'GET') sent.push(route.request().method());
          return route.fulfill({ json: start });
        });
        await servePreview(page, ROLE, previewAt(1, 1));
      };
      return visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          await page.locator('[data-pending="2"]').waitFor({ state: 'visible', timeout: 15_000 });
          await page.locator('[data-profile-draft]').fill('Name every file you touched.');
          const reason = page.locator('[data-write-reason]');
          await reason.waitFor({ state: 'visible', timeout: 5_000 });
          const said = await text(reason);
          expect(said === APPROVER_ONLY, `the write area reads "${said}", not "${APPROVER_ONLY}"`);
          const pane = await text(page.locator('[data-profile-editor]'));
          const times = pane.split(APPROVER_ONLY).length - 1;
          expect(times === 1, `the approver sentence shows ${times} times, not once`);
          expect((await page.locator('[data-decision-reason]').count()) === 0, 'a pending version repeats a reason');
          const { magentaOnes, small } = await page.evaluate(controls);
          expect(magentaOnes.length === 0, `magenta for a non-approver: ${magentaOnes.join(', ')}`);
          expect(small.length === 0, `controls under 32px: ${small.join(', ')}`);
          const live = await liveWrites(page);
          expect(live.length === 0, `write controls live for a non-approver: ${live.join(', ')}`);
          await page.locator('[data-propose]').click({ force: true });
          await sleep(300);
          expect(sent.length === 0, `a non-approver's page sent ${sent.join(', ')}`);
          const wide = await page.evaluate(sideways);
          expect(wide.length === 0, `scrolls sideways: ${wide.join('; ')}`);
        },
        size,
        prepare
      );
    });

    /**
     * #3119 (2) — a non-approver with v1 active and nothing pending: Retire v1
     * is off, and the approver sentence sits beside it, in its row.
     */
    await check(`a non-approver reads why Retire is off with nothing pending at ${size.width}px`, () => {
      const start = historyOf(1, [row(1, 'active')], { can_decide: false });
      const prepare = async (page) => {
        await page.route(isPath(versionsPath), (route) => route.fulfill({ json: start }));
        await servePreview(page, ROLE, previewAt(1, 1));
      };
      return visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          const retire = page.locator('[data-retire]');
          await retire.waitFor({ state: 'visible', timeout: 15_000 });
          expect(await retire.isDisabled(), 'Retire is live for a non-approver');
          const reason = page.locator('[data-write-reason]');
          const said = await text(reason);
          expect(said === APPROVER_ONLY, `beside Retire reads "${said}", not "${APPROVER_ONLY}"`);
          const beside = await reason.evaluate((el) => el.parentElement.contains(document.querySelector('[data-retire]')));
          expect(beside, 'the approver sentence is not in the row beside Retire');
          const look = await reason.evaluate((el) => getComputedStyle(el).fontSize);
          expect(look === '13px', `the approver sentence is ${look}, not 13px`);
          const wide = await page.evaluate(sideways);
          expect(wide.length === 0, `scrolls sideways: ${wide.join('; ')}`);
        },
        size,
        prepare
      );
    });

    /**
     * #3119 (4) — no approver is configured, so nobody can write. The write
     * area prints the server's kew.toml sentence once, its config key and
     * file in Plex Mono; nothing is magenta and no write is live.
     */
    await check(`with no approvers the editor names the kew.toml fix at ${size.width}px`, () => {
      const start = historyOf(1, [row(2, 'proposed'), row(1, 'active')], {
        can_decide: false,
        decide_refusal: UNCONFIGURED
      });
      const prepare = async (page) => {
        await page.route(isPath(versionsPath), (route) => route.fulfill({ json: start }));
        await servePreview(page, ROLE, previewAt(1, 1));
      };
      return visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          const reason = page.locator('[data-write-reason]');
          await reason.waitFor({ state: 'visible', timeout: 15_000 });
          const said = await text(reason);
          expect(said === UNCONFIGURED, `the write area reads "${said}", not "${UNCONFIGURED}"`);
          const pane = await text(page.locator('[data-profile-editor]'));
          expect(!pane.includes('Only an approver'), 'the approver sentence shows beside the kew.toml one');
          expect(pane.split(UNCONFIGURED).length - 1 === 1, 'the kew.toml sentence shows more than once');
          const mono = await reason.evaluate((el) =>
            [...el.querySelectorAll('span')]
              .filter((s) => /Plex Mono/i.test(getComputedStyle(s).fontFamily))
              .map((s) => s.textContent)
          );
          expect(
            JSON.stringify(mono) === JSON.stringify(['[approval].approvers', 'kew.toml']),
            `mono runs are [${mono.join(', ')}]`
          );
          const { magentaOnes } = await page.evaluate(controls);
          expect(magentaOnes.length === 0, `magenta with no approvers: ${magentaOnes.join(', ')}`);
          const live = await liveWrites(page);
          expect(live.length === 0, `write controls live with no approvers: ${live.join(', ')}`);
          const wide = await page.evaluate(sideways);
          expect(wide.length === 0, `scrolls sideways: ${wide.join('; ')}`);
        },
        size,
        prepare
      );
    });

    /**
     * #3092 (5) — a body of 4,000 non-BMP characters is 8,000 UTF-16 units
     * but 4,000 code points, the count the server bounds: the counter reads
     * 4000 / 4000, Propose is live, and the whole body is sent.
     */
    await check(`a 4,000-code-point body of non-BMP characters is accepted at ${size.width}px`, () => {
      const body = '𝒳'.repeat(4000);
      const state = { history: SCENARIO.empty, sent: null };
      const prepare = async (page) => {
        await page.route(isPath(versionsPath), (route) => {
          if (route.request().method() === 'GET') return route.fulfill({ json: state.history });
          state.sent = route.request().postDataJSON().body;
          state.history = { ...SCENARIO.proposed, versions: [{ ...SCENARIO.proposed.versions[0], body }] };
          return route.fulfill({ json: state.history });
        });
      };
      return visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          await page.locator('[data-history-empty]').waitFor({ state: 'visible', timeout: 15_000 });
          await page.locator('[data-profile-draft]').fill(body);
          const count = await text(page.locator('[data-profile-count]'));
          expect(count === '4000 / 4000', `the count reads "${count}", not 4000 / 4000`);
          const propose = page.locator('[data-propose]');
          expect(!(await propose.isDisabled()), 'Propose is off for a body the server accepts');
          await propose.click();
          await page.locator('[data-pending="1"]').waitFor({ state: 'visible', timeout: 5_000 });
          expect(state.sent === body, `the page sent ${Array.from(state.sent ?? '').length} code points, not the 4,000 typed`);
          const wide = await page.evaluate(sideways);
          expect(wide.length === 0, `scrolls sideways: ${wide.join('; ')}`);
        },
        size,
        prepare
      );
    });

    /**
     * The linked event's own read fails while the list and the chain status
     * answer: the page keeps both and says only that the one event could not
     * be read.
     */
    await check(`a failed read of the linked audit event keeps the list and status at ${size.width}px`, () => {
      const log = auditLog(120);
      const prepare = async (page) => {
        await page.route(isPath('/api/audit'), (route) => {
          const url = new URL(route.request().url());
          if (url.searchParams.get('limit') === '1') return route.fulfill({ status: 500, json: { detail: 'boom' } });
          return route.fulfill({ json: auditPage(log, url) });
        });
      };
      return visit(
        '/audit?event=9',
        async (page) => {
          const focus = page.locator('[data-audit-focus="9"]');
          await focus.waitFor({ state: 'visible', timeout: 15_000 });
          const said = await text(focus);
          expect(said === 'Audit event #9 could not be read.', `the failed focus reads "${said}"`);
          const rows = await page.locator('[data-audit-row]').count();
          expect(rows === 50, `the table shows ${rows} rows, not its first page of 50`);
          const header = await text(page.locator('main header').filter({ hasText: 'Audit' }).first());
          expect(!header.includes('offline'), `the header says the server is offline: "${header}"`);
          expect(/events ·/.test(header), `the chain status is gone from the header: "${header}"`);
        },
        size,
        prepare
      );
    });
  }
}

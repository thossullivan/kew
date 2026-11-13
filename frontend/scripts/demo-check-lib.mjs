/**
 * What `npm run demo-check` (#3207) decides without a browser, so
 * `scripts/demo-check.test.ts` can read it: the base a run checks, the checks
 * a remote base cannot make, the "See it in the demo" links the docs sources
 * declare and the ones a built docs page carries, whether a URL is on the
 * base, where such a link is pointed, the sentence a docs-to-demo failure
 * prints, and the walkthrough gallery's shots.
 */

/** Where the docs' demo links point, and where the demo is deployed. */
export const DEMO_ORIGIN = 'https://demo.kew.dev';

/** Where the docs are published: a built page is named as a reader finds it. */
export const DOCS_ROOT = 'kew.dev/docs/';

/** The words of every docs link into a tour step (`kew/tests/test_docs_demo_links.py`). */
export const LINK_TEXT = 'See it in the demo';

/** The checks that read the local build from disk, each with why a remote base skips it. */
export const LOCAL_BUILD_CHECKS = [
  {
    name: 'demo world: the bundle passes every leak-scan bundle rule',
    why: 'it scans build-demo/ on disk, which is not what a remote base serves'
  }
];

/** The local-build check named `name`; a module skips it by this entry. */
export const localBuildCheck = (name) => {
  const found = LOCAL_BUILD_CHECKS.find((c) => c.name === name);
  if (!found) throw new Error(`no local-build check is named "${name}"`);
  return found;
};

/**
 * The run's arguments: none checks the local static server over `build-demo/`
 * (`base: null`); one http(s) site root checks that base, as a remote one.
 */
export function parseDemoArgs(argv) {
  if (argv.length > 1) throw new Error(`one base URL at most, not ${argv.length} arguments`);
  if (argv.length === 0) return { base: null, remote: false };
  const [given] = argv;
  let url;
  try {
    url = new URL(given);
  } catch {
    url = null;
  }
  if (!url || !/^https?:$/.test(url.protocol)) throw new Error(`not an http(s) URL: ${given}`);
  if (url.pathname !== '/' || url.search || url.hash) throw new Error(`the base must be a site root, not a page: ${given}`);
  return { base: url.origin, remote: true };
}

/**
 * How `demo-check.mjs` hands its run to `ui-check.mjs`, which owns the browser,
 * `check()`, `visit()` and the summary: the base, whether it is remote, the
 * built docs site and the gallery's folder, in the child's environment.
 */
export function demoCheckEnv({ base, remote, docs, shots }) {
  return {
    KEW_DEMO_CHECK_BASE: base,
    KEW_DEMO_CHECK_REMOTE: remote ? '1' : '0',
    KEW_DEMO_CHECK_DOCS: docs,
    KEW_DEMO_CHECK_SHOTS: shots,
    // A demo-check with no browser checked nothing: never a skip.
    KEW_UI_CHECK_REQUIRE_CHROMIUM: '1'
  };
}

/** The run `demoCheckEnv` handed over, or null for an ordinary ui-check. */
export function demoCheckTarget(env = process.env) {
  const base = env.KEW_DEMO_CHECK_BASE;
  if (!base) return null;
  const { KEW_DEMO_CHECK_DOCS: docs, KEW_DEMO_CHECK_SHOTS: shots } = env;
  if (!docs || !shots) throw new Error('KEW_DEMO_CHECK_BASE is set without KEW_DEMO_CHECK_DOCS and KEW_DEMO_CHECK_SHOTS');
  return { base, remote: env.KEW_DEMO_CHECK_REMOTE === '1', docs, shots };
}

/** The checks a run skips: every local-build check against a remote base, none locally. */
export const skippedChecks = ({ remote }) => (remote ? LOCAL_BUILD_CHECKS : []);

/** A built page's path under the site (`web/epics/index.html`) as a reader finds it. */
export const docsPageName = (rel) => `${DOCS_ROOT}${rel.replace(/(^|\/)index\.html$/, '$1')}`;

const decode = (html) =>
  html
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&');

/**
 * Every "See it in the demo" link in a built page, in page order, as
 * `{ page, href, tour, step }`. A link with no `step` reads as step 0, which
 * no tour has, so the browser check fails it by name.
 */
export function demoLinks(html, rel) {
  const page = docsPageName(rel);
  const links = [];
  for (const match of html.matchAll(/<a\s([^>]*)>([\s\S]*?)<\/a>/g)) {
    if (match[2].replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim() !== LINK_TEXT) continue;
    const href = /\bhref="([^"]*)"/.exec(match[1]);
    if (!href) continue;
    const url = new URL(decode(href[1]), DEMO_ORIGIN);
    links.push({
      page,
      href: url.href,
      tour: url.searchParams.get('tour') ?? '',
      step: Number(url.searchParams.get('step') ?? 0) || 0
    });
  }
  return links;
}

/**
 * A docs source's path (`web/epics.md`) as a reader finds its built page:
 * mkdocs writes `web/epics/index.html` (directory URLs), and an `index.md` is
 * its folder's page.
 */
export const sourcePageName = (rel) =>
  docsPageName(rel.replace(/\\/g, '/').replace(/(^|\/)index\.md$/, '$1index.html').replace(/\.md$/, '/index.html'));

/**
 * The `exclude_docs` patterns of an `mkdocs.yml`'s text: the sources mkdocs
 * never builds, so the links on them are declared to no reader.
 */
export function excludedDocs(mkdocsYml) {
  const block = /^exclude_docs:\s*\|\s*\n((?:[ \t]+\S.*\n?)*)/m.exec(mkdocsYml);
  return block ? block[1].split('\n').map((line) => line.trim()).filter((line) => line && !line.startsWith('#')) : [];
}

/** Whether `rel` matches one `exclude_docs` pattern of the forms the file uses: `/dir/**` and `/file.md`. */
function excluded(rel, patterns) {
  const path = `/${rel.replace(/\\/g, '/')}`;
  return patterns.some((pattern) => (pattern.endsWith('/**') ? path.startsWith(pattern.slice(0, -2)) : path === pattern));
}

/** `[See it in the demo](https://demo.kew.dev...)` in a Markdown source, the words spaced any way. */
const SOURCE_LINK = /\[\s*See\s+it\s+in\s+the\s+demo\s*\]\(\s*<?(https:\/\/demo\.kew\.dev[^\s)>]*)>?[^)]*\)/g;

/**
 * Every "See it in the demo" link the docs sources declare, as `demoLinks`
 * reads a built page: `{ page, href, tour, step }`, the page named as its
 * built page is. `sources` is `{ rel, text }` per Markdown file under `docs/`;
 * a source `exclude` (`excludedDocs`) names is skipped, as mkdocs skips it. A
 * prose mention of the words is not a link and is not read.
 */
export function declaredDemoLinks(sources, exclude = []) {
  return sources
    .filter(({ rel }) => !excluded(rel, exclude))
    .flatMap(({ rel, text }) =>
      [...text.matchAll(SOURCE_LINK)].map((match) => {
        const url = new URL(match[1]);
        return {
          page: sourcePageName(rel),
          href: url.href,
          tour: url.searchParams.get('tour') ?? '',
          step: Number(url.searchParams.get('step') ?? 0) || 0
        };
      })
    );
}

const linkKey = (link) => `${link.page}\u0000${link.tour}\u0000${link.step}`;

/** `links` grouped by page, tour and step. */
function tally(links) {
  const counts = new Map();
  for (const link of links) counts.set(linkKey(link), [...(counts.get(linkKey(link)) ?? []), link]);
  return counts;
}

/**
 * What differs between the links the docs sources declare and the links the
 * built docs carry, each matched by its page, tour and step and counted: a
 * page dropped from the build, a link lost on the way, or one the build
 * carries that no source declares, each a sentence. No link on either side
 * is a failure too: a docs check that found nothing checked nothing.
 */
export function linkSetProblems(declared, built) {
  if (declared.length === 0) return [`the docs sources declare no "${LINK_TEXT}" link`];
  const want = tally(declared);
  const have = tally(built);
  const problems = [];
  for (const [key, links] of want) {
    const [link] = links;
    for (let i = have.get(key)?.length ?? 0; i < links.length; i += 1) {
      problems.push(`${link.page} declares a link to ${link.tour} step ${link.step} that the built docs do not carry`);
    }
  }
  for (const [key, links] of have) {
    const [link] = links;
    for (let i = want.get(key)?.length ?? 0; i < links.length; i += 1) {
      problems.push(`${link.page} carries a link to ${link.tour} step ${link.step} that no docs source declares`);
    }
  }
  return problems;
}

/**
 * Whether `href` is on the site at `base`: the same origin (scheme, host and
 * port — never a host that merely starts with the base's), and under the
 * base's path when it has one.
 */
export function onSite(href, base) {
  let url;
  let root;
  try {
    url = new URL(href);
    root = new URL(base);
  } catch {
    return false;
  }
  if (url.origin === 'null' || url.origin !== root.origin) return false;
  const prefix = root.pathname.replace(/\/$/, '');
  return prefix === '' || url.pathname === prefix || url.pathname.startsWith(`${prefix}/`);
}

/** A demo.kew.dev link, pointed at `base`: the same path, query and fragment. */
export function onBase(href, base) {
  const url = new URL(href);
  if (url.origin !== DEMO_ORIGIN) throw new Error(`not a demo.kew.dev link: ${href}`);
  return `${base.replace(/\/$/, '')}${url.pathname}${url.search}${url.hash}`;
}

export const sizeLabel = ({ width, height }) => `${width}x${height}`;

/**
 * The sentence a docs-to-demo failure prints: the docs page, the tour (its
 * title and id, when a tour has that id), the step and its passage, the size.
 */
export function linkFailure(link, tour, size, reason) {
  const passage = tour?.steps[link.step - 1]?.passage;
  const named = tour ? `the ${tour.title} tour (${tour.id})` : `the ${link.tour} tour`;
  return (
    `${link.page}: "${LINK_TEXT}" did not open ${named} on step ${link.step}` +
    `${passage ? ` (${passage})` : ''} at ${sizeLabel(size)}: ${reason}`
  );
}

/** The desktop sizes a docs link must open its step at; tours start at 1024 wide. */
export const DOCS_SIZES = [
  { width: 1440, height: 960 },
  { width: 1366, height: 650 }
];

/** The phone a docs link must open the page at, with no tour. */
export const DOCS_PHONE = { width: 390, height: 844 };

/** The gallery's sizes: both desktops and the phone. */
export const GALLERY_SIZES = [...DOCS_SIZES, DOCS_PHONE];

/** Step `index` (from 0) of `tour` as the deep link a docs page uses. */
export function stepUrl(tour, index) {
  const { route } = tour.steps[index];
  return `${route}${route.includes('?') ? '&' : '?'}tour=${encodeURIComponent(tour.id)}&step=${index + 1}`;
}

/** One shot per step of every tour, at every gallery size: what to open and where to write it. */
export function shotPlan(tours, sizes = GALLERY_SIZES) {
  return tours.flatMap((tour) =>
    tour.steps.flatMap((step, index) =>
      sizes.map((size) => ({
        name: `${tour.id}/${index + 1}`,
        tour: tour.id,
        step: index + 1,
        passage: step.passage,
        target: step.target,
        route: stepUrl(tour, index),
        width: size.width,
        height: size.height,
        file: `${tour.id}.${index + 1}.${sizeLabel(size)}.png`
      }))
    )
  );
}

/**
 * What is wrong with the gallery as a whole, beside each shot's own problems:
 * a plan with no shot (no tour was found, so nothing was checked), a manifest
 * that lists another number of shots than the plan, and each entry whose file
 * `exists(file)` says was never written.
 */
export function galleryProblems(plan, entries, exists) {
  if (plan.length === 0) return ['the gallery planned no shot: no tour step was found'];
  const problems = [];
  if (entries.length !== plan.length) problems.push(`the manifest lists ${entries.length} shots, the plan ${plan.length}`);
  for (const entry of entries) if (!exists(entry.file)) problems.push(`${entry.file}: no screenshot was written`);
  return problems;
}

/** Whether a gallery manifest entry raised anything a reviewer must read. */
export const shotProblems = (shot) => [
  ...shot.errors,
  ...shot.csp.map((v) => `CSP violation: ${v}`),
  ...(shot.pageScrollsSideways ? ['the page scrolls sideways'] : [])
];

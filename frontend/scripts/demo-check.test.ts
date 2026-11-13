/**
 * `npm run demo-check` (#3207): the helpers its runner leans on, read without a
 * browser — what a base argument means, which checks a remote base skips, the
 * "See it in the demo" links a built docs page carries, where such a link is
 * pointed, the sentence a docs-to-demo failure prints, and the gallery's shots.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  DEMO_ORIGIN,
  DOCS_PHONE,
  DOCS_SIZES,
  GALLERY_SIZES,
  LOCAL_BUILD_CHECKS,
  demoCheckEnv,
  demoCheckTarget,
  declaredDemoLinks,
  demoLinks,
  docsPageName,
  excludedDocs,
  galleryProblems,
  linkFailure,
  linkSetProblems,
  onBase,
  onSite,
  parseDemoArgs,
  shotPlan,
  shotProblems,
  skippedChecks,
  sourcePageName,
  stepUrl
} from './demo-check-lib.mjs';

const TOURS = new URL('../demo/tours/', import.meta.url);
const tours = () =>
  readdirSync(TOURS)
    .filter((name) => name.endsWith('.json'))
    .sort()
    .map((name) => JSON.parse(readFileSync(new URL(name, TOURS), 'utf8')));

describe('the base a run checks', () => {
  it('runs against the local static server when no URL is given', () => {
    expect(parseDemoArgs([])).toEqual({ base: null, remote: false });
    expect(skippedChecks(parseDemoArgs([]))).toEqual([]);
  });

  it('reads a URL as a remote base, without its trailing slash', () => {
    expect(parseDemoArgs(['https://demo.kew.dev/'])).toEqual({ base: 'https://demo.kew.dev', remote: true });
    expect(parseDemoArgs(['http://127.0.0.1:4173'])).toEqual({ base: 'http://127.0.0.1:4173', remote: true });
  });

  it('refuses what is not one http(s) URL', () => {
    expect(() => parseDemoArgs(['demo.kew.dev'])).toThrow(/not an http\(s\) URL: demo\.kew\.dev/);
    expect(() => parseDemoArgs(['file:///tmp/x'])).toThrow(/not an http\(s\) URL/);
    expect(() => parseDemoArgs(['https://demo.kew.dev/work'])).toThrow(/a site root, not a page/);
    expect(() => parseDemoArgs(['https://a.example', 'https://b.example'])).toThrow(/one base URL at most/);
  });

  it('hands ui-check the base, the docs site and the gallery folder, and demands a browser', () => {
    const run = { base: 'https://demo.kew.dev', remote: true, docs: '/tmp/site', shots: '/tmp/shots' };
    const env = demoCheckEnv(run);
    expect(env.KEW_UI_CHECK_REQUIRE_CHROMIUM).toBe('1');
    expect(demoCheckTarget(env)).toEqual(run);
    expect(demoCheckTarget(demoCheckEnv({ ...run, remote: false }))?.remote).toBe(false);
    expect(demoCheckTarget({})).toBeNull();
    expect(() => demoCheckTarget({ KEW_DEMO_CHECK_BASE: 'https://demo.kew.dev' })).toThrow(/without KEW_DEMO_CHECK_DOCS/);
  });

  it('names every local-build check it skips against a remote URL', () => {
    const skipped = skippedChecks(parseDemoArgs(['https://demo.kew.dev']));
    expect(skipped.map((c) => c.name)).toEqual(['demo world: the bundle passes every leak-scan bundle rule']);
    expect(skipped).toEqual(LOCAL_BUILD_CHECKS);
    for (const c of skipped) expect(c.why).toMatch(/build-demo\//);
  });
});

/** A trimmed copy of what mkdocs writes for a page with two demo links and one other link. */
const BUILT_PAGE = `<!doctype html><html><body><article>
<p>Each step is shown.</p>
<p><a href="https://demo.kew.dev/epics/ledger-v2?tour=follow-an-epic&amp;step=3">See it in the demo</a></p>
<p><a href="../how-it-works/">How it works</a> and <a href="https://demo.kew.dev/work">the demo</a>.</p>
<p><a class="x" href="https://demo.kew.dev/runs/1b464391?tour=where-did-the-money-go&amp;step=1" title="t">See it in the
demo</a></p>
</article></body></html>`;

describe('the docs links into the demo', () => {
  it('finds every See it in the demo link in a built docs page, with its tour and step', () => {
    expect(demoLinks(BUILT_PAGE, 'web/epics/index.html')).toEqual([
      {
        page: 'kew.dev/docs/web/epics/',
        href: 'https://demo.kew.dev/epics/ledger-v2?tour=follow-an-epic&step=3',
        tour: 'follow-an-epic',
        step: 3
      },
      {
        page: 'kew.dev/docs/web/epics/',
        href: 'https://demo.kew.dev/runs/1b464391?tour=where-did-the-money-go&step=1',
        tour: 'where-did-the-money-go',
        step: 1
      }
    ]);
    expect(demoLinks('<p><a href="https://demo.kew.dev/work">the demo</a></p>', 'index.html')).toEqual([]);
  });

  it('names a built page the way a reader finds it', () => {
    expect(docsPageName('how-it-works/index.html')).toBe('kew.dev/docs/how-it-works/');
    expect(docsPageName('index.html')).toBe('kew.dev/docs/');
    expect(docsPageName('404.html')).toBe('kew.dev/docs/404.html');
  });

  it('reports a demo link with no tour or step as a link with step 0, which no tour has', () => {
    const [link] = demoLinks('<a href="https://demo.kew.dev/work?tour=needs-you">See it in the demo</a>', 'web/index.html');
    expect(link.tour).toBe('needs-you');
    expect(link.step).toBe(0);
  });

  it('points a demo.kew.dev link at the base being checked', () => {
    const href = 'https://demo.kew.dev/epics/ledger-v2?tour=follow-an-epic&step=3';
    expect(onBase(href, 'http://127.0.0.1:41234')).toBe('http://127.0.0.1:41234/epics/ledger-v2?tour=follow-an-epic&step=3');
    expect(onBase(href, DEMO_ORIGIN)).toBe(href);
    expect(onBase('https://demo.kew.dev/budget?tour=where-did-the-money-go&step=4#x', 'https://staging.example')).toBe(
      'https://staging.example/budget?tour=where-did-the-money-go&step=4#x'
    );
    expect(() => onBase('https://kew.dev/docs/', 'http://127.0.0.1:1')).toThrow(/not a demo\.kew\.dev link/);
  });

  it('names the docs page, the tour and the step when the step does not open', () => {
    const link = { page: 'kew.dev/docs/web/epics/', tour: 'follow-an-epic', step: 3 };
    const tour = { id: 'follow-an-epic', title: 'Follow an epic', steps: [{}, {}, { passage: 'follow-an-epic/3' }] };
    expect(linkFailure(link, tour, { width: 1440, height: 960 }, '[data-tour="epic-canvas"] is not drawn')).toBe(
      'kew.dev/docs/web/epics/: "See it in the demo" did not open the Follow an epic tour (follow-an-epic) on step 3 ' +
        '(follow-an-epic/3) at 1440x960: [data-tour="epic-canvas"] is not drawn'
    );
    expect(linkFailure({ ...link, tour: 'gone' }, undefined, DOCS_PHONE, 'no tour has that id')).toBe(
      'kew.dev/docs/web/epics/: "See it in the demo" did not open the gone tour on step 3 at 390x844: no tour has that id'
    );
  });

  it('checks the link at both desktop sizes and on a phone', () => {
    expect(DOCS_SIZES).toEqual([
      { width: 1440, height: 960 },
      { width: 1366, height: 650 }
    ]);
    expect(DOCS_PHONE).toEqual({ width: 390, height: 844 });
  });
});

/** A trimmed docs source: two demo links, a prose mention of the words, and another demo link. */
const SOURCE_PAGE = `# Epics

Each tour step a page reads carries a **See it in the demo** link.

[See it in the demo](https://demo.kew.dev/epics/ledger-v2?tour=follow-an-epic&step=3)

See [the demo](https://demo.kew.dev/work) for more.

[See it in the demo](https://demo.kew.dev/runs/1b464391?tour=where-did-the-money-go&step=1)
`;

const DOCS = fileURLToPath(new URL('../../docs/', import.meta.url));
const MKDOCS = new URL('../../mkdocs.yml', import.meta.url);
/** Every Markdown file under docs/, as `{ rel, text }`. */
function docsSources(): { rel: string; text: string }[] {
  return readdirSync(DOCS, { withFileTypes: true, recursive: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith('.md'))
    .map((entry) => {
      const path = join(entry.parentPath, entry.name);
      return { rel: relative(DOCS, path), text: readFileSync(path, 'utf8') };
    });
}

describe('the demo links the docs sources declare', () => {
  it('names a source page the way mkdocs builds it, as a reader finds it', () => {
    expect(sourcePageName('web/epics.md')).toBe('kew.dev/docs/web/epics/');
    expect(sourcePageName('how-it-works.md')).toBe('kew.dev/docs/how-it-works/');
    expect(sourcePageName('web/index.md')).toBe('kew.dev/docs/web/');
    expect(sourcePageName('index.md')).toBe('kew.dev/docs/');
    // The name a built page of the same source gets.
    expect(sourcePageName('web/epics.md')).toBe(docsPageName('web/epics/index.html'));
  });

  it('reads each [See it in the demo](https://demo.kew.dev...) link, and no prose mention or other demo link', () => {
    expect(declaredDemoLinks([{ rel: 'web/epics.md', text: SOURCE_PAGE }])).toEqual([
      {
        page: 'kew.dev/docs/web/epics/',
        href: 'https://demo.kew.dev/epics/ledger-v2?tour=follow-an-epic&step=3',
        tour: 'follow-an-epic',
        step: 3
      },
      {
        page: 'kew.dev/docs/web/epics/',
        href: 'https://demo.kew.dev/runs/1b464391?tour=where-did-the-money-go&step=1',
        tour: 'where-did-the-money-go',
        step: 1
      }
    ]);
  });

  it('skips a source mkdocs excludes from the build', () => {
    const exclude = excludedDocs(readFileSync(MKDOCS, 'utf8'));
    expect(exclude).toEqual(['/audit/**', '/vhs/**', '/brand-guidelines.md']);
    const sources = [
      { rel: 'audit/pack.md', text: SOURCE_PAGE },
      { rel: 'brand-guidelines.md', text: SOURCE_PAGE },
      { rel: 'web/audit.md', text: SOURCE_PAGE }
    ];
    expect(declaredDemoLinks(sources, exclude).map((link) => link.page)).toEqual([
      'kew.dev/docs/web/audit/',
      'kew.dev/docs/web/audit/'
    ]);
  });

  it("reads the real docs: docs/web/demo.md's prose mention is not a link", () => {
    const sources = docsSources();
    const declared = declaredDemoLinks(sources, excludedDocs(readFileSync(MKDOCS, 'utf8')));
    expect(declared.length).toBeGreaterThan(0);
    expect(sources.find((s) => s.rel === join('web', 'demo.md'))?.text).toMatch(/\*\*See it in the demo\*\*/);
    expect(declared.filter((link) => link.page === 'kew.dev/docs/web/demo/')).toEqual([]);
  });
});

describe('the built docs carry exactly the links the sources declare', () => {
  const declared = declaredDemoLinks([
    { rel: 'web/epics.md', text: SOURCE_PAGE },
    { rel: 'security.md', text: '[See it in the demo](https://demo.kew.dev/work?tour=refused-pr&step=3)\n' }
  ]);
  const built = [
    ...demoLinks(BUILT_PAGE, 'web/epics/index.html'),
    ...demoLinks('<a href="https://demo.kew.dev/work?tour=refused-pr&amp;step=3">See it in the demo</a>', 'security/index.html')
  ];

  it('passes when the two sets are equal', () => {
    expect(declared).toHaveLength(3);
    expect(linkSetProblems(declared, built)).toEqual([]);
  });

  it('fails a page dropped from the build, naming its links', () => {
    const dropped = built.filter((link) => link.page !== 'kew.dev/docs/security/');
    expect(linkSetProblems(declared, dropped)).toEqual([
      'kew.dev/docs/security/ declares a link to refused-pr step 3 that the built docs do not carry'
    ]);
  });

  it('fails a built link no source declares', () => {
    const extra = [...built, { ...built[0], tour: 'needs-you', step: 2 }];
    expect(linkSetProblems(declared, extra)).toEqual([
      'kew.dev/docs/web/epics/ carries a link to needs-you step 2 that no docs source declares'
    ]);
  });

  it('counts each link: one built where two are declared fails', () => {
    const twice = [...declared, declared[0]];
    expect(linkSetProblems(twice, built)).toEqual([
      'kew.dev/docs/web/epics/ declares a link to follow-an-epic step 3 that the built docs do not carry'
    ]);
  });

  it('fails no links at all, on either side', () => {
    expect(linkSetProblems([], [])).toEqual(['the docs sources declare no "See it in the demo" link']);
  });
});

describe('a URL on the base being checked', () => {
  it('is on the same origin, never a host that merely starts with it', () => {
    expect(onSite('https://demo.kew.dev/work?tour=x&step=1', 'https://demo.kew.dev')).toBe(true);
    expect(onSite('https://demo.kew.dev', 'https://demo.kew.dev/')).toBe(true);
    expect(onSite('https://demo.kew.dev.evil.example/work', 'https://demo.kew.dev')).toBe(false);
    expect(onSite('http://demo.kew.dev/work', 'https://demo.kew.dev')).toBe(false);
  });

  it('is on the same port: 41730 is not 4173', () => {
    expect(onSite('http://127.0.0.1:4173/work', 'http://127.0.0.1:4173')).toBe(true);
    expect(onSite('http://127.0.0.1:41730/work', 'http://127.0.0.1:4173')).toBe(false);
    expect(onSite('http://127.0.0.1:4173/work', 'http://127.0.0.1:41730')).toBe(false);
  });

  it('is under the base path when the base has one', () => {
    expect(onSite('https://x.example/demo/work', 'https://x.example/demo')).toBe(true);
    expect(onSite('https://x.example/demo', 'https://x.example/demo/')).toBe(true);
    expect(onSite('https://x.example/demox/work', 'https://x.example/demo')).toBe(false);
    expect(onSite('https://x.example/work', 'https://x.example/demo')).toBe(false);
  });

  it('is never a string that is not a URL', () => {
    expect(onSite('not a url', 'https://demo.kew.dev')).toBe(false);
    expect(onSite('about:blank', 'https://demo.kew.dev')).toBe(false);
  });
});

describe('the walkthrough gallery', () => {
  it('opens a step by the deep link a docs page would use', () => {
    expect(stepUrl({ id: 'needs-you', steps: [{ route: '/work' }] }, 0)).toBe('/work?tour=needs-you&step=1');
    expect(stepUrl({ id: 'x', steps: [{ route: '/epics/a?mode=replay' }, { route: '/epics/a?mode=replay' }] }, 1)).toBe(
      '/epics/a?mode=replay&tour=x&step=2'
    );
  });

  it("lists a shot's page and console errors, CSP violations and sideways scroll", () => {
    expect(shotProblems({ errors: [], csp: [], pageScrollsSideways: false })).toEqual([]);
    expect(shotProblems({ errors: ['page error: boom'], csp: ['img-src https://x.example/a.png'], pageScrollsSideways: true })).toEqual([
      'page error: boom',
      'CSP violation: img-src https://x.example/a.png',
      'the page scrolls sideways'
    ]);
  });

  it('plans one shot per tour step and size', () => {
    const all = tours();
    const plan = shotPlan(all);
    const steps = all.reduce((n, tour) => n + tour.steps.length, 0);
    expect(GALLERY_SIZES).toEqual([
      { width: 1440, height: 960 },
      { width: 1366, height: 650 },
      { width: 390, height: 844 }
    ]);
    expect(plan).toHaveLength(steps * 3);
    expect(new Set(plan.map((shot) => shot.file)).size).toBe(plan.length);
    const first = all[0];
    expect(plan[0]).toEqual({
      name: `${first.id}/1`,
      tour: first.id,
      step: 1,
      passage: first.steps[0].passage,
      target: first.steps[0].target,
      route: stepUrl(first, 0),
      width: 1440,
      height: 960,
      file: `${first.id}.1.1440x960.png`
    });
    expect(plan.filter((shot) => shot.width === 390).every((shot) => shot.file.endsWith('.390x844.png'))).toBe(true);
  });

  it('fails a gallery with no tour to shoot', () => {
    expect(galleryProblems(shotPlan([]), [], () => true)).toEqual(['the gallery planned no shot: no tour step was found']);
  });

  it('fails a shot whose file was never written, and a manifest short of its plan', () => {
    const plan = shotPlan([{ id: 'needs-you', steps: [{ route: '/work', passage: 'needs-you/1', target: 'x' }] }]);
    const written = new Set(['needs-you.1.1440x960.png', 'needs-you.1.1366x650.png', 'needs-you.1.390x844.png']);
    const entries = plan.map((shot) => ({ file: shot.file }));
    expect(galleryProblems(plan, entries, (file: string) => written.has(file))).toEqual([]);
    written.delete('needs-you.1.1366x650.png');
    expect(galleryProblems(plan, entries, (file: string) => written.has(file))).toEqual([
      'needs-you.1.1366x650.png: no screenshot was written'
    ]);
    expect(galleryProblems(plan, entries.slice(1), () => true)).toEqual(['the manifest lists 2 shots, the plan 3']);
  });
});

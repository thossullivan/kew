#!/usr/bin/env node
/**
 * Generate kew.dev/llms.txt from the mkdocs nav.
 *
 * llms.txt (https://llmstxt.org) is a markdown index an agent reads before the
 * site itself: an H1 title, a blockquote summary, then sections of links. Ours
 * names the pages an agent needs to install and drive kew, and nothing else.
 *
 * The titles and paths come from the nav in mkdocs.yml, so a renamed or moved
 * page cannot leave a hand-written link behind. Every link is checked against
 * the BUILT docs tree before anything is written: a page the nav names but the
 * build did not produce is a hard stop, because a llms.txt that sends an agent
 * to a 404 is worse than none. That is how the exit-codes page went missing
 * from the live site without anyone noticing.
 *
 * Run via `npm run dist`, after compose-dist has folded the docs into build/.
 *   node scripts/llms-txt.mjs [--mkdocs ../mkdocs.yml] [--site ../site] [--out build/llms.txt]
 */

import { existsSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

/** The pages llms.txt links, in order, by their mkdocs source path. */
export const PAGES = [
  'getting-started.md',
  'agents.md',
  'cli/index.md',
  'cli/exit-codes.md',
  'configuration.md'
];

/** Strip YAML quoting from a scalar. */
function scalar(raw) {
  const value = raw.trim();
  if (value.length >= 2 && (value[0] === '"' || value[0] === "'") && value.at(-1) === value[0]) {
    return value.slice(1, -1);
  }
  return value;
}

/**
 * Read site_name, site_description, site_url and the nav (as [title, path]
 * leaves) out of mkdocs.yml. A full YAML parser would need a dependency and
 * would choke on mkdocs' `!!python/name` tags; the nav is a plain indented
 * list of `- Title: path` entries, so it is read line by line.
 */
export function parseMkdocs(text) {
  const top = (key) => {
    const m = text.match(new RegExp(`^${key}:[ \\t]*(.+)$`, 'm'));
    return m ? scalar(m[1]) : undefined;
  };
  const nav = [];
  let inNav = false;
  let section;
  for (const line of text.split('\n')) {
    if (/^nav:\s*$/.test(line)) {
      inNav = true;
      continue;
    }
    if (!inNav) continue;
    if (/^\S/.test(line)) break;
    const m = line.match(/^(\s*)-\s+(.+?):\s*(\S.*)?$/);
    if (!m) continue;
    const nested = m[1].length > 2;
    if (!m[3]) {
      section = scalar(m[2]);
    } else {
      // A nested entry is titled by its section too: "CLI: Overview", not "Overview".
      const title = scalar(m[2]);
      nav.push([nested && section ? `${section}: ${title}` : title, scalar(m[3])]);
    }
  }
  return {
    name: top('site_name'),
    description: top('site_description'),
    url: top('site_url'),
    nav
  };
}

/** The built page a source path becomes under mkdocs' directory URLs. */
export function pageRoute(path) {
  const stem = path.replace(/\.md$/, '');
  if (stem === 'index') return '';
  if (stem.endsWith('/index')) return stem.slice(0, -'index'.length);
  return `${stem}/`;
}

/** Render llms.txt, or throw naming every page the built site does not have. */
export function renderLlmsTxt(mkdocsText, siteDir) {
  const { name, description, url, nav } = parseMkdocs(mkdocsText);
  if (!name || !description || !url) {
    throw new Error('llms.txt: mkdocs.yml must set site_name, site_description and site_url');
  }
  const base = url.endsWith('/') ? url : `${url}/`;
  const problems = [];
  const lines = [`# ${name}`, '', `> ${description}`, '', '## Docs', ''];
  for (const path of PAGES) {
    const entry = nav.find(([, p]) => p === path);
    if (!entry) {
      problems.push(`${path} is not in the mkdocs nav`);
      continue;
    }
    const route = pageRoute(path);
    const built = resolve(siteDir, route, 'index.html');
    if (!existsSync(built) || !statSync(built).isFile()) {
      problems.push(`${path} has no built page at ${built}`);
      continue;
    }
    lines.push(`- [${entry[0]}](${base}${route})`);
  }
  if (problems.length) {
    throw new Error(`llms.txt: refusing to link pages the site does not serve:\n  ${problems.join('\n  ')}`);
  }
  return `${lines.join('\n')}\n`;
}

function arg(argv, flag, fallback) {
  const i = argv.indexOf(flag);
  return i === -1 ? fallback : argv[i + 1];
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
  const argv = process.argv.slice(2);
  const mkdocs = resolve(arg(argv, '--mkdocs', resolve(root, '../mkdocs.yml')));
  const site = resolve(arg(argv, '--site', resolve(root, '../site')));
  const out = resolve(arg(argv, '--out', resolve(root, 'build/llms.txt')));
  try {
    writeFileSync(out, renderLlmsTxt(readFileSync(mkdocs, 'utf8'), site));
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exit(1);
  }
  console.log(`llms.txt: wrote ${out}`);
}

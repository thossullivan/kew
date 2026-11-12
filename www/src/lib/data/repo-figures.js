/**
 * The dated snapshot of kew's development repository, and the one way each
 * of its figures is written on the page.
 *
 * The counts come from repo-stats.json, which the build never refreshes (see
 * scripts/fetch-repo-stats.mjs for the manual refresh). The agent share is
 * computed here from the two counts, never typed. Like pack-figures.js, every
 * element quoting one of these carries data-repo="<key>", and the build
 * (scripts/page-claims.mjs) refuses a page where a marked figure differs from
 * the string here. Plain JavaScript so that Node gate imports the same strings
 * the page renders.
 */

import STATS from './repo-stats.json' with { type: 'json' };

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** "2026-11-02" as the page writes dates: "2 Nov 2026". @param {string} iso */
function dateOf(iso) {
  const [year, month, day] = iso.split('-').map(Number);
  return `${day} ${MONTHS[month - 1]} ${year}`;
}

/** @param {number} n */
const count = (n) => n.toLocaleString('en-US');

/** The share of merged pull requests an agent opened, 0 to 1. */
export const AGENT_SHARE = STATS.agentPrs / STATS.mergedPrs;

/** Every repository figure the page quotes, as the page writes it. */
export const REPO_SHOWN = {
  captured: dateOf(STATS.capturedAt),
  mergedPrs: count(STATS.mergedPrs),
  agentPrs: count(STATS.agentPrs),
  agentShare: `${(AGENT_SHARE * 100).toFixed(1)}%`,
  otherShare: `${((1 - AGENT_SHARE) * 100).toFixed(1)}%`
};

/** REPO_SHOWN as a map from the key the page marks to the string it renders. */
export function repoFigures() {
  return new Map(Object.entries(REPO_SHOWN));
}

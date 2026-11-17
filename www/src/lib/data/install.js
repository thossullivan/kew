/**
 * The GitHub repository the site names, and the commands that install kew,
 * written once.
 *
 * The install commands, the site's GitHub links and the deployment install
 * gate (scripts/check-install.mjs) all read from here, so moving the site to
 * another repository changes one line. Plain JavaScript so the Node gate
 * imports the same values the pages render.
 *
 * Dated captures (the evidence pack, the repo-stats snapshot, the verbatim
 * `kew status` output) keep the repository they were recorded from.
 */
export const REPO_SLUG = 'thossullivan/kew';

export const REPO_URL = `https://github.com/${REPO_SLUG}`;

/** The install command: kew-cli from PyPI with the `serve` extra, which
 *  carries the web console. */
export const INSTALL = 'uv tool install "kew-cli[serve]"';

/** The second way: the create-kew launcher on npm installs kew-cli with uv
 *  and runs `kew init` in the current repository. */
export const CREATE_KEW = 'npx create-kew';

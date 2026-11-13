/**
 * The GitHub repository the site names, written once.
 *
 * The install command, the site's GitHub links and the deployment install
 * gate (scripts/check-install.mjs) all read the slug from here, so moving the
 * site to another repository changes this one line. Plain JavaScript so the
 * Node gate imports the same value the pages render.
 *
 * Dated captures (the evidence pack, the repo-stats snapshot, the verbatim
 * `kew status` output) keep the repository they were recorded from.
 */
export const REPO_SLUG = 'thossullivan/kew';

export const REPO_URL = `https://github.com/${REPO_SLUG}`;

// Use the source path until PUBLISH_RELEASE publishes kew-cli to PyPI.
export const INSTALL = `uv tool install "kew-cli[serve] @ git+${REPO_URL}.git@main#subdirectory=kew"`;

/**
 * The install command in the pieces a line may break between: after each `@`
 * and before `#subdirectory`, on top of its spaces. The pages put a `<wbr>`
 * between pieces, so a narrow column wraps the URL there and never mid-word;
 * the pieces join back to INSTALL exactly, which is what Copy copies.
 */
export const INSTALL_PARTS = INSTALL.split(/(?<=@)|(?=#subdirectory)/);

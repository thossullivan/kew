// The PyPI forms the site may show: `pip install NAME`, and the tool
// installers with an optional quoted extra, `uv tool install "NAME[serve]"`
// and `pipx install "NAME[serve]"`. The extra is what carries kew's web
// console, so the gate checks the published project declares it.
const PYPI_FORMS = [
  /^pip install\s+([A-Za-z0-9._-]+)$/,
  /^(?:uv tool install|pipx install)\s+([A-Za-z0-9._-]+)$/,
  /^(?:uv tool install|pipx install)\s+"([A-Za-z0-9._-]+)(?:\[([A-Za-z0-9._,-]+)\])?"$/
];

/** Parse a PyPI installation command into its project and extras, or null. */
export function pypiTarget(command) {
  for (const form of PYPI_FORMS) {
    const match = command.match(form);
    if (match) {
      const extras = match[2] ? match[2].split(',').filter(Boolean) : [];
      return { project: match[1], extras };
    }
  }
  return null;
}

/**
 * Verify that a documented PyPI installation resolves: the project is
 * published, and declares every extra the command names. `fetchProject`
 * returns `{ status, json }` for the project's PyPI JSON document.
 */
export async function verifyPyPIInstall(command, fetchProject) {
  const target = pypiTarget(command);
  if (!target) {
    return { ok: false, reason: 'unsupported PyPI command' };
  }

  let response;
  try {
    response = await fetchProject(target.project);
  } catch {
    return { ok: false, reason: `could not verify ${target.project} on PyPI` };
  }
  if (response.status !== 200) {
    return { ok: false, reason: `${target.project} is not on PyPI` };
  }

  const declared = response.json?.info?.provides_extra ?? [];
  const missing = target.extras.filter((extra) => !declared.includes(extra));
  if (missing.length > 0) {
    return {
      ok: false,
      reason: `${target.project} on PyPI does not declare the extra ${missing.join(', ')}`
    };
  }
  return { ok: true, reason: `${target.project} is on PyPI` };
}

/** Parse the supported GitHub source installation command. */
function sourceTarget(command) {
  const match = command.match(/^uv tool install\s+"[^"]+\s+@\s+(git\+https:\/\/github\.com\/[^"]+)"$/);
  if (!match) return null;

  const url = new URL(match[1].slice('git+'.length));
  const path = decodeURIComponent(url.pathname);
  const pathMatch = path.match(/^\/([^/]+)\/([^/]+)\.git@(.+)$/);
  const subdirectory = new URLSearchParams(url.hash.slice(1)).get('subdirectory');
  if (!pathMatch || !subdirectory) return null;

  return {
    repo: `${pathMatch[1]}/${pathMatch[2]}`,
    ref: pathMatch[3],
    subdirectory
  };
}

function contentsEndpoint(target) {
  const subdirectory = target.subdirectory
    .split('/')
    .map((part) => encodeURIComponent(part))
    .join('/');
  return `repos/${target.repo}/contents/${subdirectory}?ref=${encodeURIComponent(target.ref)}`;
}

/** Verify that a documented source installation resolves through public GitHub APIs. */
export async function verifySourceInstall(command, readme, runGh) {
  if (!readme.includes(command)) {
    return { ok: false, reason: 'source command is not documented in README.md' };
  }

  const target = sourceTarget(command);
  if (!target) {
    return { ok: false, reason: 'unsupported source command or missing explicit ref' };
  }

  let repoView;
  try {
    repoView = await runGh(['repo', 'view', target.repo, '--json', 'visibility']);
  } catch {
    return { ok: false, reason: 'could not verify repository visibility' };
  }

  let visibility;
  try {
    visibility = JSON.parse(repoView.stdout).visibility;
  } catch {
    return { ok: false, reason: 'could not verify repository visibility' };
  }
  if (visibility !== 'PUBLIC') {
    return { ok: false, reason: `${target.repo} is not public` };
  }

  let contentsResponse;
  try {
    contentsResponse = await runGh(['api', contentsEndpoint(target)]);
  } catch {
    return { ok: false, reason: 'could not verify the source ref or subdirectory' };
  }

  let contents;
  try {
    contents = JSON.parse(contentsResponse.stdout);
  } catch {
    return { ok: false, reason: 'could not verify the source ref or subdirectory' };
  }

  const targetType = Array.isArray(contents) ? 'dir' : contents?.type;
  if (targetType !== 'dir') {
    return { ok: false, reason: `${target.subdirectory} is not a directory` };
  }

  const hasPyproject = contents.some(
    (entry) => entry?.name === 'pyproject.toml' && entry.type === 'file'
  );
  if (!hasPyproject) {
    return { ok: false, reason: `${target.subdirectory} does not contain pyproject.toml` };
  }

  return { ok: true, reason: `${target.repo}@${target.ref}/${target.subdirectory} resolves` };
}

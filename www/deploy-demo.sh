#!/usr/bin/env bash
# Manual deploy for demo.kew.dev: build the console's demo bundle (a static
# site on synthetic fixtures, no backend) from a clean export of HEAD, scan it
# for leaks, and rsync it to the host directory the box's Caddy serves for
# demo.kew.dev.
#
# The same host, creds and shape as deploy.sh. The differences, all
# deliberate:
#
#   - It is a DRY RUN by default. Publishing needs `--publish`, DEPLOY_HOST/
#     DEPLOY_USER AND KEW_DEMO_PUBLISH_APPROVED=publish-demo.kew.dev, a
#     demo-only approval, so the kew.dev credentials alone never publish it.
#     Every publish needs the owner's approval (#3138), and no CI workflow
#     runs this script. A publish writes files and nothing else: the site
#     answers when the host's Caddy carries the mount and the site block
#     (DEPLOY.md), and that Caddy belongs to another compose project.
#   - It builds from `git archive HEAD` in a temp directory, never from the
#     checkout: `git status` hides gitignored files, and the demo build bundles
#     every *.json under frontend/demo/world/ and all of frontend/static/,
#     ignored files included. Only tracked files exist in the export.
#   - What it ships is that export's build-demo/, copied to
#     frontend/build-demo/, and it ships only after the leak scan passes over
#     the export's demo source (source mode) and then over that exact staged
#     tree (bundle mode), with no source map and no symbolic link in it. A map
#     would carry the source the minifier dropped, so the scan would have
#     passed a tree that is not what is served.
#
#   - The build history it ships is the real `kew report provenance` pair,
#     never the committed samples: `--publish` needs `--provenance <dir>`.
#     That step copies <dir>'s provenance.json and runs.json into the
#     export's frontend/demo/provenance/ before the source-mode scan, so the
#     scan, the build's schema check (every PR link pinned to PUBLIC_REPO)
#     and the bundle-mode scan all read the copied files. A dry run without
#     it builds the samples.
#
#   ./deploy-demo.sh                    guard, build, scan; print what it would publish
#   ./deploy-demo.sh --provenance <dir> the same, with the real build history
#   ./deploy-demo.sh --publish --provenance <dir>
#                                       guard, build, scan, publish (needs KEW_DEMO_PUBLISH_APPROVED)
set -euo pipefail

usage() { echo "usage: $0 [--publish] [--provenance <dir>]" >&2; exit 2; }
APPROVAL_TOKEN="publish-demo.kew.dev"

PUBLISH=0
PROVENANCE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --publish)
      [ "${PUBLISH}" -eq 0 ] || usage
      PUBLISH=1
      shift
      ;;
    --provenance)
      [ -z "${PROVENANCE}" ] && [ "$#" -ge 2 ] && [ -n "$2" ] || usage
      PROVENANCE="$2"
      shift 2
      ;;
    *) usage ;;
  esac
done

# The real pair, checked before anything is built: both files, each a
# regular file and not a symbolic link, so what is copied is what was named.
if [ -n "${PROVENANCE}" ]; then
  if ! [ -d "${PROVENANCE}" ]; then
    echo "deploy-demo: refusing: --provenance '${PROVENANCE}' is not a directory" >&2
    exit 1
  fi
  for name in provenance.json runs.json; do
    if [ -L "${PROVENANCE}/${name}" ] || ! [ -f "${PROVENANCE}/${name}" ]; then
      echo "deploy-demo: refusing: --provenance '${PROVENANCE}' must hold ${name} as a regular file, not a symbolic link" >&2
      exit 1
    fi
  done
  # Physical path, read before the cd below: a relative <dir> names the caller's.
  PROVENANCE="$(cd -- "${PROVENANCE}" && pwd -P)"
fi
cd "$(dirname "$0")"

SSH_PORT="${DEPLOY_PORT:-22}"
if [ "${PUBLISH}" -eq 1 ]; then
  SSH_HOST="${DEPLOY_HOST:?set DEPLOY_HOST (the host whose Caddy serves demo.kew.dev)}"
  SSH_USER="${DEPLOY_USER:?set DEPLOY_USER (the deploy user)}"
  # DEPLOY_HOST/DEPLOY_USER are deploy.sh's too: an operator with them
  # exported for kew.dev must not publish the demo by typing --publish alone.
  if [ "${KEW_DEMO_PUBLISH_APPROVED:-}" != "${APPROVAL_TOKEN}" ]; then
    echo "deploy-demo: refusing to publish: set KEW_DEMO_PUBLISH_APPROVED=${APPROVAL_TOKEN}." >&2
    echo "  demo.kew.dev goes public only with the owner's approval." >&2
    exit 1
  fi
else
  SSH_HOST="${DEPLOY_HOST:-DEPLOY_HOST}"
  SSH_USER="${DEPLOY_USER:-DEPLOY_USER}"
fi
# DEPLOY_HOST and DEPLOY_USER become ssh's and rsync's destination argument:
# a value that starts with '-' or holds anything but plain characters would be
# read as an option (-oProxyCommand=… runs a command), so refuse it here.
check_name() {
  local name="$1" value="$2"
  if ! [[ "${value}" =~ ^[A-Za-z0-9._][A-Za-z0-9._-]*$ ]]; then
    echo "deploy-demo: refusing: ${name} '${value}' must be non-empty [A-Za-z0-9._-], not starting with '-'" >&2
    exit 1
  fi
}
check_name DEPLOY_HOST "${SSH_HOST}"
check_name DEPLOY_USER "${SSH_USER}"
# DEPLOY_PORT becomes ssh's -p argument, unquoted inside rsync's -e command:
# anything but a port number would add options there.
if ! [[ "${SSH_PORT}" =~ ^[1-9][0-9]{0,4}$ ]] || [ "${SSH_PORT}" -gt 65535 ]; then
  echo "deploy-demo: refusing: DEPLOY_PORT '${SSH_PORT}' must be a port number, 1-65535, digits only" >&2
  exit 1
fi
# The host dir bind-mounted into the box's caddy container (see DEPLOY.md).
REMOTE_DIR="${KEW_DEMO_REMOTE_DIR:-/home/${SSH_USER}/sites/kew-demo}"
# rsync --delete mirrors over this directory, and it is quoted into a remote
# shell command: it must be an absolute kew-demo directory of plain characters,
# so it can be neither kew.dev's tree, nor a parent of it, nor a way out of the
# quotes.
case "/${REMOTE_DIR}/" in
  */../*) remote_ok=0 ;;
  *) remote_ok=1 ;;
esac
if [ "${remote_ok}" -eq 0 ] \
  || ! [[ "${REMOTE_DIR}" =~ ^/[A-Za-z0-9._/-]+$ ]] \
  || [ "${REMOTE_DIR##*/}" != "kew-demo" ]; then
  echo "deploy-demo: refusing: KEW_DEMO_REMOTE_DIR (or DEPLOY_USER in its default) gives '${REMOTE_DIR}';" >&2
  echo "  it must be an absolute path of [A-Za-z0-9._/-], with no '..', ending in /kew-demo" >&2
  exit 1
fi
# Without the real pair the build bundles the committed samples, an invented
# history on an invented repository: never published.
if [ "${PUBLISH}" -eq 1 ] && [ -z "${PROVENANCE}" ]; then
  echo "deploy-demo: refusing to publish: this build would bundle the sample provenance (frontend/demo/provenance/provenance.sample.json)." >&2
  echo "  Pass --provenance <dir> with the real provenance.json and runs.json from kew report provenance." >&2
  exit 1
fi

# Guards first: nothing below may build from a commit that is not what main says.
branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "${branch}" != "main" ]; then
  echo "deploy-demo: refusing to deploy from '${branch}' — check out main" >&2
  exit 1
fi
# The local origin/main ref, no fetch: the operator fetches first.
head="$(git rev-parse HEAD)"
if ! upstream="$(git rev-parse --verify -q refs/remotes/origin/main)"; then
  echo "deploy-demo: refusing: there is no origin/main ref to compare HEAD with — fetch first" >&2
  exit 1
fi
if [ "${head}" != "${upstream}" ]; then
  echo "deploy-demo: refusing: HEAD ${head} is not origin/main ${upstream} — fetch, then match origin/main" >&2
  exit 1
fi
# The build below reads only the export, so this guards the operator's
# intent (a clean checkout is the commit they think they are shipping), not
# the bundle's contents.
dirty="$(git -C .. status --porcelain)"
if [ -n "${dirty}" ]; then
  echo "deploy-demo: refusing to deploy from a dirty checkout:" >&2
  echo "${dirty}" >&2
  exit 1
fi

FRONTEND="../frontend"
OUT="${FRONTEND}/build-demo"

EXPORT=""
STAGED=""
# On any refusal after staging, the staged tree goes too: it can hold the real
# provenance, and a refused build must not sit in the checkout.
cleanup() {
  local status=$?
  if [ -n "${EXPORT}" ]; then rm -rf "${EXPORT}"; fi
  if [ "${status}" -ne 0 ] && [ -n "${STAGED}" ]; then rm -rf "${STAGED}"; fi
}
trap cleanup EXIT
EXPORT="$(mktemp -d "${TMPDIR:-/tmp}/kew-deploy-demo.XXXXXX")"
# Physical path, so every path printed below is the one on disk. The leak scan
# runs whatever path reaches it and fails a scan of zero files; scan() below
# also demands its "is clean" line.
EXPORT="$(cd "${EXPORT}" && pwd -P)"
echo "==> Exporting HEAD (${head}) to a clean tree…"
# The whole commit: the console imports files outside frontend/ too.
# The checked sha, not HEAD again: the checkout is shared, and HEAD can move
# between the check above and this read.
git -C .. archive --format=tar "${head}" | tar -x -C "${EXPORT}"
SRC="${EXPORT}/frontend"
if [ -n "${PROVENANCE}" ]; then
  echo "==> Copying the real build history from ${PROVENANCE} into the export…"
  # Exactly demo/provenance/: the leak scan's allowlist names these two paths,
  # and the build reads the real pair from there instead of the samples.
  mkdir -p "${SRC}/demo/provenance"
  # -P copies a symbolic link as a link, never its target: a file swapped for
  # one since the check above lands as a link, and the check below refuses it.
  cp -P "${PROVENANCE}/provenance.json" "${PROVENANCE}/runs.json" "${SRC}/demo/provenance/"
  for name in provenance.json runs.json; do
    if [ -L "${SRC}/demo/provenance/${name}" ] || ! [ -f "${SRC}/demo/provenance/${name}" ]; then
      echo "deploy-demo: refusing: --provenance '${PROVENANCE}' ${name} became a symbolic link or stopped being a regular file before it was copied" >&2
      exit 1
    fi
  done
fi

# scan <dir> <label> [--bundle], from the directory holding <dir>: the
# export's scanner, with HEAD's allow and deny lists. Passing means exit 0 AND
# the scanner's own "<dir> is clean" line.
scan() {
  local dir="$1" label="$2" output
  shift 2
  if output="$(node "${SRC}/demo/leak-scan.mjs" "${dir}" "$@" 2>&1)"; then
    echo "${output}"
    if grep -qxF "leak scan: ${dir} is clean" <<<"${output}"; then return 0; fi
    echo "deploy-demo: refusing: the leak scan did not report ${label} clean" >&2
    return 1
  fi
  echo "${output}"
  echo "deploy-demo: refusing: the leak scan found something in ${label}" >&2
  return 1
}

echo "==> Checking the demo world is synthetic…"
# A world generated from the real run corpus is not staged or published until
# the owner approves it.
if ! node -e '
const t = JSON.parse(require("node:fs").readFileSync(process.argv[1], "utf8"));
process.exit(t && t.source === "fake" ? 0 : 1);' "${SRC}/demo/world/timings.json"; then
  echo "deploy-demo: refusing: frontend/demo/world/timings.json is not \"source\": \"fake\"." >&2
  echo "  A corpus-sourced demo world is not staged or published without the owner's approval." >&2
  exit 1
fi

echo "==> Running the leak scan over frontend/demo/ (source mode)…"
# Source mode refuses bare public host names, which bundle mode leaves to it.
(cd "${SRC}" && scan demo frontend/demo/)

echo "==> Building the demo console in the export (npm ci; npm run build:demo)…"
(cd "${SRC}" && npm ci --no-audit --no-fund && npm run build:demo)
if [ -z "$(find "${SRC}/build-demo" -type f 2>/dev/null | head -1)" ]; then
  echo "deploy-demo: refusing: the build left no files in build-demo/" >&2
  exit 1
fi

echo "==> Staging the build in frontend/build-demo/…"
# A stale tree from an earlier build must not ride along with this one.
rm -rf "${OUT}"
STAGED="${OUT}"
cp -R "${SRC}/build-demo" "${OUT}"

echo "==> Refusing symbolic links and source maps…"
links="$(find "${OUT}" -type l)"
if [ -n "${links}" ]; then
  echo "deploy-demo: refusing: frontend/build-demo/ holds symbolic links:" >&2
  echo "${links}" >&2
  exit 1
fi
maps="$(find "${OUT}" -name '*.map')"
if [ -n "${maps}" ]; then
  echo "deploy-demo: refusing: frontend/build-demo/ holds source maps:" >&2
  echo "${maps}" >&2
  exit 1
fi
inline="$(grep -rl -e 'sourceMappingURL=data:' "${OUT}" || true)"
if [ -n "${inline}" ]; then
  echo "deploy-demo: refusing: frontend/build-demo/ holds an inline source map:" >&2
  echo "${inline}" >&2
  exit 1
fi

echo "==> Running the leak scan over frontend/build-demo/ (bundle mode)…"
(cd "${FRONTEND}" && scan build-demo frontend/build-demo/ --bundle)

RSYNC_SSH="ssh -p ${SSH_PORT} -o StrictHostKeyChecking=accept-new"
# The browser checks to run against the live site once it is up (#3207):
# printed, never run here — this script drives no browser and no CI runs it.
DEMO_CHECK="cd frontend && npm run demo-check -- https://demo.kew.dev"

if [ "${PUBLISH}" -eq 0 ]; then
  echo "==> Dry run: nothing was published. With --publish this would run:"
  echo "  ssh -p ${SSH_PORT} -- ${SSH_USER}@${SSH_HOST} \"mkdir -p '${REMOTE_DIR}'\""
  echo "  rsync -avz --delete -e \"${RSYNC_SSH}\" -- frontend/build-demo/ ${SSH_USER}@${SSH_HOST}:${REMOTE_DIR}/"
  echo "  and send these files:"
  (cd "${OUT}" && find . -type f | sed 's|^\./|    |' | sort)
  if [ -n "${PROVENANCE}" ]; then
    echo "  The build history is the real pair from ${PROVENANCE}."
  else
    echo "  The build history is the committed sample; --publish needs --provenance <dir>."
  fi
  echo "✓ Dry run: frontend/build-demo/ is built and scanned, and nothing was published."
  echo "  demo.kew.dev goes public only with the owner's approval."
  echo "  After a real deploy, check the live demo: ${DEMO_CHECK}"
  exit 0
fi

echo "demo.kew.dev goes public only with the owner's approval."

echo "==> Ensuring ${REMOTE_DIR} exists (owned by ${SSH_USER})"
# Create the target dir AS the deploy user before the caddy container's bind
# mount can make Docker create it as root (the same ordering as deploy.sh).
ssh -p "${SSH_PORT}" -o StrictHostKeyChecking=accept-new -- \
  "${SSH_USER}@${SSH_HOST}" "mkdir -p '${REMOTE_DIR}'"

echo "==> Publishing frontend/build-demo/ → ${SSH_USER}@${SSH_HOST}:${REMOTE_DIR}"
# --delete keeps the remote an exact mirror of the scanned tree. Scoped to the
# kew-demo dir checked above — it cannot reach kew.dev's files or the other sites'.
rsync -avz --delete -e "${RSYNC_SSH}" -- "${OUT}/" "${SSH_USER}@${SSH_HOST}:${REMOTE_DIR}/"

echo "✓ Published. The host's Caddy serves these files when it carries the mount and the site block (DEPLOY.md); no restart is needed."
echo "  Verify: curl -sI https://demo.kew.dev | head -1"
echo "  Then check it: ${DEMO_CHECK}"

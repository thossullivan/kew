#!/usr/bin/env bash
# Manual deploy: build the composed site tree and rsync it to the host directory
# the deploy host's Caddy serves for kew.dev. Caddy serves files live, so no
# restart is needed for a content change — only the one-time Caddyfile edit does
# (DEPLOY.md). The host, its SSH user and port come from DEPLOY_HOST, DEPLOY_USER
# and DEPLOY_PORT; nothing here names a machine.
#
# kew.dev ships two trees — the marketing build and the mkdocs
# output — so `npm run dist` folds them into one directory first. That is why
# this rsyncs build/ rather than a bare framework output dir.
#
# The docs are built HERE, from this checkout, every time. A deploy that took
# whatever `../site` happened to hold shipped a stale tree (the exit-codes page
# 404'd on the live site), and mkdocs builds untracked files, which is how a
# never-committed page once went live. So the checkout must be clean and on
# main, at origin/main, before anything is built.
#
#   ./deploy.sh            guard, build, check, publish
#   ./deploy.sh --dry-run  guard, build, check; publish nothing
set -euo pipefail
cd "$(dirname "$0")"

DRY_RUN=0
case "${1:-}" in
  "") ;;
  --dry-run) DRY_RUN=1 ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

# DEPLOY_HOST and DEPLOY_USER become ssh's and rsync's destination argument:
# a value that starts with '-' or holds anything but plain characters would be
# read as an option (-oProxyCommand=… runs a command), so refuse it here.
check_name() {
  local name="$1" value="$2"
  if ! [[ "${value}" =~ ^[A-Za-z0-9._][A-Za-z0-9._-]*$ ]]; then
    echo "deploy: refusing: ${name} '${value}' must be non-empty [A-Za-z0-9._-], not starting with '-'" >&2
    exit 1
  fi
}

# DEPLOY_PORT becomes ssh's -p argument, unquoted inside rsync's -e command:
# anything but a port number would add options there.
check_port() {
  local value="$1"
  if ! [[ "${value}" =~ ^[1-9][0-9]{0,4}$ ]] || [ "${value}" -gt 65535 ]; then
    echo "deploy: refusing: DEPLOY_PORT '${value}' must be a port number, 1-65535, digits only" >&2
    exit 1
  fi
}

# Guards first: nothing below may build from a tree that is not what main says.
# `git status --porcelain` lists untracked files too, which matters because
# mkdocs would build them.
branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "${branch}" != "main" ]; then
  echo "deploy: refusing to deploy from '${branch}' — check out main" >&2
  exit 1
fi
# The local origin/main ref, no fetch: the operator fetches first.
head="$(git rev-parse HEAD)"
if ! upstream="$(git rev-parse --verify -q refs/remotes/origin/main)"; then
  echo "deploy: refusing: there is no origin/main ref to compare HEAD with — fetch first" >&2
  exit 1
fi
if [ "${head}" != "${upstream}" ]; then
  echo "deploy: refusing: HEAD ${head} is not origin/main ${upstream} — fetch, then match origin/main" >&2
  exit 1
fi
dirty="$(git -C .. status --porcelain)"
if [ -n "${dirty}" ]; then
  echo "deploy: refusing to deploy from a dirty checkout:" >&2
  echo "${dirty}" >&2
  exit 1
fi

if [ "${DRY_RUN}" -eq 0 ]; then
  SSH_HOST="${DEPLOY_HOST:?set DEPLOY_HOST (the host whose Caddy serves kew.dev)}"
  SSH_USER="${DEPLOY_USER:?set DEPLOY_USER (the deploy user)}"
  check_name DEPLOY_HOST "${SSH_HOST}"
  check_name DEPLOY_USER "${SSH_USER}"
  SSH_PORT="${DEPLOY_PORT:-22}"
  check_port "${SSH_PORT}"
  # The host dir bind-mounted into the box's caddy container (see DEPLOY.md).
  REMOTE_DIR="${KEW_REMOTE_DIR:-/home/${SSH_USER}/sites/kew}"
  # rsync --delete mirrors over this directory, and it is quoted into a remote
  # shell command: it must be an absolute kew directory of plain characters,
  # so it can be neither a parent of the box's other sites nor a way out of
  # the quotes.
  case "/${REMOTE_DIR}/" in
    */../*) remote_ok=0 ;;
    *) remote_ok=1 ;;
  esac
  if [ "${remote_ok}" -eq 0 ] \
    || ! [[ "${REMOTE_DIR}" =~ ^/[A-Za-z0-9._/-]+$ ]] \
    || [ "${REMOTE_DIR##*/}" != "kew" ]; then
    echo "deploy: refusing: KEW_REMOTE_DIR (or DEPLOY_USER in its default) gives '${REMOTE_DIR}';" >&2
    echo "  it must be an absolute path of [A-Za-z0-9._/-], with no '..', ending in /kew" >&2
    exit 1
  fi
fi

# The install command must be real before anything is published. The gate fails
# closed when GitHub or PyPI cannot verify the displayed command.
echo "==> Checking the install command resolves…"
npm run --silent check:install

echo "==> Building the docs (mkdocs → ../site)…"
# The same pinned mkdocs/material versions the docs gate builds with.
source ../kew/scripts/tool-pins.env
uvx --from mkdocs=="${MKDOCS_VERSION}" --with mkdocs-material=="${MKDOCS_MATERIAL_VERSION}" \
  mkdocs build --strict --config-file ../mkdocs.yml

echo "==> Running the docs gate…"
# Proves no internal working material reached the built site. It rebuilds
# ../site with the same pins, so what it checks is what ships.
../scripts/check-docs.sh

echo "==> Building (marketing + docs + llms.txt → build/)…"
npm run dist

if [ "${DRY_RUN}" -eq 1 ]; then
  echo "✓ Dry run: build/ is ready and nothing was published."
  exit 0
fi

echo "==> Ensuring ${REMOTE_DIR} exists (owned by ${SSH_USER})"
# Create the target dir AS the deploy user first. Critical ordering: if the caddy
# container's `docker compose up` runs before this dir exists, Docker auto-creates
# the bind-mount source as root, and then rsync (as deploy) cannot write to it.
ssh -p "${SSH_PORT}" -o StrictHostKeyChecking=accept-new -- \
  "${SSH_USER}@${SSH_HOST}" "mkdir -p '${REMOTE_DIR}'"

echo "==> Publishing build/ → ${SSH_USER}@${SSH_HOST}:${REMOTE_DIR}"
# --delete keeps the remote an exact mirror of build/, so a page deleted from the
# docs cannot survive on the host. Scoped to THIS dir only — it cannot reach
# another site's files.
rsync -avz --delete \
  -e "ssh -p ${SSH_PORT} -o StrictHostKeyChecking=accept-new" \
  -- build/ \
  "${SSH_USER}@${SSH_HOST}:${REMOTE_DIR}/"

echo "✓ Deployed. Caddy serves it live (auto-TLS); no restart needed."
echo "  Verify: curl -sI https://kew.dev | head -1"
echo "          curl -sI https://kew.dev/docs/ | head -1"

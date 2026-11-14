# Deploying kew.dev

Static files served by a **Caddy that already fronts the deploy host**
(`DEPLOY_HOST`), next to whatever other sites it serves. Caddy does automatic
TLS, so there is no certbot and no second web server. We add one site block and
a directory Caddy serves; deploys just write files there.

## What makes kew.dev different

It ships **two trees**: the marketing build (`www/build`, SvelteKit) and the docs
(`../site`, mkdocs Material). `npm run dist` folds the second into the first at
`build/docs/`, so what gets rsynced is one directory.

That composition is also why `www/Caddyfile` no longer strips a `/docs` prefix
with `handle_path`. The docs now live at the path they are served from, both
locally and on the box, so the local Caddyfile and the production site block
differ only in the address line and the local log block.

## Resource cost on the host

About 44 MB across 400 files. Serving static files adds no resident process: it
is a site block on a Caddy that is already running. The build happens on your
machine, never on the host.

## One-time setup

### 1. DNS (at your DNS provider)

```
A   kew.dev      → BOX_IP
A   www.kew.dev  → BOX_IP
```

Caddy provisions TLS for both names automatically once DNS resolves and :80/:443
are reachable — they already are, for the existing sites.

### 2. A directory the deploy user owns

```sh
ssh $DEPLOY_USER@$BOX 'mkdir -p ~/sites/kew'
```

That path becomes `KEW_REMOTE_DIR`. Deploy-user-owned so rsync needs no sudo.
`deploy.sh` also creates it: if `docker compose up` gets there first, Docker
creates the bind-mount source as root and rsync can no longer write to it.

### 3. Add the site to the host's Caddy

**a. Mount the static dir into the caddy service:**

```yaml
  caddy:
    volumes:
      - /home/DEPLOY_USER/sites/kew:/srv/kew:ro
```

**b. Add the site block.** This is `www/Caddyfile` with the address line changed
and the local-only log block dropped:

```caddy
kew.dev {
    root * /srv/kew
    encode gzip zstd

    redir /docs /docs/

    handle /docs/* {
        file_server
    }

    handle {
        try_files {path} {path}/index.html /index.html
        file_server
    }

    header {
        Content-Security-Policy "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'; script-src 'self' 'unsafe-inline'"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "strict-origin-when-cross-origin"
    }

    @immutable {
        path /_app/immutable/*
        file
    }
    header @immutable Cache-Control "public, max-age=31536000, immutable"
}

www.kew.dev {
    redir https://kew.dev{uri} permanent
}
```

The immutable header carries the `file` condition because the marketing
handler answers a missing path with `/index.html`. A header matched on the
path alone would send that page, for a missing `/_app/immutable/` file, with a
cache lifetime of a year.

Keep it in step with `www/Caddyfile`. That file is the local mirror, so a header
that exists in only one of them means the thing you test is not the thing you
ship. The CSP is the load-bearing one. `tests/site-blocks.test.mjs` fails
`npm test` when this block and the `:80` block of `www/Caddyfile` differ in
anything but the address line and the log block, and when the kew.dev and
demo.kew.dev blocks of this file and their two copies in `www/Caddyfile` do
not all send the same CSP. It reads those four blocks and no other.

**c. Reload Caddy once** so it picks up the block. After that, content deploys
never need it again — Caddy serves the mounted files live.

## Every deploy

```sh
cd www
DEPLOY_HOST=… DEPLOY_USER=… ./deploy.sh
```

or `npm run deploy`. It checks the install command resolves, builds both trees,
composes them, and rsyncs with `--delete` scoped to the kew dir — it cannot reach
another site's files on the host.

### Install gate

The page uses the README source installation until the Release workflow
publishes the package. The install gate checks that command against the README.
It also verifies the public repository, explicit ref, and source subdirectory.
After the command changes to a PyPI form (`uv tool install "kew-cli[serve]"`,
`pipx install "kew-cli[serve]"` or `pip install kew-cli`), the gate checks that
PyPI holds the project and that it declares the extra the command names.
Network and authentication failures block deployment.

No GitHub Actions workflow deploys the site: the manual script is the path.

### What `deploy.sh` builds

`deploy.sh` builds the docs it ships, every time. In order, it:

1. refuses to run unless the checkout is on `main`, HEAD is the local
   `origin/main` ref (no fetch: fetch first) and `git status` is clean,
   untracked files included (mkdocs builds them), before building anything.
   A publish also refuses, before anything runs on the host, unless
   `DEPLOY_HOST` and `DEPLOY_USER` are plain names (`[A-Za-z0-9._-]`, not
   starting with `-`), `DEPLOY_PORT` (default 22) is a port number, 1-65535,
   digits only, and the remote directory (`KEW_REMOTE_DIR`, default
   `/home/$DEPLOY_USER/sites/kew`) is an absolute path of `[A-Za-z0-9._/-]`,
   with no `..`, ending in `/kew`: it is quoted into a remote shell command
   and `rsync --delete` mirrors over it;
2. checks the install command (above);
3. runs `mkdocs build --strict` with the pinned mkdocs/material line, then
   `scripts/check-docs.sh`, the same docs gate CI runs;
4. runs `npm run dist`, which builds the marketing site, folds the docs in at
   `build/docs/`, and writes `build/llms.txt`;
5. rsyncs `build/`.

`./deploy.sh --dry-run` does steps 1 to 4 and publishes nothing. It needs no
`DEPLOY_*` variables.

The build never fetches repository statistics. The "built with itself" figures
come from the committed snapshot, `src/lib/data/repo-stats.json`, and nothing
else; `npm run gates` fails if a build changes it. The public repository is a
snapshot with no pull request history, so the figures describe kew's development
repository instead. Refreshing them is a deliberate, manual step that names that
repository: `npm run stats -- --repo <owner/name>` (or set `KEW_STATS_REPO`).
The script refuses to run without one. Commit the refreshed file through a PR.

`kew.dev/llms.txt` is generated by `scripts/llms-txt.mjs` from the mkdocs nav:
the site title and description, then links to getting started, agents, the CLI
overview, exit codes and configuration. It refuses to write a link to a page
the built docs do not contain, so a missing page stops the deploy instead of
shipping a dead link.

## Verify

```sh
curl -sI https://kew.dev | head -1                    # 200
curl -sI https://kew.dev/docs/ | head -1              # 200
curl -sI https://kew.dev | grep -i content-security   # CSP present
curl -s https://kew.dev | grep -o 'og:image[^>]*'     # social card wired
```

## Rollback

Static and git-backed: check out the previous commit, `npm run dist`, deploy
again. No state, no migrations.

## demo.kew.dev

`deploy-demo.sh` builds a second site for the same host: the web console as a
static site on synthetic fixtures (`npm run build:demo` in `frontend/`, output
`frontend/build-demo/`), with no backend. It is a dry run unless it is given
`--publish`, and no CI workflow runs it.

The owner approved publishing the demo, and the build was first published to
the host's `kew-demo` directory on 2026-11-14. Every publish still needs the
approval variable, `KEW_DEMO_PUBLISH_APPROVED`, and must pass every guard
below.

Publishing writes files to that directory and nothing else. demo.kew.dev
answers only when the host's Caddy carries the bind mount and the site block
described under [Directory and bind mount](#directory-and-bind-mount) and
[Caddy site block](#caddy-site-block). That Caddy belongs to another compose
project, so the mount and the block are a change in that project, not a step
of this script.

```sh
cd www
./deploy-demo.sh                          # guard, build, scan; print what it would publish
./deploy-demo.sh --provenance <dir>       # the same, with the real build history
./deploy-demo.sh --publish --provenance <dir>   # publish (needs KEW_DEMO_PUBLISH_APPROVED)
```

Both the dry run and `--publish` end by printing the check to run once the
site answers, `cd frontend && npm run demo-check -- https://demo.kew.dev`
(the demo's browser checks, the docs links into it and the tour gallery). One
of those checks is about the host's site block, not the build: a missing file
under `/_app/` must answer 404 and must not be sent as immutable (see
[Caddy site block](#caddy-site-block)). The script runs nothing more itself,
and no CI job runs that check.

It takes `--publish` and `--provenance <dir>`, each at most once, in either
order; anything else prints the usage and does nothing. Before it builds
anything it refuses unless:

- it is on `main`, HEAD is the local `origin/main` ref (no fetch: fetch
  first), and the checkout is clean (`git status --porcelain` empty);
- the remote directory (`KEW_DEMO_REMOTE_DIR`, default
  `/home/$DEPLOY_USER/sites/kew-demo`) is an absolute path of
  `[A-Za-z0-9._/-]`, with no `..`, ending in `/kew-demo`, so `rsync --delete`
  can never mirror over kew.dev's tree or a parent of it;
- `DEPLOY_HOST` and `DEPLOY_USER` are plain names (`[A-Za-z0-9._-]`, not
  starting with `-`), and `DEPLOY_PORT` (default 22) is a port number,
  1-65535, digits only, so none of them can add an option to `ssh` or `rsync`;
- a `--provenance <dir>` holds `provenance.json` and `runs.json`, each a
  regular file and not a symbolic link;
- a `--publish` has a `--provenance <dir>`. Without one the build bundles the
  committed samples (`frontend/demo/provenance/provenance.sample.json`), an
  invented history on an invented repository, which is never published. A dry
  run without it still builds the samples;
- `frontend/demo/world/timings.json` declares `"source": "fake"`. A demo world
  generated from the real run corpus is not staged or published until the
  owner approves it.

It builds from a clean export of HEAD (`git archive HEAD` into a temp
directory, removed on exit), never from the checkout: `git status` does not
list gitignored files, and the demo build bundles every `*.json` under
`frontend/demo/world/` and all of `frontend/static/`, ignored files included.
In the export it first copies a `--provenance` pair (the files
`kew report provenance` writes) to `frontend/demo/provenance/`, the exact
paths the leak scan's allowlist names. Then it runs the demo leak scan in
source mode over `frontend/demo/` (`node demo/leak-scan.mjs demo`, which
refuses bare public host names), so the copied pair is scanned, then `npm ci` and `npm run build:demo`, and copies only the
export's `build-demo/` to `frontend/build-demo/`, replacing any earlier one.
Over that staged tree it refuses a symbolic link, a source map (`*.map`) or an
inline one (`sourceMappingURL=data:`), then runs the leak scan in bundle mode
(`node demo/leak-scan.mjs build-demo --bundle`). Each scan must exit 0 and
report the tree clean. The build itself checks the copied pair against the
provenance schema before it bundles anything, holding every PR link to the
public repository (`frontend/demo/public-repo.json`), and ships the checked
`provenance.json` as `demo-provenance.json`, which the bundle-mode scan reads.

The leak scan reads every file: UTF-32 or UTF-16 by its byte-order mark, else
UTF-8, else Latin-1, and text holding NULs (UTF-16 with no mark) again with
them removed. Gzip is read decompressed by its signature, whatever the name
(`.svgz` too), nested up to four layers, and a `.br` file decompressed; a
`.gz` or `.br` file that does not decompress is read as it is only when it is
UTF-8 text, and compressed bytes it cannot read whole (truncated, trailing
bytes) are a finding. Only fonts, images and WebAssembly whose extension AND
signature agree are skipped as binaries, the command line names each one, and
an image's printable strings are still read for paths, emails, tokens and the
denylist. In bundle mode no directory is skipped (`node_modules/`, `.git/` and
`.svelte-kit/` ship like any other); a FIFO, socket or device is a finding,
never read; and a tree it read no file of fails.

A `--provenance` file swapped for a symbolic link after the first check is
copied as a link (`cp -P`) and refused. Any refusal after staging removes
`frontend/build-demo/`, which can hold the real provenance.

The dry run prints the `ssh` and `rsync` commands it would run and every file
it would send. A real publish needs `--publish`, `--provenance <dir>`,
`DEPLOY_HOST`/`DEPLOY_USER`, AND `KEW_DEMO_PUBLISH_APPROVED=publish-demo.kew.dev`: the first two are
`deploy.sh`'s too, so kew.dev's credentials alone never publish the demo. Set
that variable only with the owner's approval. It prints the approval line
before it touches the host.

The setup below is what the host needs before demo.kew.dev answers. The DNS
record is set at the DNS provider, and the script creates the directory. The
bind mount and the site block go into the compose project that owns the
host's Caddy.

### DNS

```
A   demo.kew.dev → BOX_IP
```

### Directory and bind mount

```sh
ssh $DEPLOY_USER@$BOX 'mkdir -p ~/sites/kew-demo'
```

That path is the script's default target (`KEW_DEMO_REMOTE_DIR` overrides it),
and the script creates it too, for the same root-owned-mount reason as above.
Mount it into the caddy service:

```yaml
  caddy:
    volumes:
      - /home/DEPLOY_USER/sites/kew-demo:/srv/kew-demo:ro
```

### Caddy site block

The demo is a single-page app, so a page path falls back to `/index.html`:
that is what makes a deep link load. A missing file under `/_app/` answers
404. The CSP is kew.dev's, unchanged: the demo bundles everything it runs,
driver.js included, and makes no request off its own origin. `www/Caddyfile`
carries the same block, every line commented out, as the local reference, and
`frontend/scripts/static-server.mjs` answers the same way for the local
checks.

```caddy
demo.kew.dev {
    root * /srv/kew-demo
    encode gzip zstd

    @missingAsset {
        path /_app/*
        not file {path}
    }
    handle @missingAsset {
        respond 404
    }

    handle {
        try_files {path} /index.html
        file_server
    }

    header {
        Content-Security-Policy "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'; script-src 'self' 'unsafe-inline'"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "strict-origin-when-cross-origin"
    }

    @immutable {
        path /_app/immutable/*
        file
    }
    header @immutable Cache-Control "public, max-age=31536000, immutable"
}
```

The two matchers close one fault. A fallback over every path answers a
missing `/_app/immutable/` chunk with `index.html` and status 200, and a
header matched on the path alone sends it as immutable for a year.
`deploy-demo.sh` uploads with `rsync --delete`, so each publish removes the
previous build's chunks. A page loaded before the publish still asks for
them, and the browser would keep the page shell under a script's URL.
`@missingAsset` keeps `/_app/` out of the fallback, and `@immutable` sends the
cache header only for a file that exists.

Two checks hold this. `tests/site-blocks.test.mjs` fails `npm test` when the
block above and the commented one in `www/Caddyfile` differ. `npm run
demo-check -- https://demo.kew.dev` asks the host for a file under
`/_app/immutable/` that no build writes, and fails unless the answer is 404
with no `immutable` in its Cache-Control. A host that still runs a fallback
over every path loads every page, so no other check sees it.

## Open decision: give Caddy its own edge project

This setup borrows a Caddy that another compose project runs, so redeploying
that project briefly interrupts every site it fronts, kew.dev included. The
clean move is to promote Caddy to its own top-level "edge" compose project on a
shared Docker network, with one block per domain. It is deliberately not part
of this setup: re-pointing live domains and migrating certificates is its own
piece of work, worth doing once kew.dev is up and stable.

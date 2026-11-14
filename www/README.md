# kew.dev

The launch site. SvelteKit 5 + Tailwind 4, static output, dark only.

## Why it lives here

In the same repo as the product so the palette cannot drift. `frontend/src/app.css`
is the **system of record** for kew's design tokens; `scripts/sync-tokens.mjs`
lifts the dark block from it into `src/lib/styles/tokens.css` on every dev and
build. That file is generated and gitignored — never hand-edit it.

```bash
npm run tokens        # regenerate
npm run tokens:check  # exit 1 if stale — a local check
npm run gates         # what CI's `www` job runs: tests, tokens, check, build
```

`tokens:check` is deliberately *not* what CI runs. `tokens.css` is gitignored, so
a fresh checkout has nothing to compare against and `--check` would always fail.
What matters in CI is that the dark block still extracts from `app.css` at all,
which `npm run tokens` proves by throwing if it cannot.

kew previously had five disagreeing visual identities (the TUI as recorded,
`docs/brand-guidelines.md`, the Svelte app, mkdocs, and the evidence pack).
This script is what stops a sixth appearing.

## Design rules

**Colour is a governance signal, never decoration.** Four signals, one meaning each:

| Token | Signal | Meaning |
| --- | --- | --- |
| `--accent` | judgment | a human must decide |
| `--accent-2` | machine | an agent is working |
| `--warn` | money | spend, burn, caps |
| `--success` | attested | verified by the chain |

Presence comes from motion, depth-by-value, type at scale and density — not from
adding hue. If a section needs to feel louder, it does not get a fifth colour.

**Dark only.** The app ships a light theme; this is a product screen, and
committing to one world is the stronger statement.

**No emoji, no icon fonts** — same rule as the TUI. Typography, contrast and ASCII
geometry carry the design.

## Develop

```bash
npm install
npm run dev      # http://localhost:5180
npm run build    # -> build/
npm run check    # svelte-check
```

## Serve locally (Docker)

Marketing at `/`, the mkdocs build at `/docs` — the same origin split as production.

```bash
cd .. && mkdocs build   # ./site   (once, or whenever docs change)
cd www && npm run serve # builds, starts, and reloads the mount
```

http://localhost:8088 by default. Set `KEW_WWW_PORT` to serve on another host
port when 8088 is taken.

> **Rebuilds need a container restart.** `adapter-static` deletes and recreates
> `build/`, so the bind mount ends up pointing at a stale inode and every request
> 404s. `npm run serve` handles this; if you run `vite build` directly, follow it
> with `docker compose restart`.

## Page order

1. **Hero** — the rule (a change never defines its own proof) and the
   channels it holds on, complete at rest; links the proof and threat-model
   docs. Under it, four real frames of the demo console's epic view on a time
   rail (one phone capture below 640px), and the demo link (`static/console/`, captured by
   `node scripts/shoot-console.mjs <demo-url>`; the build never runs it)
2. **Evidence** — built with itself: a dated snapshot of kew's development
   repository (`src/lib/data/repo-stats.json`; the build never fetches it,
   `npm run stats -- --repo <owner/name>` refreshes it by hand), one real
   evidence pack with its spend by run kind and its per-model figures folded,
   and three real anchors the browser hashes again
3. **The loop** — six stages, two of them yours, and how a change leaves the
   agent's sandbox on the contained route
4. **Get kew** — the install command, the six runners, the docs to read next

## Deploy

To the deploy host (`DEPLOY_HOST`), behind the Caddy that already fronts it.
Full setup in **[DEPLOY.md](DEPLOY.md)**.

```bash
cd www && DEPLOY_HOST=… DEPLOY_USER=… npm run deploy   # from a clean main
```

`deploy.sh` refuses a dirty checkout or a branch other than `main`, gates the
install command, builds the docs and runs the docs gate, composes both trees
and `llms.txt` into `build/`, and
rsyncs with `--delete` scoped to the kew directory. Builds happen on your
machine, never on the host.

`npm run dist` is the composition step on its own: the marketing build with the
mkdocs output folded in at `build/docs/`. Local serving mounts that same single
tree, so `Caddyfile` here and the production site block differ only in the
address line and the local log block — keep the two in step, especially the
CSP. `tests/site-blocks.test.mjs` fails `npm test` when they drift.

## Before launch

- The source installation in `src/lib/data/install.js` remains until PyPI publication.
  `Hero.svelte` and `Install.svelte` import that shared value. The deployment gate
  verifies its public repository, explicit ref, source subdirectory, and README entry.
  It checks PyPI after the command changes to a plain PyPI install.
- `REPO_SLUG` in the same file is the one place the site names its GitHub
  repository. The install command, the GitHub links and the install gate read it,
  so moving to another repository changes that line (and the README command).

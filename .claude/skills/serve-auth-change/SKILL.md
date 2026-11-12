---
name: serve-auth-change
description: Use when touching the web serve surface — src/kew/serve/ (auth.py, security.py, api_*.py, app.py), new routes, session/CSRF/Host/Origin logic, or kew serve bind/expose behavior
---

# Serve Auth Changes

All paths relative to `kew/`. Run commands from `kew/`.

## Invariants a change must never weaken

- Loopback default bind (127.0.0.1:8765). Non-loopback bind without `--expose` is rejected; wildcard binds (`0.0.0.0`, `::`) are rejected EVEN WITH `--expose` (src/kew/cli.py:1617–1631).
- Under `--expose`, ALL reads require a session (`read_guard`, src/kew/serve/security.py:94 — audit finding F7, fixed in PR #446). Without expose, only loopback peers read session-free.
- Every write route depends on `require_write` (session + `x-kew-csrf` header + Origin allowlist, all via `secrets.compare_digest`). Every read route depends on `read_guard`. Prefer router-level dependencies (models: api_runs.py:40, api_epics.py:51). No unguarded routes, ever.
- The auth token reaches the browser ONLY in the URL fragment (`/inbox#token=…`), never a query param or access log; the client strips it with `history.replaceState`.
- `kew_session` cookie: httponly, samesite=strict, secure under https; TTL 12h fixed; in-process store (restart = revocation).
- SSE streams re-check the session on every poll whenever `needs_session` required one at entry (`stream_authorization`, src/kew/serve/security.py).
- Host header is checked app-wide by `HostHeaderMiddleware` against `allowed_hosts` (src/kew/serve/security.py:103).

## Layering (import-linter enforced)

`kew.serve.auth` is stdlib-only. `kew.serve` must not import `kew.run_store` / `kew.audit` / `kew.orchestrator` — stores are injected via `store_factory` (src/kew/serve/app.py).

## Checklist

- [ ] Failing test first: guards → tests/test_serve_security.py; token/session → tests/test_serve_auth.py; expose-read gating → tests/test_serve_api_authz.py
- [ ] New/changed route has an explicit `read_guard` or `require_write` dependency
- [ ] Ran bandit — host/secret-looking literals trip it (#252/#344): `uv run bandit -r src/ -c pyproject.toml`
- [ ] `../docs/security.md` updated if the auth model changed

## Acceptance

```bash
uv run pytest tests/test_serve_auth.py tests/test_serve_security.py tests/test_serve_api_authz.py -v
```

Then the full pre-commit gate chain — `CLAUDE.md` Conventions + Trap Gates (ruff format + check, mypy, xenon, bandit, semgrep; becomes one `bash scripts/verify.sh` call once Epic 1 lands it).

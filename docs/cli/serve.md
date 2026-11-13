# kew serve

Launch the approval inbox and web UI.

```text
Usage: kew serve [OPTIONS] [COMMAND] [ARGS]...

Options:
  --host TEXT       Bind host (default from [flight_recorder].host)
  --port INT        Bind port (default from [flight_recorder].port)
  --expose          Allow binding a non-loopback host (needs a declared TLS
                    proxy in [flight_recorder].trusted_proxies)
  --no-expose       Do not enable CLI exposure; cannot override
                    [flight_recorder].expose = true
  --new-token       Rotate the serve token

Commands:
  url   Print the access URL (with token) for the running kew serve instance.
```

## What it does

Starts an HTTP server (default `127.0.0.1:8765`) that serves:

- **`GET /inbox`** — the approval inbox UI, kew's first authenticated web surface
- **`GET /api/approvals`** — list open `needs-approval` holds (read)
- **`POST /api/approvals/{issue}`** — approve or deny a hold (authenticated write)
- **`GET /api/fleet/status`** — fleet run status (read)
- **`POST /api/fleet/stop`** — stop the fleet (authenticated write)

On startup in a terminal, kew prints the access URL with the token in the URL fragment:

```text
kew serve → http://127.0.0.1:8765/
http://127.0.0.1:8765/#token=<token>
```

The second line is a bare, copy-friendly URL. Open it in a browser to access the UI. The fragment (`#token=…`) is read client-side and exchanged for a session cookie — it is never sent to the server.

When stdout is not a terminal — a systemd or launchd unit, a CI job, output piped to a file — kew prints the plain URL and a pointer instead, so the token never lands in a log:

```text
kew serve → http://127.0.0.1:8765/
Run `kew serve url` for the link that signs you in.
```

With `KEW_SERVE_TOKEN` set, kew writes no token file, so the pointer names the variable instead (never its value):

```text
kew serve → http://127.0.0.1:8765/
Sign in at http://127.0.0.1:8765/#token=<value of KEW_SERVE_TOKEN>.
```

To get the tokened URL again, after the terminal has scrolled or when kew runs under a service manager, run [`kew serve url`](#kew-serve-url).

kew does not serve the API schema or the interactive docs pages (`/openapi.json`, `/docs`, `/redoc`).

## Authentication

kew generates a random token and writes it to `<logging.dir>/serve_token` (mode 0600). The `KEW_SERVE_TOKEN` environment variable overrides the file; when set, no file is written.

The token is exchanged once for an HttpOnly `SameSite=Strict` session cookie. Sessions expire after 12 hours. Restarting `kew serve` clears all active sessions.

Writes (approve, deny, stop) also require an `X-Kew-CSRF` header and pass an Origin check. All requests are checked against a Host-header allowlist to defend against DNS-rebinding.

See [Security: Approval inbox](../security.md#approval-inbox) for the full auth model.

## Options

### `--expose`

By default, `kew serve` binds loopback only (`127.0.0.1`). To bind a non-loopback address, pass `--expose` or set `[flight_recorder].expose = true` in `kew.toml`. Either form is sufficient; attempting to bind a non-loopback host without at least one will fail with an error.

`--no-expose` disables only the CLI opt-in. It cannot override `[flight_recorder].expose = true`.

When `--expose` is active, all **API** reads (`/api/*`) require a valid session — the loopback-peer bypass is disabled. (The `/inbox` HTML shell itself is static and unauthenticated; it contains no data — every data call it makes hits the authenticated API.)

Exposing also requires a declared TLS terminator. kew speaks plaintext HTTP and never terminates TLS, so an exposed bind with no reverse proxy in front would put the admin token and the session cookie on the wire in the clear. `kew serve` refuses to start unless `[flight_recorder].trusted_proxies` names the proxy's address:

```bash
# Refused: nothing declares that TLS terminates in front of kew
kew serve --host 192.168.1.10 --expose

# Accepted: the TLS proxy is declared in kew.toml
#   [flight_recorder]
#   trusted_proxies = ["127.0.0.1"]
kew serve --host 127.0.0.1 --expose
```

In exposed mode both session cookies are set `Secure`, and `X-Forwarded-For` / `X-Forwarded-Proto` are honoured only from the declared proxy addresses. See [Reverse proxy + TLS](#reverse-proxy-tls).

### `--new-token`

Rotates the token on disk (deletes and regenerates `serve_token`). To fully revoke existing sessions, rotate and restart:

```bash
kew serve --new-token   # rotates on disk
# then restart kew serve to clear in-memory sessions
```

## `kew serve url`

Print the access URL (with token fragment) for the running `kew serve` instance:

```text
Usage: kew serve url
```

```bash
kew serve url
# http://127.0.0.1:8765/#token=<token>
```

Reads the token from `<logging.dir>/serve_token` (or `KEW_SERVE_TOKEN` if set) and the bind address from `<logging.dir>/serve_state.json`. Both files are written by `kew serve` at startup — no network call is made. The token stays in the URL fragment and is never sent to the server.

Exits non-zero with a clear error if serve has not been started (no state file found).

## Access URL and token rotation

The access URL format is `http://<host>:<port>/#token=<token>`. The fragment (`#token=…`) is handled client-side: the browser reads it, exchanges it for a session cookie, and removes it from the address bar via `history.replaceState`. The token is never sent in a request header, query string, or server log.

**Recovering the URL after the terminal scrolls:** run `kew serve url` to print it again.

**Rotating the token:** use `--new-token` to generate a fresh token and restart to revoke existing sessions. The old `serve_state.json` continues to point at the same bind address — only the token changes.

```bash
# Rotate and restart
kew serve --new-token
# sessions revoked; in a terminal the new link prints on startup,
# otherwise `kew serve url` prints it
```

## Examples

```bash
# Default: loopback, port 8765
kew serve

# Custom port
kew serve --port 9000

# LAN-accessible (requires explicit opt-in, a concrete address, and a declared
# TLS proxy in [flight_recorder].trusted_proxies — see Reverse proxy + TLS)
kew serve --host 192.168.1.10 --expose

# Rotate the token (then restart to revoke sessions)
kew serve --new-token
```

Exposing a **wildcard** bind (`0.0.0.0` / `::`) is rejected — the browser Host/Origin allowlist needs the concrete address clients connect to, so bind that address (or front kew with a reverse proxy).

## Reverse proxy + TLS

kew does not terminate TLS itself. For HTTPS, front `kew serve` with a reverse proxy (nginx, Caddy, Traefik). The proxy must be declared or `kew serve` refuses to start exposed, and the origin must line up or **writes silently fail with `403 origin not allowed`** while reads keep working:

0. **Declare the proxy.** List the address kew sees the proxy connect from in `[flight_recorder].trusted_proxies` (IP or CIDR, e.g. `["127.0.0.1"]`). This is both the operator's statement that TLS terminates in front — without it, exposing is refused, because kew would otherwise serve the admin token over plaintext HTTP — and the allowlist for `X-Forwarded-For` / `X-Forwarded-Proto`, which kew ignores from every other peer.

1. **Declare the public origin.** The browser sends its public `Origin` — e.g. `https://kew.example.com`, with no port — which never matches kew's internal `host:port`. Every write (approve, deny, stop) checks `Origin` against an allowlist, so add the public origin to `[flight_recorder].extra_origins`:

   ```toml
   [flight_recorder]
   host = "127.0.0.1"
   expose = true                                 # see step 2
   extra_origins = ["https://kew.example.com"]   # scheme + host, no path
   trusted_proxies = ["127.0.0.1"]               # see step 0
   ```

2. **Keep reads authenticated.** kew decides whether a read needs a session from the TCP **peer** address. A proxy that connects to kew over loopback looks like a loopback peer. kew asks for a session on any loopback read that carries a forwarding header (`Forwarded`, `Via`, `X-Forwarded-For`, `X-Forwarded-Host`, `X-Forwarded-Port`, `X-Forwarded-Proto`, `X-Forwarded-Server`, `X-Real-IP`). That detection fails open: a proxy that adds none of them (nginx's default `proxy_pass`, HAProxy without `option forwardfor`, `stunnel`, `socat`, an `ssh -L` tunnel) gets every read served session-free to anyone who can reach it. Set `expose = true` (or pass `--expose`) so reads require a valid session regardless of peer or headers; it is the control for any proxy.

3. **Forward the Host header.** kew rejects any request whose `Host` is not in its allowlist (DNS-rebinding defense). The allowlist is derived from the bind address, so configure the proxy to bind kew to the concrete address it connects to, and pass the client's `Host` through.

Minimal Caddy example (Caddy auto-provisions TLS):

```caddyfile
kew.example.com {
    reverse_proxy 127.0.0.1:8765 {
        header_up Host {upstream_hostport}
    }
}
```

Run kew bound to the address the proxy targets, with the public origin and the proxy declared:

```bash
kew serve --host 127.0.0.1 --expose   # with extra_origins + trusted_proxies in kew.toml
```

> The reverse proxy is now the only externally reachable surface. Because kew's token still gates writes, keep the token secret; optionally add an auth layer at the proxy for defense in depth.

## Security headers

Every `kew serve` response carries a baseline set of hardening headers (identical whether or not `--expose` is set):

| Header | Value |
|---|---|
| `Content-Security-Policy` | `default-src 'self'` (same-origin only; inline script/style allowed for the SPA shell), `frame-ancestors 'none'`, `object-src 'none'`, `base-uri 'self'` |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `no-referrer` |

If your reverse proxy also injects these headers, kew's already-present values will be on the response — configure the proxy to replace rather than append, so the client never receives a conflicting second `Content-Security-Policy`.

## Approve and deny from the inbox

The inbox lists all `needs-approval` holds. Approving a hold releases it to `ready` — the running scheduler picks it up and dispatches. The web process does not spawn an agent.

Approving requires `gh` to be authenticated in the process running `kew serve`. If GitHub is unreachable, the inbox shows a distinct error (`gh_error`) rather than silently marking the hold resolved.

Denying parks the issue without closing it.

Both actions are audited with `source="web"` and the actor set to the OS user running `kew serve`. The `[approval].approvers` allowlist (if set) still applies. See [Security: Actor honesty](../security.md#actor-honesty).

## Web UI bundle

The single-page web UI is a pre-built SvelteKit bundle shipped inside the
package (`src/kew/serve/web/`). Released wheels include it; a source checkout
must build it first:

```bash
bash scripts/build_web.sh   # builds frontend/ → src/kew/serve/web/
```

If the bundle is missing, the JSON API under `/api` still works, but visiting
the UI serves a **hint page** (not a bare 404) telling you to run the build, and
`kew serve` prints a startup warning.

The build stamps the bundle with its git sha and build time. `kew serve`
surfaces this as a footer badge, and — when run from a source checkout whose
`frontend/` source is newer than the deployed bundle — warns at startup and
marks the badge **stale**, a reminder to re-run `scripts/build_web.sh`.

## Installation

`kew serve` requires the `serve` extra:

```bash
pip install 'kew-cli[serve]'
```

## Notes

- A background sweep runs once a minute to keep PR outcomes and the orphan-PR list current. It makes one batched GitHub GraphQL request per cycle. A PR read in the last five minutes is read again only when GitHub shows it changed, or when a new run lands on its branch. A merge or close still settles within one cycle. The health page shows each cycle's GraphQL calls.
- Sessions are in-memory — they do not persist across restarts.
- kew does not provide TLS. Place a reverse proxy (nginx, Caddy) in front for HTTPS — see [Reverse proxy + TLS](#reverse-proxy-tls) for the origin/host/expose recipe.
- See [`kew stop`](stop.md) for the CLI equivalent of the inbox stop action.
- See [`kew approve`](approve.md) for the terminal and GitHub-comment approval flows.

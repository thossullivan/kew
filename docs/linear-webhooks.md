# Linear webhooks

!!! warning "Experimental"
    The Linear epic driver and delegation rail have run against a live Linear
    workspace. Webhook delivery and its security checks have automated
    coverage only; they have not been exercised against a live workspace.
    Without webhooks the driver polls. See
    [What works with Linear](configuration.md#what-works-with-linear).

kew can receive Linear webhook events in near-real-time via `POST /webhooks/linear` on the existing `kew ingest serve` receiver. This turns polling into a low-frequency reconcile pass and makes epic state changes (issue moves, comment activity) surface immediately.

## Overview

| What | Detail |
|------|--------|
| Endpoint | `POST /webhooks/linear` |
| Auth | HMAC-SHA256 — `Linear-Signature` header, verified against `LINEAR_WEBHOOK_SECRET` |
| Supported events | `Issue`, `Comment`, `Project` |
| Replay protection | 60-second timestamp window (`webhookTimestamp`) |
| Payload size limit | 1 MiB (same cap as the OTLP receiver) |
| Idempotency | Deduplicated on `Linear-Delivery` header (falls back to signature) |

## Step 1 — Create the webhook in Linear

1. Open **Linear → Settings → API → Webhooks** (workspace-level, not team-level).
2. Click **New webhook**.
3. Set the **URL** to your tunnel endpoint (see [dev-tunnel recipe](#dev-tunnel-recipe) below), e.g. `https://<subdomain>.trycloudflare.com/webhooks/linear`.
4. Under **Resource types**, enable at minimum **Issues** and **Comments**. Enable **Projects** if you track epic-level state changes.
5. Click **Create webhook**. Copy the **Signing secret** — Linear shows it only once.

## Step 2 — Set the signing secret

Export the secret as an environment variable **before** starting `kew ingest serve`. Never hard-code it in `kew.toml` or commit it to the repository.

```bash
export LINEAR_WEBHOOK_SECRET=your_signing_secret_here
kew ingest serve
```

If `LINEAR_WEBHOOK_SECRET` is absent, every delivery is rejected with `401`. kew never logs the secret or echo it back in error responses.

A `.env`-file approach (sourced by your shell profile or a process manager) keeps the secret out of shell history:

```bash
# .env  (git-ignored)
LINEAR_WEBHOOK_SECRET=your_signing_secret_here
```

```bash
# shell session
source .env
kew ingest serve
```

## Dev-tunnel recipe

`kew ingest serve` binds to `127.0.0.1` by default and cannot be configured to bind a non-loopback address — this is enforced at startup. To receive webhooks from Linear during local development you need a tunnel that forwards public HTTPS traffic to your loopback receiver.

### cloudflared (recommended for dev)

```bash
# Terminal 1 — start the receiver
LINEAR_WEBHOOK_SECRET=<secret> kew ingest serve

# Terminal 2 — open a tunnel on the default ingest port (4318)
cloudflared tunnel --url http://127.0.0.1:4318
```

cloudflared prints a `trycloudflare.com` URL. Use `<url>/webhooks/linear` as the webhook URL in Linear workspace settings.

### ngrok

```bash
# Terminal 1 — start the receiver
LINEAR_WEBHOOK_SECRET=<secret> kew ingest serve

# Terminal 2 — open a tunnel
ngrok http 4318
```

ngrok prints a forwarding URL. Use `<forwarding-url>/webhooks/linear` as the webhook URL.

!!! tip
    If you use a non-default ingest port (`[ingest].port` in `kew.toml`), substitute that port number in the tunnel commands above.

## Reconcile semantics

When events arrive, kew records them in the `tracker_events` table (off-chain, not covered by `kew audit verify`). The driver uses fresh events to short-circuit "anything changed?" checks — a full Linear GraphQL fetch becomes:

- **Event-driven refresh** — triggered immediately when a relevant event arrives.
- **Reconcile pass** — a periodic full-fetch on a configurable interval (default: generous, e.g. 10 minutes) that heals missed or delayed deliveries.

This means **missed webhooks are self-healing**: if a delivery fails or the tunnel is briefly down, the next reconcile pass catches up. The reconcile interval is intentionally generous because event-driven updates handle the common case; polling is the fallback, not the primary path.

When no webhook events exist (cold path, or before the webhook is configured), behavior is identical to today's polling-only path. GitHub-tracked projects are not affected.

## Security note

`kew ingest serve` is **loopback-only by design**. The `[ingest].host` field is validated at startup — any non-loopback address is rejected with an actionable error. Exposing the receiver to the network is the operator's decision and is done entirely at the tunnel layer, not through kew configuration.

**Threat posture for the tunnel surface:**

- HMAC-SHA256 verification rejects deliveries with a missing, wrong, or replayed signature — an attacker who cannot forge the `Linear-Signature` header cannot inject events.
- The 60-second replay window limits the usefulness of captured deliveries.
- The 1 MiB payload cap prevents memory-exhaustion attempts over the tunnel.
- Unsigned deliveries receive a bare `401` with no body — no information is echoed to a probing attacker.

For permanent production deployments, prefer a proper reverse proxy (nginx, Caddy) with TLS termination over a dev tunnel — see the guidance in [Security](security.md) and [Reverse proxies](security.md#reverse-proxies).

## See also

- [`kew ingest serve`](cli/ingest.md) — the receiver this endpoint lives on.
- [Configuration — `[ingest]`](configuration.md#ingest) — port and host reference.
- [Security](security.md) — loopback-default posture and exposure model.
- [Recipes](recipes.md) — other operational snippets.

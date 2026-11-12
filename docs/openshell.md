# OpenShell runtime setup

`[sandbox] runtime = "openshell"` replaces the container boundary with an
[NVIDIA OpenShell](https://github.com/NVIDIA/openshell) gateway. Nothing else
moves: kew still owns host-side publication, the publication filter, the audit
chain and image-ID launch. The agent runs in an OpenShell sandbox instead of a
Docker container, and its credential stays on the gateway instead of entering
the agent's environment.

This page is the whole setup, in order. You do not need to read the design to
follow it, and you do not have to know whether it worked: `kew doctor` checks
what every step below has to leave true — read off the running gateway, not off
any file this host wrote — and names the fix for each failure. The one thing it
cannot check after the fact is the install step's checksum, so verify that one
while you are there.

!!! warning "Experimental"

    This runtime is piloted on autonomous dispatches only. Admission refuses it
    unless a passing egress certification no older than 24 hours matches the
    exact boundary a dispatch would launch — see
    [`kew sandbox certify`](cli/sandbox.md#kew-sandbox-certify).

## 1. Install the CLI and the gateway

Install from the project's **checksum-verified release tarballs**, and verify
the checksum before unpacking:

```bash
curl -fLO https://github.com/NVIDIA/openshell/releases/download/v0.1.2/openshell_0.1.2_linux_amd64.tar.gz
curl -fLO https://github.com/NVIDIA/openshell/releases/download/v0.1.2/checksums.txt
sha256sum --check --ignore-missing checksums.txt
tar -xzf openshell_0.1.2_linux_amd64.tar.gz -C /usr/local/bin openshell openshell-gateway
```

Do **not** use the Homebrew installer. It registers the gateway as a brew
service, and a gateway started by a service manager never read your shell's
settings — including the telemetry setting. The result is a host where
`openshell config` says telemetry is off and the running gateway is still
sending it. kew reads the **running gateway**, not any file on this host, for
exactly that reason, so `kew doctor` catches this; installing the tarballs and
starting the gateway yourself avoids it.

The version must be the one kew's client is written against:

```toml
[sandbox.openshell]
version = "0.1.2"
```

`kew doctor` compares that pin against **both** the installed CLI and the
running gateway. A matching CLI against a mismatched gateway is the failure
worth catching: every call succeeds and something else is enforcing the policy.

## 2. Configure and start the gateway

The gateway listens on TLS, sends no telemetry, accepts no policy proposals
from the agent, approves policy changes only when a person does, and launches
the supervisor and the sandbox base image from digests:

```yaml
# ~/.openshell/gateway.yaml
listen: 127.0.0.1:8443
tls:
  enabled: true
  cert: ~/.openshell/tls/gateway.crt
  key: ~/.openshell/tls/gateway.key
telemetry:
  enabled: false
agent:
  proposals: false
  approval: manual
images:
  supervisor: ghcr.io/nvidia/openshell-supervisor@sha256:<digest>
  sandbox: ghcr.io/nvidia/openshell-sandbox@sha256:<digest>
```

Start it in the foreground (or under your own supervisor) and point kew at it:

```bash
openshell-gateway --config ~/.openshell/gateway.yaml
```

```toml
[sandbox]
runtime = "openshell"

[sandbox.openshell]
gateway = "https://127.0.0.1:8443"
```

`OPENSHELL_GATEWAY` — the CLI's own variable — wins over
`[sandbox.openshell] gateway` when it is set, and kew records which of the two
decided the endpoint so a failure names the gateway it actually contacted.

`kew doctor` checks the TLS half from both ends: the gateway has to report TLS
on, and the endpoint kew resolved — `OPENSHELL_GATEWAY` or
`[sandbox.openshell] gateway` — has to be the `https://` one. A gateway
listening on TLS that kew reaches over `http://` puts every sandbox name,
provider name and policy path on the wire in the clear, so either half alone
fails the check.

Two further gateway-side requirements `kew doctor` also checks, because neither
is visible in the config file:

- **Digest pins.** A tag is a mutable pointer: the bytes that enforce the
  boundary can be replaced under a certification that still matches the name.
  Both images must read `<image>@sha256:<digest>`.
- **Landlock ABI 3 or higher** in the container VM kernel. ABI 1 and 2 have no
  network rules, so a supervisor on either one applies the filesystem half of a
  policy and allows every endpoint — containment to read, open egress to run.
  On OrbStack or Docker Desktop, update the VM's kernel.

## 3. Create one credential provider per runner

On this runtime no credential **value** reaches the agent's environment. The
gateway holds it in a provider and injects it at the endpoint, so the provider
map — not the host environment — is what admission checks:

```toml
[sandbox.openshell.providers]                      # runner name -> provider
claude-code = "kew-claude-code"
codex = "kew-codex"
```

An unmapped runner cannot authenticate, so kew refuses the dispatch naming the
missing entry rather than falling through to another runner's provider and
spending the wrong account.

For an API-key runner, create the provider from the key in your environment:

```bash
openshell provider create kew-claude-code --profile anthropic \
  --credential-env ANTHROPIC_API_KEY
```

### The Codex provider needs its own device login

Codex authenticates with a device login, not an API key, and **its refresh
tokens are single use**. A gateway that logged in against your host
`~/.codex` would rotate the refresh token on its first call and log you out of
your own CLI — and the next host command would rotate it back. Give the gateway
an isolated `CODEX_HOME` so the two logins never share a token:

```bash
export CODEX_HOME=~/.openshell/codex
mkdir -p "$CODEX_HOME"
codex login            # completes the device flow into $CODEX_HOME, not ~/.codex
openshell provider create kew-codex --profile codex \
  --credential-env CODEX_HOME
```

Run those four lines in a shell you then close. `CODEX_HOME` exported in a
long-lived shell silently redirects every later host `codex` command too.

## 4. Build the agent images and certify the boundary

The sandbox runs the same packaged agent images every other runtime does, so
build them first:

```bash
kew sandbox build --runner claude-code
kew sandbox build --runner codex
```

Then prove the boundary. Admission will not claim a container boundary kew has
not watched hold, so each mapped runner needs its own certification:

```bash
kew sandbox certify --runtime openshell --runner claude-code
kew sandbox certify --runtime openshell --runner codex
```

Each run reads the runner's own policy under `docker/openshell/` once, creates
a throwaway sandbox from the runner's immutable image ID for each binary that
policy governs, runs one probe inside each, records the passing matrices on the
audit chain, and deletes every sandbox on every path. The proof is bound to the
exact image ID, policy bytes, gateway version, gateway identity and in-image
launcher a dispatch would use — the launch re-checks the policy bytes and the
gateway when it creates the sandbox — and it expires after 24 hours —
so this is a step you repeat, not one you finish. See
[`kew sandbox certify`](cli/sandbox.md#kew-sandbox-certify) for the full probe
matrix and [the audit log](audit.md) for the recorded event.

## 5. Check the setup

```bash
kew doctor
```

On `runtime = "openshell"` it adds these checks. Each one prints a pass or a
`FAIL` with its own fix; a skipped check is never counted as a pass.

| Check | What has to be true |
| --- | --- |
| `openshell-gateway` | the gateway answered |
| `openshell-version` | the CLI **and** the gateway are `[sandbox.openshell] version` |
| `openshell-tls` | the gateway reports TLS on **and** the endpoint kew dials is `https://` |
| `openshell-telemetry` | the gateway has telemetry off |
| `openshell-image-pins` | the supervisor and sandbox images are pinned by digest |
| `openshell-providers` | the gateway holds every provider in `[sandbox.openshell] providers` |
| `openshell-agent-authority` | agent proposals are off and approval is manual |
| `openshell-landlock` | the container VM kernel reports Landlock ABI 3 or higher |
| `openshell-certification` | a fresh passing egress certification matches every mapped boundary |

A gateway kew cannot read fails as one `openshell-gateway` issue rather than as
eight separate ones: a read that did not complete says nothing about the
setting it was going to report. The failure names which of the two it was — the
gateway did not answer, or it answered with a report kew cannot parse — because
those are different host problems. The certification check is read-only — bare
`kew doctor` never probes and never spends.

The policy bytes a boundary enforces are not in this table, because the gateway
holds no policy of its own to compare against: kew hands each sandbox its
policy file from `docker/openshell/` when it creates the sandbox. Those bytes
are covered by `openshell-certification` instead — a certification is bound to
the packaged policy's digest, so a policy edited in an installed wheel stops
matching and the check fails.

## Teardown

Delete what kew created on the gateway, then stop the gateway:

```bash
openshell sandbox delete <name>        # for anything `openshell sandbox list` still shows
openshell provider delete kew-claude-code
openshell provider delete kew-codex
rm -rf ~/.openshell/codex              # the gateway's isolated Codex login
```

Stop the gateway process, then point kew back at a runtime you have:

```toml
[sandbox]
runtime = "docker"
```

Certifications already on the audit chain are evidence of what held, so they
are not deleted; they expire on their own after 24 hours and no dispatch can
use one without a matching boundary. Deleting the providers is the step that
matters: a provider left on a stopped gateway still holds a credential.

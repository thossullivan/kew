# Pinned semgrep ruleset

`pinned.yml` is a **recorded snapshot** of the Semgrep registry `p/python`
ruleset. The SAST gate (`.github/workflows/ci.yml` job `sast` and
`scripts/verify.sh`) scans `kew/src/` against this vendored file with
`--config .semgrep/pinned.yml` instead of `--config auto`.

## Why it is pinned

`--config auto` fetches the latest registry rules on every run, so the security
gate was non-deterministic: an upstream rule change could turn CI red (or green)
with no code change and no review. Vendoring the exact rules makes the gate
reproducible — the ruleset only changes when this file changes, in a reviewed
commit.

`p/python` is the registry's curated Python security ruleset, which matches this
Python codebase. Generic secret-detection and other-language rules that `auto`
also pulled are already covered by the dedicated `secrets` (gitleaks) and
`lint` (bandit) gates, so the pin scopes SAST to the language it actually
protects.

## Recorded revision

- **Source:** `https://semgrep.dev/c/p/python`
- **Ruleset:** `p/python`
- **Recorded:** 2026-07-21
- **Rules:** 151

## Suppressions

The scan excludes `…jinja2…incorrect-autoescape-disabled` — the codebase
intentionally renders non-HTML templates (PR/issue comment markdown) with
`autoescape=False`. The exclude-rule id carries the `semgrep.` prefix semgrep
derives from this file's path (`.semgrep/pinned.yml`); keep the SAST command's
scope run from `kew/` so the prefix stays stable.

## Drift detection

`.github/workflows/sast-rule-drift.yml` is a non-blocking scheduled canary that
re-fetches `p/python` from the registry and diffs it against this file. On drift
it opens (or comments on) a digest issue labelled `security-audit` so the pin
can be refreshed deliberately. Refresh by re-downloading the source URL over
this file, re-running `bash scripts/verify.sh`, and committing.

---
name: audit
description: Run a comprehensive codebase audit of kew — security, architecture, reliability, testing, DX, and competitive positioning
user_invocable: true
---

# Codebase Audit

You are performing a comprehensive codebase audit of **kew**, an open-source control plane for autonomous coding agents. Act as a senior staff engineer + product strategist doing a thorough review. Be direct, opinionated, and specific — reference actual files, functions, and line numbers.

## Internal docs directory

This skill writes internal working material, which never belongs in a project's
published `docs/` tree — that tree is built and published. Resolve
`<internal-docs>` once, before writing anything:

1. Use the internal docs directory the project declares in its `CLAUDE.md` or
   `AGENTS.md` (look for "internal docs directory").
2. Otherwise use the default, `.kew/docs/` — create it if missing, and make sure
   the project gitignores it.

Held audit packs that name open findings stay where they are (kew's own repo
keeps them in a site-excluded tree under the public docs root); read them for
the delta, never write there.

## Context

Check `<internal-docs>/audit/` (and any held packs) for previous audit reports. Compare your findings against the last audit to track what was fixed, what regressed, and what's new.

## Audit Procedure

### Phase 1: Read the Codebase

Read ALL source files in `kew/src/kew/` — every `.py` file, every template in `_templates/`, every runner in `runners/`. Also read:
- `README.md`
- `CLAUDE.md`
- `kew.toml` (root config defaults)
- `pyproject.toml`
- `.github/workflows/ci.yml`
- `create-kew/index.js`
- `<internal-docs>/audit/` and any held packs (previous audits for delta comparison)

Use parallel Agent subagents where possible to speed up reading.

### Phase 2: Analyze Each Section

For each section, provide:
1. **Current state** — what exists, how mature/complete it is
2. **Gaps** — what's missing, incomplete, or inconsistent
3. **Recommendations** — prioritized, actionable next steps with effort estimates (S = hours, M = 1-3 days, L = week+)
4. **Risk level** — Critical / Important / Nice-to-have

#### Section 1: Vision Alignment & Feature Completeness
- Map every README feature claim to its implementation. Status: Complete / Partial / Stub / Missing / Fragile
- Identify config fields with no implementation behind them
- Identify undocumented features in the codebase

#### Section 2: Code Quality & Architecture
- Module boundary cleanliness (dispatch_core, orchestrator, scheduler, cli)
- Pydantic config validation completeness
- Error handling patterns (bare excepts, swallowed errors, missing rate limit handling)
- Type safety (Any types, missing annotations, type: ignore comments)
- SQLite concurrent access safety (WAL, locking, missing indexes, BUSY handling)
- Code duplication across modules

#### Section 3: Security Audit (CRITICAL)
- Security hook coverage: which tools are inspected? Can Write/Edit bypass it?
- Hook bypass vectors: subshells, base64, eval, pipe-to-bash, command chaining
- Hook fail-open vs fail-closed behavior
- Credential exposure: can agent access API keys via env, logs, or GitHub comments?
- Worktree isolation: can agent escape to main checkout or other worktrees?
- Prompt injection: can malicious issue bodies/review comments override instructions?
- Supply chain: create-kew package integrity

#### Section 4: Prompt Engineering
- Dispatch template quality and edge cases
- Review template: comment injection risks
- CI fix template: log truncation strategy
- Self-evaluation: is it implemented or stub?
- Prompt injection defense posture

#### Section 5: Testing & CI
- Coverage ratio per module
- Critical untested paths (concurrent budget, security bypass, template injection, SQLite BUSY)
- CI pipeline completeness (lint, format, type check, test, integration)

#### Section 6: Reliability & Edge Cases
- Crash recovery: labels, worktrees, SQLite consistency
- TOCTOU race conditions on issue claiming
- Scale behavior: large queues, large repos, long-running agents, large diffs
- External dependency failure handling (gh rate limits, git failures, network issues)

#### Section 7: DX & Open-Source Readiness
- First-run experience quality
- Documentation gaps (CONTRIBUTING, troubleshooting, runner protocol guide)
- create-kew wizard completeness and sync with kew setup
- Error message actionability

#### Section 8: Competitive Positioning
- Feature gaps vs Devin, Factory, Sweep, CodeGen
- Missing integrations (GitLab, Linear/Jira, Slack)
- Missing workflow patterns (dependency-aware dispatch, approval gates, rollback)
- Observability gaps

### Phase 3: Write the Report

Save the audit report to `<internal-docs>/audit/YYYY-MM-DD-codebase-audit.md` using today's date.

Include:
1. **Executive Summary** — 5-7 bullet points
2. **Detailed Findings** — one section per audit area with status tables and file:line references
3. **Recommended Roadmap** — Now / Next / Later groupings
4. **Appendix: File-Level Notes** — every file with an issue
5. **Appendix: Security Findings** — severity table with reproduction steps

### Phase 4: Delta Report (if previous audit exists)

If a previous audit exists in `<internal-docs>/audit/` or a held pack, add a **Delta** section at the top:
- **Fixed since last audit** — findings that were addressed
- **Regressions** — things that got worse
- **New findings** — issues not present in the last audit
- **Unchanged** — known issues that remain open

## Output

After writing the report, summarize:
1. The top 3 most critical findings
2. How many findings improved vs regressed since last audit (if applicable)
3. The single highest-leverage action item

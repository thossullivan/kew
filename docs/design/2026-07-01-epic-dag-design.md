# Epic DAG — build, visualize, auto-drive (phased)

**Date:** 2026-07-01 (rev. after adversarial review)
**Status:** Phase 1 approved for planning; Phases 2–3 deferred (each needs its own spec + adversarial pass)
**Owner:** repository maintainer

## Problem

kew has dependency *primitives* but no dependency *graph*. Today
`kew/src/kew/dependencies.py` parses `<!-- kew:depends N M -->` markers and a
`blocked` label, and `find_blocked_dependents()` unblocks dependents **one hop
at a time**; epics are grouped informally by label. There is no assembled graph,
so kew can't validate an epic (cycles, orphans, dangling deps), *show* it, or
drive it in dependency order. The "holy shit" win is to turn those primitives
into a real DAG kew can **build, visualize, and eventually auto-execute**, with
failures contained to their exact blast radius.

Hard constraint: kew is GitHub-only today (`dependencies.py` shells out to
`gh`); Linear is on the roadmap (#365/#366). The graph *domain* must be
provider-neutral so Linear is additive later.

## Why phased (adversarial review, 2026-07-01)

A four-lens adversarial review of the original all-in-one v1 confirmed:

- The **auto-driver was under-designed**: nothing merges PRs yet "DONE" needs a
  closed issue (`--auto` stalls after one node); `DispatchScheduler` has no
  DAG-frontier seam to "reuse"; `_auto_unblock_dependents` fires unconditionally
  and defeats the gate; no cross-loop slot accounting; the approval inbox only
  triggers on a cost threshold; post-PR CI failures are invisible; no DAG-level
  retry counter.
- The **"provider-agnostic / one adapter" claim was oversold**: `run_store`,
  the scheduler, and dispatch are all int-keyed to GitHub issue numbers, so an
  opaque `Node.id` breaks at the dispatch/persistence boundary; two `NodeStatus`
  states are kew-run-derived, not provider-derived.
- The **web view was underpriced**: SSE is a single-run `.events.jsonl` tail
  (30-min cap), not a pub/sub bus; `serve/web` is compiled SvelteKit.
- **What held up:** edge direction is correct; the domain/build/validate/export
  layers are genuinely provider-neutral; `epic_ref: str` is fine; CLI naming
  fits; stateless re-derivation is a strength.

Conclusion: ship the **sound, low-risk core + a real visualization (mermaid)
now**; give the driver and the interactive web view each their own spec and
adversarial pass.

## Phase map

| Phase | Scope | Risk | When |
|------|-------|------|------|
| **1 (this spec)** | domain + port + build + validate + GitHub convention adapter + **mermaid/DOT export** + `kew epic validate\|graph\|status` | Low (pure logic + `gh` reads, no serve/driver) | Build now (freeze-lifted) |
| **2 (own spec)** | auto-driver (`kew epic run`), gated/`--auto`, failure containment | High — see §Phase 2 open problems | After Phase 1, after its own adversarial pass |
| **3 (own spec)** | interactive web graph in `kew serve` | Medium — new SvelteKit route + graph lib + a real live-update design | After Phase 2 |

---

# Phase 1 — build, validate, visualize (this spec)

## Goal
A provider-neutral graph model + a `DependencySource` port + one GitHub
convention adapter; DAG construction and validation (cycles/orphans/dangling
deps); a **mermaid/DOT visualization** that renders in GitHub issues, PRs, and
docs; and read-only CLI (`kew epic validate|graph|status`). No dispatch, no
serve, no frontend.

## Non-goals (Phase 1)
Auto-driver, interactive web view, live updates, GitHub-native sub-issues API,
Linear adapter. `NodeStatus` execution states are display-only in Phase 1 (no
driver acts on them).

## Architecture — three layers
GitHub-specific code touches only the thinnest layer.

```
kew/src/kew/graph/
  domain.py     # Node(id:str, title, status, epic_ref), Edge(src→dst), Graph
  port.py       # DependencySource protocol (the read seam Linear implements later)
  build.py      # assemble a Graph from a DependencySource
  validate.py   # cycles, orphans, dangling deps, topological levels
  export.py     # Graph → Mermaid / DOT (Phase-1 visualization)
  adapters/
    __init__.py
    github_convention.py  # epic:<name> label + kew:depends markers, via gh
```
`build`, `validate`, `export` import **only** `domain`/`port`. Only `adapters/`
imports `gh`/`subprocess_utils`. Mirrors `runners/protocol.py` + `registry.py`.

## Components

### 1. Domain model (`domain.py`)
- `NodeStatus` enum: `TODO`, `READY`, `IN_PROGRESS`, `IN_REVIEW`, `DONE`,
  `BLOCKED`, `FAILED`. **Provenance is explicit** (adversarial B2/B4):
  provider-derived states (`TODO/READY/BLOCKED/IN_REVIEW/DONE`) come from the
  port; execution states (`IN_PROGRESS/FAILED`) are **kew-run-store-derived**,
  not a provider fact. Documented as such in the enum docstring. In Phase 1
  these are display-only.
- `Node`: `id: str` (see honesty note below), `title`, `status`, `epic_ref: str`,
  display extras (`url`, `pr_url`, `last_run_id`).
- `Edge`: `src`, `dst` = "**dst depends on src**"; `src` must be `DONE` before
  `dst` is ready. (Confirmed correct vs `find_blocked_dependents`: `kew:depends N`
  on issue X ⇒ edge `N→X`; `dependents_of(N) ∋ X`.)
- `Graph`: nodes by id + edges; helpers `dependents_of`, `dependencies_of`,
  `transitive_dependents`, `roots`, `leaves`. **Edges are deduped and self-edges
  (`src == dst`) are dropped/flagged** (adversarial graph-M3). Pure data + pure
  functions.

**Honesty note on `Node.id` (adversarial B1):** in Phase 1 the id is a **string,
but in practice the GitHub issue number as a string.** The full opaque-id promise
is *not* delivered by Phase 1 alone — dispatch, `run_store` (`issue_num INTEGER`),
and the scheduler are int-keyed, so a real non-GitHub id needs those changes too.
That work belongs to Phase 2 (§Phase 2 open problems), not here. Phase 1 keeps
the domain string-typed so it does not *add* new int assumptions.

### 2. Port (`port.py`)
```python
class DependencySource(Protocol):
    def list_epic_nodes(self, epic_ref: str) -> list[Node]: ...
    def list_edges(self, nodes: list[Node]) -> list[Edge]: ...
    def status_of(self, node_id: str) -> NodeStatus: ...   # resolves ANY id, node or not
```
- `status_of` takes an **id, not a Node**, so it can resolve the status of a
  dependency that is *not* an epic node (see §4 out-of-epic fix).
- `list_edges` may source edges from a marker convention **or** native provider
  relations — the *port* is the seam, not the marker (adversarial B6).
- Dispatch and status-write-back are **deliberately not in the Phase-1 port**;
  they are added in Phase 2 when there is a driver to use them.

### 3. GitHub convention adapter (`adapters/github_convention.py`)
- **Nodes:** open *and* closed issues carrying the `epic:<name>` label.
- **Edges:** `kew:depends` markers via a **new `parse_depends_ids(body) ->
  list[str]`** that tolerates native ids (`kew:depends ENG-123 144`). The
  existing `parse_depends(body) -> list[int]` and the one-hop unblock path are
  **left untouched** (adversarial B3/graph — changing the shared function's
  return type breaks `find_blocked_dependents` + ~5 tests).
- **Status derivation (explicit predicates, adversarial graph-H2/driver-H1):**
  - `DONE` ⇔ issue `state == CLOSED` (matches today's
    `check_dependencies_resolved`; merge is a display detail, not a gate). A
    "PR merged but issue still open" case stays non-DONE and is surfaced as an
    **anomaly** in `kew epic status`, never silently satisfied.
  - `BLOCKED` = `blocked` label; `IN_REVIEW` = `pr-pending-review` label;
    `IN_PROGRESS`/`FAILED` = live/exhausted kew run rows for that issue
    (run-store lookup); `READY` = all deps `DONE` and none of the above;
    `TODO` otherwise.

### 4. Build (`build.py`)
`build_graph(source, epic_ref) -> Graph`. **Out-of-epic dependency fix
(adversarial graph-CRITICAL-1):** when an edge references an id that is *not* an
epic node, build calls `source.status_of(id)`:
- resolves (e.g. `DONE` because closed) → the dependency is satisfied; it is
  recorded as an **external, satisfied** boundary node (not "missing");
- genuinely nonexistent (404) → recorded as a **dangling** dependency
  (validation error), which is the only real "missing" case.

This preserves today's label-independent "a closed dependency is satisfied"
behavior instead of deadlocking epics that depend on out-of-epic groundwork.

### 5. Validate (`validate.py`)
`validate(graph) -> ValidationReport`: **cycles** (hard error; cycle path
emitted in a **canonicalized order** for deterministic output/tests —
adversarial graph-M3), **dangling deps** (404 targets), **orphans** (info),
**topological levels**. Exposed via `kew epic validate` independently.

### 6. Visualization — mermaid/DOT export (`export.py`)
`render(graph, fmt: "mermaid"|"dot") -> str`, nodes styled by `NodeStatus`.
Renders natively in GitHub issues/PRs and docs — the Phase-1 "show the DAG" win
with **zero serve/frontend cost**. (Interactive web view = Phase 3.)

### 7. CLI surface
- `kew epic validate <name>` — build + validate; print report; nonzero exit on
  cycles/dangling.
- `kew epic graph <name> [--format mermaid|dot]` — export.
- `kew epic status <name>` — counts by status, ready frontier, blocked subtrees,
  and any "PR-merged-but-issue-open" anomalies.
- **Empty-epic guard (adversarial graph-4):** a zero-node result errors
  ("no issues found for label `epic:<name>`"), never reports success — catches
  a typo'd epic name.

### 8. Config additions
- `epic_prefix` lives in **adapter-scoped config** (e.g. a `[graph.github]`
  block or the adapter's own settings), **not** core `LabelConfig`
  (adversarial B5) — the provider convention stays with the provider.
- No other config changes; no dispatch/serve config touched in Phase 1.

## Error handling (Phase 1)
- Cyclic graph → `validate` fails hard, `graph`/`status` still render with the
  cycle annotated.
- Dangling dep (404) → validation error, surfaced per dependent.
- Out-of-epic but resolvable dep → satisfied boundary node (§4).
- `gh`/API failure → fail safe (report error), never emit a partial graph as if
  complete.

## Testing (Phase 1)
- **domain/build/validate:** pure unit tests over synthetic graphs — cycles,
  diamonds, orphans, dangling, self-edges, dup edges, out-of-epic-satisfied,
  empty/single-node/forest. No GitHub.
- **adapter:** fixture `gh` payloads — status derivation (incl. closed-not-merged
  and merged-not-closed), `parse_depends_ids` native-id parsing, and a guard
  test that the legacy `parse_depends`/one-hop path is unchanged.
- **export:** golden mermaid/DOT for a known graph.

## Provider-neutral guarantee (what Phase 1 actually delivers)
`domain/build/validate/export` are provider-neutral and a `LinearSource` could
implement the three read methods. **But** Phase 1 does *not* claim
Linear-drop-in for execution — dispatch/run-store/scheduler are still GitHub-int
-keyed; that is Phase 2's problem, stated plainly here rather than hand-waved.

---

# Phase 2 — auto-driver (deferred; own spec + adversarial pass)

Scope: `kew epic run <name> [--auto]`, gated-by-default sequencing, failure
containment. **Do not start until these confirmed problems are designed and
adversarially reviewed:**

1. **Advancement signal:** define what merges/advances a node. Nothing merges
   PRs today, but `DONE` = closed issue → `--auto` stalls after node 1. Options:
   an explicit gated auto-merge step, or rescope `--auto` to *dispatch-only*.
2. **Scheduler integration contract:** `DispatchScheduler` has no DAG-frontier
   seam and `_dispatch_issue` is private/pool-bound. Either add an
   `allowed_issues`/select-hook injection, or the driver owns its own pool via
   `execute_dispatch` (and re-implements claim/overlap) — pick one.
3. **Label-transition ownership:** `_auto_unblock_dependents` fires
   unconditionally on dispatch and bypasses the gate. The driver must own
   `blocked`/`ready` transitions for epic nodes (suppress auto-unblock when
   epic-managed).
4. **Cross-loop concurrency:** per-instance in-memory slots let `kew epic run` +
   `kew dispatch` over-provision workers. Needs a single-driver lock or shared
   slot accounting in `dispatch.db`.
5. **Gated-readiness inbox path:** the approval inbox triggers on a *cost*
   threshold, not DAG-readiness. Needs a distinct epic-ready hold + an explicit
   approve→dispatch flow-back.
6. **Post-PR failure state:** a red CI check leaves a node `IN_REVIEW` forever
   (flake wedges a subtree). Needs a checks-failing/timeout → `FAILED`
   derivation so containment can fire.
7. **DAG-level retry + restart idempotency:** stateless re-derivation has no
   retry counter (redispatch storm) and needs `IN_PROGRESS` derived from live
   `dispatch.db` run rows to avoid double-dispatch on restart.
8. **Opaque-id execution boundary:** to ever drive a non-GitHub node, migrate
   the `run_store` id + scheduler + dispatch to opaque ids and add
   dispatch/status-write-back to the port (the real cost of Linear execution).

**Implementation note — graph library trigger (#429):** Phase 1 ships a
hand-rolled, zero-dependency `Graph` (DFS cycles, Kahn levels) — correct at
GitHub-issue-epic scale (`gh` latency dominates; ~60 tested, deterministic
lines). **Do not** adopt a graph library (`networkx`/`rustworkx`) until the
driver needs an algorithm we don't want to own — **critical-path/longest-path
scheduling, minimum feedback-set cycle-breaking *suggestions*, or graph diffing
between poll cycles.** Node count is never the trigger; algorithm sophistication
is. When it fires, introduce the library **behind the existing graph seam**
(keep `domain.py`; delegate only traversal) so callers don't change. Full
rationale + candidates in #429.

# Phase 3 — interactive web view (deferred; own spec)

Scope: a live graph in `kew serve`. Open problems: SSE is a single-run
`.events.jsonl` tail (30-min cap), **not** a pub/sub bus — needs a new broadcast
hub or a poll endpoint + a driver→browser cross-process channel; `serve/web` is
compiled SvelteKit (new `frontend/src/routes/epic/` route + a graph lib +
`build_web.sh` rebuild + committed artifacts + vitest). **v1 web is read-only**;
node approval reuses the existing CSRF-guarded `/api/approvals` write; any
driver-control endpoint (retry/skip/override) must go through `require_write`
and is CLI-only until then.

## Related
- #365 (Add Linear), #366 (dependency management) — Phase 2's opaque-id work is
  the groundwork both need.
- #429 (graph library consideration) — when/whether to swap the hand-rolled
  graph core for `networkx`/`rustworkx` behind the seam.
- `dependencies.py` / `find_blocked_dependents` — the one-hop primitive Phase 2
  generalizes (and which Phase 1 leaves intact).

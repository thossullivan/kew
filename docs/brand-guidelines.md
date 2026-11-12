# kew Brand Guidelines

`kew` is the open-source control plane for autonomous coding agents. Its interface should reflect the power and precision of the tool.

## Core Identity: "The Cyberpunk Agent Cockpit"
The `kew` CLI and TUI are designed to feel like a high-end netrunner deck, a flight computer, or a piece of brutalist intrusion hardware. 

We prioritize **data density, high contrast, and strict terminal aesthetics.** We do not try to emulate a web browser inside the terminal.

### The Golden Rule: No Emojis, No Icons
**We do not use emoji or Nerd Font icons** — no coloured pictographs from the
Unicode emoji blocks, no private-use glyphs from a patched font. (This document
names the rule rather than illustrating it; a style guide that prints the thing
it bans is one copy-paste away from putting it on a surface.)

We rely entirely on:
1. Typography (bold, dim, italics)
2. Color contrast
3. Raw ASCII/ANSI geometry (blocks `█`, lines `─` `│` `━`, shading `▒` `░`, and
   markers `◈` `◉` `◆` `▸` `▮` `·`)

*Why?* It forces the design to remain utilitarian and system-agnostic, feeling like an authentic piece of low-level UNIX machinery rather than a modern web app.

#### Where the line falls

Geometry is a shape; a pictograph depicts a thing. The first is in the
vocabulary, the second is not.

| | Verdict |
| :--- | :--- |
| Blocks, rules, shading, arrows, geometric markers (`█ ─ │ ▒ ░ ▸ ◈ ◉ ◆`) | **In** — this is the vocabulary |
| Dingbats and pictographs (`✓ ✗ ⚠ ⏸`) | **Out** — a mark that depicts a thing |
| Emoji, Nerd Font / private-use glyphs | **Out** |

A handful of dingbats predate this rule and still ship in the TUI, the CLI and
PR comments (CI ticks, warning banners, the held-issue marker). Replacing them
is a visible change to dense status columns and is tracked separately — it is
not something to do piecemeal.

**This rule applies to every surface, not just the terminal.** The web cockpit,
the docs site, kew.dev, and the evidence pack use the same vocabulary — no
emoji, no icon fonts, no illustrative glyphs. A web surface that reaches for an
icon where the TUI would reach for a block or a rule has left the brand. Inline
SVG for genuinely structural marks (a chart axis, a DAG edge) is fine; decorative
pictograms are not.

---

## System of record

`frontend/src/app.css` is the single source of truth for kew's colour. Every
other surface transcribes it:

| Surface | Transcript |
| :--- | :--- |
| Web cockpit, kew.dev, docs site | `frontend/src/app.css` (the record itself) |
| TUI and CLI | `kew/src/kew/tui/palette.py` |
| This document | the tables below |

A brand colour changes in `app.css` first. `tests/tui/test_palette.py` fails if
`palette.py` drifts from it, and it also fails if any module under `tui/` or
`cli/` reintroduces a raw hex or a named ANSI colour (`[green]`, `[yellow]`,
`[red]`) — named colours resolve through the *user's* terminal theme, not kew's,
which is how the demo recordings ended up off-brand.

---

## The Semantic Contract

Four signals carry meaning. Each one means exactly one thing on every surface —
TUI, web, docs, evidence pack. Nothing else may borrow them.

| Signal | Dark | Light | Meaning |
| :--- | :--- | :--- | :--- |
| **Judgment** | `#ff2a6d` | `#c81e5a` | A human must decide — approvals, denials, kill switch, P0, failed runs, CI failure |
| **Machine** | `#05d9e8` | `#0892a5` | An agent is working — live, streaming, focused, active workers, simple complexity |
| **Money** | `#ffc94a` | `#b5790e` | Spend, burn, caps. Dollars and nothing else |
| **Attested** | `#2ee6a6` | `#128a5e` | Chain-verified — merged, anchored, hash-matched, CI green |

`Money` and the `warn` token share a value in `app.css`. That is deliberate:
`money` is dollars, `warn` is non-blocking degradation. They are spelled
differently so a future divergence is a one-line change — which means the
spelling has to be right at the call site *today*, while the two are
indistinguishable on screen.

Reach for `MONEY` only when the thing being coloured is spend: a dollar figure,
a burn rate, a budget ratio, a token count, a cap. Everything else amber is
`WARN` — a middling success rate, medium complexity, an epic hold, CI still
pending, a `kew doctor` check that passed with caveats, the mid-step of a
progress ramp.

---

## Color Palette: "Synth-Sunset"

A deep, twilight canvas with highly saturated neon highlights. Values below are
`app.css` verbatim; the Python column is the constant exported by
`kew.tui.palette`.

### Canvas and chrome

| Token | Dark | Light | Python | Usage |
| :--- | :--- | :--- | :--- | :--- |
| `--bg` | `#0d0221` | `#f4f0fb` | `BG` | The deep canvas — log streams, analytics panes, modal bodies |
| `--bg-glow` | `#1a0b2e` | `#fbe6f1` | `BG_GLOW` | Glow wash behind the canvas (web only) |
| `--surface` | `#1a0b2e` | `#fcfaff` | `SURFACE` | Screen and panel background; invisible borders |
| `--surface-2` | `#2d1b4e` | `#f1ebf9` | `SURFACE_2` | Raised strips — summary bars, separators |
| `--surface-3` | `#0d0221` | `#ece3f7` | `SURFACE_3` | Recessed wells |
| `--border` | `#2d1b4e` | `#e2d7f0` | `BORDER` | Resting borders |
| `--border-strong` | `#533a73` | `#c9b8e0` | `BORDER_STRONG` | Emphasised dividers |
| `--fg` | `#e0d8ea` | `#1a0b2e` | `FG` | Primary readable text (issue titles, project name) |
| `--fg-dim` | `#b8aad0` | `#4a3768` | `FG_DIM` | Secondary text |
| `--muted` | `#8b7fa8` | `#6c5b7b` | `MUTED` | Labels and metadata |
| `--faint` | `#6c5b7b` | `#9a8db5` | `FAINT` | Inactive panel titles, empty-log states |

### Accents

| Token | Dark | Light | Python | Usage |
| :--- | :--- | :--- | :--- | :--- |
| `--accent` | `#ff2a6d` | `#c81e5a` | `ACCENT` / `JUDGMENT` | Sunset Pink. The Judgment signal; focused panel titles, active hotkeys |
| `--accent-bright` | `#ff5c8f` | `#e0246a` | `ACCENT_BRIGHT` / `ELEVATED` | One step below Judgment — P1 |
| `--accent-dim` | `#9c2148` | `#e59ab6` | `ACCENT_DIM` | Pressed / disabled accent |
| `--accent-fg` | `#0d0221` | `#fff5f9` | `ACCENT_FG` | Text on an accent fill |
| `--accent-2` | `#05d9e8` | `#0892a5` | `ACCENT_2` / `MACHINE` | Synth Cyan. The Machine signal; focused panel borders, status bar |
| `--accent-2-fg` | `#0d0221` | `#f2fdff` | `ACCENT_2_FG` | Text on a cyan fill |

### Status

| Token | Dark | Light | Python | Usage |
| :--- | :--- | :--- | :--- | :--- |
| `--success` | `#2ee6a6` | `#128a5e` | `SUCCESS` / `ATTESTED` | The Attested signal |
| `--warn` | `#ffc94a` | `#b5790e` | `WARN` / `MONEY` | `MONEY` for spend; `WARN` for everything else amber |
| `--danger` | `#ff4365` | `#c81e3c` | `DANGER` | Destructive-action affordances (web) |
| `--info` | `#05d9e8` | `#0892a5` | `INFO` | Informational, equal to Machine |
| `--status-blocked` | `#b48ade` | `#7a4fa3` | `STATUS_BLOCKED` | Epic DAG blocked node (hatched for colour-blind viewers) |
| `--status-cancelled` | `#6c5b7b` | `#9a8db5` | `STATUS_CANCELLED` | Epic DAG cancelled node ("ghosted", carries an `x` glyph) |

### Factory UI

Additive tokens for the factory surfaces (Work, Monitor, Epics), taken from the
direction-D artboards. Web only: they exist in `palette.py` so the parity test
passes, and the TUI paints none of them. The light column is provisional — each
token is mapped onto the existing light token that carries the same job, so the
theme toggle stays readable until the designed light theme lands.

| Token | Dark | Light | Usage |
| :--- | :--- | :--- | :--- |
| `--ink` | `#0a0714` | `#f4f0fb` | Factory ground |
| `--ink-raised` | `#0d0a18` | `#fcfaff` | Raised panel on the ground |
| `--ink-selected` | `#151025` | `#f1ebf9` | Selected row or node |
| `--ink-track` | `#1d1730` | `#ece3f7` | Gauge and progress tracks |
| `--hair` | `#221c33` | `#e2d7f0` | Standard hairline rule |
| `--hair-quiet` | `#1a1528` | `#f1ebf9` | Row separators inside a list |
| `--hair-strong` | `#342a4a` | `#c9b8e0` | Emphasised rule, gauge ticks |
| `--hair-box` | `#2a2240` | `#c9b8e0` | Box outlines |
| `--guide` | `#16111f` | `#ece3f7` | Layout guides, barely above the ground |
| `--text-1` | `#ebe6f2` | `#1a0b2e` | Primary factory text |
| `--text-2` | `#b5acc8` | `#4a3768` | Secondary text |
| `--text-3` | `#8d84a3` | `#6c5b7b` | Labels and metadata; the smallest AA-safe step |
| `--text-4` | `#6a6082` | `#9a8db5` | **Non-text** (3.4:1 on `--ink`) — ticks, rules, marks |
| `--text-5` | `#5a5070` | `#c9b8e0` | Separators and idle rings |
| `--edge-done` | `#3a3052` | `#c9b8e0` | Completed DAG edge |
| `--halo-cyan` | `rgba(5,217,232,.16)` | `rgba(8,146,165,.12)` | Halo behind a working mark |
| `--halo-magenta` | `rgba(255,42,109,.16)` | `rgba(200,30,90,.12)` | Halo behind a needs-you mark |

The factory type scale is five sizes, defined as utilities in `app.css`:
`text-11` (mono, uppercase, .14em tracking), `text-13`, `text-15`, `text-26`
(mono 500, -.015em tracking) and `text-44` (mono 500, line-height 1). The
factory root carries `tabular-nums`.

### Retired values

These appeared in the TUI or in earlier versions of this document and are no
longer part of the brand:

| Value | Was | Now |
| :--- | :--- | :--- |
| `#ff7a59` | Neon Orange, P1 | `--accent-bright` (`ELEVATED`) — the P1 step belongs on the accent ramp, not on a hue of its own |
| `#ffa500` | Generic orange, "changes requested" | Judgment — a requested change is a denial |
| `#d93f0b` | Mirror of the `agent-failed` GitHub label | Judgment — label-mirroring is **not** a sanctioned exception; a failed run needs a human |
| `#ffd166` | Near-miss of Money | Money (`#ffc94a`) |


---

## TUI Styling Principles

### 1. Focus Illumination (De-cluttering)
Do not draw borders around every panel at all times. It creates grid claustrophobia.
* **Unfocused panels:** Borders should match the background (`--surface`) so they become invisible.
* **Focused panels:** Illuminate the border with Synth Cyan (`--accent-2`) and the title in Sunset Pink (`--accent`).

### 2. Brutalist Progress
Do not use smooth or graphical progress bars if avoidable. Use hard ASCII blocks to represent token burn and agent progress.
* **Active:** `███████▒▒▒` (Filled with `█`, empty with `▒`)
* **Colors:** Transition from Cyan (early) -> Yellow (mid) -> Pink (late stage/high burn) — `--accent-2` -> `--warn` -> `--accent`.

### 3. Context-Aware Footers
Do not show users commands they cannot use. The footer must be dynamic, showing only the hotkeys relevant to the currently focused panel.

### 4. Empty States as Atmosphere
Blank spaces are bugs. When a panel or queue is empty, fill it with atmospheric, system-level ASCII text or art.
* Example: `/// TARGET ACQUISITION \\\ > NO TARGETS IN QUEUE`

---

## ASCII Art
When introducing logos or large graphics (like in the Help Overlay or Splash Screens), use raw Python strings (`r""" """`). 
Preferred fonts for ASCII generation: **Slant, Graffiti, or ANSI Shadow.** 

Always color ASCII art using the primary Pink and Cyan accents to maintain the Synthwave identity.
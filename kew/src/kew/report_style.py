"""The evidence pack's stylesheet — kew's light theme, self-contained (#1126).

The pack is the one artifact that leaves the operator's machine and lands in a
client's inbox, so it has to read as the same product as the TUI and the web
cockpit. Everything here is derived from :data:`kew.tui.palette.LIGHT`, which is
the verbatim transcript of the light theme in ``frontend/src/app.css`` — the
brand's system of record. **No hex literal may appear in this module**;
``tests/test_report_style.py`` fails the build if one does, and also fails if
any text/background pair the stylesheet paints drops below WCAG AA (4.5:1) at
body size.

Two constraints shape the rest:

*Light only.* A pack is a printable client deliverable, not a screen surface, so
there is no ``prefers-color-scheme: dark`` branch — the dark accent ``#ff2a6d``
measures 3.62:1 on white and that is exactly the bug this module fixes.

*Offline.* The pack must stay a single file with no network fetches, so the four
IBM Plex faces are inlined as base64 woff2 (latin subsets, ~110 KB total). If
the shipped font files are unreadable the ``@font-face`` block is simply
omitted and the CSS falls back to the platform stack, the same way an
unreadable brand logo degrades to no logo.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path
from typing import Final

from kew.tui.palette import LIGHT

_SHIPPED_FONT_DIR: Final[Path] = Path(__file__).resolve().parent / "assets" / "fonts"

#: Overridable for tests; production always reads the shipped directory.
_FONT_DIR: Path = _SHIPPED_FONT_DIR

#: ``(family, weight, filename)`` for every face the pack renders in. Latin
#: subsets only — the pack's text is ASCII plus a handful of punctuation.
FONT_FACES: Final[tuple[tuple[str, int, str], ...]] = (
    ("IBM Plex Sans", 400, "ibm-plex-sans-latin-400-normal.woff2"),
    ("IBM Plex Sans", 600, "ibm-plex-sans-latin-600-normal.woff2"),
    ("IBM Plex Mono", 400, "ibm-plex-mono-latin-400-normal.woff2"),
    ("IBM Plex Mono", 600, "ibm-plex-mono-latin-600-normal.woff2"),
)

_FONT_STACKS: Final[dict[str, str]] = {
    "font-sans": "'IBM Plex Sans', ui-sans-serif, system-ui, sans-serif",
    "font-mono": "'IBM Plex Mono', ui-monospace, 'SF Mono', monospace",
}


def _face_css(family: str, weight: int, filename: str) -> str:
    """One ``@font-face`` with the woff2 inlined, or "" if it is unreadable."""
    try:
        raw = (_FONT_DIR / filename).read_bytes()
    except OSError:
        return ""
    payload = base64.b64encode(raw).decode("ascii")
    return (
        f"@font-face{{font-family:'{family}';font-style:normal;"
        f"font-weight:{weight};font-display:swap;"
        f"src:url(data:font/woff2;base64,{payload}) format('woff2');}}"
    )


@lru_cache(maxsize=1)
def font_face_css() -> str:
    """Every shipped IBM Plex face, inlined — no CDN, no network."""
    return "\n".join(filter(None, (_face_css(*face) for face in FONT_FACES)))


def _root_css() -> str:
    """The light token set as CSS custom properties, straight from app.css."""
    tokens = "".join(f"--{name}:{value};" for name, value in LIGHT.items())
    stacks = "".join(f"--{name}:{value};" for name, value in _FONT_STACKS.items())
    # Evidence packs always use kew's light accent.
    return f":root{{color-scheme:light;{tokens}{stacks}--brand-accent:var(--accent);}}"


# Layout, type and the four signals. Colour is only ever referenced through a
# token — grep this block for "#" and you should find nothing.
_LAYOUT_CSS: Final = """
* { box-sizing: border-box; }
body {
  font-family: var(--font-sans); font-size: 15px; line-height: 1.55;
  margin: 0; padding: 2rem; margin-inline: auto; max-width: 60rem;
  color: var(--fg); background: var(--bg);
  font-feature-settings: 'cv05', 'ss03';
  -webkit-font-smoothing: antialiased;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
header.brand { display: flex; align-items: center; gap: .7rem; margin: 0 0 1.25rem;
  padding-bottom: .9rem; border-bottom: 3px solid var(--brand-accent); }
header.brand .brand-logo { width: 40px; height: 40px; flex: none; }
header.brand .brand-name { font-family: var(--font-mono); font-size: 1.35rem;
  font-weight: 600; letter-spacing: -.01em; }
header.brand .brand-tag { color: var(--muted); margin-left: .5rem; font-size: .85rem;
  text-transform: uppercase; letter-spacing: .04em; }
h1, h2, h3 { font-family: var(--font-mono); letter-spacing: -.01em; }
h1 { font-size: 1.6rem; margin: 0 0 .25rem; }
h2 { font-size: 1.2rem; margin: 2rem 0 .75rem;
  border-bottom: 2px solid var(--brand-accent); padding-bottom: .3rem; }
h3 { font-size: 1rem; margin: 1.25rem 0 .5rem; }
.meta { color: var(--muted); margin: 0 0 1rem; }
.cards { display: flex; flex-wrap: wrap; gap: 1rem; margin: 1rem 0; }
.card { flex: 1 1 10rem; border: 1px solid var(--border); border-radius: 11px;
  padding: .9rem 1rem; background: var(--surface); }
.card .label { color: var(--muted); font-size: .8rem; text-transform: uppercase;
  letter-spacing: .03em; font-weight: 600; }
.card .value { font-family: var(--font-mono); font-size: 1.5rem; font-weight: 600;
  margin-top: .2rem; font-variant-numeric: tabular-nums; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1rem;
  font-size: .92rem; background: var(--surface); }
th, td { text-align: left; padding: .4rem .6rem;
  border-bottom: 1px solid var(--border); }
th { color: var(--fg-dim); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums;
  font-family: var(--font-mono); }
tr.sub td:first-child { padding-left: 1.4rem; color: var(--muted); }
/* Signals ride the fill and the border, never the text: #128a5e on its own
   wash is 4.2:1, under AA at badge size. The label already says MERGED. */
.badge { display: inline-block; padding: .05rem .45rem; border-radius: 4px;
  font-family: var(--font-mono); font-size: .8rem; font-weight: 600;
  color: var(--fg); background: var(--surface-2);
  border: 1px solid var(--border-strong); }
.badge.MERGED { border-color: var(--success);
  background: color-mix(in oklab, var(--success) 14%, var(--surface)); }
.badge.OPEN { border-color: var(--accent-2);
  background: color-mix(in oklab, var(--accent-2) 14%, var(--surface)); }
.badge.CLOSED { border-color: var(--danger);
  background: color-mix(in oklab, var(--danger) 14%, var(--surface)); }
.badge.none { border-color: var(--border-strong); background: var(--surface-2);
  color: var(--muted); }
.empty { color: var(--muted); font-style: italic; }
code { font-family: var(--font-mono); font-size: .85em; word-break: break-all; }
footer { margin-top: 2.5rem; color: var(--muted); font-size: .82rem;
  border-top: 1px solid var(--border); padding-top: 1rem; }
footer .gen-line { color: var(--fg-dim); }
a { color: var(--accent); }
@media print {
  body { padding: 0; max-width: none; background: var(--surface); }
  h2 { break-after: avoid; }
  table, tr, .card { break-inside: avoid; }
  footer { break-before: avoid; }
  a { text-decoration: underline; }
}
"""


@lru_cache(maxsize=1)
def evidence_pack_css() -> str:
    """The pack's complete stylesheet: fonts, tokens, layout, print."""
    return f"{font_face_css()}\n{_root_css()}\n{_LAYOUT_CSS}"

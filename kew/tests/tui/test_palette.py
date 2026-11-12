"""Regression guards for the single-palette rule (#1095).

Two rules, both mechanically enforced here:

1. No named ANSI colour markup anywhere in ``tui/`` or ``cli/`` — those resolve
   through the *user's* terminal theme, not kew's palette.
2. No hex literal in ``tui/`` or ``cli/`` outside ``tui/palette.py``.

Plus the parity rule: every palette constant must equal its
``frontend/src/app.css`` counterpart exactly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kew.tui import palette

_SRC = Path(palette.__file__).resolve().parents[1]
_PALETTE_FILE = Path(palette.__file__).resolve()

# A colour name held in a variable (``color = "green"``) escapes the markup grep
# but reaches the terminal the same way.
_COLOR_NAME = r"red|green|yellow|blue|magenta|cyan|white|black|bright_[a-z]+"
_BARE_NAME_RE = re.compile(rf"""(['"])({_COLOR_NAME})\1""")

# Mirrors the acceptance-criteria grep, widened to also catch closing tags,
# stacked modifiers (``[bold dim red]``, ``[/bold green]``) and — the form that
# slipped past the first pass — background colours on either side of Rich's
# ``on`` (``[red on blue]``, ``[bold white on red]``, ``[#ff2a6d on red]``).
_STYLE_WORD = r"(?:bold|dim|italic|underline|reverse|blink|strike|not)"
_ANY_COLOR = r"(?:#[0-9a-fA-F]{6}|[a-z_]+)"
_NAMED_COLOR_RE = re.compile(
    rf"\[/?(?:{_STYLE_WORD} )*"
    rf"(?:(?:{_COLOR_NAME})(?: on {_ANY_COLOR})?|{_ANY_COLOR} on (?:{_COLOR_NAME}))\]"
)
_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b")


def _terminal_surface_files() -> list[Path]:
    """Every module the palette rule governs — palette.py itself excepted.

    ``palette.py`` is the one module allowed to hold hexes, and its docstring
    quotes the banned named-colour syntax to explain the rule.
    """
    files = [
        path
        for path in sorted(_SRC.glob("tui/**/*.py")) + sorted(_SRC.glob("cli/**/*.py"))
        if path != _PALETTE_FILE
    ]
    assert files, "expected to find tui/ and cli/ modules to scan"
    return files


def _app_css() -> Path:
    """Locate ``frontend/src/app.css`` by walking up from the package."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "frontend" / "src" / "app.css"
        if candidate.is_file():
            return candidate
    pytest.skip("frontend/src/app.css not present in this checkout")


def _css_tokens(block: str) -> dict[str, str]:
    return {
        name: value.lower()
        for name, value in re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\b", block)
    }


def _css_themes() -> tuple[dict[str, str], dict[str, str]]:
    text = _app_css().read_text(encoding="utf-8")
    light_start = text.index("[data-theme='light']")
    theme_end = text.index("@theme inline", light_start)
    return (
        _css_tokens(text[:light_start]),
        _css_tokens(text[light_start:theme_end]),
    )


class TestNoTerminalPaletteColors:
    @pytest.mark.policy
    def test_no_named_color_markup_in_tui_or_cli(self) -> None:
        offenders = [
            f"{path.relative_to(_SRC)}:{lineno}: {match.group(0)}"
            for path in _terminal_surface_files()
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            )
            for match in _NAMED_COLOR_RE.finditer(line)
        ]
        assert not offenders, (
            "named colours resolve through the host terminal's theme, not kew's "
            "palette — use kew.tui.palette instead:\n" + "\n".join(offenders)
        )

    @pytest.mark.policy
    def test_no_bare_color_name_literals_in_tui_or_cli(self) -> None:
        offenders = [
            f"{path.relative_to(_SRC)}:{lineno}: {match.group(0)}"
            for path in _terminal_surface_files()
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            )
            for match in _BARE_NAME_RE.finditer(line)
        ]
        assert not offenders, (
            "a colour name in a variable reaches the terminal palette just like "
            "markup does — use kew.tui.palette instead:\n" + "\n".join(offenders)
        )

    @pytest.mark.policy
    def test_no_hex_literal_outside_the_palette_module(self) -> None:
        offenders = [
            f"{path.relative_to(_SRC)}:{lineno}: {match.group(0)}"
            for path in _terminal_surface_files()
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            )
            for match in _HEX_RE.finditer(line)
        ]
        assert not offenders, (
            "hex literals belong in kew/tui/palette.py, which mirrors "
            "frontend/src/app.css:\n" + "\n".join(offenders)
        )


class TestNamedColorPattern:
    """The guard is only as good as its pattern — pin what it must catch."""

    @pytest.mark.parametrize(
        "tag",
        [
            "[red]",
            "[bold red]",
            "[bold dim red]",
            "[/bold green]",
            "[bright_magenta]",
            # Background colours — the form that let `[bold white on red]`
            # survive the first migration.
            "[red on blue]",
            "[bold white on red]",
            "[/bold white on red]",
            "[#ff2a6d on red]",
            "[white on #0d0221]",
        ],
    )
    def test_catches_named_colors(self, tag: str) -> None:
        assert _NAMED_COLOR_RE.search(tag), f"{tag} should be rejected"

    @pytest.mark.parametrize(
        "tag",
        ["[bold]", "[dim]", "[/bold]", "[#ff2a6d]", "[dim #8b7fa8]", "[/dim #8b7fa8]"],
    )
    def test_allows_palette_and_modifier_only_tags(self, tag: str) -> None:
        assert not _NAMED_COLOR_RE.search(tag), f"{tag} should be allowed"


class TestAppCssParity:
    def test_dark_tokens_match_app_css(self) -> None:
        dark, _ = _css_themes()
        assert {k: v for k, v in dark.items() if k in palette.DARK} == palette.DARK
        assert set(palette.DARK) == set(dark), "palette and app.css token sets differ"

    def test_light_tokens_match_app_css(self) -> None:
        _, light = _css_themes()
        assert light == palette.LIGHT

    def test_constants_are_the_dark_theme(self) -> None:
        assert palette.DARK["surface"] == palette.SURFACE
        assert palette.DARK["muted"] == palette.MUTED
        assert palette.DARK["faint"] == palette.FAINT
        assert palette.DARK["fg"] == palette.FG


class TestSemanticContract:
    @pytest.mark.parametrize(
        ("signal", "dark", "light"),
        [
            ("JUDGMENT", "#ff2a6d", "#c81e5a"),
            ("MACHINE", "#05d9e8", "#0892a5"),
            ("MONEY", "#ffc94a", "#b5790e"),
            ("ATTESTED", "#2ee6a6", "#128a5e"),
        ],
    )
    def test_four_signals_carry_their_contract_values(
        self, signal: str, dark: str, light: str
    ) -> None:
        _, light_theme = _css_themes()
        token = {
            "JUDGMENT": "accent",
            "MACHINE": "accent-2",
            "MONEY": "warn",
            "ATTESTED": "success",
        }[signal]
        assert getattr(palette, signal) == dark
        assert light_theme[token] == light

    @pytest.mark.policy
    def test_retired_off_palette_values_are_gone(self) -> None:
        """#ffa500, #d93f0b, #ffd166 and #ff7a59 were reconciled away.

        Checked against the palette contract *and* the surfaces that consume
        it — a retired value readmitted to ``DARK``/``LIGHT`` would sail past a
        scan of the call sites, since those hold constants rather than hexes.
        """
        retired = {"#ffa500", "#d93f0b", "#ffd166", "#ff7a59"}
        in_contract = {
            value.lower()
            for theme in (palette.DARK, palette.LIGHT)
            for value in theme.values()
        }
        in_call_sites = {
            match.group(0).lower()
            for path in _terminal_surface_files()
            for match in _HEX_RE.finditer(path.read_text(encoding="utf-8"))
        }
        assert not (retired & (in_contract | in_call_sites))


class TestCssExpansion:
    def test_expands_known_tokens(self) -> None:
        assert (
            palette.expand_css("background: $surface;")
            == f"background: {palette.SURFACE};"
        )

    def test_leaves_textual_variables_untouched(self) -> None:
        assert palette.expand_css("color: $text-muted;") == "color: $text-muted;"

    def test_app_css_block_has_no_unexpanded_kew_tokens(self) -> None:
        from kew.tui.app import KewApp

        assert "$" not in KewApp.CSS
        assert palette.SURFACE in KewApp.CSS

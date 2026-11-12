"""The evidence pack's stylesheet is on-palette, legible and offline (#1126).

The pack is the one artifact that leaves the operator's machine, so its colours
must come from the light theme in ``frontend/src/app.css`` (transcribed in
``kew.tui.palette.LIGHT``) and every text/background pair must clear WCAG AA at
body size. These tests are the enforcement.
"""

from __future__ import annotations

import re

import pytest

from kew import report_style
from kew.report_style import FONT_FACES, evidence_pack_css, font_face_css
from kew.tui.palette import LIGHT

_HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")

#: Tokens that fail 4.5:1 on the pack's canvas. They may tint a fill or draw a
#: border; they may never be a text colour. The pairing of the two tests below
#: keeps this list honest: a token that starts clearing AA fails the second one
#: and gets promoted.
_SUB_AA = ("faint", "success", "warn", "accent-2", "border-strong")

#: Every (text token, background token) pair the stylesheet actually paints.
_TEXT_PAIRS = (
    ("fg", "bg"),
    ("fg", "surface"),
    ("fg", "surface-2"),
    ("fg-dim", "bg"),
    ("fg-dim", "surface"),
    ("muted", "bg"),
    ("muted", "surface"),
    ("muted", "surface-2"),
    ("accent", "bg"),
    ("accent", "surface"),
)


def _channel(value: float) -> float:
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(hex_color: str) -> float:
    raw = hex_color.lstrip("#")
    r, g, b = (_channel(int(raw[i : i + 2], 16) / 255) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    """WCAG 2.1 contrast ratio between two ``#rrggbb`` colours."""
    light, dark = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _squash(css: str) -> str:
    """Drop whitespace so declarations match regardless of formatting."""
    return re.sub(r"\s+", "", css)


class TestPaletteConformance:
    def test_stylesheet_carries_no_off_palette_hex(self):
        found = {h.lower() for h in _HEX.findall(evidence_pack_css())}
        allowed = {v.lower() for v in LIGHT.values()}
        assert found <= allowed, f"off-palette: {sorted(found - allowed)}"

    def test_every_light_token_is_declared_as_a_custom_property(self):
        css = _squash(evidence_pack_css())
        missing = [n for n, v in LIGHT.items() if f"--{n}:{v}" not in css]
        assert missing == []

    def test_dark_theme_accent_is_not_used_on_the_light_ground(self):
        # #ff2a6d is the dark-theme accent; on white it measures 3.62:1.
        assert "#ff2a6d" not in evidence_pack_css().lower()

    def test_retired_ad_hoc_colours_are_gone(self):
        css = evidence_pack_css().lower()
        for retired in ("#1c5fd0", "#1e7e34", "#b71c1c", "#e6f4ea", "#9aa0aa"):
            assert retired not in css

    def test_neutrals_carry_the_violet_bias(self):
        # A pure grey has r == g == b; kew's neutrals must not.
        for name in ("bg", "surface", "border", "muted", "fg"):
            raw = LIGHT[name].lstrip("#")
            r, g, b = (int(raw[i : i + 2], 16) for i in (0, 2, 4))
            assert not (r == g == b), f"{name} is a pure grey"


class TestContrast:
    @pytest.mark.parametrize(("fg", "bg"), _TEXT_PAIRS)
    def test_text_pairs_meet_wcag_aa_body_text(self, fg, bg):
        assert contrast(LIGHT[fg], LIGHT[bg]) >= 4.5

    @pytest.mark.parametrize("token", _SUB_AA)
    def test_sub_aa_tokens_are_never_a_text_colour(self, token):
        # Anchored so "border-color:" does not read as a "color:" declaration.
        used = re.search(rf"[;{{]color:var\(--{token}\)", _squash(evidence_pack_css()))
        assert used is None

    @pytest.mark.parametrize("token", _SUB_AA)
    def test_sub_aa_tokens_really_are_sub_aa(self, token):
        assert contrast(LIGHT[token], LIGHT["bg"]) < 4.5

    def test_the_attestation_accent_clears_aa(self):
        # The single most important element in the document.
        assert contrast(LIGHT["accent"], LIGHT["bg"]) >= 4.5


class TestFonts:
    def test_every_face_is_inlined_as_base64_woff2(self):
        css = font_face_css()
        assert css.count("@font-face") == len(FONT_FACES)
        assert css.count("data:font/woff2;base64,") == len(FONT_FACES)

    def test_faces_fetch_nothing(self):
        # Checked on the src() urls, not the whole string: a base64 payload can
        # contain the letters "http" by chance.
        urls = re.findall(r"url\(([^)]*)\)", font_face_css())
        assert urls
        assert all(u.startswith("data:font/woff2;base64,") for u in urls)

    def test_type_is_ibm_plex(self):
        css = evidence_pack_css()
        assert "IBM Plex Sans" in css
        assert "IBM Plex Mono" in css
        assert "SFMono-Regular" not in css

    def test_payload_stays_within_budget(self):
        # Four latin-subset faces; ~110 KB of base64 on a client deliverable.
        assert len(font_face_css()) < 150_000

    def test_missing_font_files_degrade_to_the_fallback_stack(self, tmp_path):
        font_face_css.cache_clear()
        try:
            report_style._FONT_DIR = tmp_path
            assert font_face_css() == ""
        finally:
            report_style._FONT_DIR = report_style._SHIPPED_FONT_DIR
            font_face_css.cache_clear()


class TestPrintStylesheet:
    def test_print_block_survives(self):
        css = _squash(evidence_pack_css())
        assert "@mediaprint" in css
        assert "break-after:avoid" in css
        assert "break-inside:avoid" in css

    def test_backgrounds_print(self):
        # Badge tints must survive the browser's "no backgrounds" default.
        assert "print-color-adjust:exact" in _squash(evidence_pack_css())

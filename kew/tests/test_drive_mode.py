"""The single shared loop_style -> drive_mode mapping (UI vocab, no drift)."""

from kew.runners.protocol import loop_style_to_drive_mode


def test_native_maps_to_delegated():
    assert loop_style_to_drive_mode("native") == "delegated"

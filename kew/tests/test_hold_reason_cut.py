"""The hold-reason cap cuts between words, never inside one (#2847 P10).

The live Monitor's needs-you caption printed ``the conflict...lve executed,
exit 1)``: the cap kept a fixed number of characters either side of its
ellipsis, and the tail began in the middle of ``resolve``.
"""

from __future__ import annotations

from kew.epic_holds import HOLD_REASON_MAX_CHARS, safe_hold_reason

LIVE = (
    "resolve_refused: the conflict on agent/issue-2766 could not be resolved by the "
    "agent because the rebase stopped twice on the same hunk in merge_queue.py and "
    "the second attempt gave up without a patch (the resolve executed, exit 1)"
)


def _sides(capped: str) -> tuple[list[str], list[str]]:
    head, tail = capped.split("...")
    return head.split(), tail.split()


def test_the_live_reason_is_cut_between_words_on_both_sides() -> None:
    capped = safe_hold_reason(LIVE)
    words = set(LIVE.split())

    assert len(LIVE) > HOLD_REASON_MAX_CHARS, "this reason must pass the cap"
    assert len(capped) <= HOLD_REASON_MAX_CHARS
    head, tail = _sides(capped)
    assert head[0] == "resolve_refused:", "the prefix that routes the hold survives"
    assert head[-1] in words, f"the head ends inside a word: {capped!r}"
    assert tail[0] in words, f"the tail starts inside a word: {capped!r}"
    assert capped.endswith("(the resolve executed, exit 1)")


def test_a_cut_reason_is_stable_when_re_flattened() -> None:
    capped = safe_hold_reason(LIVE)

    assert safe_hold_reason(capped) == capped


def test_a_path_keeps_its_own_segments_either_side_of_the_cut() -> None:
    path = "/scratch/" + "/".join(f"segment{n:02d}" for n in range(30)) + "/pr-2352"
    capped = safe_hold_reason(f"publication_held: {path}")
    segments = set(path.split("/"))

    head, tail = capped.split("...")
    assert head.startswith("publication_held: /scratch/")
    assert head.endswith("/"), f"the head ends inside a segment: {capped!r}"
    assert tail.startswith("/"), f"the tail starts inside a segment: {capped!r}"
    assert all(part in segments for part in tail.split("/"))
    assert capped.endswith("/pr-2352")


def test_one_unbroken_token_is_still_capped() -> None:
    capped = safe_hold_reason("x" * 400)

    assert len(capped) <= HOLD_REASON_MAX_CHARS
    assert capped.startswith("x") and capped.endswith("x")
    assert "..." in capped

import re
import shutil
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from kew.runners.capabilities import (
    RUNNER_CAPABILITIES,
    ContainmentRequirement,
    CostSignal,
)
from kew.runners.registry import KNOWN_RUNNER_NAMES, get_runner, runner_capabilities

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNERS_DOC = REPO_ROOT / "docs/runners/index.md"
GETTING_STARTED_DOC = REPO_ROOT / "docs/getting-started.md"
MANIFEST_HEADING = "## Runner capability manifest"
MANIFEST_LINK = "runners/index.md#runner-capability-manifest"
MANIFEST_COLUMNS = (
    "runner",
    "resume",
    "telemetry",
    "cost signal",
    "loop behavior",
    "containment requirement",
)


@pytest.fixture
def copied_docs(tmp_path: Path) -> tuple[Path, Path]:
    docs_dir = tmp_path / "docs"
    runners_dir = docs_dir / "runners"
    runners_dir.mkdir(parents=True)
    runners_doc = runners_dir / "index.md"
    getting_started_doc = docs_dir / "getting-started.md"
    shutil.copy2(RUNNERS_DOC, runners_doc)
    shutil.copy2(GETTING_STARTED_DOC, getting_started_doc)
    return runners_doc, getting_started_doc


def _section(text: str, heading: str) -> str:
    headings = list(re.finditer(rf"^{re.escape(heading)}\s*$", text, re.MULTILINE))
    assert len(headings) == 1, f"expected one {heading!r} heading"
    start = headings[0].end()
    following = re.search(r"^##\s+", text[start:], re.MULTILINE)
    end = start + following.start() if following else len(text)
    return text[start:end]


def _code_values(cell: str) -> tuple[str, ...]:
    return tuple(re.findall(r"`([^`]+)`", cell))


def _single_code_value(cell: str) -> str:
    values = _code_values(cell)
    assert len(values) == 1, f"expected one exact value in {cell!r}"
    return values[0]


def _documented_manifest(path: Path) -> dict[str, tuple[object, ...]]:
    section = _section(path.read_text(), MANIFEST_HEADING)
    table_lines = [line for line in section.splitlines() if line.startswith("|")]
    assert len(table_lines) >= 3, "capability manifest table is missing"
    table = [
        tuple(cell.strip() for cell in line.strip("|").split("|"))
        for line in table_lines
    ]
    assert tuple(cell.casefold() for cell in table[0]) == MANIFEST_COLUMNS
    assert all(re.fullmatch(r":?-{3,}:?", cell) for cell in table[1])

    rows: list[tuple[object, ...]] = []
    for cells in table[2:]:
        assert len(cells) == len(MANIFEST_COLUMNS)
        rows.append(
            (
                _single_code_value(cells[0]),
                _single_code_value(cells[1]),
                _code_values(cells[2]),
                _single_code_value(cells[3]),
                _single_code_value(cells[4]),
                _single_code_value(cells[5]),
            )
        )

    runner_names = [str(row[0]) for row in rows]
    assert len(runner_names) == len(set(runner_names)), "duplicate runner row"
    assert set(runner_names) == set(RUNNER_CAPABILITIES), "runner rows differ"
    return {str(row[0]): row for row in rows}


def _assert_manifest_matches_runtime(path: Path) -> None:
    documented = _documented_manifest(path)
    for runner, capabilities in RUNNER_CAPABILITIES.items():
        assert documented[runner] == (
            capabilities.runner,
            capabilities.resume,
            capabilities.telemetry,
            capabilities.cost.value,
            capabilities.loop,
            capabilities.containment.value,
        )


def _assert_manifest_qualifications(path: Path) -> None:
    text = path.read_text()
    section = " ".join(_section(text, MANIFEST_HEADING).casefold().split())
    for qualification in (
        "adapter declarations",
        "not current route readiness",
        "does not certify every first-class kew loop path",
        "native containment does not imply container execution",
        "unpriced local inference does not mean zero operating cost",
        "unavailable native dollar total does not mean absent token accounting",
        "does not replay prior transcript state",
    ):
        assert qualification in section
    for separate_owner in (
        "verification",
        "review",
        "publication",
        "metering",
        "tracker workflow",
        "exclusion evidence",
    ):
        assert separate_owner in section

    normalized_page = " ".join(text.casefold().split())
    assert "every runner runs inside a container" not in normalized_page
    assert (
        "a runner does not run as a host subprocess — it runs inside a container"
        not in normalized_page
    )


def _assert_first_dispatch_link(runners_doc: Path, getting_started_doc: Path) -> None:
    first_dispatch = _section(
        getting_started_doc.read_text(), '!!! warning "Before the first dispatch"'
    )
    targets = re.findall(r"\[[^]]+\]\(([^)]+)\)", first_dispatch)
    matches = [target for target in targets if "runner-capability-manifest" in target]
    assert matches == [MANIFEST_LINK]
    page, anchor = matches[0].split("#", maxsplit=1)
    assert (getting_started_doc.parent / page).resolve() == runners_doc.resolve()
    expected_anchor = MANIFEST_HEADING.removeprefix("## ").casefold().replace(" ", "-")
    assert anchor == expected_anchor
    assert MANIFEST_HEADING in runners_doc.read_text()


def _write_mutation(path: Path, old: str, new: str) -> str:
    original = path.read_text()
    assert original.count(old) == 1, f"mutation target must occur once: {old!r}"
    path.write_text(original.replace(old, new))
    return original


def test_capability_manifest_covers_every_registered_runner() -> None:
    manifests = {name: runner_capabilities(name) for name in KNOWN_RUNNER_NAMES}

    assert set(manifests) == set(KNOWN_RUNNER_NAMES)
    assert set(RUNNER_CAPABILITIES) == set(KNOWN_RUNNER_NAMES)
    assert all(manifest.runner == name for name, manifest in manifests.items())
    assert all(
        get_runner(name).capabilities is manifest  # type: ignore[attr-defined]
        for name, manifest in manifests.items()
    )
    assert all(manifest.resume == "session" for manifest in manifests.values())
    assert manifests["codex"].containment is ContainmentRequirement.NATIVE
    assert manifests["lemonade"].containment is ContainmentRequirement.UNAVAILABLE
    assert manifests["claude-code"].cost is CostSignal.NATIVE_TOTAL
    assert manifests["hermes"].cost is CostSignal.ESTIMATED_TOTAL


def test_capability_manifest_is_immutable_and_unknown_runner_refuses() -> None:
    manifest = runner_capabilities("codex")

    with pytest.raises(FrozenInstanceError):
        manifest.resume = "none"  # type: ignore[misc]
    with pytest.raises(ValueError, match="Unknown runner"):
        runner_capabilities("reference-runner")


def test_runner_manifest_does_not_claim_route_owned_capabilities() -> None:
    manifest = runner_capabilities("claude-code")

    for route_owned in (
        "tracker_workflow",
        "verification",
        "independent_review",
        "publication",
        "metering",
        "exclusions",
    ):
        assert not hasattr(manifest, route_owned)


def test_documented_runner_capabilities_match_runtime_declarations() -> None:
    _assert_manifest_matches_runtime(RUNNERS_DOC)
    _assert_manifest_qualifications(RUNNERS_DOC)


def test_documented_manifest_rejects_a_wrong_value(
    copied_docs: tuple[Path, Path],
) -> None:
    runners_doc, _ = copied_docs
    original = _write_mutation(runners_doc, "`native_total`", "`unavailable`")
    try:
        with pytest.raises(AssertionError):
            _assert_manifest_matches_runtime(runners_doc)
    finally:
        runners_doc.write_text(original)
    _assert_manifest_matches_runtime(runners_doc)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "additional"])
def test_documented_manifest_rejects_runner_set_drift(
    copied_docs: tuple[Path, Path], mutation: str
) -> None:
    runners_doc, _ = copied_docs
    original = runners_doc.read_text()
    lemonade_row = next(
        line
        for line in original.splitlines(keepends=True)
        if line.startswith("| `lemonade`")
    )
    claude_row = next(
        line
        for line in original.splitlines(keepends=True)
        if line.startswith("| `claude-code`")
    )
    if mutation == "missing":
        changed = original.replace(lemonade_row, "")
    elif mutation == "duplicate":
        changed = original.replace(claude_row, claude_row * 2)
    else:
        changed = original.replace(
            lemonade_row,
            lemonade_row.replace("`lemonade`", "`reference-runner`"),
        )

    try:
        runners_doc.write_text(changed)
        with pytest.raises(AssertionError):
            _assert_manifest_matches_runtime(runners_doc)
    finally:
        runners_doc.write_text(original)
    _assert_manifest_matches_runtime(runners_doc)


def test_runner_docs_reject_a_blanket_container_claim(
    copied_docs: tuple[Path, Path],
) -> None:
    runners_doc, _ = copied_docs
    original = runners_doc.read_text()
    changed = original.replace(
        MANIFEST_HEADING,
        f"{MANIFEST_HEADING}\n\nEvery runner runs inside a container.",
    )
    assert changed != original
    try:
        runners_doc.write_text(changed)
        with pytest.raises(AssertionError):
            _assert_manifest_qualifications(runners_doc)
    finally:
        runners_doc.write_text(original)
    _assert_manifest_qualifications(runners_doc)


def test_first_dispatch_links_to_the_capability_manifest() -> None:
    _assert_first_dispatch_link(RUNNERS_DOC, GETTING_STARTED_DOC)


def test_first_dispatch_rejects_a_broken_manifest_link(
    copied_docs: tuple[Path, Path],
) -> None:
    runners_doc, getting_started_doc = copied_docs
    original = _write_mutation(
        getting_started_doc,
        MANIFEST_LINK,
        "runners/index.md#missing-capability-manifest",
    )
    try:
        with pytest.raises(AssertionError):
            _assert_first_dispatch_link(runners_doc, getting_started_doc)
    finally:
        getting_started_doc.write_text(original)
    _assert_first_dispatch_link(runners_doc, getting_started_doc)

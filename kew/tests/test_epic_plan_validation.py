"""Dispatchability Gate: manifest-level <=10-target-files enforcement (Node I5)."""

from __future__ import annotations

from typing import Any

from kew.graph.plan import (
    EpicPlanManifest,
    build_resplit_instruction,
    oversized_issues,
    validate_manifest,
)


def _criteria(lid: str) -> str:
    """Two item-specific criteria — the planning contract's minimum (#2292)."""
    return (
        "\n\n## Acceptance Criteria\n\n"
        f"- [ ] `step_{lid}()` returns the parsed row "
        f"(`uv run pytest tests/test_{lid}.py -k parsed`)\n"
        f"- [ ] `kew epic plan --dry-run` lists node {lid} in its wave "
        "(`uv run kew epic plan demo --dry-run`)\n"
    )


def _issue(lid: str, deps: list[str] | None = None, **over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "local_id": lid,
        "title": f"feat: part {lid} (Tasks 1-2)",
        "body": (
            f"## Plan Reference\n\n**Plan file:** `docs/design/x.md`\n\npart {lid}"
            + _criteria(lid)
        ),
        "priority": "p2-medium",
        "complexity": "simple",
        "type": "feature",
        "depends_on": deps or [],
    }
    d.update(over)
    return d


def _manifest(issues: list[dict[str, Any]]) -> dict[str, Any]:
    return {"epic": "demo", "plan_path": "docs/design/x.md", "issues": issues}


def test_validate_manifest_rejects_oversized_node() -> None:
    files = [f"src/f{i}.py" for i in range(11)]
    m = EpicPlanManifest.model_validate(_manifest([_issue("a", target_files=files)]))
    problems = validate_manifest(m)
    assert any("re-split" in p and "'a'" in p and "11" in p for p in problems), problems


def test_validate_manifest_accepts_within_limit() -> None:
    files = [f"src/f{i}.py" for i in range(10)]
    m = EpicPlanManifest.model_validate(_manifest([_issue("a", target_files=files)]))
    assert validate_manifest(m) == []


def test_oversized_issues_flags_only_over_limit() -> None:
    over = [f"src/f{i}.py" for i in range(11)]
    within = [f"src/g{i}.py" for i in range(10)]
    m = EpicPlanManifest.model_validate(
        _manifest(
            [
                _issue("a", target_files=over),
                _issue("b", target_files=within),
                _issue("c", target_files=over),
            ]
        )
    )
    assert [i.local_id for i in oversized_issues(m)] == ["a", "c"]


def test_build_resplit_instruction_names_offending_nodes() -> None:
    files = [f"src/f{i}.py" for i in range(11)]
    m = EpicPlanManifest.model_validate(_manifest([_issue("a", target_files=files)]))
    instr = build_resplit_instruction(oversized_issues(m))
    assert "re-split" in instr.lower()
    assert "'a'" in instr
    assert "11 target_files" in instr

"""What leaves the public snapshot, and that its own CI never misses it.

The public repository is a `git archive` of private `main`
(scripts/make-public-snapshot.sh). A path leaves the snapshot through an
``export-ignore`` line in ``.gitattributes``, and two things can go wrong:

* a misspelt line matches nothing, so the file it meant to drop is published;
* a test or a workflow reads a dropped file, so the public repository's first
  CI run fails on a file it does not have.

Every listed path must exist here. In the snapshot itself none of them does
(``git archive`` dropped them all); a tree holding some and not others has a
stale line. The snapshot script also refuses a line that leaves nothing out.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.policy

REPO_ROOT = Path(__file__).resolve().parents[2]
ATTRIBUTES = REPO_ROOT / ".gitattributes"

# Where a read of a dropped path would break the snapshot's CI: the tests, the
# workflows and every script or package manifest a gate runs.
_READER_GLOBS = (
    "kew/tests/**/*.py",
    "kew/tests/**/*.json",
    "kew/scripts/*",
    "scripts/*",
    ".github/workflows/*.yml",
    ".github/scripts/*",
    "www/tests/*.mjs",
    "www/scripts/*.mjs",
    "www/package.json",
    "www/deploy.sh",
    "frontend/package.json",
    "frontend/src/**/*.test.ts",
    "frontend/scripts/*.test.ts",
    "mkdocs.yml",
)


def _attributes_text() -> str:
    return ATTRIBUTES.read_text(encoding="utf-8")


def _attribute_lines(text: str | None = None) -> list[tuple[str, str]]:
    """(the comment block above it, path) for every export-ignore line."""
    entries: list[tuple[str, str]] = []
    comment: list[str] = []
    for raw in (_attributes_text() if text is None else text).splitlines():
        line = raw.strip()
        if line.startswith("#"):
            comment.append(line)
            continue
        fields = line.split()
        if "export-ignore" in fields[1:]:
            entries.append(("\n".join(comment), fields[0]))
        # A blank line or any other entry ends the comment that explains one.
        comment = []
    return entries


def _ignored_paths() -> list[str]:
    return [path for _, path in _attribute_lines()]


def _dropped(rel: str) -> bool:
    """Whether the snapshot leaves *rel* out: it or a directory above it is
    named by an export-ignore line."""
    return any(rel == path or rel.startswith(path + "/") for path in _ignored_paths())


#: The one file that runs in this checkout alone and names a dropped path on
#: purpose: the snapshot script runs this checkout's private-name scans
#: against the snapshot it exported.
SNAPSHOT_SCRIPT = "scripts/make-public-snapshot.sh"
PRIVATE_SCANS = "kew/tests/test_private_names.py"


def _reader_texts() -> dict[str, str]:
    """Every file a gate runs or reads that the snapshot carries, by path.
    A dropped file is no reader: the snapshot's CI never runs it."""
    this_file = Path(__file__).resolve()
    texts: dict[str, str] = {}
    for pattern in _READER_GLOBS:
        for path in sorted(REPO_ROOT.glob(pattern)):
            if not path.is_file() or path.resolve() == this_file:
                continue
            if _dropped(path.relative_to(REPO_ROOT).as_posix()):
                continue
            try:
                texts[path.relative_to(REPO_ROOT).as_posix()] = path.read_text(
                    encoding="utf-8"
                )
            except UnicodeDecodeError:
                continue
    return texts


def test_the_snapshot_leaves_something_out() -> None:
    # An empty list would pass every check below while the snapshot carried
    # everything this file exists to drop.
    assert _ignored_paths(), ".gitattributes marks nothing export-ignore"


def _assert_plain_paths_with_reasons(entries: list[tuple[str, str]]) -> None:
    for reason, path in entries:
        assert reason, f"{path}: say why it leaves the snapshot, in a comment above"
        assert not re.search(r"[*?\[\]!\\]", path), f"{path}: name one path, no glob"
        assert not path.startswith("/") and not path.endswith("/"), path


def test_every_export_ignore_line_names_a_plain_path_with_its_reason() -> None:
    _assert_plain_paths_with_reasons(_attribute_lines())


@pytest.mark.parametrize(
    "text",
    [
        "# reason\nkept.md -diff\n\nwww/notes.md export-ignore\n",
        "# reason\nwww/*.md export-ignore\n",
        "# reason\nwww/notes/ export-ignore\n",
    ],
    ids=["no reason above it", "a glob", "a trailing slash"],
)
def test_a_line_without_a_reason_or_naming_a_glob_is_refused(text: str) -> None:
    with pytest.raises(AssertionError):
        _assert_plain_paths_with_reasons(_attribute_lines(text))


def test_every_export_ignore_path_exists_unless_this_is_the_snapshot() -> None:
    present = {path: (REPO_ROOT / path).exists() for path in _ignored_paths()}
    if not any(present.values()):
        # The exported snapshot: `git archive` dropped every listed path.
        return
    missing = sorted(path for path, exists in present.items() if not exists)
    assert missing == [], (
        "export-ignore lines name paths this tree does not have; a stale or "
        f"misspelt line drops nothing: {missing}"
    )


def test_no_test_or_workflow_reads_a_path_the_snapshot_drops() -> None:
    texts = _reader_texts()
    # The reader set must include the gates this rule protects.
    assert ".github/workflows/ci.yml" in texts
    assert "kew/tests/conftest.py" in texts
    readers = sorted(
        f"{rel} reads {path}"
        for path in _ignored_paths()
        for rel, text in texts.items()
        if path in text and (rel, path) != (SNAPSHOT_SCRIPT, PRIVATE_SCANS)
    )
    assert readers == [], "the public snapshot would miss these files:\n" + (
        "\n".join(readers)
    )


def _script_text() -> str:
    return (REPO_ROOT / SNAPSHOT_SCRIPT).read_text(encoding="utf-8")


def test_the_private_scans_leave_the_snapshot() -> None:
    """The file that lists what the snapshot must not carry is not in it."""
    assert PRIVATE_SCANS in _ignored_paths()


# --- the snapshot script, run against a scratch repository ------------------
#
# A process boundary: the script runs `uv` (the scans) and `gitleaks`, so stubs
# on PATH stand in for them and record what they were asked to run. `git`,
# `bash` and the coreutils are real; a machine without them fails this test,
# it does not skip it.

_UV_STUB = """#!/bin/bash
printf '%s\\t%s\\t%s\\t%s\\n' "${KEW_TREE_SCAN_ROOT:-}" \\
    "${KEW_TREE_SCAN_EXPECT_FILES:-}" "$PWD" "$*" >> "$UV_LOG"
if [ -n "${UV_FAIL_ON:-}" ] && [[ "$*" == *"$UV_FAIL_ON"* ]]; then exit 1; fi
if [ -n "${UV_LEAVES_A_FILE:-}" ] && [ -n "${KEW_TREE_SCAN_ROOT:-}" ]; then
    echo stray > "$KEW_TREE_SCAN_ROOT/left-behind.txt"
fi
exit 0
"""
_GITLEAKS_STUB = """#!/bin/bash
if [ "$1" = version ]; then echo 8.30.0; exit 0; fi
# What it was asked to scan, and whether that is a commit with its config.
source=""
config=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
    case "${args[$i]}" in
        --source) source="${args[$((i + 1))]}" ;;
        --config) config="${args[$((i + 1))]}" ;;
    esac
done
state=no-commit
if [ -f "$config" ] && git -C "$source" rev-parse -q --verify HEAD > /dev/null 2>&1 \\
    && [ "$(git -C "$source" rev-parse --show-toplevel)" = "$source" ]; then
    state=commit
fi
printf '%s\\t%s\\n' "$state" "$*" >> "$GITLEAKS_LOG"
if [ -n "${GITLEAKS_FINDS_A_LEAK:-}" ]; then exit 1; fi
exit 0
"""
_IDENTITY = (
    "-c",
    "user.name=snapshot-test",
    "-c",
    "user.email=snapshot-test@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - fixed git argv in a scratch repository
        ["git", *_IDENTITY, *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _snapshot_repo(
    tmp_path: Path, *, private: bool = True, ignore: bool = True
) -> Path:
    """A repository on `main` holding what the script reads."""
    repo = tmp_path / "repo"
    files = {
        "README.md": "# scratch\n",
        ".gitleaks.toml": "[extend]\nuseDefault = true\n",
        "kew/scripts/verify.sh": 'GITLEAKS_VERSION="8.30.0"\n',
        "kew/tests/test_tracked_tree.py": "# public rules\n",
        ".gitattributes": (
            f"# Maintainer-only scan data.\n{PRIVATE_SCANS} export-ignore\n"
            if ignore
            else "# nothing left out\n"
        ),
    }
    if private:
        files[PRIVATE_SCANS] = "# private scans\n"
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    script = repo / SNAPSHOT_SCRIPT
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(_script_text())
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "scratch")
    return repo


def _run_snapshot(
    tmp_path: Path, repo: Path, **env: str
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("uv", _UV_STUB), ("gitleaks", _GITLEAKS_STUB)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "uv.log"
    out = tmp_path / "out"
    variables = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "UV_LOG": str(log),
        "GITLEAKS_LOG": str(tmp_path / "gitleaks.log"),
        **env,
    }
    result = subprocess.run(  # noqa: S603 - the script under test, fixed argv
        ["bash", str(repo / SNAPSHOT_SCRIPT), str(out)],  # noqa: S607
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=variables,
        check=False,
        timeout=120,
    )
    calls = (
        [line.split("\t") for line in log.read_text().splitlines()]
        if log.exists()
        else []
    )
    return result, calls, out


def test_the_script_runs_the_private_scans_against_the_snapshot(
    tmp_path: Path,
) -> None:
    repo = _snapshot_repo(tmp_path)
    result, calls, out = _run_snapshot(tmp_path, repo)
    assert result.returncode == 0, result.stdout + result.stderr
    root, snapshot = str(repo.resolve()), str(out.resolve())
    # The private scans: this repository's copy, the override on the snapshot,
    # and the file count the archive wrote (six: every file but the private
    # scans).
    private, tracked_tree = calls
    assert private[0] == snapshot and private[2] == f"{root}/kew"
    assert private[3].endswith(f" {root}/{PRIVATE_SCANS}"), private[3]
    assert private[1] == "6", private
    # The snapshot's own public rules, against itself.
    assert tracked_tree[0] == "", "the snapshot's copy reads its own tree"
    assert tracked_tree[3].endswith(f" {snapshot}/kew/tests/test_tracked_tree.py")
    assert not (out / PRIVATE_SCANS).exists()


def test_the_script_refuses_a_checkout_without_the_private_scans(
    tmp_path: Path,
) -> None:
    result, calls, _ = _run_snapshot(tmp_path, _snapshot_repo(tmp_path, private=False))
    assert result.returncode != 0
    assert f"{PRIVATE_SCANS} is missing" in result.stderr
    assert calls == []


def test_the_script_refuses_an_archive_that_carries_the_private_scans(
    tmp_path: Path,
) -> None:
    result, calls, _ = _run_snapshot(tmp_path, _snapshot_repo(tmp_path, ignore=False))
    assert result.returncode != 0
    assert f"the archive holds {PRIVATE_SCANS}" in result.stderr
    assert calls == []


def test_a_failing_private_scan_fails_the_script(tmp_path: Path) -> None:
    repo = _snapshot_repo(tmp_path)
    result, calls, _ = _run_snapshot(tmp_path, repo, UV_FAIL_ON=PRIVATE_SCANS)
    assert result.returncode != 0
    assert len(calls) == 1 and calls[0][3].endswith(PRIVATE_SCANS)
    assert "Snapshot of" not in result.stdout


def _gitleaks_runs(tmp_path: Path) -> list[str]:
    log = tmp_path / "gitleaks.log"
    return log.read_text().splitlines() if log.exists() else []


def test_the_script_scans_the_snapshot_for_secrets(tmp_path: Path) -> None:
    result, _, out = _run_snapshot(tmp_path, _snapshot_repo(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    (run,) = _gitleaks_runs(tmp_path)
    state, argv = run.split("\t")
    snapshot = str(out.resolve())
    assert argv.startswith("detect ")
    assert f"--source {snapshot} " in argv
    assert f"--config {snapshot}/.gitleaks.toml" in argv
    # A found secret is never printed in the script's output.
    assert "--redact" in argv.split()
    # The scan ran on the snapshot's own commit, not on a bare directory that
    # a repository above it would answer for.
    assert state == "commit"


def test_a_secret_in_the_snapshot_fails_the_script(tmp_path: Path) -> None:
    result, _, _ = _run_snapshot(
        tmp_path, _snapshot_repo(tmp_path), GITLEAKS_FINDS_A_LEAK="1"
    )
    assert result.returncode != 0
    assert _gitleaks_runs(tmp_path), "the secret scan did not run"
    assert "Snapshot of" not in result.stdout


def test_a_check_that_leaves_a_file_in_the_snapshot_fails_the_script(
    tmp_path: Path,
) -> None:
    result, _, out = _run_snapshot(
        tmp_path, _snapshot_repo(tmp_path), UV_LEAVES_A_FILE="1"
    )
    assert (out / "left-behind.txt").exists()
    assert result.returncode != 0
    assert "the checks changed" in result.stderr
    assert "Snapshot of" not in result.stdout


def _quality_ledger() -> dict:
    return json.loads((REPO_ROOT / "kew/ratchets/test_quality.json").read_text())


def test_the_quality_ledger_names_no_file_the_snapshot_drops() -> None:
    """scripts/test_quality_scan.py --check fails on a ledger entry whose file
    is gone, and the snapshot's CI runs it: a dropped test file stays off the
    ledger (keep its counts at zero)."""
    ledger = _quality_ledger()
    listed = [*ledger["files"], *ledger["legacy_names"]]
    dropped = sorted(name for name in listed if _dropped(f"kew/{name}"))
    assert dropped == []


def test_a_directory_line_drops_the_files_under_it() -> None:
    assert _dropped(".claude/skills/ship-pr/SKILL.md")
    assert not _dropped(".claude/skills/ship-pr-notes.md")

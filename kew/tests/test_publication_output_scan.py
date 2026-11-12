"""Agent output is scanned for held credentials before every push (#1969, #1980).

A contained run that held a reusable provider credential, and a native run that
can read host credential files, publish only a head the host scanned clean
immediately before the push. Every secret here is a placeholder.
"""

from __future__ import annotations

import base64
import json
import logging
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import quote

import pytest
from test_publication_credentials import (
    _ROUND10_HASH,
    _profile,
    _round10_issued_profile,
    _round10_issuer_event,
    _run,
)

from kew import worktree
from kew.publication_canonical import CanonicalPublication
from kew.publication_credentials import (
    CREDENTIAL_PUBLICATION_REFUSAL,
    CredentialPublicationRefused,
    IssuedKeyPublicationHeld,
    require_publication_credentials,
    require_publication_launch_credentials,
)
from kew.readiness import Containment
from kew.run_store import SqliteRunStore

BRANCH = "agent/issue-7"
#: Placeholder, never a real credential. Contains "+" and "/" so its percent
#: encoding differs from the raw value.
PLACEHOLDER = "kew-placeholder+oauth/token-0123456789abcdef"
SK_ANT_SHAPE = "sk-ant-api03-" + "Q" * 40
#: The recorded scan event and the documented value bound. Imported lazily below
#: so the behaviour tests run, and fail on their assertions, without the scanner.
OUTPUT_SCAN_EVENT = "publication_output_scan"
MIN_VALUE_CHARS = 16


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603  # nosec B603, B607 — fixture setup
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _git_on_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    from kew import subprocess_utils

    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git)


#: Placeholder secret of the default fake Codex login every test starts with.
DEFAULT_CODEX_TOKEN = "kew-placeholder-default-codex-token-0123"


@pytest.fixture(autouse=True)
def _no_host_codex_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the operator's real Codex login out of every scan in this module.

    A readable placeholder login stands in for it: a native run needs one to
    be scanned at all (round 12).
    """
    home = tmp_path / "default-codex-home"
    home.mkdir()
    (home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": DEFAULT_CODEX_TOKEN}})
    )
    monkeypatch.setenv("CODEX_HOME", str(home))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteRunStore]:
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        yield opened


@pytest.fixture
def scene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Path | str]]:
    yield from _scene_under(tmp_path, monkeypatch)


#: Nine 61-character directories: a state root deeper than the 500-character
#: provider-output bound by itself, so a refusal that loses its action behind a
#: long checkout path fails here however short pytest's base temp is.
_DEEP_STATE_ROOT = Path(*["kew-state-" + "d" * 50] * 9)


@pytest.fixture
def deep_scene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Path | str]]:
    root = tmp_path / _DEEP_STATE_ROOT
    root.mkdir(parents=True)
    yield from _scene_under(root, monkeypatch)


def _scene_under(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Path | str]]:
    origin = tmp_path / "origin.git"
    subprocess.run(  # noqa: S603  # nosec B603, B607
        ["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True
    )
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    _git(seed, "config", "user.name", "Test")
    _git(seed, "config", "user.email", "test@example.invalid")
    (seed / "keep.txt").write_text("keep\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "base")
    _git(seed, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")
    parent = _git(seed, "rev-parse", "HEAD")
    checkout = tmp_path / "agent" / "issue-7"
    checkout.parent.mkdir()
    _git(tmp_path, "clone", "-q", "-b", BRANCH, str(origin), str(checkout))
    worktree.write_checkout_generation(checkout)
    _git(checkout, "config", "user.name", "Agent")
    _git(checkout, "config", "user.email", "agent@example.invalid")
    monkeypatch.setattr(worktree, "publication_remote", lambda repo: str(origin))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", PLACEHOLDER)
    try:
        yield {"origin": origin, "checkout": checkout, "parent": parent}
    finally:
        worktree.release_checkout_claims()


def _remote_head(scene: dict[str, Path | str]) -> str:
    return _git(Path(scene["origin"]), "rev-parse", f"refs/heads/{BRANCH}")


def _publish(scene: dict[str, Path | str], store: SqliteRunStore, run_id: str):
    return worktree.publish_existing_pr_branch(
        Path(scene["checkout"]),
        BRANCH,
        commit_message="fix: address review",
        expected_remote_sha=str(scene["parent"]),
        repo="owner/repo",
        publish="canonical",
        store=store,
        run_id=run_id,
    )


def _contained_env_run(store: SqliteRunStore, run_id: str = "run") -> None:
    _run(store, run_id, branch=BRANCH)
    _profile(
        store,
        run_id,
        1,
        containment=Containment.CONTAINED,
        auth_path="env:CLAUDE_CODE_OAUTH_TOKEN",
    )


def _native_codex_run(store: SqliteRunStore, run_id: str = "native") -> None:
    _run(store, run_id, branch=BRANCH)
    _profile(store, run_id, 1, containment=Containment.NATIVE, auth_path="cli-login")


def _scans(store: SqliteRunStore, run_id: str) -> list:
    return store.list_audit_events(event=OUTPUT_SCAN_EVENT, run_id=run_id)


def _all_audit_text(store: SqliteRunStore) -> str:
    return "\n".join(
        json.dumps(event.payload, sort_keys=True)
        for event in store.list_audit_events(limit=None)
    )


# -- #1969: a contained claude-code run on a reusable credential -------------------


def test_contained_env_run_is_admitted_at_launch(store: SqliteRunStore) -> None:
    _contained_env_run(store)

    require_publication_launch_credentials(
        store, run_id="run", run_kind="dispatch", attempt_number=1
    )


def test_contained_env_run_with_clean_output_publishes(
    scene: dict[str, Path | str], store: SqliteRunStore
) -> None:
    _contained_env_run(store)
    (Path(scene["checkout"]) / "fix.txt").write_text("an ordinary change\n")

    published = _publish(scene, store, "run")

    assert isinstance(published, CanonicalPublication)
    assert _remote_head(scene) == published.commit_sha
    (scan,) = _scans(store, "run")
    assert (scan.actor_type, scan.actor_id) == ("policy", "host-publication")
    assert scan.payload == {
        "attempt_number": 1,
        "base_sha": scene["parent"],
        "head_sha": published.commit_sha,
        "min_value_chars": MIN_VALUE_CHARS,
        "already_published": False,
        "verdict": "clean",
        "files_scanned": 1,
        "matches": {},
        "findings": [],
    }


def test_default_dispatch_publication_of_a_contained_env_run(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kew import dispatch_core

    fresh = "agent/issue-8"
    _run(store, "run", branch=fresh)
    _profile(
        store,
        "run",
        1,
        containment=Containment.CONTAINED,
        auth_path="env:CLAUDE_CODE_OAUTH_TOKEN",
    )
    monkeypatch.setattr(
        dispatch_core, "publication_remote", lambda repo: str(scene["origin"])
    )
    (Path(scene["checkout"]) / "fix.txt").write_text("an ordinary change\n")

    published = dispatch_core._git_push_branch(
        fresh,
        Path(scene["checkout"]),
        repo="owner/repo",
        base_sha=str(scene["parent"]),
        store=store,
        run_id="run",
        item_id="7",
    )

    assert isinstance(published, CanonicalPublication)
    assert (
        _git(Path(scene["origin"]), "rev-parse", f"refs/heads/{fresh}")
        == published.commit_sha
    )
    (scan,) = _scans(store, "run")
    assert scan.payload["head_sha"] == published.commit_sha
    assert scan.payload["verdict"] == "clean"


def _encoded(kind: str) -> bytes:
    raw = PLACEHOLDER.encode()
    return {
        "raw": raw,
        "base64": base64.b64encode(raw),
        "base64-misaligned": base64.b64encode(b"TOKEN=" + raw),
        "base64-urlsafe": base64.urlsafe_b64encode(b"x" + raw),
        "hex": raw.hex().encode(),
        "hex-upper": raw.hex().upper().encode(),
        "percent": quote(PLACEHOLDER, safe="").encode(),
        "binary": b"\x00\xff" + raw + b"\x00",
        "sk-ant-shape": SK_ANT_SHAPE.encode(),
    }[kind]


_EXPECTED_CLASS = {
    "raw": {"value_raw"},
    "base64": {"value_base64"},
    "base64-misaligned": {"value_base64"},
    "base64-urlsafe": {"value_base64", "value_base64url"},
    "hex": {"value_hex"},
    "hex-upper": {"value_hex"},
    "percent": {"value_percent"},
    "binary": {"value_raw"},
    "sk-ant-shape": {"anthropic_key_shape"},
}


@pytest.mark.parametrize("kind", sorted(_EXPECTED_CLASS))
def test_output_with_a_credential_is_refused_and_stays_local(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    caplog: pytest.LogCaptureFixture,
    kind: str,
) -> None:
    _contained_env_run(store)
    leaked = Path(scene["checkout"]) / "notes.bin"
    leaked.write_bytes(b"prefix " + _encoded(kind) + b" suffix\n")
    caplog.set_level(logging.DEBUG)

    with pytest.raises(CredentialPublicationRefused) as raised:
        _publish(scene, store, "run")

    assert raised.value.category == CREDENTIAL_PUBLICATION_REFUSAL
    assert raised.value.reason == "credential_in_output"
    assert "nothing was pushed and the checkout stays local" in str(raised.value)
    assert store.list_audit_events(event="credential_already_published") == []
    assert _remote_head(scene) == scene["parent"]
    assert leaked.exists()
    assert store.list_audit_events(event="publication_intent", run_id="run") == []
    (scan,) = _scans(store, "run")
    assert scan.payload["verdict"] == "found"
    assert scan.payload["files_scanned"] == 1
    assert set(scan.payload["matches"]) & _EXPECTED_CLASS[kind]
    secret_forms = {PLACEHOLDER, SK_ANT_SHAPE, _encoded(kind).decode("latin-1")}
    for surface in (str(raised.value), _all_audit_text(store), caplog.text):
        for secret in secret_forms:
            assert secret not in surface


def test_short_values_are_below_the_scan_bound(tmp_path: Path) -> None:
    from kew.publication_scan import OutputScan, OutputScanTarget, scan_output

    at_bound = "abcdefghijklmnop"
    assert len(at_bound) == MIN_VALUE_CHARS == 16
    repo, base, head = _repo_with_change(tmp_path, at_bound.encode() + b"\n")
    target = OutputScanTarget(repository=repo, base_sha=base, head_sha=head)

    short = scan_output(target, (("TEST_KEY", at_bound[:-1]),))
    bounded = scan_output(target, (("TEST_KEY", at_bound),))

    assert short == OutputScan(verdict="clean", files_scanned=1, matches={})
    assert bounded.verdict == "found"
    assert bounded.matches == {"value_raw": 1}
    assert [finding.describe() for finding in bounded.findings] == [
        "TEST_KEY in blob (value_raw)"
    ]


def test_documentation_placeholders_are_not_key_shapes(tmp_path: Path) -> None:
    from kew.publication_scan import OutputScan, OutputScanTarget, scan_output

    repo, base, head = _repo_with_change(
        tmp_path, b"export ANTHROPIC_API_KEY=sk-ant-...\nghp_xxxx sk-or-v1-...\n"
    )

    result = scan_output(
        OutputScanTarget(repository=repo, base_sha=base, head_sha=head), ()
    )

    assert result == OutputScan(verdict="clean", files_scanned=1, matches={})


def test_merge_parent_content_is_not_agent_output(tmp_path: Path) -> None:
    from kew.publication_scan import OutputScan, OutputScanTarget, scan_output

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "upstream")
    (repo / "upstream.txt").write_text(SK_ANT_SHAPE + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "upstream")
    upstream = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    branch_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "upstream")
    merged = _git(repo, "rev-parse", "HEAD")

    result = scan_output(
        OutputScanTarget(
            repository=repo,
            base_sha=branch_head,
            head_sha=merged,
            other_parents=(upstream,),
        ),
        (),
    )

    assert result == OutputScan(verdict="clean", files_scanned=0, matches={})


def _repo_with_base_file(
    tmp_path: Path, base_content: bytes, edits: dict[str, bytes]
) -> tuple[Path, str, str]:
    """A base holding ``fixtures.txt``, then one commit writing *edits*."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "fixtures.txt").write_bytes(base_content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    for name, content in edits.items():
        (repo / name).write_bytes(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "edit")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def _scan_edit(
    tmp_path: Path,
    base_content: bytes,
    edits: dict[str, bytes],
    values: tuple[tuple[str, str], ...] = (),
):
    from kew.publication_scan import OutputScanTarget, scan_output

    repo, base, head = _repo_with_base_file(tmp_path, base_content, edits)
    return scan_output(
        OutputScanTarget(repository=repo, base_sha=base, head_sha=head), values
    )


_BASE_PLACEHOLDER = f"key = {SK_ANT_SHAPE!r}\n".encode()


def test_editing_a_file_whose_base_holds_a_key_shape_placeholder_is_allowed(
    tmp_path: Path,
) -> None:
    from kew.publication_scan import OutputScan

    result = _scan_edit(
        tmp_path,
        _BASE_PLACEHOLDER,
        {"fixtures.txt": _BASE_PLACEHOLDER + b"another ordinary line\n"},
    )

    assert result == OutputScan(verdict="clean", files_scanned=1, matches={})


def test_a_new_key_shape_value_in_a_file_with_a_base_placeholder_is_refused(
    tmp_path: Path,
) -> None:
    added = "sk-ant-api03-" + "R" * 40
    result = _scan_edit(
        tmp_path,
        _BASE_PLACEHOLDER,
        {"fixtures.txt": _BASE_PLACEHOLDER + f"new = {added!r}\n".encode()},
    )

    assert result.verdict == "found"
    # Only the new value counts; the placeholder the base carries does not.
    assert result.matches == {"anthropic_key_shape": 1}


def test_a_key_shape_value_in_a_new_file_is_refused(tmp_path: Path) -> None:
    # Not the base blob itself: an object the base already has is never sent.
    result = _scan_edit(
        tmp_path, _BASE_PLACEHOLDER, {"new.txt": b"moved " + _BASE_PLACEHOLDER}
    )

    assert result.verdict == "found"
    assert result.matches == {"anthropic_key_shape": 1}


def test_a_blob_also_pushed_at_a_new_path_still_counts(tmp_path: Path) -> None:
    edited = _BASE_PLACEHOLDER + b"another ordinary line\n"
    result = _scan_edit(
        tmp_path, _BASE_PLACEHOLDER, {"fixtures.txt": edited, "copy.txt": edited}
    )

    assert result.verdict == "found"
    assert result.matches == {"anthropic_key_shape": 1}


def test_a_held_credential_counts_even_when_the_base_already_held_it(
    tmp_path: Path,
) -> None:
    base_content = f"token = {PLACEHOLDER}\n".encode()
    result = _scan_edit(
        tmp_path,
        base_content,
        {"fixtures.txt": base_content + b"another ordinary line\n"},
        values=(("CLAUDE_CODE_OAUTH_TOKEN", PLACEHOLDER),),
    )

    assert result.verdict == "found"
    assert result.matches == {"value_raw": 1}
    assert [finding.describe() for finding in result.findings] == [
        "CLAUDE_CODE_OAUTH_TOKEN in blob (value_raw)"
    ]


def _counting_base_carries(
    monkeypatch: pytest.MonkeyPatch, on_check=None
) -> list[bytes]:
    """Record every key-shape string settled against the base."""
    from kew import publication_scan

    real = publication_scan._base_carries
    checked: list[bytes] = []

    def base_carries(*args, **kwargs):
        carried = real(*args, **kwargs)

        def counted(oid: str, text: bytes) -> bool:
            checked.append(text)
            if on_check is not None:
                on_check()
            return carried(oid, text)

        return counted

    monkeypatch.setattr(publication_scan, "_base_carries", base_carries)
    return checked


def test_distinct_key_shapes_over_the_cap_refuse_without_checking_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew import publication_scan
    from kew.publication_credentials import publication_output_guard
    from kew.publication_scan import OutputScanTarget

    def unchecked(*args, **kwargs):
        raise AssertionError("an overflowing scan must not settle key shapes")

    monkeypatch.setattr(publication_scan, "MAX_KEY_SHAPE_MATCHES", 4)
    monkeypatch.setattr(publication_scan, "_base_carries", unchecked)
    # Six distinct placeholders the base already carries: under the cap each
    # would clear, but over it none is checked and the push refuses.
    keys = [f"sk-ant-api03-{letter * 40}" for letter in "ABCDEF"]
    base_content = "".join(f"key = {key!r}\n" for key in keys).encode()
    repo, base, head = _repo_with_base_file(
        tmp_path, base_content, {"fixtures.txt": base_content + b"another line\n"}
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_output_guard(None, run_id=None)(
            OutputScanTarget(repository=repo, base_sha=base, head_sha=head)
        )

    assert raised.value.reason == "credential_in_output"
    assert raised.value.diagnosis == (
        "more than 4 distinct key-shape strings in blobs;"
        " kew cannot clear that many against the base"
    )
    for key in keys:
        assert key not in str(raised.value)
        assert key[:20] not in (raised.value.diagnosis or "")


def test_a_deadline_hit_while_settling_key_shapes_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from kew import publication_scan
    from kew.publication_scan import OutputScanTimeout

    clock = [1000.0]
    monkeypatch.setattr(
        publication_scan, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )

    def expire() -> None:
        clock[0] += 10_000.0

    checked = _counting_base_carries(monkeypatch, on_check=expire)
    keys = [f"sk-ant-api03-{letter * 40}" for letter in "AB"]
    base_content = "".join(f"key = {key!r}\n" for key in keys).encode()

    with pytest.raises(OutputScanTimeout) as raised:
        _scan_edit(
            tmp_path,
            base_content,
            {"fixtures.txt": base_content + b"another line\n"},
        )

    assert raised.value.reason == "credential_scan_timeout"
    # The first check spent the deadline; the second was never made.
    assert len(checked) == 1


def test_duplicates_of_a_base_placeholder_are_settled_once_and_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew.publication_scan import OutputScan

    checked = _counting_base_carries(monkeypatch)

    result = _scan_edit(
        tmp_path, _BASE_PLACEHOLDER, {"fixtures.txt": _BASE_PLACEHOLDER * 50}
    )

    assert result == OutputScan(verdict="clean", files_scanned=1, matches={})
    assert checked == [SK_ANT_SHAPE.encode()]


def _repo_with_base_files(
    tmp_path: Path, names: list[str], base: bytes, edit: Callable[[str], bytes]
) -> tuple[Path, str, str]:
    """A base holding every name with *base* content, then one commit editing all."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    for name in names:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(base)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    for name in names:
        (repo / name).write_bytes(edit(name))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "edit")
    return repo, base_sha, _git(repo, "rev-parse", "HEAD")


def test_one_base_carried_placeholder_in_many_blobs_is_allowed(
    tmp_path: Path,
) -> None:
    """The cap counts distinct strings, not (blob, string) pairs (#2054)."""
    from kew.publication_scan import (
        MAX_KEY_SHAPE_MATCHES,
        OutputScan,
        OutputScanTarget,
        scan_output,
    )

    # More blobs than the cap, all carrying the one placeholder the base has.
    names = [f"fixture{index}.txt" for index in range(MAX_KEY_SHAPE_MATCHES + 44)]
    repo, base, head = _repo_with_base_files(
        tmp_path,
        names,
        _BASE_PLACEHOLDER,
        lambda name: _BASE_PLACEHOLDER + f"edit to {name}\n".encode(),
    )

    result = scan_output(
        OutputScanTarget(repository=repo, base_sha=base, head_sha=head), ()
    )

    assert result == OutputScan(verdict="clean", files_scanned=len(names), matches={})


def test_distinct_strings_at_the_cap_are_settled_in_every_blob_that_holds_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the cap, each held string still settles against every blob's paths."""
    from kew import publication_scan
    from kew.publication_scan import OutputScan, OutputScanTarget, scan_output

    monkeypatch.setattr(publication_scan, "MAX_KEY_SHAPE_MATCHES", 4)
    checked = _counting_base_carries(monkeypatch)
    keys = [f"sk-ant-api03-{letter * 40}" for letter in "ABCD"]
    base_content = "".join(f"key = {key!r}\n" for key in keys).encode()
    names = ["one.txt", "two.txt"]
    repo, base, head = _repo_with_base_files(
        tmp_path,
        names,
        base_content,
        lambda name: base_content + f"edit to {name}\n".encode(),
    )

    result = scan_output(
        OutputScanTarget(repository=repo, base_sha=base, head_sha=head), ()
    )

    assert result == OutputScan(verdict="clean", files_scanned=2, matches={})
    # Eight (blob, string) pairs, four distinct strings: within the cap, and
    # every pair is settled — a string cleared in one blob is not assumed in
    # the next.
    assert len(checked) == len(keys) * len(names)
    assert set(checked) == {key.encode() for key in keys}


def test_more_distinct_key_shapes_than_the_cap_are_refused(tmp_path: Path) -> None:
    """One over the cap of distinct strings refuses, and says what it counted."""
    from kew.publication_scan import KEY_SHAPE_OVERFLOW, MAX_KEY_SHAPE_MATCHES

    keys = [
        f"sk-ant-api03-{index:04d}{'K' * 36}"
        for index in range(MAX_KEY_SHAPE_MATCHES + 1)
    ]
    added = "".join(f"key = {key!r}\n" for key in keys).encode()

    result = _scan_edit(tmp_path, _BASE_PLACEHOLDER, {"new.txt": added})

    assert result.verdict == "found"
    assert result.matches == {KEY_SHAPE_OVERFLOW: 1}
    assert [finding.describe() for finding in result.findings] == [
        f"more than {MAX_KEY_SHAPE_MATCHES} distinct key-shape strings in blobs;"
        " kew cannot clear that many against the base"
    ]


def _counting_base_searches(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Record every search of base content for a held key-shape string."""
    from kew import publication_scan

    searched: list[bytes] = []

    class _Counted(bytes):
        def __contains__(self, item: object) -> bool:
            searched.append(bytes(item))  # type: ignore[call-overload]
            return bytes.__contains__(self, item)  # type: ignore[operator]

    real = publication_scan._read_blobs

    def read_blobs(*args, **kwargs) -> dict[str, bytes]:
        return {oid: _Counted(raw) for oid, raw in real(*args, **kwargs).items()}

    monkeypatch.setattr(publication_scan, "_read_blobs", read_blobs)
    return searched


def test_repeats_cost_one_base_search_per_distinct_base_blob_and_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settling is per (base blob, string), not per (blob, string, path).

    Bounding only the distinct strings left the (blob, string) pair count, and
    the paths each blob is pushed at, driving the settle work: a push well
    inside every byte bound turned a fraction of a second into a
    ``credential_scan_timeout`` refusal (#2054 review).
    """
    from kew.publication_scan import OutputScan, OutputScanTarget, scan_output

    searched = _counting_base_searches(monkeypatch)
    keys = [f"sk-ant-api03-{letter * 40}" for letter in "ABCD"]
    base_content = "".join(f"key = {key!r}\n" for key in keys).encode()
    names = [f"g{group}/f{copy}.txt" for group in range(2) for copy in range(3)]
    repo, base, head = _repo_with_base_files(
        tmp_path,
        names,
        base_content,
        # One blob per group, pushed at all three of the group's paths.
        lambda name: base_content + f"edit to {name.split('/')[0]}\n".encode(),
    )

    result = scan_output(
        OutputScanTarget(repository=repo, base_sha=base, head_sha=head), ()
    )

    assert result == OutputScan(verdict="clean", files_scanned=2, matches={})
    # Twenty-four (blob, string, path) checks over one base blob: four searches.
    assert sorted(searched) == sorted(key.encode() for key in keys)


def test_more_key_shape_pairs_than_the_settle_bound_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over the (blob, string) pair bound refuses, and says which bound it was."""
    from kew import publication_scan
    from kew.publication_scan import (
        KEY_SHAPE_SETTLE_OVERFLOW,
        OutputScanTarget,
        scan_output,
    )

    bound = 3
    monkeypatch.setattr(publication_scan, "MAX_KEY_SHAPE_SETTLEMENTS", bound)
    # One distinct string, so only the pair bound can be the one that refuses.
    names = [f"fixture{index}.txt" for index in range(bound + 1)]
    repo, base, head = _repo_with_base_files(
        tmp_path,
        names,
        _BASE_PLACEHOLDER,
        lambda name: _BASE_PLACEHOLDER + f"edit to {name}\n".encode(),
    )

    result = scan_output(
        OutputScanTarget(repository=repo, base_sha=base, head_sha=head), ()
    )

    assert result.verdict == "found"
    assert result.matches == {KEY_SHAPE_SETTLE_OVERFLOW: 1}
    assert [finding.describe() for finding in result.findings] == [
        f"more than {bound} (file, key-shape string) pairs in blobs;"
        " kew cannot settle that many against the base"
    ]


def _repo_with_change(tmp_path: Path, content: bytes) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "keep.txt").write_text("keep\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "change.bin").write_bytes(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def test_scan_values_are_the_exact_declared_and_held_names() -> None:
    from kew.credential_surface import issued_secret_redaction
    from kew.publication_scan import MIN_VALUE_CHARS as BOUND
    from kew.publication_scan import output_scan_names, output_scan_values

    assert BOUND == MIN_VALUE_CHARS
    assert output_scan_names() == (
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "KEW_DETACHED_PUBLICATION_TOKEN",
        "OPENROUTER_API_KEY",
        "OPENROUTER_PROVISIONING_KEY",
    )

    issued = "sk-or-v1-" + "7" * 40
    with issued_secret_redaction(issued):
        values = dict(
            output_scan_values(
                {
                    "CLAUDE_CODE_OAUTH_TOKEN": PLACEHOLDER,
                    "SOME_SERVICE_TOKEN": "placeholder-service-token-value",
                    "GIT_AUTHOR_NAME": "an-ordinary-author-name",
                    "GH_TOKEN": "short",
                }
            )
        )

    assert values == {"CLAUDE_CODE_OAUTH_TOKEN": PLACEHOLDER, "issued_run_key": issued}


# -- the gate binds a clean scan to one exact head ---------------------------------


def _record(store: SqliteRunStore, run_id: str, head: str, verdict: str) -> None:
    from kew.publication_scan import OutputScan, OutputScanTarget, record_output_scan

    record_output_scan(
        store,
        run_id=run_id,
        attempt_number=1,
        item_id="7",
        target=OutputScanTarget(
            repository=Path("unused"), base_sha="a" * 40, head_sha=head
        ),
        result=OutputScan(
            verdict="found" if verdict == "found" else "clean",
            files_scanned=1,
            matches={"value_raw": 1} if verdict == "found" else {},
        ),
    )


def test_a_scan_of_another_head_does_not_admit_a_new_head(
    store: SqliteRunStore,
) -> None:
    _contained_env_run(store)
    _record(store, "run", "b" * 40, "clean")

    require_publication_credentials(store, run_id="run", published_head="b" * 40)
    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(store, run_id="run", published_head="c" * 40)

    assert raised.value.reason == "credential_scan_missing"


def test_a_scan_recorded_for_another_run_does_not_admit_this_run(
    store: SqliteRunStore,
) -> None:
    _contained_env_run(store)
    _contained_env_run(store, "other")
    _record(store, "other", "b" * 40, "clean")

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(store, run_id="run", published_head="b" * 40)

    assert raised.value.reason == "credential_scan_missing"


def test_a_found_scan_of_the_head_outranks_a_clean_one(
    store: SqliteRunStore,
) -> None:
    _contained_env_run(store)
    _record(store, "run", "b" * 40, "clean")
    _record(store, "run", "b" * 40, "found")

    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(store, run_id="run", published_head="b" * 40)

    assert raised.value.reason == "credential_in_output"


def test_a_found_head_stays_found_after_many_later_scans_of_other_heads(
    store: SqliteRunStore,
) -> None:
    from kew.publication_scan import recorded_scan_verdict

    _contained_env_run(store)
    _record(store, "run", "b" * 40, "found")
    for index in range(70):
        _record(store, "run", f"{index:040x}", "clean")

    assert recorded_scan_verdict(store, run_id="run", head_sha="b" * 40) == "found"
    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(store, run_id="run", published_head="b" * 40)

    assert raised.value.reason == "credential_in_output"


@pytest.mark.parametrize("posture", ["legacy", "host", "uncontained"])
def test_a_found_head_refuses_whatever_the_posture(
    store: SqliteRunStore, posture: str
) -> None:
    """Governed review round 9 (H): the finding outranks the run's posture."""
    _run(store, "run", branch=BRANCH)
    if posture != "legacy":
        _profile(
            store, "run", 1, containment=Containment(posture), auth_path="cli-login"
        )
    _record(store, "run", "c" * 40, "clean")
    require_publication_credentials(store, run_id="run", published_head="c" * 40)
    _record(store, "run", "b" * 40, "found")

    for _tick in range(2):
        with pytest.raises(CredentialPublicationRefused) as raised:
            require_publication_credentials(
                store, run_id="run", published_head="b" * 40
            )
        assert raised.value.reason == "credential_in_output"


def test_issued_key_runs_still_need_recorded_revocation(
    store: SqliteRunStore,
) -> None:
    _round10_issued_profile(store)
    _round10_issuer_event(store)
    _record(store, "issued", "b" * 40, "clean")

    with pytest.raises(IssuedKeyPublicationHeld):
        require_publication_credentials(store, run_id="issued", published_head="b" * 40)

    _round10_issuer_event(store, phase="revocation", revoked=True)
    require_publication_credentials(store, run_id="issued", published_head="b" * 40)
    assert _ROUND10_HASH in store.list_route_profiles("issued")[0].auth_path


# -- #1980: native Codex reads are unrestricted ------------------------------------


def test_native_codex_output_with_a_host_credential_is_refused(
    scene: dict[str, Path | str], store: SqliteRunStore
) -> None:
    _native_codex_run(store)
    copied = Path(scene["checkout"]) / "notes.txt"
    copied.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={PLACEHOLDER}\n")

    with pytest.raises(CredentialPublicationRefused) as raised:
        _publish(scene, store, "native")

    assert raised.value.reason == "credential_in_output"
    assert _remote_head(scene) == scene["parent"]
    assert copied.exists()
    assert PLACEHOLDER not in _all_audit_text(store)


def test_native_codex_clean_output_publishes_after_its_scan(
    scene: dict[str, Path | str], store: SqliteRunStore
) -> None:
    _native_codex_run(store)
    (Path(scene["checkout"]) / "fix.txt").write_text("an ordinary change\n")

    published = _publish(scene, store, "native")

    assert isinstance(published, CanonicalPublication)
    (scan,) = _scans(store, "native")
    assert scan.payload["head_sha"] == published.commit_sha
    assert scan.payload["verdict"] == "clean"


def test_native_output_needs_a_scan_of_the_published_head(
    store: SqliteRunStore,
) -> None:
    _native_codex_run(store)

    require_publication_credentials(store, run_id="native")
    with pytest.raises(CredentialPublicationRefused) as raised:
        require_publication_credentials(store, run_id="native", published_head="d" * 40)

    assert raised.value.reason == "credential_scan_missing"


# -- the checkout escape hatch is scanned too --------------------------------------


def test_checkout_publication_is_scanned_before_its_push(
    scene: dict[str, Path | str], store: SqliteRunStore
) -> None:
    _run(store, "host", branch=BRANCH)
    checkout = Path(scene["checkout"])
    (checkout / "notes.txt").write_text(SK_ANT_SHAPE + "\n")

    with pytest.raises(CredentialPublicationRefused) as raised:
        worktree.publish_existing_pr_branch(
            checkout,
            BRANCH,
            commit_message="fix: address review",
            expected_remote_sha=str(scene["parent"]),
            publish="checkout",
            dangerously_skip_containment=True,
            store=store,
            run_id="host",
        )

    assert raised.value.reason == "credential_in_output"
    assert _remote_head(scene) == scene["parent"]
    (scan,) = _scans(store, "host")
    assert scan.payload["matches"] == {"anthropic_key_shape": 1}


_COMMIT_LEAKS = {
    # Where the agent put the credential -> (git environment, message, class)
    "message": ({}, f"fix: tidy\n\ntoken {PLACEHOLDER}\n", "commit_value_raw"),
    "author-email": (
        {"GIT_AUTHOR_EMAIL": f"{SK_ANT_SHAPE}@example.invalid"},
        "fix: tidy\n",
        "commit_anthropic_key_shape",
    ),
    "committer-name": (
        {"GIT_COMMITTER_NAME": PLACEHOLDER},
        "fix: tidy\n",
        "commit_value_raw",
    ),
}


@pytest.mark.parametrize("where", sorted(_COMMIT_LEAKS))
def test_checkout_escape_scans_the_commits_it_pushes(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    caplog: pytest.LogCaptureFixture,
    where: str,
) -> None:
    import os

    _native_codex_run(store, "agent")
    checkout = Path(scene["checkout"])
    (checkout / "fix.txt").write_text("an ordinary change\n")
    _git(checkout, "add", "fix.txt")
    extra, message, label = _COMMIT_LEAKS[where]
    subprocess.run(  # noqa: S603  # nosec B603, B607 — the agent's own commit
        ["git", "commit", "-q", "-F", "-"],
        cwd=checkout,
        input=message,
        text=True,
        check=True,
        env={**os.environ, **extra},
    )
    caplog.set_level(logging.DEBUG)

    with pytest.raises(CredentialPublicationRefused) as raised:
        worktree.publish_existing_pr_branch(
            checkout,
            BRANCH,
            commit_message="fix: address review",
            expected_remote_sha=str(scene["parent"]),
            publish="checkout",
            dangerously_skip_containment=True,
            store=store,
            run_id="agent",
        )

    assert raised.value.reason == "credential_in_output"
    assert _remote_head(scene) == scene["parent"]
    (scan,) = _scans(store, "agent")
    assert scan.payload["verdict"] == "found"
    assert scan.payload["matches"] == {label: 1}
    for surface in (str(raised.value), _all_audit_text(store), caplog.text):
        assert PLACEHOLDER not in surface
        assert SK_ANT_SHAPE not in surface


def _escape_publish(scene: dict[str, Path | str], store: SqliteRunStore) -> None:
    worktree.publish_existing_pr_branch(
        Path(scene["checkout"]),
        BRANCH,
        commit_message="fix: address review",
        expected_remote_sha=str(scene["parent"]),
        publish="checkout",
        dangerously_skip_containment=True,
        store=store,
        run_id="agent",
    )


def _assert_escape_refused(
    scene: dict[str, Path | str], store: SqliteRunStore, label: str
) -> None:
    with pytest.raises(CredentialPublicationRefused) as raised:
        _escape_publish(scene, store)

    assert raised.value.reason == "credential_in_output"
    assert _remote_head(scene) == scene["parent"]
    (scan,) = _scans(store, "agent")
    assert scan.payload["verdict"] == "found"
    assert scan.payload["matches"].get(label, 0) >= 1
    audit_text = _all_audit_text(store)
    for secret in (PLACEHOLDER, SK_ANT_SHAPE):
        assert secret not in str(raised.value)
        assert secret not in audit_text


def test_checkout_escape_ignores_an_agent_replace_ref(
    scene: dict[str, Path | str], store: SqliteRunStore
) -> None:
    """refs/replace/<head> shows a clean decoy; the push would send the real head."""
    _native_codex_run(store, "agent")
    checkout = Path(scene["checkout"])
    (checkout / "notes.txt").write_text(f"token {PLACEHOLDER}\n")
    _git(checkout, "add", "notes.txt")
    _git(checkout, "commit", "-qm", "fix: tidy")
    real = _git(checkout, "rev-parse", "HEAD")
    clean_tree = _git(checkout, "rev-parse", f"{scene['parent']}^{{tree}}")
    decoy = _git(
        checkout, "commit-tree", clean_tree, "-p", str(scene["parent"]), "-m", "tidy"
    )
    _git(checkout, "replace", real, decoy)
    assert _git(checkout, "cat-file", "-p", "HEAD").splitlines()[0].endswith(clean_tree)

    _assert_escape_refused(scene, store, "value_raw")


def test_checkout_escape_scans_a_credential_removed_by_a_later_commit(
    scene: dict[str, Path | str], store: SqliteRunStore
) -> None:
    _native_codex_run(store, "agent")
    checkout = Path(scene["checkout"])
    (checkout / "notes.txt").write_text(f"token {PLACEHOLDER}\n")
    _git(checkout, "add", "notes.txt")
    _git(checkout, "commit", "-qm", "fix: add notes")
    (checkout / "notes.txt").write_text("nothing to see\n")
    _git(checkout, "commit", "-qam", "fix: tidy notes")

    _assert_escape_refused(scene, store, "value_raw")


def test_checkout_escape_scans_file_names(
    scene: dict[str, Path | str], store: SqliteRunStore
) -> None:
    _native_codex_run(store, "agent")
    checkout = Path(scene["checkout"])
    (checkout / f"{SK_ANT_SHAPE}.txt").write_text("an ordinary change\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "fix: add a file")

    _assert_escape_refused(scene, store, "tree_anthropic_key_shape")


# -- the dispatch escape hatch of a continuation, which records no base ------------


def _baseless_escape(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    remote_base: str,
    queries: list[tuple[str, str, Path]],
) -> None:
    """Publish through the real guard with no recorded base, logging queries."""
    from kew import dispatch_core

    _native_codex_run(store, "agent")

    def trusted_query(remote: str, branch: str, *, scratch: Path) -> str:
        queries.append((remote, branch, scratch))
        return remote_base

    monkeypatch.setattr(
        dispatch_core, "publication_remote", lambda repo: str(scene["origin"])
    )
    monkeypatch.setattr(dispatch_core, "remote_branch_sha", trusted_query)
    dispatch_core._git_push_branch(
        BRANCH,
        Path(scene["checkout"]),
        base_sha=None,
        base_branch="develop",
        publish="checkout",
        dangerously_skip_containment=True,
        store=store,
        run_id="agent",
    )


def _assert_canonical_query(
    scene: dict[str, Path | str], queries: list[tuple[str, str, Path]]
) -> None:
    ((remote, branch, scratch),) = queries
    assert (remote, branch) == (str(scene["origin"]), "develop")
    assert Path(scene["checkout"]) not in scratch.parents


def test_baseless_checkout_escape_scans_from_the_canonical_remote_base(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = Path(scene["checkout"])
    (checkout / "fix.txt").write_text("an ordinary change\n")
    _git(checkout, "add", "fix.txt")
    _git(checkout, "commit", "-qm", "fix: tidy")
    head = _git(checkout, "rev-parse", "HEAD")

    queries: list[tuple[str, str, Path]] = []

    _baseless_escape(
        scene, store, monkeypatch, remote_base=str(scene["parent"]), queries=queries
    )

    _assert_canonical_query(scene, queries)
    assert _remote_head(scene) == head
    (scan,) = _scans(store, "agent")
    assert scan.payload["base_sha"] == scene["parent"]
    assert scan.payload["head_sha"] == head
    assert scan.payload["verdict"] == "clean"
    assert scan.payload["files_scanned"] == 1


def test_baseless_checkout_escape_defaults_an_unset_base_branch_to_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kew import dispatch_core

    queries: list[tuple[str, str]] = []

    def trusted_query(remote: str, branch: str, *, scratch: Path) -> str:
        queries.append((remote, branch))
        return "b" * 40

    monkeypatch.setattr(dispatch_core, "publication_remote", lambda repo: repo)
    monkeypatch.setattr(dispatch_core, "remote_branch_sha", trusted_query)

    assert dispatch_core._remote_base_sha("/srv/canonical.git", "") == "b" * 40
    assert queries == [("/srv/canonical.git", "main")]


def test_baseless_checkout_escape_refuses_when_the_remote_has_no_base(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kew.publication import PublicationError

    checkout = Path(scene["checkout"])
    (checkout / "fix.txt").write_text("an ordinary change\n")
    _git(checkout, "add", "fix.txt")
    _git(checkout, "commit", "-qm", "fix: tidy")

    queries: list[tuple[str, str, Path]] = []

    with pytest.raises(PublicationError, match="no trusted base"):
        _baseless_escape(scene, store, monkeypatch, remote_base="", queries=queries)

    # It refused after asking the canonical remote, not for want of asking.
    _assert_canonical_query(scene, queries)
    assert _remote_head(scene) == scene["parent"]
    assert _scans(store, "agent") == []


def test_baseless_checkout_escape_refuses_actionably_when_the_base_is_not_local(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """main moved on the canonical remote; the checkout never fetched it."""
    from kew.publication import PublicationError

    seed = tmp_path / "seed"
    (seed / "upstream.txt").write_text("main moved on\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "upstream")
    moved = _git(seed, "rev-parse", "HEAD")
    checkout = Path(scene["checkout"])
    (checkout / "fix.txt").write_text("an ordinary change\n")
    _git(checkout, "add", "fix.txt")
    _git(checkout, "commit", "-qm", "fix: tidy")

    queries: list[tuple[str, str, Path]] = []

    with pytest.raises(PublicationError) as raised:
        _baseless_escape(scene, store, monkeypatch, remote_base=moved, queries=queries)

    message = str(raised.value)
    assert f"base commit {moved} is not in the checkout" in message
    assert f"checkout preserved at {checkout}" in message
    assert "Publish canonically" in message
    assert "re-run the continuation from a fresh base" in message
    _assert_canonical_query(scene, queries)
    assert _remote_head(scene) == scene["parent"]
    assert _scans(store, "agent") == []


def test_baseless_checkout_escape_names_its_action_at_a_deep_checkout_path(
    deep_scene: dict[str, Path | str],
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long state directory must not cut the refusal off before its action."""
    from kew.publication import PublicationError

    checkout = Path(deep_scene["checkout"])
    assert len(str(checkout)) > 500
    seed = checkout.parent.parent / "seed"
    (seed / "upstream.txt").write_text("main moved on\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "upstream")
    moved = _git(seed, "rev-parse", "HEAD")
    (checkout / "fix.txt").write_text("an ordinary change\n")
    _git(checkout, "add", "fix.txt")
    _git(checkout, "commit", "-qm", "fix: tidy")

    with pytest.raises(PublicationError) as raised:
        _baseless_escape(deep_scene, store, monkeypatch, remote_base=moved, queries=[])

    message = str(raised.value)
    assert f"base commit {moved} is not in the checkout" in message
    assert f"checkout preserved at {checkout}" in message
    assert message.endswith(
        'Publish canonically (the default: [publication] publish = "canonical"), '
        "or re-run the continuation from a fresh base."
    )
    assert _remote_head(deep_scene) == deep_scene["parent"]


def test_stale_remote_branch_refusal_names_its_action_at_a_deep_checkout_path(
    tmp_path: Path,
) -> None:
    from kew import dispatch_core
    from kew.publication import PublicationError

    checkout = tmp_path / _DEEP_STATE_ROOT / "agent" / "issue-7"
    stale = "a" * 40

    with pytest.raises(PublicationError) as raised:
        dispatch_core._require_own_publication(
            None,
            existing=stale,
            branch=BRANCH,
            item_id=None,
            work_dir=checkout,
            owner={},
        )

    message = str(raised.value)
    assert f"checkout preserved at {checkout})" in message
    assert message.endswith("Delete or rename the remote branch and retry.")


def test_baseless_checkout_escape_still_catches_a_committed_credential(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = Path(scene["checkout"])
    (checkout / "notes.txt").write_text(f"token {PLACEHOLDER}\n")
    _git(checkout, "add", "notes.txt")
    _git(checkout, "commit", "-qm", "fix: add notes")

    queries: list[tuple[str, str, Path]] = []

    with pytest.raises(CredentialPublicationRefused) as raised:
        _baseless_escape(
            scene, store, monkeypatch, remote_base=str(scene["parent"]), queries=queries
        )

    _assert_canonical_query(scene, queries)
    assert raised.value.reason == "credential_in_output"
    assert _remote_head(scene) == scene["parent"]
    (scan,) = _scans(store, "agent")
    assert scan.payload["base_sha"] == scene["parent"]
    assert scan.payload["verdict"] == "found"
    assert scan.payload["matches"].get("value_raw", 0) >= 1
    assert PLACEHOLDER not in str(raised.value)
    assert PLACEHOLDER not in _all_audit_text(store)


def _command_config(env: dict[str, str]) -> dict[str, str]:
    return {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(int(env["GIT_CONFIG_COUNT"]))
    }


def test_checkout_push_sends_the_scanned_head_with_the_scan_hardening() -> None:
    import os

    command, env = worktree._publication_push_command(
        BRANCH,
        force_with_lease=False,
        expected_remote_sha="a" * 40,
        repo=None,
        head_sha="c" * 40,
    )

    assert command[:3] == ["git", "push", "--no-follow-tags"]
    assert command[-1] == f"{'c' * 40}:refs/heads/{BRANCH}"
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert env["GIT_GRAFT_FILE"] == os.devnull
    config = _command_config(env)
    assert config["core.useReplaceRefs"] == "false"
    assert config["core.hooksPath"] == os.devnull
    assert config["core.fsmonitor"] == "false"
    assert config["advice.graftFileDeprecated"] == "false"


def test_scan_hardening_adds_no_graft_hint_before_a_real_git_error(
    tmp_path: Path,
) -> None:
    """A push-failure handler keeps only the head of stderr, so the hardening
    must not put git's graft deprecation hint ahead of the real error."""
    import subprocess

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
        + ["commit", "-q", "--allow-empty", "-m", "x"],
        check=True,
    )
    # Walking commits reads the graft file; without the advice setting git
    # prints "hint: Support for <GIT_DIR>/info/grafts is deprecated" here.
    walk = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "HEAD"],
        env=worktree.checkout_object_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert walk.returncode == 0
    assert "graft" not in walk.stderr.lower()


# -- the gate matches an exact needle set, and says where it matched ---------------

_ORDINARY_HOST = {
    "GIT_AUTHOR_NAME": "Ordinary Author Name",
    "GIT_AUTHOR_EMAIL": "ordinary.author@example.invalid",
    "GIT_COMMITTER_NAME": "Ordinary Committer Name",
    "GIT_COMMITTER_EMAIL": "ordinary.committer@example.invalid",
    "SSH_AUTH_SOCK": "/private/tmp/ordinary-agent-socket/Listeners",
    "XAUTHORITY": "/home/ordinary-user/.Xauthority-file",
}


def test_escape_hatch_publishes_a_commit_carrying_ordinary_host_identity(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host's own commit embeds GIT_AUTHOR_* and friends: not credentials."""
    for name, value in _ORDINARY_HOST.items():
        monkeypatch.setenv(name, value)
    _native_codex_run(store, "agent")
    checkout = Path(scene["checkout"])
    (checkout / "notes.txt").write_text(
        "".join(f"{value}\n" for value in _ORDINARY_HOST.values())
    )

    assert worktree.publish_existing_pr_branch(
        checkout,
        BRANCH,
        commit_message="fix: address review",
        expected_remote_sha=str(scene["parent"]),
        publish="checkout",
        dangerously_skip_containment=True,
        store=store,
        run_id="agent",
    )

    assert _remote_head(scene) != scene["parent"]
    author = _git(Path(scene["origin"]), "log", "-1", "--format=%an <%ae>", BRANCH)
    assert author == "Ordinary Author Name <ordinary.author@example.invalid>"
    (scan,) = _scans(store, "agent")
    assert scan.payload["verdict"] == "clean"


def test_a_declared_credential_in_the_same_position_is_refused_by_name(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", PLACEHOLDER)
    _native_codex_run(store, "agent")
    (Path(scene["checkout"]) / "notes.txt").write_text("an ordinary change\n")
    caplog.set_level(logging.DEBUG)

    with pytest.raises(CredentialPublicationRefused) as raised:
        _escape_publish(scene, store)

    diagnosis = "CLAUDE_CODE_OAUTH_TOKEN in commit (value_raw)"
    assert raised.value.reason == "credential_in_output"
    assert raised.value.diagnosis == diagnosis
    assert diagnosis in str(raised.value)
    (scan,) = _scans(store, "agent")
    assert scan.payload["findings"] == [
        {
            "source": "CLAUDE_CODE_OAUTH_TOKEN",
            "object": "commit",
            "encoding": "value_raw",
            "count": 1,
        }
    ]
    for surface in (str(raised.value), _all_audit_text(store), caplog.text):
        assert PLACEHOLDER not in surface
    assert _remote_head(scene) == scene["parent"]


def test_a_key_shape_finding_names_its_class_and_object(
    scene: dict[str, Path | str], store: SqliteRunStore
) -> None:
    _native_codex_run(store, "agent")
    (Path(scene["checkout"]) / "notes.txt").write_text(SK_ANT_SHAPE + "\n")

    with pytest.raises(CredentialPublicationRefused) as raised:
        _escape_publish(scene, store)

    assert raised.value.diagnosis == "anthropic_key_shape in blob (key_shape)"
    assert SK_ANT_SHAPE not in str(raised.value)


# -- governed review round 9 (M): OpenAI key shapes and the Codex login file --------

#: Placeholders shaped like OpenAI keys, never real ones.
SK_PROJ_SHAPE = "sk-proj-" + "Zq9_" * 12
SK_LEGACY_SHAPE = "sk-" + "T3BlbkFJ" * 6
#: A placeholder standing in for the Codex login's access token: no key shape,
#: so only the needle taken from the login file can catch it.
CODEX_ACCESS_PLACEHOLDER = "kew-placeholder.codex-access.0123456789abcdef"
CODEX_API_KEY_PLACEHOLDER = "kew-placeholder-codex-api-key-0123456789"


def _fake_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fake-codex-home"
    home.mkdir()
    (home / "auth.json").write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": CODEX_API_KEY_PLACEHOLDER,
                "tokens": {
                    "id_token": "kew-placeholder.codex-id.0123456789abcdef",
                    "access_token": CODEX_ACCESS_PLACEHOLDER,
                    "refresh_token": "kew-placeholder-codex-refresh-0123456789",
                    "account_id": "00000000-0000-4000-8000-000000000000",
                },
                "last_refresh": "2026-09-24T00:00:00.000000Z",
            }
        )
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.mark.parametrize(
    "shape", [SK_PROJ_SHAPE, SK_LEGACY_SHAPE], ids=["project-key", "legacy-key"]
)
def test_an_openai_key_in_pushed_output_is_refused(
    scene: dict[str, Path | str], store: SqliteRunStore, shape: str
) -> None:
    _native_codex_run(store)
    (Path(scene["checkout"]) / "notes.txt").write_text(f"OPENAI_API_KEY={shape}\n")

    with pytest.raises(CredentialPublicationRefused) as raised:
        _publish(scene, store, "native")

    assert raised.value.reason == "credential_in_output"
    assert raised.value.diagnosis == "openai_key_shape in blob (key_shape)"
    assert shape not in str(raised.value)
    assert shape not in _all_audit_text(store)
    assert _remote_head(scene) == scene["parent"]


def test_the_host_codex_login_token_in_pushed_output_is_refused(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _fake_codex_home(tmp_path, monkeypatch)
    _native_codex_run(store)
    copied = base64.b64encode(CODEX_ACCESS_PLACEHOLDER.encode()).decode()
    (Path(scene["checkout"]) / "notes.txt").write_text(
        f"copied {CODEX_ACCESS_PLACEHOLDER}\nencoded {copied}\n"
    )
    caplog.set_level(logging.DEBUG)

    with pytest.raises(CredentialPublicationRefused) as raised:
        _publish(scene, store, "native")

    assert raised.value.reason == "credential_in_output"
    assert "codex_auth:tokens.access_token in blob (value_raw)" in (
        raised.value.diagnosis
    )
    assert "codex_auth:tokens.access_token in blob (value_base64)" in (
        raised.value.diagnosis
    )
    assert _remote_head(scene) == scene["parent"]
    for surface in (str(raised.value), _all_audit_text(store), caplog.text):
        assert CODEX_ACCESS_PLACEHOLDER not in surface
        assert copied not in surface


def test_ordinary_sk_words_are_not_openai_key_shapes(
    scene: dict[str, Path | str], store: SqliteRunStore, tmp_path: Path
) -> None:
    from kew.publication_scan import KEY_SHAPES, OutputScanTarget, scan_output

    prose = (
        b"pip install sk-learn scikit-learn\nsee sk-proj-... in the docs\n"
        b"task-" + b"a" * 50 + b"\nrisk-" + b"b" * 50 + b"\n"
    )
    repo, base, head = _repo_with_change(tmp_path, prose)
    result = scan_output(
        OutputScanTarget(repository=repo, base_sha=base, head_sha=head), ()
    )
    assert result.verdict == "clean"
    shape = KEY_SHAPES["openai_key_shape"]
    for other in (SK_ANT_SHAPE, "sk-or-v1-" + "7" * 40):
        assert shape.search(other) is None
    for key in ("sk-svcacct-" + "x" * 40, "sk-admin-" + "y" * 40):
        assert shape.fullmatch(key)

    _native_codex_run(store)
    (Path(scene["checkout"]) / "notes.txt").write_text("uses sk-learn for the fit\n")
    _publish(scene, store, "native")
    assert _remote_head(scene) != scene["parent"]


def test_codex_login_values_are_the_secret_fields_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew.publication_scan import codex_auth_values, output_scan_values

    home = _fake_codex_home(tmp_path, monkeypatch)

    values = dict(codex_auth_values({"CODEX_HOME": str(home)}))
    assert sorted(values) == [
        "codex_auth:OPENAI_API_KEY",
        "codex_auth:tokens.access_token",
        "codex_auth:tokens.id_token",
        "codex_auth:tokens.refresh_token",
    ]
    assert values["codex_auth:tokens.access_token"] == CODEX_ACCESS_PLACEHOLDER
    # HOME is the fallback when CODEX_HOME is unset.
    dotted = tmp_path / "home"
    (dotted / ".codex").mkdir(parents=True)
    shutil.copy(home / "auth.json", dotted / ".codex" / "auth.json")
    assert dict(codex_auth_values({"HOME": str(dotted)})) == values
    # The process environment is where the push-time scan reads it from.
    assert set(values.items()) <= set(output_scan_values())


def test_an_absent_or_unusable_codex_login_adds_no_needle(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import os

    from kew.publication_scan import codex_auth_values

    assert codex_auth_values({"CODEX_HOME": str(tmp_path / "missing")}) == ()
    assert codex_auth_values({}) == ()
    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / "auth.json").write_text("{not json")
    fifo = tmp_path / "fifo"
    fifo.mkdir()
    os.mkfifo(fifo / "auth.json")
    caplog.set_level(logging.WARNING)

    assert codex_auth_values({"CODEX_HOME": str(malformed)}) == ()
    assert codex_auth_values({"CODEX_HOME": str(fifo)}) == ()
    assert caplog.text.count("was not read for the publication scan") == 2


# -- the scan is bounded like the canonical reader, and fails closed ----------------


def test_an_oversize_object_refuses_without_publishing(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kew import publication_scan

    def small_bounds() -> tuple[int, int, float]:
        return 64, 1024 * 1024, 300.0

    monkeypatch.setattr(publication_scan, "_scan_bounds", small_bounds)
    _native_codex_run(store, "agent")
    (Path(scene["checkout"]) / "big.bin").write_bytes(b"x" * 65)

    with pytest.raises(CredentialPublicationRefused) as raised:
        _escape_publish(scene, store)

    assert raised.value.reason == "credential_scan_oversize"
    assert _remote_head(scene) == scene["parent"]


def test_an_oversize_push_total_refuses(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kew import publication_scan

    def small_total() -> tuple[int, int, float]:
        return 1024, 64, 300.0

    monkeypatch.setattr(publication_scan, "_scan_bounds", small_total)
    _native_codex_run(store, "agent")
    for index in range(4):
        (Path(scene["checkout"]) / f"part-{index}.txt").write_text("y" * 40)

    with pytest.raises(CredentialPublicationRefused) as raised:
        _escape_publish(scene, store)

    assert raised.value.reason == "credential_scan_oversize"


def test_a_listing_timeout_refuses_without_publishing(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kew import publication_scan

    def stalled(cmd, *, stdin=None, cwd=None, timeout=None, env=None):
        assert timeout is not None and timeout > 0
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(publication_scan, "run_bytes", stalled)
    _native_codex_run(store, "agent")
    (Path(scene["checkout"]) / "fix.txt").write_text("an ordinary change\n")

    with pytest.raises(CredentialPublicationRefused) as raised:
        _escape_publish(scene, store)

    assert raised.value.reason == "credential_scan_timeout"
    assert _remote_head(scene) == scene["parent"]


class _StalledBatch:
    """A cat-file process that never answers until the watchdog kills it."""

    def __init__(self) -> None:
        import threading

        self.killed = threading.Event()
        self.stdin = self
        self.stdout = self
        self.stderr = self

    def write(self, data: bytes) -> int:
        return len(data)

    def flush(self) -> None:
        return None

    def readline(self) -> bytes:
        self.killed.wait(30)
        return b""

    def read(self, size: int = -1) -> bytes:
        return b""

    def close(self) -> None:
        return None

    def kill(self) -> None:
        self.killed.set()

    def poll(self) -> int | None:
        return -9 if self.killed.is_set() else None

    def wait(self, timeout: float | None = None) -> int:
        self.killed.wait(timeout)
        return -9


def test_a_stalled_object_stream_times_out_and_refuses(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kew import publication_scan

    def quick_bounds() -> tuple[int, int, float]:
        return 1024 * 1024, 8 * 1024 * 1024, 2.0

    stalled = _StalledBatch()

    def popen_bytes(cmd, *, stdin=None, stdout=None, stderr=None, cwd=None, env=None):
        assert cmd[-2:] == ["cat-file", "--batch"]
        return stalled

    monkeypatch.setattr(publication_scan, "_scan_bounds", quick_bounds)
    monkeypatch.setattr(publication_scan, "popen_bytes", popen_bytes)
    _native_codex_run(store, "agent")
    (Path(scene["checkout"]) / "fix.txt").write_text("an ordinary change\n")

    with pytest.raises(CredentialPublicationRefused) as raised:
        _escape_publish(scene, store)

    assert raised.value.reason == "credential_scan_timeout"
    assert stalled.killed.is_set()
    assert _remote_head(scene) == scene["parent"]


# -- a credential the agent already pushed is reported as already published ---------


def test_republish_of_an_agent_pushed_credential_says_it_is_already_published(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _native_codex_run(store, "agent")
    checkout = Path(scene["checkout"])
    (checkout / "notes.txt").write_text(f"token {PLACEHOLDER}\n")
    _git(checkout, "add", "notes.txt")
    _git(checkout, "commit", "-qm", "fix: address review")
    _git(checkout, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")
    observed = _remote_head(scene)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(CredentialPublicationRefused) as raised:
        worktree.republish_agent_head(
            checkout,
            BRANCH,
            expected_sha=str(scene["parent"]),
            observed_sha=observed,
            commit_message="fix: address review feedback",
            repo="owner/repo",
            store=store,
            run_id="agent",
        )

    message = str(raised.value)
    assert raised.value.reason == "credential_already_published"
    assert "already pushed to the remote branch" in message
    assert "rotate that credential and remove the commit from the branch" in message
    assert "nothing was pushed" not in message
    assert raised.value.diagnosis == "CLAUDE_CODE_OAUTH_TOKEN in blob (value_raw)"
    (fact,) = store.list_audit_events(event="credential_already_published")
    assert (fact.run_id, fact.actor_type, fact.actor_id) == (
        "agent",
        "policy",
        "host-publication",
    )
    assert fact.payload["head_sha"] == observed
    assert fact.payload["findings"] == [
        {
            "source": "CLAUDE_CODE_OAUTH_TOKEN",
            "object": "blob",
            "encoding": "value_raw",
            "count": 1,
        }
    ]
    (scan,) = _scans(store, "agent")
    assert scan.payload["already_published"] is True
    assert scan.payload["verdict"] == "found"
    # The host neither rewrites nor republishes: the branch keeps the agent head.
    assert _remote_head(scene) == observed
    for surface in (message, _all_audit_text(store), caplog.text):
        assert PLACEHOLDER not in surface


def test_a_finding_only_in_the_host_built_republish_commit_is_not_already_published(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Governed review round 9 (L): ``already_published`` is per scanned target.

    The agent's pushed head is clean; the commit the host would build carries
    the credential (here, through its message). The remote never held that
    commit, so the refusal is ``credential_in_output`` and no
    ``credential_already_published`` fact is written.
    """
    _native_codex_run(store, "agent")
    checkout = Path(scene["checkout"])
    (checkout / "notes.txt").write_text("an ordinary review fix\n")
    _git(checkout, "add", "notes.txt")
    _git(checkout, "commit", "-qm", "fix: address review")
    _git(checkout, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")
    observed = _remote_head(scene)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(CredentialPublicationRefused) as raised:
        worktree.republish_agent_head(
            checkout,
            BRANCH,
            expected_sha=str(scene["parent"]),
            observed_sha=observed,
            commit_message=f"fix: address review feedback {PLACEHOLDER}",
            repo="owner/repo",
            store=store,
            run_id="agent",
        )

    assert raised.value.reason == "credential_in_output"
    assert raised.value.diagnosis == "CLAUDE_CODE_OAUTH_TOKEN in commit (value_raw)"
    assert store.list_audit_events(event="credential_already_published") == []
    scans = _scans(store, "agent")
    assert len(scans) == 2
    (agent_scan,) = [s for s in scans if s.payload["head_sha"] == observed]
    (host_scan,) = [s for s in scans if s.payload["head_sha"] != observed]
    assert agent_scan.payload["verdict"] == "clean"
    assert agent_scan.payload["already_published"] is True
    assert host_scan.payload["verdict"] == "found"
    assert host_scan.payload["already_published"] is False
    assert _remote_head(scene) == observed
    for surface in (str(raised.value), _all_audit_text(store), caplog.text):
        assert PLACEHOLDER not in surface


# -- review round 11 (H): the whole agent-pushed range is scanned, not its top ------


def _agent_commit(checkout: Path, text: str, message: str) -> None:
    (checkout / "notes.txt").write_text(text)
    _git(checkout, "add", "notes.txt")
    _git(checkout, "commit", "-qm", message)


def _push_agent_head(scene: dict[str, Path | str]) -> str:
    _git(Path(scene["checkout"]), "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")
    return _remote_head(scene)


def _republish_observed(
    scene: dict[str, Path | str], store: SqliteRunStore, observed: str
) -> CanonicalPublication:
    return worktree.republish_agent_head(
        Path(scene["checkout"]),
        BRANCH,
        expected_sha=str(scene["parent"]),
        observed_sha=observed,
        commit_message="fix: address review feedback",
        repo="owner/repo",
        store=store,
        run_id="agent",
    )


def test_a_credential_an_intermediate_agent_commit_added_and_removed_is_refused(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The canonical repository fetched the agent head one commit deep, so the
    scan saw only that head's final tree (round 11, H). The whole range counts.
    """
    _native_codex_run(store, "agent")
    checkout = Path(scene["checkout"])
    _agent_commit(checkout, f"token {PLACEHOLDER}\n", "wip")
    _agent_commit(checkout, "an ordinary review fix\n", "fix: address review")
    observed = _push_agent_head(scene)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(CredentialPublicationRefused) as raised:
        _republish_observed(scene, store, observed)

    assert raised.value.reason == "credential_already_published"
    assert raised.value.diagnosis == "CLAUDE_CODE_OAUTH_TOKEN in blob (value_raw)"
    assert _remote_head(scene) == observed
    for surface in (str(raised.value), _all_audit_text(store), caplog.text):
        assert PLACEHOLDER not in surface


def test_a_credential_in_an_intermediate_agent_commit_message_is_refused(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _native_codex_run(store, "agent")
    checkout = Path(scene["checkout"])
    _agent_commit(checkout, "first\n", f"wip {PLACEHOLDER}")
    _agent_commit(checkout, "an ordinary review fix\n", "fix: address review")
    observed = _push_agent_head(scene)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(CredentialPublicationRefused) as raised:
        _republish_observed(scene, store, observed)

    assert raised.value.reason == "credential_already_published"
    assert raised.value.diagnosis == "CLAUDE_CODE_OAUTH_TOKEN in commit (value_raw)"
    assert _remote_head(scene) == observed
    for surface in (str(raised.value), _all_audit_text(store), caplog.text):
        assert PLACEHOLDER not in surface


def test_an_agent_range_deeper_than_the_bound_is_refused_as_unavailable(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kew import publication_canonical

    monkeypatch.setattr(
        publication_canonical, "AGENT_RANGE_MAX_COMMITS", 2, raising=False
    )
    _native_codex_run(store, "agent")
    checkout = Path(scene["checkout"])
    for index in range(6):
        _agent_commit(checkout, f"step {index}\n", f"step {index}")
    observed = _push_agent_head(scene)

    with pytest.raises(CredentialPublicationRefused) as raised:
        _republish_observed(scene, store, observed)

    assert raised.value.reason == "credential_scan_unavailable"
    assert _remote_head(scene) == observed
    assert _scans(store, "agent") == []


def test_an_agent_head_that_does_not_descend_from_the_reviewed_head_is_refused(
    scene: dict[str, Path | str], store: SqliteRunStore
) -> None:
    _native_codex_run(store, "agent")
    checkout = Path(scene["checkout"])
    _git(checkout, "checkout", "-q", "--orphan", "rewritten")
    _git(checkout, "commit", "-qm", "rewritten history")
    _git(checkout, "push", "-q", "-f", "origin", f"HEAD:refs/heads/{BRANCH}")
    observed = _remote_head(scene)

    with pytest.raises(CredentialPublicationRefused) as raised:
        _republish_observed(scene, store, observed)

    assert raised.value.reason == "credential_scan_unavailable"
    assert _remote_head(scene) == observed


# -- round 12: the scan holds every credential the run was admitted with -----------

#: A provider credential name no runner declares, so only the run's recorded
#: ``env:`` auth path puts it in the needle set.
ADMITTED_ONLY_NAME = "KEW_TEST_ADMITTED_PROVIDER_TOKEN"
ADMITTED_ONLY_PLACEHOLDER = "kew-placeholder-admitted-provider-0123456789"


def _env_run(store: SqliteRunStore, name: str, containment: Containment) -> None:
    _run(store, "run", branch=BRANCH)
    _profile(store, "run", 1, containment=containment, auth_path=f"env:{name}")


@pytest.mark.parametrize("held", ["unset", "empty", "short"])
@pytest.mark.parametrize("containment", [Containment.CONTAINED, Containment.NATIVE])
def test_an_env_run_whose_publisher_lacks_the_credential_is_unavailable(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
    held: str,
    containment: Containment,
) -> None:
    _env_run(store, "CLAUDE_CODE_OAUTH_TOKEN", containment)
    if held == "unset":
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    else:
        monkeypatch.setenv(
            "CLAUDE_CODE_OAUTH_TOKEN", "" if held == "empty" else "short-value"
        )
    (Path(scene["checkout"]) / "fix.txt").write_text("an ordinary change\n")

    with pytest.raises(CredentialPublicationRefused) as raised:
        _publish(scene, store, "run")

    assert raised.value.reason == "credential_scan_unavailable"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in raised.value.diagnosis
    assert _remote_head(scene) == scene["parent"]
    assert _scans(store, "run") == []


_ADMITTED_VALUES = {
    "CLAUDE_CODE_OAUTH_TOKEN": PLACEHOLDER,
    ADMITTED_ONLY_NAME: ADMITTED_ONLY_PLACEHOLDER,
}


@pytest.mark.parametrize("name", sorted(_ADMITTED_VALUES))
def test_an_env_run_holding_its_credential_refuses_it_in_output(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    value = _ADMITTED_VALUES[name]
    monkeypatch.setenv(name, value)
    _env_run(store, name, Containment.CONTAINED)
    (Path(scene["checkout"]) / "notes.txt").write_text(f"copied {value}\n")

    with pytest.raises(CredentialPublicationRefused) as raised:
        _publish(scene, store, "run")

    assert raised.value.reason == "credential_in_output"
    assert f"{name} in blob (value_raw)" in raised.value.diagnosis
    assert _remote_head(scene) == scene["parent"]
    (scan,) = _scans(store, "run")
    assert scan.payload["verdict"] == "found"
    assert value not in _all_audit_text(store)


@pytest.mark.parametrize("name", sorted(_ADMITTED_VALUES))
def test_an_env_run_holding_its_credential_publishes_clean_output(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    monkeypatch.setenv(name, _ADMITTED_VALUES[name])
    _env_run(store, name, Containment.CONTAINED)
    (Path(scene["checkout"]) / "fix.txt").write_text("an ordinary change\n")

    published = _publish(scene, store, "run")

    assert _remote_head(scene) == published.commit_sha
    (scan,) = _scans(store, "run")
    assert scan.payload["verdict"] == "clean"


def _unusable_codex_home(tmp_path: Path, kind: str) -> Path:
    import os

    home = tmp_path / f"unusable-codex-{kind}"
    home.mkdir()
    login = home / "auth.json"
    if kind == "malformed":
        login.write_text("{not json")
    elif kind == "fifo":
        os.mkfifo(login)
    elif kind == "directory":
        login.mkdir()
    elif kind == "oversize":
        login.write_text(json.dumps({"pad": "x" * (1024 * 1024)}))
    elif kind == "no_secret":
        login.write_text(json.dumps({"tokens": {"account_id": "0" * 36}}))
    return home


@pytest.mark.parametrize(
    "kind", ["absent", "malformed", "fifo", "directory", "oversize", "no_secret"]
)
def test_a_native_run_without_a_readable_codex_login_is_unavailable(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(_unusable_codex_home(tmp_path, kind)))
    _native_codex_run(store)
    (Path(scene["checkout"]) / "fix.txt").write_text("an ordinary change\n")

    with pytest.raises(CredentialPublicationRefused) as raised:
        _publish(scene, store, "native")

    assert raised.value.reason == "credential_scan_unavailable"
    assert _remote_head(scene) == scene["parent"]
    assert _scans(store, "native") == []


def test_a_native_run_refuses_its_codex_login_jwt_in_output(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jwt = ".".join(
        base64.urlsafe_b64encode(part).decode().rstrip("=")
        for part in (b'{"alg":"none"}', b'{"sub":"kew-placeholder"}', b"sig-0000")
    )
    home = tmp_path / "jwt-codex-home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": jwt}}))
    monkeypatch.setenv("CODEX_HOME", str(home))
    _native_codex_run(store)
    (Path(scene["checkout"]) / "notes.txt").write_text(f"id {jwt}\n")

    with pytest.raises(CredentialPublicationRefused) as raised:
        _publish(scene, store, "native")

    assert raised.value.reason == "credential_in_output"
    assert "codex_auth:tokens.id_token in blob (value_raw)" in raised.value.diagnosis
    assert jwt not in _all_audit_text(store)


@pytest.mark.parametrize("kind", ["absent", "malformed", "fifo", "no_secret"])
def test_a_non_native_run_with_an_unusable_codex_login_still_scans(
    scene: dict[str, Path | str],
    store: SqliteRunStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(_unusable_codex_home(tmp_path, kind)))
    _contained_env_run(store)
    checkout = Path(scene["checkout"])
    (checkout / "fix.txt").write_text("an ordinary change\n")

    published = _publish(scene, store, "run")

    assert _remote_head(scene) == published.commit_sha
    (clean,) = _scans(store, "run")
    assert clean.payload["verdict"] == "clean"
    (checkout / "notes.txt").write_text(f"copied {PLACEHOLDER}\n")
    with pytest.raises(CredentialPublicationRefused) as raised:
        worktree.publish_existing_pr_branch(
            checkout,
            BRANCH,
            commit_message="fix: again",
            expected_remote_sha=published.commit_sha,
            repo="owner/repo",
            publish="canonical",
            store=store,
            run_id="run",
        )
    assert raised.value.reason == "credential_in_output"

"""Detached host-publication credential and diagnostic boundaries."""

from __future__ import annotations

from typing import get_args
from unittest.mock import MagicMock

import pytest

import kew.publication as publication
from kew.publication import (
    DETACHED_PUBLICATION_TOKEN_ENV,
    PublicationError,
    PublicationStep,
    capture_detached_publication_token,
    publication_failure,
    publication_git_env,
    sanitize_publication_error,
    sanitize_publication_evidence,
    select_publication_mode,
)
from kew.publication_job import STEP_COMMIT, STEP_PR_CREATE, STEP_PUSH

_EXTRA_HEADER_KEY = "http.https://github.com/.extraHeader"


@pytest.mark.parametrize("scheme", ["", "socks5://"])
@pytest.mark.parametrize(
    "sanitize", [sanitize_publication_error, sanitize_publication_evidence]
)
def test_proxy_value_redacted_before_publication_truncation(
    monkeypatch, scheme, sanitize
):
    secret = f"{scheme}user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    detail = "git push failed " + "x" * 455 + " " + secret + " tail"

    cleaned = sanitize(detail)

    assert "s3cret" not in cleaned
    assert "<HTTP_PROXY redacted>" in cleaned
    assert len(cleaned) <= 500


def test_checkout_publication_requires_explicit_uncontained_admission() -> None:
    with pytest.raises(
        PublicationError,
        match=r"#1218.*uncontained-publication",
    ):
        select_publication_mode("checkout")


def test_checkout_publication_accepts_explicit_uncontained_admission() -> None:
    assert (
        select_publication_mode(
            "checkout",
            dangerously_skip_containment=True,
        )
        == "checkout"
    )


def test_capture_reads_stored_gh_auth_before_detach(monkeypatch) -> None:
    monkeypatch.delenv("KEW_DETACHED_PUBLICATION_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setitem(publication.TOOL_PATHS, "gh", "/usr/bin/gh")
    mock_run = MagicMock(
        return_value=MagicMock(returncode=0, stdout="gho_from_keychain\n")
    )
    monkeypatch.setattr("kew.publication.run", mock_run)

    assert capture_detached_publication_token() == "gho_from_keychain"
    mock_run.assert_called_once_with(["gh", "auth", "token"], check=False, timeout=10)


def test_publication_error_detail_is_bounded_and_secret_free() -> None:
    detail = sanitize_publication_error(
        "fatal: https://alice:password@github.com/o/r?token=ghp_abcdefgh "
        "authorization: Bearer bearer-secret\n" + "x" * 800
    )

    assert len(detail) == 500
    assert "alice" not in detail
    assert "password" not in detail
    assert "ghp_abcdefgh" not in detail
    assert "bearer-secret" not in detail
    assert "https://github.com/o/r" in detail


_evidence = sanitize_publication_evidence


def test_operator_facing_errors_keep_the_salvage_path() -> None:
    """The push-failure message names the preserved checkout on purpose;
    only durable evidence is path-redacted."""
    detail = sanitize_publication_error(
        "git push failed (checkout preserved at /tmp/wt) — push manually"
    )

    assert "/tmp/wt" in detail


def test_evidence_redacts_absolute_path_ancestry() -> None:
    detail = _evidence(
        "missing /Users/someone/Projects/x/logs/secret-layout/manifest.json"
    )

    assert "/Users/" not in detail
    assert "Projects" not in detail
    assert "secret-layout" not in detail
    assert "<path>/manifest.json" in detail


def test_evidence_redacts_home_relative_path_ancestry() -> None:
    detail = _evidence("missing ~/Projects/x/logs/secret-layout/manifest.json")

    assert "~/" not in detail
    assert "Projects" not in detail
    assert "secret-layout" not in detail
    assert "<path>/manifest.json" in detail


def test_evidence_redacts_file_uri_paths() -> None:
    detail = _evidence(
        "unable to open file:///Users/someone/Projects/kew/logs/dispatch/dispatch.db"
    )

    assert "/Users/" not in detail
    assert "file://" not in detail
    assert "<path>/dispatch.db" in detail


def test_evidence_preserves_sanitized_url_path() -> None:
    detail = _evidence(
        "fetch https://example.com/api/v1/resource?token=secret failed at "
        "/Users/someone/private/result.json"
    )

    assert "https://example.com/api/v1/resource" in detail
    assert "token=secret" not in detail
    assert "/Users/" not in detail
    assert "<path>/result.json" in detail


def test_evidence_preserves_relative_path() -> None:
    detail = _evidence("missing logs/dispatch/x.txt")

    assert detail == "missing logs/dispatch/x.txt"


def test_evidence_redacts_colon_prefixed_paths() -> None:
    """The ``word:/abs/path`` shape — fatal:, sqlite: — is a path, not prose."""
    detail = _evidence("fatal:/Users/alice/private/file.txt")

    assert detail == "fatal:<path>/file.txt"


def test_evidence_redacts_every_file_uri_form() -> None:
    """RFC 8089 allows one slash and an authority, not only ``file:///``."""
    assert (
        _evidence("missing file:/Users/alice/private/file.txt")
        == "missing <path>/file.txt"
    )
    assert (
        _evidence("missing file://localhost/Users/alice/private/file.txt")
        == "missing <path>/file.txt"
    )


def test_evidence_collapses_slash_runs_before_redacting() -> None:
    """``//Users`` and ``a//b`` are the same paths wearing extra separators."""
    assert (
        _evidence("missing //Users/alice/private/file.txt") == "missing <path>/file.txt"
    )
    assert (
        _evidence("missing /Users/alice//private/file.txt") == "missing <path>/file.txt"
    )


def test_evidence_redacts_quoted_paths_with_spaces() -> None:
    """Git quotes the path it reports; the quoted span is one path, spaces
    and all."""
    detail = _evidence(
        "unable to access '/Users/alice/Client X/secret-layout/manifest.json'"
    )

    assert detail == "unable to access '<path>/manifest.json'"


def test_evidence_decodes_encoded_separators() -> None:
    """%2F ancestry is still ancestry."""
    detail = _evidence("missing file:///%2FUsers%2Falice%2Fprivate%2Ffile.txt")

    assert "Users" not in detail
    assert "<path>/file.txt" in detail


def test_evidence_redacts_a_spliced_secret_after_decoding() -> None:
    """A %2F splice must not hide a secret assignment from the secret rule —
    decoding runs before every judging rule, not after."""
    detail = _evidence("prefix%2Ftoken=after_decode_secret")

    assert "after_decode_secret" not in detail
    assert "token=<redacted>" in detail


def test_evidence_decodes_nested_encodings() -> None:
    """%252F is %2F wearing a second coat."""
    detail = _evidence("missing file:///%252FUsers%252Falice%252Fprivate%252Ffile.txt")

    assert "Users" not in detail
    assert "%2F" not in detail.upper().replace("%252F", "")
    assert "<path>/file.txt" in detail


def test_evidence_redacts_named_home_and_home_variable_paths() -> None:
    """``~alice/``, ``$HOME/`` and ``${HOME}/`` are ancestry like ``~/``."""
    assert _evidence("missing ~alice/private/x") == "missing <path>/x"
    assert _evidence("missing $HOME/private/x") == "missing <path>/x"
    assert _evidence("missing ${HOME}/private/x") == "missing <path>/x"


def test_evidence_redacts_rsync_and_drive_prefixed_paths() -> None:
    assert _evidence("missing host:/Users/x/private/y") == "missing host:<path>/y"
    assert _evidence("missing C:/Users/x/private/y") == "missing C:<path>/y"


def test_evidence_redacts_a_double_quoted_path_with_an_apostrophe() -> None:
    """One branch per quote character: the other quote may sit inside."""
    detail = _evidence('missing "/Users/x/it\'s odd/f.txt"')

    assert detail == 'missing "<path>/f.txt"'


def test_evidence_redacts_after_an_unclosed_quote() -> None:
    """A dangling quote must not disarm the token rule."""
    detail = _evidence('missing "/Users/x/private/y')

    assert detail == 'missing "<path>/y'


def test_evidence_leaves_non_path_slashes_alone() -> None:
    """The boundary rules must not eat scheme remnants, versions, or dates."""
    assert (
        _evidence("ssh://git@host/a/b and 3.12/site-packages")
        == "ssh://git@host/a/b and 3.12/site-packages"
    )
    assert _evidence("2026/08/18 ratio 1/2") == "2026/08/18 ratio 1/2"


def test_evidence_leaves_tilde_fractions_alone() -> None:
    """``~`` names a home only before ``/`` or a username — not a fraction."""
    assert _evidence("approx ~5/10 done") == "approx ~5/10 done"
    assert _evidence("~=3/4") == "~=3/4"


def test_git_env_appends_after_inherited_git_config_entries(monkeypatch) -> None:
    """The auth entry must extend the caller's git config, never overwrite it."""
    monkeypatch.setenv(DETACHED_PUBLICATION_TOKEN_ENV, "gho_tok")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")

    env = publication_git_env()

    assert env is not None
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert env["GIT_CONFIG_KEY_2"] == _EXTRA_HEADER_KEY
    assert env["GIT_CONFIG_VALUE_2"].startswith("Authorization: Basic ")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # The credential rides base64 in the header, never in cleartext.
    assert "gho_tok" not in env["GIT_CONFIG_VALUE_2"]


def test_git_env_resets_an_unusable_git_config_count(monkeypatch) -> None:
    """A malformed inherited count must not drop the credential entry."""
    monkeypatch.setenv(DETACHED_PUBLICATION_TOKEN_ENV, "gho_tok")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "not-a-number")

    env = publication_git_env()

    assert env is not None
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == _EXTRA_HEADER_KEY


def test_git_env_is_none_without_a_captured_token(monkeypatch) -> None:
    monkeypatch.delenv(DETACHED_PUBLICATION_TOKEN_ENV, raising=False)

    assert publication_git_env() is None


def test_publication_failure_keeps_the_typed_step() -> None:
    step, detail = publication_failure(PublicationError("pr_create", "boom"))

    assert step == "pr_create"
    assert detail == "boom"


def test_publication_failure_prefers_the_terminal_step_over_quoted_commits() -> None:
    """A failed push quotes commit text; the step must stay the real one."""
    step, _ = publication_failure(
        RuntimeError(
            "error: failed to push some refs; "
            "hint: Updates were rejected because a commit exists remotely"
        )
    )

    assert step == "push"


def test_publication_failure_infers_unknown_for_opaque_errors() -> None:
    step, detail = publication_failure(RuntimeError("connection reset"))

    assert step == "unknown"
    assert detail == "connection reset"


def test_the_push_and_pr_create_steps_are_the_typed_publication_steps() -> None:
    """I09B records a step before every effect, and a failure reports one after.

    They have to be the same strings: a row saying ``pr_create`` was attempted
    and an error saying ``pr_create`` failed are the same fact from two sides,
    and two vocabularies for one step would let a reader believe they are not.
    """
    assert {STEP_COMMIT, STEP_PUSH, STEP_PR_CREATE} <= set(get_args(PublicationStep))

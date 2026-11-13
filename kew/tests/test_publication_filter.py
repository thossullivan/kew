"""W1.1 node 4: what may reach a host-owned clone (D4 filter).

Each test attempts the thing rather than describing it. A filter suite that
only asserts "a symlink is rejected" tells you the symlink branch is reachable;
it does not tell you the ``.git`` check survives a fullwidth character.

No callers yet, by construction — node 5 shadow-compares before anything flips.
"""

from __future__ import annotations

import pytest

from kew.publication_filter import (
    FilterVerdict,
    ManifestEntry,
    PublicationRefused,
    ReviewRequired,
    filter_artifact,
    filter_manifest,
    protected_path_reason,
)


def _entry(path: str, mode: str = "100644") -> ManifestEntry:
    return ManifestEntry(path=path, mode=mode, sha256="a" * 64)


def _filter(entries=(), **overrides):
    """``filter_manifest`` with the destination inputs stated once.

    The production signature REQUIRES ``deletions``, ``existing_modes`` and
    ``destination_paths`` on purpose: a caller that omits one gets less
    checking than it thinks it does. Tests say the empty defaults here so each
    case shows only what it actually exercises — and so this file does not
    model the omission-prone call shape it exists to guard.
    """
    kwargs = {"deletions": (), "existing_modes": {}, "destination_paths": ()}
    kwargs.update(overrides)
    return filter_manifest(entries, **kwargs)


def _rules(verdict: FilterVerdict) -> set[str]:
    return {r.rule for r in verdict.rejections}


class TestTheApiCannotExpressAPartialPublication:
    """ "Reject loudly, never silently drop" — structurally, not by convention."""

    def test_reading_approved_entries_raises_when_anything_was_rejected(self):
        verdict = _filter([_entry("src/main.py"), _entry(".git/config")])

        with pytest.raises(PublicationRefused, match="dot_git|\\.git"):
            verdict.approved()

    def test_the_refusal_carries_every_rejection_for_the_caller(self):
        verdict = _filter([_entry("../escape"), _entry("/abs")])

        try:
            verdict.approved()
        except PublicationRefused as exc:
            assert len(exc.rejections) == 2
        else:  # pragma: no cover - the assertion above is the point
            pytest.fail("expected refusal")

    def test_a_clean_manifest_yields_its_entries(self):
        verdict = _filter([_entry("src/main.py"), _entry("bin/run", "100755")])

        assert not verdict.refused
        assert [e.path for e in verdict.approved()] == ["src/main.py", "bin/run"]


class TestGeneratedCacheScope:
    @pytest.mark.parametrize(
        "filename",
        [
            ".mypy_cache",
            "__pycache__",
            ".ruff_cache",
            ".pytest_cache",
            ".import_linter_cache",
        ],
    )
    def test_reserved_name_as_a_regular_file_remains_in_scope(self, filename):
        entry = _entry(f"src/{filename}")

        assert _filter([entry]).approved() == (entry,)

    @pytest.mark.parametrize(
        "directory",
        [
            ".mypy_cache",
            "__pycache__",
            ".ruff_cache",
            ".pytest_cache",
            ".import_linter_cache",
        ],
    )
    def test_untracked_cache_writes_are_outside_publication_scope(self, directory):
        verdict = _filter([_entry(f"nested/{directory}/state.json")])

        assert verdict.approved() == ()

    @pytest.mark.parametrize(
        "path",
        [".mypy_cacheable/state", "__pycache___/state", ".pytest_cache.txt"],
    )
    def test_similar_names_remain_in_publication_scope(self, path):
        verdict = _filter([_entry(path)])

        assert verdict.approved() == (_entry(path),)

    def test_tracked_cache_write_remains_in_publication_scope(self):
        path = ".mypy_cache/tracked-fixture.json"
        verdict = _filter(
            [_entry(path)],
            existing_modes={path: "100644"},
            destination_paths=(path,),
        )

        assert verdict.approved() == (_entry(path),)

    def test_destination_paths_alone_preserve_a_tracked_cache_write(self):
        path = ".mypy_cache/tracked-fixture.json"
        verdict = _filter([_entry(path)], destination_paths=(path,))

        assert verdict.approved() == (_entry(path),)

    def test_existing_modes_alone_preserve_a_tracked_cache_write(self):
        path = ".mypy_cache/tracked-fixture.json"
        verdict = _filter([_entry(path)], existing_modes={path: "100644"})

        assert verdict.approved() == (_entry(path),)

    def test_tracked_cache_deletion_remains_in_publication_scope(self):
        path = ".mypy_cache/tracked-fixture.json"
        verdict = _filter(
            deletions=(path,),
            existing_modes={path: "100644"},
            destination_paths=(path,),
        )

        assert verdict.deletions() == (path,)

    def test_unsafe_cache_entry_is_refused_before_scope_exclusion(self):
        verdict = _filter([_entry(".ruff_cache/link", "120000")])

        assert _rules(verdict) == {"mode_not_allowed"}
        with pytest.raises(PublicationRefused):
            verdict.approved()

    def test_cache_entry_requiring_review_is_not_silently_excluded(self):
        verdict = _filter([_entry(".pytest_cache/pyproject.toml")])

        with pytest.raises(ReviewRequired):
            verdict.approved()

    def test_cache_gitattributes_driver_is_refused_before_exclusion(self):
        verdict = _filter(
            [_entry(".import_linter_cache/.gitattributes")],
            read_text=lambda _path: "* filter=attack\n",
        )

        assert _rules(verdict) == {"gitattributes_driver"}


class TestDependencyInstallScope:
    """#2099: untracked node_modules/.venv content is not work."""

    @pytest.mark.parametrize(
        "path",
        [
            "frontend/node_modules/acorn/bin/acorn",
            "kew/.venv/lib/python3.13/site.py",
            "a/b/node_modules/x",
            "node_modules/pkg/node_modules/inner/index.js",
        ],
    )
    def test_untracked_install_writes_are_outside_publication_scope(self, path):
        assert _filter([_entry(path)]).approved() == ()

    def test_a_tracked_install_directory_keeps_its_entries(self):
        tracked = "vendor/node_modules/pkg/index.js"
        added = "vendor/node_modules/pkg/added.js"
        untracked = "frontend/node_modules/acorn/index.js"
        verdict = _filter(
            [_entry(tracked), _entry(added), _entry(untracked)],
            existing_modes={tracked: "100644"},
            destination_paths=(tracked,),
        )

        assert verdict.approved() == (_entry(tracked), _entry(added))

    def test_a_tracked_case_variant_counts_as_tracked(self):
        verdict = _filter(
            [_entry("web/node_modules/pkg/index.js")],
            destination_paths=("WEB/node_modules/other.js",),
        )

        assert verdict.approved() == (_entry("web/node_modules/pkg/index.js"),)

    @pytest.mark.parametrize(
        "path", ["src/node_modules", "tools/.venv", "node_modules_old/x", ".venvs/x"]
    )
    def test_the_name_itself_or_a_lookalike_remains_in_scope(self, path):
        assert _filter([_entry(path)]).approved() == (_entry(path),)

    def test_a_symlink_beneath_an_install_is_refused_before_exclusion(self):
        verdict = _filter([_entry("frontend/node_modules/.bin/acorn", "120000")])

        assert _rules(verdict) == {"mode_not_allowed"}
        with pytest.raises(PublicationRefused):
            verdict.approved()

    def test_an_installs_own_package_json_does_not_hold_the_publication(self):
        """#2105: every npm package carries one, and none of them is published."""
        work = _entry("src/main.py")
        verdict = _filter([work, _entry("frontend/node_modules/acorn/package.json")])

        assert verdict.flags == ()
        assert not verdict.review_required
        assert verdict.approved() == (work,)

    def test_a_flag_on_published_work_survives_an_install_in_the_same_manifest(self):
        verdict = _filter(
            [
                _entry(".github/workflows/ci.yml"),
                _entry("frontend/node_modules/acorn/package.json"),
            ]
        )

        assert verdict.review_reasons() == (
            ".github/workflows/ci.yml: CI workflow code, "
            "which runs with repository secrets",
        )

    def test_an_install_flag_survives_inside_a_tracked_install_directory(self):
        tracked = "vendor/node_modules/pkg/index.js"
        verdict = _filter(
            [_entry(tracked), _entry("vendor/node_modules/pkg/package.json")],
            destination_paths=(tracked,),
        )

        assert verdict.review_reasons() == (
            "vendor/node_modules/pkg/package.json: executed by common local "
            "or build tooling",
        )

    def test_two_install_paths_that_fold_together_do_not_refuse_the_publication(self):
        work = _entry("src/main.py")
        verdict = _filter(
            [
                work,
                _entry("frontend/node_modules/pkg/README.md"),
                _entry("frontend/node_modules/pkg/readme.md"),
            ]
        )

        assert verdict.rejections == ()
        assert verdict.approved() == (work,)

    def test_a_duplicated_install_path_does_not_refuse_the_publication(self):
        path = "frontend/node_modules/acorn/index.js"
        verdict = _filter([_entry(path), _entry(path)])

        assert verdict.rejections == ()
        assert verdict.approved() == ()

    def test_a_destination_collision_means_a_tracked_install_and_still_refuses(self):
        """A destination path folding onto an entry is tracked BENEATH the install."""
        verdict = _filter(
            [_entry("frontend/node_modules/pkg/README.md")],
            destination_paths=("FRONTEND/node_modules/pkg/readme.md",),
        )

        assert _rules(verdict) == {"name_collision"}

    def test_a_file_and_a_directory_of_one_name_inside_an_install_do_not_refuse(self):
        verdict = _filter(
            [
                _entry("frontend/node_modules/pkg/lib"),
                _entry("frontend/node_modules/pkg/lib/index.js"),
            ]
        )

        assert verdict.rejections == ()
        assert verdict.approved() == ()

    def test_an_install_path_written_and_deleted_leaves_only_the_deletion(self):
        """Out of scope, the write no longer races the removal for the same name."""
        path = "frontend/node_modules/acorn/index.js"
        verdict = _filter([_entry(path)], deletions=(path,))

        assert verdict.rejections == ()
        assert verdict.approved() == ()
        assert verdict.deletions() == (path,)

    def test_a_collision_inside_a_tracked_install_directory_still_refuses(self):
        tracked = "vendor/node_modules/pkg/index.js"
        verdict = _filter(
            [
                _entry(tracked),
                _entry("vendor/node_modules/pkg/README.md"),
                _entry("vendor/node_modules/pkg/readme.md"),
            ],
            destination_paths=(tracked,),
        )

        assert _rules(verdict) == {"name_collision"}


class TestUntrackedWorktreeState:
    @pytest.mark.parametrize(
        "path",
        [
            ".worktrees/agent/issue-1.lock",
            "kew/.worktrees/agent/issue-1.lock",
            "services/api/.worktrees/agent/issue-SFD-251.lock",
        ],
    )
    def test_untracked_worktree_segment_is_refused_at_every_depth(self, path: str):
        verdict = _filter([_entry(path)])

        assert _rules(verdict) == {"untracked_worktree_state"}
        assert verdict.rejections[0].path == path

    def test_refusal_runs_before_generated_cache_scope_exclusion(self):
        path = ".ruff_cache/.worktrees/agent/issue-1.lock"

        verdict = _filter([_entry(path)])

        assert _rules(verdict) == {"untracked_worktree_state"}
        with pytest.raises(PublicationRefused):
            verdict.approved()

    def test_trusted_destination_membership_preserves_a_tracked_update(self):
        path = "kew/.worktrees/fixtures/tracked.lock"
        entry = _entry(path)
        verdict = _filter(
            [entry],
            existing_modes={path: "100644"},
            destination_paths=(path,),
        )

        assert verdict.approved() == (entry,)

    def test_tracked_worktree_deletion_uses_ordinary_validation(self):
        path = "kew/.worktrees/fixtures/tracked.lock"
        verdict = _filter(
            deletions=(path,),
            existing_modes={path: "100644"},
            destination_paths=(path,),
        )

        assert verdict.deletions() == (path,)

    def test_similar_directory_name_remains_publishable(self):
        entry = _entry("kew/.worktrees-cache/state.lock")

        assert _filter([entry]).approved() == (entry,)

    def test_one_worktree_refusal_preserves_the_complete_artifact(self):
        safe = _entry("src/safe.py")
        refused = _entry("kew/.worktrees/agent/issue-1010.lock")
        verdict = _filter([safe, refused])

        assert verdict.rejections[0].path == refused.path
        with pytest.raises(PublicationRefused):
            verdict.approved()


class TestDotGitIsUnreachable:
    """The reach that doors 1-4 were all about, now at the artifact layer."""

    @pytest.mark.parametrize(
        "path",
        [
            ".git/config",
            ".git/hooks/pre-commit",
            "nested/deep/.git/config",
            ".GIT/config",
            ".Git/config",
            ".git./config",  # Windows strips the trailing dot
            ".git /config",  # ...and the trailing space
            "GIT~1/config",  # 8.3 short name resolves to .git
            "git~2/config",
            ".ｇit/config",  # NFKC fullwidth g
        ],
    )
    def test_it_is_rejected(self, path: str):
        verdict = _filter([_entry(path)])

        assert _rules(verdict) == {"dot_git"}, path

    # HFS+ omits these code points entirely when comparing filenames, so on
    # macOS every one of these spellings opens the real .git directory. This is
    # git's own `next_hfs_char` set, the one behind core.protectHFS.
    HFS_IGNORABLE = [
        0x200C, 0x200D, 0x200E, 0x200F,
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
        0x206A, 0x206B, 0x206C, 0x206D, 0x206E, 0x206F,
        0xFEFF,
    ]  # fmt: skip

    @pytest.mark.parametrize("code_point", HFS_IGNORABLE)
    def test_an_hfs_ignorable_character_does_not_hide_dot_git(self, code_point: int):
        """macOS is this project's primary platform, so this is not theoretical."""
        path = f".gi{chr(code_point)}t/config"
        verdict = _filter([_entry(path)])

        assert _rules(verdict) == {"dot_git"}, hex(code_point)

    def test_an_ignorable_character_cannot_hide_the_short_name_alias_either(self):
        """Both .git spellings go through the same normalization.

        Written as an escape, not a literal: an invisible character pasted into
        source is unreviewable, and this file is the record of what we refuse.
        """
        verdict = _filter([_entry("GI\u200cT~1/config")])

        assert _rules(verdict) == {"dot_git"}

    @pytest.mark.parametrize(
        "path", [".gitignore", ".github/CODEOWNERS", "src/gitlab.py", "a/.gitkeep"]
    )
    def test_names_that_merely_start_with_git_are_not(self, path: str):
        """Over-rejecting is its own failure: it makes the gate get disabled."""
        verdict = _filter([_entry(path)])

        assert "dot_git" not in _rules(verdict), path


class TestOnlyRegularFiles:
    @pytest.mark.parametrize(
        ("mode", "fragment"),
        [
            ("120000", "symlink"),
            ("160000", "gitlink"),
            ("040000", "directory"),
            ("100664", "non-canonical"),
            ("777", "not a regular file"),
        ],
    )
    def test_anything_but_a_file_is_rejected(self, mode: str, fragment: str):
        verdict = _filter([_entry("payload", mode)])

        assert _rules(verdict) == {"mode_not_allowed"}
        assert fragment in verdict.rejections[0].detail

    def test_an_unknown_mode_is_rejected_without_being_enumerated(self):
        """The allowlist is the defence; the names only make refusals legible."""
        verdict = _filter([_entry("payload", "010600")])

        assert _rules(verdict) == {"mode_not_allowed"}

    def test_making_a_tracked_file_executable_is_rejected(self):
        """No content change needed — the diff would look empty."""
        verdict = _filter(
            [_entry("scripts/build.sh", "100755")],
            existing_modes={"scripts/build.sh": "100644"},
        )

        assert _rules(verdict) == {"mode_escalation"}

    def test_escalation_is_caught_on_a_case_differing_destination_name(self):
        """`existing_modes` and `destination_paths` must key the same way.

        Comparing one by exact string and the other by folded name left a hole:
        a caller supplying modes but NOT paths got no escalation check at all
        here, because the collision rule that would otherwise have caught the
        name never ran.
        """
        verdict = _filter(
            [_entry("scripts/Build.sh", "100755")],
            existing_modes={"scripts/build.sh": "100644"},
        )

        assert _rules(verdict) == {"mode_escalation"}

    def test_a_file_that_was_already_executable_is_fine(self):
        verdict = _filter(
            [_entry("scripts/build.sh", "100755")],
            existing_modes={"scripts/build.sh": "100755"},
        )

        assert not verdict.refused

    def test_a_new_executable_is_fine(self):
        """Only ESCALATION of an existing file is the finding."""
        verdict = _filter([_entry("scripts/new.sh", "100755")])

        assert not verdict.refused


class TestPathShape:
    @pytest.mark.parametrize(
        ("path", "rule"),
        [
            ("../outside", "traversal"),
            ("a/../../outside", "traversal"),
            ("a//b", "traversal"),
            ("./a", "traversal"),
            ("/etc/passwd", "absolute_path"),
            ("a\\b", "backslash"),
            # A drive-absolute path: the colon rule now covers what a
            # drive-letter pattern used to, so there is one rule, not two.
            ("C:/Windows/x", "colon"),
            # `C:file` is DRIVE-RELATIVE on Windows — no separator, so a
            # `^[A-Za-z]:[\\/]` absolute-path pattern misses it entirely.
            ("C:file", "colon"),
            # An NTFS alternate data stream: content that does not show up in
            # an ordinary listing of notes.txt.
            ("notes.txt:hidden", "colon"),
            ("a/b:stream", "colon"),
            # NFKC maps FULLWIDTH FULL STOP to '.', so these are not '..' to a
            # raw string comparison but are to every other rule in the module.
            ("．．/secret", "traversal"),
            ("a/．．/b", "traversal"),
            ("．/a", "traversal"),
            # ...and FULLWIDTH SOLIDUS to '/', so one component to split('/')
            # becomes two to anything that normalizes first.
            ("a／b", "hidden_separator"),
            ("a＼b", "hidden_separator"),
            ("bad\nname", "control_characters"),
            ("bad\x7fname", "control_characters"),
            (" padded", "empty_or_padded_path"),
            ("", "empty_or_padded_path"),
        ],
    )
    def test_it_is_rejected(self, path: str, rule: str):
        verdict = _filter([_entry(path)])

        assert rule in _rules(verdict), path


class TestNameCollisions:
    def test_two_entries_a_filesystem_cannot_tell_apart(self):
        """One silently overwrites the other, and the manifest does not say which."""
        verdict = _filter([_entry("README.md"), _entry("readme.md")])

        assert _rules(verdict) == {"name_collision"}

    def test_a_collision_with_something_already_in_the_destination(self):
        verdict = _filter([_entry("src/Utils.py")], destination_paths=["src/utils.py"])

        assert _rules(verdict) == {"name_collision"}

    def test_unicode_normalization_collisions_count_too(self):
        """NFC and NFD spellings of é are one file on macOS."""
        verdict = _filter([_entry("caf\u00e9.txt"), _entry("cafe\u0301.txt")])

        assert _rules(verdict) == {"name_collision"}

    def test_stripping_an_ignorable_can_create_a_composable_pair(self):
        """One normalization pass is not a fixed point.

        Deleting U+200C leaves ``a`` adjacent to a combining acute it was not
        next to before, giving the DECOMPOSED form \u2014 a different string from
        precomposed ``\u00e1``, while HFS (which ignores U+200C and decomposes
        canonically) treats both as one file. Found by adversarial review.
        """
        verdict = _filter([_entry("a\u200c\u0301.txt"), _entry("\u00e1.txt")])

        assert _rules(verdict) == {"name_collision"}

    def test_the_same_path_twice_is_rejected(self):
        """Found by enumerating the SET dimension, not by imagining an attack.

        The exact-duplicate case is not the harmless one. Two entries naming
        one path carry different content, so the apply step writes both and the
        winner depends on iteration order rather than on anything the manifest
        states.
        """
        verdict = _filter(
            [
                ManifestEntry("src/a.py", "100644", "a" * 64),
                ManifestEntry("src/a.py", "100644", "b" * 64),
            ]
        )

        assert _rules(verdict) == {"duplicate_path"}

    def test_updating_a_file_that_already_exists_is_not_a_collision(self):
        """The common case: the manifest changes a file the destination has."""
        verdict = _filter(
            [_entry("src/utils.py")], destination_paths=["src/utils.py", "src/other.py"]
        )

        assert not verdict.refused

    def test_distinct_names_do_not_collide(self):
        verdict = _filter([_entry("a.py"), _entry("b.py"), _entry("dir/a.py")])

        assert not verdict.refused


class TestAnAncestorThatIsAlreadyAFile:
    """Adversarial review, round 2 — the High finding.

    The path rules cannot see this one, because the danger is in the
    DESTINATION rather than in the name.
    """

    def test_writing_through_a_tracked_symlink_is_refused(self):
        """The destination tracks `admin -> .git`.

        Nothing in `admin/hooks/pre-commit` contains `.git`, every component is
        clean, and the mode is a regular file — but the write follows `admin`
        and lands in the repository's own hooks directory.
        """
        verdict = _filter(
            [_entry("admin/hooks/pre-commit")],
            existing_modes={"admin": "120000"},
            destination_paths=["admin"],
        )

        assert _rules(verdict) == {"prefix_is_a_file"}

    def test_a_manifest_carrying_both_a_file_and_its_child_is_refused(self):
        """No filesystem holds `a` as a file and a directory at once."""
        verdict = _filter([_entry("a"), _entry("a/b")])

        assert _rules(verdict) == {"prefix_is_a_file"}

    def test_order_within_the_manifest_does_not_change_the_verdict(self):
        """It is one property of the set, so the child arriving first is the same."""
        verdict = _filter([_entry("a/b"), _entry("a")])

        assert _rules(verdict) == {"prefix_is_a_file"}

    def test_an_ordinary_nested_path_is_fine(self):
        verdict = _filter(
            [_entry("src/kew/cli.py")], destination_paths=["src/kew/other.py"]
        )

        assert not verdict.refused


class TestGitattributesDrivers:
    def test_a_filter_driver_is_rejected(self):
        verdict = _filter(
            [_entry(".gitattributes")],
            read_text=lambda _p: "*.py filter=evil\n",
        )

        assert _rules(verdict) == {"gitattributes_driver"}

    def test_a_merge_driver_is_rejected(self):
        """The third driver attribute. Two of three being caught is what made
        this one look safe — it runs on merge, revert and cherry-pick."""
        verdict = _filter(
            [_entry(".gitattributes")],
            read_text=lambda _p: "*.py merge=evil\n",
        )

        assert _rules(verdict) == {"gitattributes_driver"}

    def test_a_commented_out_driver_is_not_a_refusal(self):
        """git ignores comment lines, so rejecting this refuses a file that does
        nothing — and over-rejection is how a gate gets turned off."""
        verdict = _filter(
            [_entry(".gitattributes")],
            read_text=lambda _p: "# *.py filter=evil\n*.png binary\n",
        )

        assert not verdict.refused

    def test_a_driver_after_a_comment_line_is_still_caught(self):
        """Stripping comments must not blind the scan to the rest of the file."""
        verdict = _filter(
            [_entry(".gitattributes")],
            read_text=lambda _p: "# a note\n*.py filter=evil\n",
        )

        assert _rules(verdict) == {"gitattributes_driver"}

    def test_a_diff_driver_is_rejected(self):
        verdict = _filter(
            [_entry("docs/.gitattributes")],
            read_text=lambda _p: "*.md diff=pandoc\n",
        )

        assert _rules(verdict) == {"gitattributes_driver"}

    def test_the_check_survives_a_case_variant_filename(self):
        """The basename match normalizes; a filter suite that only tries the
        lowercase spelling never learns whether it does."""
        verdict = _filter(
            [_entry(".GITATTRIBUTES")],
            read_text=lambda _p: "*.py filter=evil\n",
        )

        assert _rules(verdict) == {"gitattributes_driver"}

    def test_an_ordinary_gitattributes_is_fine(self):
        verdict = _filter(
            [_entry(".gitattributes")],
            read_text=lambda _p: "*.png binary\n*.sh text eol=lf\n",
        )

        assert not verdict.refused

    def test_not_looking_is_refusal_not_permission(self):
        """ "We did not look" must never read as "we looked and it was fine".

        Asserting the DETAIL, not just the rule: without the explicit
        no-reader branch this still refuses, because calling ``None`` raises
        into the read-failure arm — but the operator is then told
        "'NoneType' object is not callable", which explains nothing. The
        branch exists for the message, so the message is what pins it.
        """
        verdict = _filter([_entry(".gitattributes")])

        assert _rules(verdict) == {"gitattributes_unreadable"}
        assert verdict.rejections[0].detail == (
            "cannot inspect .gitattributes for filter/diff drivers"
        )

    def test_a_read_failure_is_refusal_too(self):
        def _boom(_path: str) -> str:
            raise OSError("truncated artifact")

        verdict = _filter([_entry(".gitattributes")], read_text=_boom)

        assert _rules(verdict) == {"gitattributes_unreadable"}
        assert "truncated artifact" in verdict.rejections[0].detail


class TestPathsThatStopForAHuman:
    """Not attacks — code the repository runs later, with its own credentials."""

    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/ci.yml",
            ".github/actions/setup/action.yml",
            ".pre-commit-config.yaml",
            ".husky/pre-commit",
            "tests/conftest.py",
            "Makefile",
            "package.json",
            "setup.py",
            ".envrc",
            "kew/src/kew/hosts/provision/trivy.pin",
            "kew/src/kew/hosts/provision/ubuntu-24.04.sh",
        ],
    )
    def test_it_is_flagged_but_not_rejected(self, path: str):
        verdict = _filter([_entry(path)])

        assert not verdict.refused, path
        assert verdict.needs_human, path
        assert verdict.flags[0].path == path

    @pytest.mark.parametrize(
        "path",
        [
            ".vscode/tasks.json",
            ".devcontainer/devcontainer.json",
            ".cargo/config.toml",
            "build.rs",
            "noxfile.py",
            "tox.ini",
            "justfile",
            "Dockerfile",
            ".npmrc",
        ],
    )
    def test_executed_config_paths_are_held_for_a_human(self, path: str):
        """Editor tasks, dev containers, build scripts and task runners all run
        with the operator's or the repository's credentials, so each one is a
        hold for a person — and not a refusal, which nobody could clear."""
        verdict = _filter([_entry(path)])

        assert not verdict.refused, path
        assert verdict.needs_human, path
        assert verdict.flags[0].path == path

    @pytest.mark.parametrize(
        "path",
        [
            "crates/core/build.rs",
            "frontend/.npmrc",
            "docker/DOCKERFILE",
            ".VSCode/Tasks.json",
            ".devcontainer/rust/devcontainer.json",
            "sub/.devcontainer/devcontainer.json",
            "crates/x/.cargo/config.toml",
            "packages/web/.vscode/tasks.json",
        ],
    )
    def test_executed_config_is_held_below_the_root_and_in_any_case(self, path: str):
        verdict = _filter([_entry(path)])

        assert not verdict.refused, path
        assert verdict.needs_human, path

    @pytest.mark.parametrize(
        "path",
        [
            ".devcontainer.json",
            ".cargo/config",
            "crates/x/.cargo/config",
            ".justfile",
            "Containerfile",
            "Dockerfile.dev",
            "docker/api.Dockerfile",
        ],
    )
    def test_alternate_spellings_of_executed_config_are_held(self, path: str):
        """Each tool reads these names with the same effect as the canonical
        one — the legacy Cargo config, the single-file dev container, podman's
        image recipe, a suffixed Dockerfile — so each is the same hold."""
        verdict = _filter([_entry(path)])

        assert not verdict.refused, path
        assert verdict.needs_human, path

    @pytest.mark.parametrize(
        "path",
        [
            "docs/vscode/tasks.json",
            ".cargo/config.toml.orig",
            "docs/devcontainer.md",
            ".dockerignore",
            "src/dockerfile_parser.py",
            "src/kew/dockerfile.py",
            "docs/dockerfile.md",
            "Dockerfile.md",
            "docs/Dockerfile-guide.rst",
            "makefile.py",
            "docs/idea/notes.md",
            "src/githooks/install.py",
        ],
    )
    def test_names_that_only_resemble_executed_config_are_not_held(self, path: str):
        """Matched by whole path component, so a backup copy or a lookalike
        name is not a hold — over-holding is how a gate gets turned off. A
        Dockerfile-named source module or document is not an image recipe."""
        verdict = _filter([_entry(path)])

        assert not verdict.needs_human, path

    @pytest.mark.parametrize(
        "path",
        [
            ".vscode/settings.json",
            ".vscode/launch.json",
            "kew.code-workspace",
            ".idea/workspace.xml",
            ".idea/runConfigurations/build.xml",
            "GNUmakefile",
            "BSDmakefile",
            "rules.mk",
            "Taskfile.yml",
            "Taskfile.yaml",
            ".yarnrc.yml",
            ".yarnrc",
            ".pnpmfile.cjs",
            "bunfig.toml",
            "lefthook.yml",
            ".lefthook.yml",
            ".githooks/pre-commit",
            ".husky/pre-push",
            ".overcommit.yml",
            ".mise.toml",
            "mise.toml",
            ".tool-versions",
            "pytest.ini",
            "Dockerfile-dev",
            "Dockerfile_prod",
            "compose.yaml",
            "compose.yml",
            "docker-compose.yml",
            "docker-compose.yaml",
            "rust-toolchain.toml",
        ],
    )
    def test_config_that_runs_on_open_or_install_is_held_at_any_depth(self, path: str):
        """Editor settings and launch configs, task runners, package-manager
        rc files, hook managers, toolchain pins and compose files each run a
        command when the checkout is opened, installed or built. Held at the
        root, below it, and in any case a case-insensitive filesystem merges —
        and never refused, which nobody could clear."""
        for variant in (path, f"packages/web/{path}", path.upper()):
            verdict = _filter([_entry(variant)])

            assert not verdict.refused, variant
            assert verdict.needs_human, variant
            assert protected_path_reason(variant) is not None, variant

    @pytest.mark.parametrize(
        "path",
        [
            ".vscode/tasks.json",
            "packages/web/.vscode/settings.json",
            ".devcontainer/devcontainer.json",
            ".devcontainer.json",
            ".idea/workspace.xml",
            "kew.code-workspace",
        ],
    )
    def test_editor_config_names_the_editor_as_what_runs_it(self, path: str):
        """The editor runs it on open — before any build or git command — so
        the reason tells the person to read it without opening the checkout."""
        reason = protected_path_reason(path)

        assert reason is not None
        assert "opened in an editor" in reason
        assert "git show" in reason
        assert "opened in an editor" not in (protected_path_reason("Makefile") or "")

    def test_a_flag_blocks_the_convenient_accessor(self):
        """The hold is enforced by the API, not left to the caller.

        This test used to assert the opposite — that flags were advisory and
        `approved()` handed the entries back regardless. That made the hold a
        comment: a caller reaching for the obvious accessor got
        `.github/workflows/pwn.yml` with no human involved.
        """
        verdict = _filter([_entry("Makefile")])

        with pytest.raises(ReviewRequired, match="Makefile"):
            verdict.approved()

    def test_releasing_the_hold_has_to_be_typed(self):
        """`reviewed=True` is the caller stating a person looked.

        It is deliberately not the default, so releasing a hold is an act
        rather than the path of least resistance.
        """
        verdict = _filter([_entry("Makefile")])

        assert [e.path for e in verdict.approved(reviewed=True)] == ["Makefile"]

    def test_a_rejection_outranks_the_review_hold(self):
        """`reviewed=True` acknowledges flags; it must not wave through a refusal."""
        verdict = _filter([_entry("Makefile"), _entry(".git/config")])

        with pytest.raises(PublicationRefused):
            verdict.approved(reviewed=True)

    def test_deletions_are_gated_the_same_way(self):
        """Both accessors go through one gate, so neither can drift."""
        verdict = _filter([], deletions=[".gitattributes"])

        with pytest.raises(ReviewRequired):
            verdict.deletions()
        assert verdict.deletions(reviewed=True) == (".gitattributes",)

    def test_ordinary_source_is_not_flagged(self):
        verdict = _filter([_entry("src/kew/cli.py"), _entry("README.md")])

        assert not verdict.needs_human


class TestVerificationHostProvisioning:
    """#2462: the scanner pin moved out of CI, where a person had to read it.

    `.github/workflows/ci.yml` stops for a human because an agent editing it
    changes what runs with the repository's credentials. The trivy pin the
    workflow now reads is the same decision in a file no flag covered, so a
    publication could have rewritten the scanner version and its archive
    digests — what the supply-chain gate checks — with nobody looking.
    """

    REASON = "verification-host provisioning and scanner pins"

    def test_the_scanner_pin_alone_is_held_for_review(self):
        pin = "kew/src/kew/hosts/provision/trivy.pin"
        verdict = _filter([_entry(pin)])

        assert not verdict.refused
        assert verdict.review_required
        assert verdict.review_reasons() == (f"{pin}: {self.REASON}",)
        with pytest.raises(ReviewRequired, match=pin):
            verdict.approved()

    def test_the_provisioning_recipe_is_held_on_the_same_reason(self):
        recipe = "kew/src/kew/hosts/provision/ubuntu-24.04.sh"
        verdict = _filter([_entry(recipe, "100755")])

        assert verdict.review_reasons() == (f"{recipe}: {self.REASON}",)

    def test_deleting_the_pin_is_held_too(self):
        """Removing the pin is how a gate stops reading one at all."""
        pin = "kew/src/kew/hosts/provision/trivy.pin"
        verdict = _filter([], deletions=[pin])

        assert verdict.review_reasons() == (f"{pin}: {self.REASON}",)
        with pytest.raises(ReviewRequired):
            verdict.deletions()

    def test_an_ordinary_source_change_is_not_held(self):
        verdict = _filter(
            [_entry("kew/src/kew/hosts/providers.py"), _entry("kew/src/kew/cli.py")]
        )

        assert not verdict.review_required
        assert verdict.review_reasons() == ()
        assert len(verdict.approved()) == 2

    def test_a_sibling_name_that_merely_starts_the_same_is_not_held(self):
        verdict = _filter([_entry("kew/src/kew/hosts/provisioning_notes.md")])

        assert not verdict.review_required


class TestWhatTheHumanReviewPolicyReads:
    """I13: `review_required` is what a person can clear — a refusal is not."""

    def test_a_flag_makes_the_verdict_review_required(self):
        verdict = _filter([_entry("Makefile")])

        assert verdict.review_required
        assert verdict.review_reasons() == (
            "Makefile: executed by common local or build tooling",
        )

    def test_ordinary_source_needs_no_review(self):
        verdict = _filter([_entry("src/kew/cli.py")])

        assert not verdict.review_required
        assert verdict.review_reasons() == ()

    def test_a_refusal_is_never_review_required(self):
        """No approver, allowlist entry or threshold clears a rejection.

        `needs_human` still reports the flag, because it describes the entries.
        `review_required` is the policy's question — *is there a decision a
        person is allowed to make here?* — and for a refused artifact there is
        not, so offering one would be offering a decision nobody may take.
        """
        verdict = _filter([_entry("Makefile"), _entry(".git/config")])

        assert verdict.needs_human
        assert not verdict.review_required
        assert verdict.refusal_reasons()

    def test_the_reasons_name_the_paths_they_came_from(self):
        verdict = _filter([_entry(".git/config")])

        assert verdict.refusal_reasons()[0].startswith(".git/config: ")


class TestTheWholeSet:
    def test_one_bad_entry_refuses_the_publication_not_just_itself(self):
        """The rejected entry is evidence about the agent that made all of them."""
        verdict = _filter(
            [_entry("src/a.py"), _entry("src/b.py"), _entry("evil", "120000")]
        )

        assert len(verdict.rejections) == 1
        with pytest.raises(PublicationRefused):
            verdict.approved()

    def test_a_flag_survives_the_entry_being_rejected_later(self):
        """Flags are raised per entry; collisions are decided over the set.

        So an entry can be flagged and then rejected. The verdict reports both
        — the refusal is what stops the publication, and the flag still says
        what a human would have been asked about.
        """
        verdict = _filter([_entry("Makefile"), _entry("makefile")])

        assert verdict.refused
        assert verdict.needs_human

    def test_an_empty_manifest_is_clean_and_empty(self):
        verdict = _filter([])

        assert verdict.approved() == ()
        assert not verdict.refused
        assert not verdict.needs_human


class TestDeletions:
    """A removed path is as attacker-controlled as a written one.

    The format gained deletions because a manifest that cannot express them
    silently leaves every file the agent removed in place — a publication that
    quietly differs from the work it claims to carry.
    """

    def test_an_ordinary_deletion_is_approved(self):
        verdict = _filter([_entry("src/new.py")], deletions=["src/old.py"])

        assert not verdict.refused
        assert verdict.deletions() == ("src/old.py",)

    @pytest.mark.parametrize(
        ("path", "rule"),
        [
            (".git/config", "dot_git"),
            ("../escape", "traversal"),
            ("/etc/passwd", "absolute_path"),
            ("notes.txt:hidden", "colon"),
            (".gi\u200ct/hooks/pre-commit", "dot_git"),
        ],
    )
    def test_a_deletion_gets_the_same_path_rules(self, path: str, rule: str):
        """`rm` on an attacker-chosen path destroys the operator's file as
        surely as a write overwrites it. Deleting is not the safe direction."""
        verdict = _filter([], deletions=[path])

        assert _rules(verdict) == {rule}, path

    def test_writing_and_deleting_one_path_is_refused(self):
        """Which wins depends on apply order, and the manifest does not say."""
        verdict = _filter([_entry("x.py")], deletions=["x.py"])

        assert _rules(verdict) == {"written_and_deleted"}

    def test_deleting_one_path_twice_is_refused(self):
        verdict = _filter([], deletions=["x.py", "x.py"])

        assert _rules(verdict) == {"duplicate_path"}

    def test_reading_deletions_raises_when_anything_was_rejected(self):
        """Same contract as approved() — no partial publication, either half."""
        verdict = _filter([_entry(".git/config")], deletions=["fine.py"])

        with pytest.raises(PublicationRefused):
            verdict.deletions()


class TestWritesAndDeletionsShareTheSetLevelRules:
    """The asymmetry that let a rule reach one operation and not the other.

    The ancestor-symlink defence was added for writes and silently did not
    cover removals — so `rm` followed a tracked `admin -> .git` symlink into
    the operator's own hooks directory. These pin BOTH operations against the
    same rules, so a future rule cannot be added to one column alone.
    """

    @pytest.mark.parametrize("writes", [True, False])
    def test_an_ancestor_that_is_a_file_stops_either_operation(self, writes: bool):
        """`admin` is a tracked symlink to `.git`; both writing and deleting
        `admin/hooks/pre-commit` go through it."""
        path = "admin/hooks/pre-commit"
        verdict = _filter(
            [_entry(path)] if writes else [],
            deletions=[] if writes else [path],
            existing_modes={"admin": "120000"},
            destination_paths=["admin"],
        )

        assert _rules(verdict) == {"prefix_is_a_file"}, writes

    @pytest.mark.parametrize("writes", [True, False])
    def test_a_destination_collision_stops_either_operation(self, writes: bool):
        """One manifest that means different things on different filesystems."""
        verdict = _filter(
            [_entry("README.md")] if writes else [],
            deletions=[] if writes else ["README.md"],
            destination_paths=["readme.md"],
        )

        assert _rules(verdict) == {"name_collision"}, writes

    @pytest.mark.parametrize("writes", [True, False])
    def test_the_path_rules_reach_either_operation(self, writes: bool):
        verdict = _filter(
            [_entry(".git/config")] if writes else [],
            deletions=[] if writes else [".git/config"],
        )

        assert _rules(verdict) == {"dot_git"}, writes


class TestTheRequiredInputs:
    """Every one of these silently weakened a different rule when omitted."""

    @pytest.mark.parametrize(
        "omit", ["deletions", "existing_modes", "destination_paths"]
    )
    def test_it_cannot_be_left_out(self, omit: str):
        kwargs = {"deletions": (), "existing_modes": {}, "destination_paths": ()}
        del kwargs[omit]

        with pytest.raises(TypeError, match=omit):
            filter_manifest([], **kwargs)


class TestTheArtifactAdapter:
    """The seam is where operations get dropped, so it is closed by construction."""

    @staticmethod
    def _artifact(content: str):
        class _FakeArtifact:
            entries = (ManifestEntry(".gitattributes", "100644", "a" * 64),)
            deletions = ("gone.py",)

            @staticmethod
            def text_for(_path: str) -> str:
                return content

        return _FakeArtifact()

    def test_it_forwards_the_entries_and_the_deletions(self):
        """Split from the reader test on purpose.

        These were one case asserting only the driver rejection — which proves
        `read_text` was wired and says nothing about `deletions`. A rejection
        also makes `deletions()` raise, so the two claims cannot be checked in
        the same verdict: with them combined, the deletion could have been
        dropped entirely and the test would still have passed.
        """
        verdict = filter_artifact(
            self._artifact("*.png binary\n"), existing_modes={}, destination_paths=[]
        )

        assert not verdict.refused
        assert [e.path for e in verdict.approved()] == [".gitattributes"]
        assert verdict.deletions() == ("gone.py",)

    def test_it_forwards_the_content_reader(self):
        """The driver is only catchable if `read_text` reached THIS artifact —
        not left None (which refuses for a different reason) and not pointed at
        some other source."""
        verdict = filter_artifact(
            self._artifact("*.py filter=evil\n"),
            existing_modes={},
            destination_paths=[],
        )

        assert _rules(verdict) == {"gitattributes_driver"}


class TestWindowsDeviceNames:
    """Held for a human rather than refused: `aux.py` is a plausible source file."""

    @pytest.mark.parametrize(
        "path", ["NUL", "NUL.txt", "dir/CON", "AUX", "com1", "LPT1.log", "prn.md"]
    )
    def test_a_reserved_device_stem_is_flagged(self, path: str):
        """Win32 resolves these to devices with or without an extension, so the
        file a collaborator checks out on Windows is not a file at all."""
        verdict = _filter([_entry(path)])

        assert not verdict.refused, path
        assert verdict.needs_human, path

    @pytest.mark.parametrize("path", ["console.py", "auxiliary.js", "connect.go"])
    def test_a_name_that_merely_starts_with_one_is_not(self, path: str):
        verdict = _filter([_entry(path)])

        assert not verdict.needs_human, path

    def test_a_deleted_device_name_is_flagged_too(self):
        verdict = _filter([], deletions=["NUL.txt"])

        assert verdict.needs_human


class TestPathsThatCannotBeEncoded:
    def test_a_lone_surrogate_is_refused_not_crashed_on(self):
        """JSON permits it, Python holds it, every filesystem call rejects it —
        so without this the refusal arrives as a UnicodeEncodeError downstream."""
        verdict = _filter([_entry("bad\udcff.py")])

        assert _rules(verdict) == {"unencodable"}

    def test_a_deleted_path_is_checked_the_same_way(self):
        verdict = _filter([], deletions=["bad\udcff.py"])

        assert _rules(verdict) == {"unencodable"}

    def test_ordinary_non_ascii_is_fine(self):
        """Over-rejecting unicode would refuse most of the world's filenames."""
        verdict = _filter([_entry("docs/café.md"), _entry("src/日本語.py")])

        assert not verdict.refused


class TestTheBrokerWillNotInstallAGitHook:
    """A git hook is code the repository runs on ordinary developer commands
    (commit, checkout, push, merge). The broker must never let published work
    install one, and no human review may release that refusal.

    Git runs hooks only from its own hooks directory, so landing anything there
    is refused as a ``.git`` path regardless of mode; the other ways in are
    refused by their own rules and are checked elsewhere: a tracked symlink into
    the hooks dir (``test_writing_through_a_tracked_symlink_is_refused``),
    redirecting ``core.hooksPath`` (a ``.git/config`` write, refused as
    ``dot_git``), a ``.gitattributes`` filter/diff driver
    (``test_a_filter_driver_is_rejected``), and making a tracked script
    executable (``test_making_a_tracked_file_executable_is_rejected``). A brand
    new executable file at a non-hook path is deliberately allowed: git will not
    run it, so it is not a hook. This states the guarantee once, by name, and
    proves the one property the piecewise tests do not: the hook refusal is not
    releasable by a reviewer.
    """

    HOOK_NAMES = ("pre-commit", "post-checkout", "pre-push", "post-merge")

    @pytest.mark.parametrize("mode", ["100644", "100755"])
    @pytest.mark.parametrize("name", HOOK_NAMES)
    def test_a_hook_in_the_git_hooks_dir_is_refused_and_unreleasable(
        self, name: str, mode: str
    ):
        # Both an inert (100644) and an executable (100755) hook: the path
        # reaches ``.git`` either way, so mode never gets a chance to matter.
        verdict = _filter([_entry(f".git/hooks/{name}", mode=mode)])

        assert verdict.refused
        assert "dot_git" in _rules(verdict)
        # A refusal, not a flag. `review_required` is the only thing a person may
        # clear, and a hook is never offered as a decision anyone may take.
        assert not verdict.review_required
        assert verdict.refusal_reasons()
        with pytest.raises(PublicationRefused):
            verdict.approved(reviewed=True)

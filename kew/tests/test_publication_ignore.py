"""Gitignore matching for host extraction (#2108).

Each rule of gitignore(5) the matcher implements has a unit test here, and a
differential test runs the same rules through real git in a throwaway
repository so "faithful" is checked against git rather than asserted.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from kew.publication_ignore import IgnoreRules, parse_gitignore


def _rules(**files: str) -> IgnoreRules:
    """Keyword ``root`` is the root .gitignore; ``a__b`` is ``a/b/.gitignore``."""
    return IgnoreRules(
        {
            ("" if name == "root" else name.replace("__", "/")): parse_gitignore(
                text.encode()
            )
            for name, text in files.items()
        }
    )


def _ignored(rules: IgnoreRules, path: str, *, is_dir: bool = False) -> bool:
    return rules.is_ignored(path, is_dir=is_dir)


class TestLineSyntax:
    def test_blank_lines_and_comments_are_skipped(self) -> None:
        assert parse_gitignore(b"\n# a comment\n   \n\n") == ()

    def test_escaped_hash_is_a_literal_pattern(self) -> None:
        rules = _rules(root="\\#notes\n")
        assert _ignored(rules, "#notes")
        assert not _ignored(rules, "notes")

    def test_a_hash_after_the_start_is_literal(self) -> None:
        assert _ignored(_rules(root="a#b\n"), "a#b")

    def test_trailing_spaces_are_dropped_unless_escaped(self) -> None:
        assert _ignored(_rules(root="foo   \n"), "foo")
        rules = _rules(root="bar\\ \n")
        assert _ignored(rules, "bar ")
        assert not _ignored(rules, "bar")

    def test_leading_spaces_are_kept(self) -> None:
        rules = _rules(root=" lead\n")
        assert _ignored(rules, " lead")
        assert not _ignored(rules, "lead")

    def test_crlf_line_endings_and_a_bom(self) -> None:
        rules = IgnoreRules({"": parse_gitignore(b"\xef\xbb\xbfa.txt\r\nb.txt")})
        assert _ignored(rules, "a.txt")
        assert _ignored(rules, "b.txt")

    def test_non_utf8_bytes_match_the_same_surrogate_escaped_name(self) -> None:
        name = b"caf\xe9".decode("utf-8", errors="surrogateescape")
        assert _ignored(IgnoreRules({"": parse_gitignore(b"caf\xe9\n")}), name)


class TestBytesNotCharacters:
    """wildmatch matches bytes; ``é`` is two of them in UTF-8 (#2117 round 2)."""

    def test_question_mark_is_one_byte(self) -> None:
        rules = _rules(root="b?\nc??\n")
        assert not _ignored(rules, "bé")
        assert _ignored(rules, "cé")

    def test_a_non_ascii_bracket_member_is_its_bytes(self) -> None:
        rules = _rules(root="[é]x\n")
        assert not _ignored(rules, "éx")
        # Either byte of é, then x.
        assert _ignored(rules, "\udcc3x")

    def test_star_and_literals_still_match_whole_names(self) -> None:
        rules = _rules(root="日本*/\nnotes-é.md\n")
        assert _ignored(rules, "日本語", is_dir=True)
        assert _ignored(rules, "日本語/x.txt")
        assert _ignored(rules, "d/notes-é.md")
        assert not _ignored(rules, "d/notes-e.md")

    def test_a_nested_gitignore_under_a_non_ascii_directory(self) -> None:
        rules = _rules(**{"日本": "??????\n"})
        assert _ignored(rules, "日本/日本")
        assert not _ignored(rules, "日本/ab")


class TestNegation:
    def test_a_later_negation_re_includes(self) -> None:
        rules = _rules(root="*.log\n!keep.log\n")
        assert _ignored(rules, "a.log")
        assert not _ignored(rules, "keep.log")

    def test_last_match_wins(self) -> None:
        rules = _rules(root="!keep.log\n*.log\n")
        assert _ignored(rules, "keep.log")

    def test_escaped_bang_is_a_literal(self) -> None:
        rules = _rules(root="\\!important\n")
        assert _ignored(rules, "!important")
        assert not _ignored(rules, "important")

    def test_a_negation_alone_matches_nothing(self) -> None:
        assert parse_gitignore(b"!\n") == ()

    def test_a_parent_directory_exclusion_cannot_be_undone(self) -> None:
        rules = _rules(root="build/\n!build/keep.txt\n")
        assert _ignored(rules, "build/keep.txt")
        rules = _rules(root="build/\n", build="!keep.txt\n")
        assert _ignored(rules, "build/keep.txt")

    def test_dir_star_with_negation_re_includes_a_file(self) -> None:
        rules = _rules(root="dir/*\n!dir/keep.txt\n")
        assert not _ignored(rules, "dir", is_dir=True)
        assert not _ignored(rules, "dir/keep.txt")
        assert _ignored(rules, "dir/other.txt")
        assert _ignored(rules, "dir/sub", is_dir=True)
        assert _ignored(rules, "dir/sub/x.txt")


class TestAnchoring:
    def test_a_slashless_pattern_matches_at_any_depth(self) -> None:
        rules = _rules(root="out\n")
        assert _ignored(rules, "out")
        assert _ignored(rules, "a/b/out")
        assert _ignored(rules, "a/out/file.txt")
        assert not _ignored(rules, "output")

    def test_a_leading_slash_anchors_to_the_root(self) -> None:
        rules = _rules(root="/out\n")
        assert _ignored(rules, "out")
        assert not _ignored(rules, "a/out")

    def test_a_middle_slash_anchors_too(self) -> None:
        rules = _rules(root="doc/frotz\n")
        assert _ignored(rules, "doc/frotz")
        assert not _ignored(rules, "a/doc/frotz")

    def test_a_trailing_slash_alone_does_not_anchor(self) -> None:
        rules = _rules(root="frotz/\n")
        assert _ignored(rules, "a/frotz", is_dir=True)
        assert _ignored(rules, "a/frotz/x.txt")

    def test_nested_rules_are_relative_to_their_directory(self) -> None:
        rules = _rules(pkg="/local.txt\ndocs/*.html\nany.txt\n")
        assert _ignored(rules, "pkg/local.txt")
        assert not _ignored(rules, "pkg/sub/local.txt")
        assert not _ignored(rules, "local.txt")
        assert _ignored(rules, "pkg/docs/a.html")
        assert not _ignored(rules, "pkg/x/docs/a.html")
        assert _ignored(rules, "pkg/x/y/any.txt")
        assert not _ignored(rules, "any.txt")

    def test_deeper_files_take_precedence(self) -> None:
        rules = _rules(root="*.gen\n", pkg="!keep.gen\n")
        assert _ignored(rules, "keep.gen")
        assert not _ignored(rules, "pkg/keep.gen")
        assert _ignored(rules, "pkg/other.gen")
        rules = _rules(root="!*.gen\n", pkg="*.gen\n")
        assert _ignored(rules, "pkg/a.gen")

    def test_a_directorys_own_gitignore_does_not_apply_to_itself(self) -> None:
        rules = _rules(pkg="pkg\n")
        assert not _ignored(rules, "pkg", is_dir=True)
        assert _ignored(rules, "pkg/pkg", is_dir=True)


class TestDirectoryOnly:
    def test_a_trailing_slash_matches_directories_only(self) -> None:
        rules = _rules(root="cache/\n")
        assert _ignored(rules, "cache", is_dir=True)
        assert not _ignored(rules, "cache")
        assert _ignored(rules, "cache/data.bin")

    def test_without_a_slash_files_and_directories_both_match(self) -> None:
        rules = _rules(root="cache\n")
        assert _ignored(rules, "cache", is_dir=True)
        assert _ignored(rules, "cache")

    def test_an_anchored_directory_pattern(self) -> None:
        rules = _rules(root="/site/\n")
        assert _ignored(rules, "site", is_dir=True)
        assert not _ignored(rules, "docs/site", is_dir=True)
        assert not _ignored(rules, "site")


class TestWildcards:
    def test_star_does_not_cross_a_slash(self) -> None:
        rules = _rules(root="a/*.txt\n")
        assert _ignored(rules, "a/x.txt")
        assert not _ignored(rules, "a/b/x.txt")

    def test_question_mark_is_one_non_slash_character(self) -> None:
        rules = _rules(root="a?c\nx/?/z\n")
        assert _ignored(rules, "abc")
        assert not _ignored(rules, "ac")
        assert not _ignored(rules, "abbc")
        assert not _ignored(rules, "x//z")

    def test_bracket_sets_ranges_and_negation(self) -> None:
        rules = _rules(root="file[0-9].txt\nv[!a-c]\nw[^a]\n")
        assert _ignored(rules, "file7.txt")
        assert not _ignored(rules, "filex.txt")
        assert _ignored(rules, "vd")
        assert not _ignored(rules, "vb")
        assert _ignored(rules, "wb")
        assert not _ignored(rules, "wa")

    def test_bracket_literal_close_bracket_and_dash(self) -> None:
        rules = _rules(root="a[]]b\nc[a-]d\n")
        assert _ignored(rules, "a]b")
        assert _ignored(rules, "c-d")
        assert _ignored(rules, "cad")
        assert not _ignored(rules, "cbd")

    def test_bracket_escapes_and_classes(self) -> None:
        rules = _rules(root="e[\\]]\nn[[:digit:]]\nu[[:upper:][:space:]]\n")
        assert _ignored(rules, "e]")
        assert _ignored(rules, "n5")
        assert not _ignored(rules, "nx")
        assert _ignored(rules, "uQ")
        assert _ignored(rules, "u ")
        assert not _ignored(rules, "uq")

    def test_a_bracket_never_matches_a_slash(self) -> None:
        rules = _rules(root="a[/]b\nc[!x]d\n")
        assert not _ignored(rules, "a/b")
        assert not _ignored(rules, "c/d")

    def test_invalid_patterns_match_nothing(self) -> None:
        rules = _rules(root="bad[\nx[[:nope:]]\ntrail\\\n")
        assert not _ignored(rules, "bad[")
        assert not _ignored(rules, "bad")
        assert not _ignored(rules, "xa")
        assert not _ignored(rules, "trail")
        assert not _ignored(rules, "trail\\")

    def test_an_invalid_pattern_is_logged_by_name(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="kew.publication_ignore"):
            parse_gitignore("ok.txt\nbad[é\nx[[:nope:]]\n".encode())
        warnings = [r.getMessage() for r in caplog.records]
        assert len(warnings) == 2
        assert "invalid .gitignore pattern 'bad[é' matches nothing" in warnings[0]
        assert "unterminated bracket" in warnings[0]
        assert "'x[[:nope:]]'" in warnings[1]
        assert "unknown character class 'nope'" in warnings[1]

    def test_a_reversed_range_matches_its_start_only(self) -> None:
        rules = _rules(root="k[b-a]x\nj[!b-a]z\n")
        assert _ignored(rules, "kbx")
        assert not _ignored(rules, "kax")
        assert not _ignored(rules, "kcx")
        assert not _ignored(rules, "jbz")
        assert _ignored(rules, "jcz")

    def test_an_unclosed_class_is_a_literal_bracket(self) -> None:
        rules = _rules(root="k[[:a]\n")
        assert _ignored(rules, "k[")
        assert _ignored(rules, "k:")
        assert _ignored(rules, "ka")

    def test_backslash_escapes_a_wildcard(self) -> None:
        rules = _rules(root="lit\\*\n")
        assert _ignored(rules, "lit*")
        assert not _ignored(rules, "litx")

    def test_regex_metacharacters_are_literal(self) -> None:
        rules = _rules(root="a+b(c).d$\n")
        assert _ignored(rules, "a+b(c).d$")
        assert not _ignored(rules, "aab(c)xd")


class TestDoubleStar:
    def test_leading_double_star_matches_at_any_depth(self) -> None:
        rules = _rules(root="**/foo\n**/bar/baz\n")
        assert _ignored(rules, "foo")
        assert _ignored(rules, "a/b/foo")
        assert _ignored(rules, "bar/baz")
        assert _ignored(rules, "x/bar/baz")
        assert not _ignored(rules, "bar/x/baz")

    def test_trailing_double_star_matches_everything_inside(self) -> None:
        rules = _rules(root="abc/**\n")
        assert not _ignored(rules, "abc", is_dir=True)
        assert _ignored(rules, "abc/x")
        assert _ignored(rules, "abc/x/y", is_dir=True)

    def test_middle_double_star_matches_zero_or_more_directories(self) -> None:
        rules = _rules(root="a/**/b\n")
        assert _ignored(rules, "a/b")
        assert _ignored(rules, "a/x/b")
        assert _ignored(rules, "a/x/y/b")
        assert not _ignored(rules, "a/xb")

    def test_an_escaped_slash_after_double_star_has_no_zero_directory_form(
        self,
    ) -> None:
        # wildmatch takes the zero-directory shortcut only for a literal `/`.
        rules = _rules(root="a/**\\/b\n**\\/o\n")
        assert not _ignored(rules, "a/b")
        assert _ignored(rules, "a/x/b")
        assert _ignored(rules, "a/x/y/b")
        assert not _ignored(rules, "o")
        assert _ignored(rules, "p/o")

    def test_escaped_stars_are_literals(self) -> None:
        rules = _rules(root="f/\\*\\*/b\n\\**/q\n")
        assert _ignored(rules, "f/**/b")
        assert not _ignored(rules, "f/x/b")
        assert _ignored(rules, "*x/q")
        assert not _ignored(rules, "x/q")

    def test_other_star_runs_are_single_stars(self) -> None:
        rules = _rules(root="x**y\nfoo/**bar\n")
        assert _ignored(rules, "xzzy")
        assert not _ignored(rules, "x/y")
        assert _ignored(rules, "foo/zbar")
        assert not _ignored(rules, "foo/a/bar")

    def test_double_star_alone_matches_everything(self) -> None:
        rules = _rules(root="**\n!keep\n")
        assert _ignored(rules, "a/b/c")
        assert not _ignored(rules, "keep")


class TestTheLiteralPrefixKeepsOneCharacterLikeGit:
    """Git compares an anchored pattern's literal prefix as a string, then
    wildmatches the rest with the prefix's last character in front, so a run
    of stars right after a prefix that does not end in ``/`` is one ``*``.

    Pinned here whatever git is installed: git before its late-2025
    ``match_pathname`` fix let that run cross directories, and
    :func:`test_the_matcher_agrees_with_git` compares these patterns against
    git only where git has the fix."""

    def test_double_star_after_a_literal_prefix_stays_in_its_directory(
        self,
    ) -> None:
        rules = _rules(root="q/foo**/bar\n")
        assert _ignored(rules, "q/fooX/bar")
        assert _ignored(rules, "q/foo/bar")
        assert not _ignored(rules, "q/fooX/Y/bar")
        assert not _ignored(rules, "q/foo/Z/bar")
        assert not _ignored(rules, "q/other/bar")

    def test_a_directory_git_ignores_is_ignored_so_it_is_not_walked(self) -> None:
        rules = _rules(root="v/gen**/out/\n")
        assert _ignored(rules, "v/gen/out", is_dir=True)
        assert _ignored(rules, "v/gen/out/g.txt")
        assert _ignored(rules, "v/genA/out/link")
        assert not _ignored(rules, "v/genA/b/out", is_dir=True)
        assert not _ignored(rules, "v/genA/b/out/f.txt")

    def test_a_trailing_double_star_after_a_literal_prefix_is_one_star(
        self,
    ) -> None:
        rules = _rules(root="r/ab**\n")
        assert _ignored(rules, "r/abc.txt")
        # Through its directory, which ``ab*`` matches, not by crossing ``/``.
        assert _ignored(rules, "r/abX", is_dir=True)
        assert _ignored(rules, "r/abX/c.txt")
        assert not _ignored(rules, "r/zz.txt")

    def test_a_prefix_ending_in_a_slash_still_leads_a_double_star(self) -> None:
        rules = _rules(root="a/**/b\nlogs/**/*.log\n")
        assert _ignored(rules, "a/b")
        assert _ignored(rules, "a/q/r/b")
        assert _ignored(rules, "logs/y/z/b.log")
        assert not _ignored(rules, "logs/a.txt")

    def test_a_star_run_not_followed_by_a_slash_stays_a_star(self) -> None:
        rules = _rules(root="s/**x\n")
        assert _ignored(rules, "s/ax")
        assert not _ignored(rules, "s/a/bx")

    def test_a_basename_pattern_has_no_prefix_split(self) -> None:
        rules = _rules(root="ab**\n")
        assert _ignored(rules, "abX")
        assert _ignored(rules, "d/abY")


class TestNoBacktracking:
    """Names are agent-chosen; matching must stay polynomial (#2117 review)."""

    @pytest.mark.parametrize(
        "line",
        [
            "a*a*a*a*a*a*a*a*b",
            "*" * 200 + "b",
            "x/" + "a*" * 40 + "b",
            "**/" * 30 + "a*a*a*a*b",
            "[a]*[!b]*[[:alpha:]]*?*a*b",
        ],
    )
    def test_a_hostile_name_is_matched_quickly(self, line: str) -> None:
        pattern = parse_gitignore(line.encode())[0]
        name = "a" * 5000
        subjects = [name, "x/" + name, "/".join(["a"] * 2500)]
        started = time.perf_counter()
        for subject in subjects:
            assert not pattern.matches(subject, is_dir=False)
        assert time.perf_counter() - started < 1.0

    def test_a_deeply_nested_tree_is_decided_quickly(self) -> None:
        rules = _rules(root="a*a*a*a*a*b\n**/x*x*x*y\n", a="*.tmp\n")
        # 200 levels of 40-character names: an 8 KB path, twice PATH_MAX.
        parts = ["a" * 40] * 200
        started = time.perf_counter()
        # The walker meets every directory on the way down, then the files.
        for depth in range(1, len(parts) + 1):
            assert not _ignored(rules, "/".join(parts[:depth]), is_dir=True)
        assert not _ignored(rules, "/".join(parts) + "/" + "a" * 200)
        assert time.perf_counter() - started < 1.0


class TestRuleSources:
    def test_only_regular_gitignore_files_are_rule_sources(self) -> None:
        state = {
            ".gitignore": "100644",
            "a/.gitignore": "100755",
            "b/.gitignore": "120000",
            "c/.gitignore/x": "100644",
            "d/not.gitignore": "100644",
        }
        assert list(IgnoreRules.gitignore_paths(state)) == [
            ".gitignore",
            "a/.gitignore",
        ]

    def test_no_rules_ignores_nothing(self) -> None:
        assert not _ignored(IgnoreRules({}), "anything")
        assert not _ignored(IgnoreRules({"": ()}), "anything", is_dir=True)


# --------------------------------------------------------------------------
# Differential: the same rules through real git.
# --------------------------------------------------------------------------

_ROOT_RULES = """\
# generated
*.log
!keep.log
/site/
.kew/docs/
dir/*
!dir/keep.txt
cache/
**/tmp/**
logs/**/*.log
build/
!build/keep.txt
file[0-9].txt
v[!a-c]
a/**/b
x**y
lit\\*
trail\\ \x20
\\#hash
\\!bang
q/foo**/bar
r/ab**
s/**x
v/gen**/out/
w/**/**/z
b?
c??
[é]x
k[b-a]x
j[!b-a]z
n/??????/
m/??/
e/**\\/b
f/\\*\\*/b
\\*\\*/x
\\[ab]
h\\?
i\\*
tw\\ \x20\\\x20
k2\\/y
\\/l3
m3\\/
**\\/o4
\\**/q6
r7[a\\-c]
s8[\\a-c]
"""

_NESTED_RULES = {
    "frontend/.gitignore": ".svelte-kit\n/local.txt\n",
    "pkg/.gitignore": "!keep.gen\ndocs/*.html\n",
    "gen/.gitignore": "*\n!*.md\n!.gitignore\n",
    "日本/.gitignore": "??????\n",
}

_PATHS = [
    "a.log",
    "sub/b.log",
    "keep.log",
    "site/index.html",
    "docs/site/index.html",
    ".kew/docs/note.md",
    ".kew/other.md",
    "dir/keep.txt",
    "dir/other.txt",
    "dir/sub/deep.txt",
    "cache/data.bin",
    "x/cache/data.bin",
    "p/q/tmp/t.txt",
    "logs/a.txt",
    "logs/y/z/b.log",
    "build/keep.txt",
    "build/new.txt",
    "file7.txt",
    "filex.txt",
    "vd",
    "vb",
    "a/b",
    "a/q/r/b",
    "xzzy",
    "x/y",
    "lit*",
    "litx",
    "trail ",
    "#hash",
    "!bang",
    "frontend/.svelte-kit/ambient.d.ts",
    "frontend/local.txt",
    "frontend/src/local.txt",
    "local.txt",
    "pkg/keep.gen",
    "pkg/docs/index.html",
    "pkg/x/docs/index.html",
    "gen/readme.md",
    "gen/out.bin",
    "gen/sub/readme.md",
    "plain.py",
    # `**` right after a literal prefix, one `*` since git's late-2025 fix
    # (#2117 review round 1).
    "q/fooX/Y/bar",
    "q/foo/Z/bar",
    "q/fooX/bar",
    "q/other/bar",
    "r/abX/c.txt",
    "r/abc.txt",
    "r/zz.txt",
    "s/ax",
    "s/a/bx",
    "v/genA/b/out/f.txt",
    "v/gen/out/g.txt",
    "v/genA/out.txt",
    "w/z",
    "w/a/b/z",
    # Bytes, not characters, and reversed ranges (#2117 review round 2).
    "bé",
    "cé",
    "éx",
    "kbx",
    "kax",
    "kcx",
    "jbz",
    "jcz",
    "n/日本/x",
    "m/日本/y",
    "m/ab/y",
    "日本/日本",
    "日本/ab",
    # Escaped specials are literals; only an unescaped `/` after `**` takes
    # the zero-directory shortcut (#2117 review round 3).
    "e/b",
    "e/x/b",
    "e/x/y/b",
    "f/**/b",
    "f/x/b",
    "**/x",
    "z9/x",
    "[ab]",
    "b",
    "h?",
    "hx",
    "i*",
    "ix",
    "tw   ",
    "tw ",
    "k2/y",
    "d/k2/y",
    "l3",
    "m3/z",
    "o4",
    "p5/o4",
    "*x/q6",
    "*/q6",
    "a6/q6",
    "r7b",
    "r7-",
    "r7a",
    "s8b",
    "s8d",
]


def _git_ignored(
    repo: Path, ignore_files: dict[str, str], paths: list[str]
) -> set[str]:
    """What the installed git ignores of *paths* under *ignore_files*."""
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for relative, text in ignore_files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for relative in paths:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")

    listed = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "core.excludesFile=/dev/null",
            "-c",
            "core.ignoreCase=false",
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
        ],
        check=True,
        capture_output=True,
    ).stdout
    return {path for path in listed.decode().split("\0") if path} & set(paths)


def _git_has_the_prefix_fix(tmp_path: Path) -> bool:
    """Whether the installed git keeps one prefix character for wildmatch.

    Asked of git's behavior, not its version string: Apple, distributions
    and source builds number their releases differently.
    """
    probe = {".gitignore": "q/foo**/bar\n"}
    return not _git_ignored(tmp_path / "probe", probe, ["q/fooX/Y/bar"])


#: What git before its late-2025 ``match_pathname`` fix also ignores of
#: :data:`_PATHS` and kew, which follows the fix, does not: there a ``**``
#: right after a literal prefix not ending in ``/`` crossed directories.
_ONLY_UNFIXED_GIT_IGNORES = {"q/fooX/Y/bar", "q/foo/Z/bar", "v/genA/b/out/f.txt"}


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_matcher_agrees_with_git(tmp_path: Path) -> None:
    ignore_files = {".gitignore": _ROOT_RULES, **_NESTED_RULES}
    git_ignored = _git_ignored(tmp_path / "repo", ignore_files, _PATHS)
    # A git without the fix (Ubuntu 24.04's 2.43 is one) still gives the old,
    # buggy answer for that one pattern class. Every rule is still compared,
    # and the gap must be exactly those paths, all on git's side; the fixed
    # answers are pinned by TestTheLiteralPrefixKeepsOneCharacterLikeGit.
    gap = set() if _git_has_the_prefix_fix(tmp_path) else _ONLY_UNFIXED_GIT_IGNORES
    assert gap <= git_ignored
    git_ignored -= gap

    rules = IgnoreRules(
        {
            relative.rpartition("/")[0]: parse_gitignore(text.encode())
            for relative, text in ignore_files.items()
        }
    )
    ours = {path for path in _PATHS if rules.is_ignored(path, is_dir=False)}
    assert ours == git_ignored
    # The fixture exercises both verdicts, or the comparison proves little.
    assert 10 < len(ours) < len(_PATHS) - 10

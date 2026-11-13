"""``kew verify --explain-selection`` / ``--selection-report`` (#3189).

Every fixture is a real git repository: the selector reads git objects and
never the working tree, so the tests build commits and assert on what the
command prints — the selected files, their import chains, the rule that forced
the full suite, and the digest.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kew import verify_selection, verify_selection_report
from kew.cli import app
from kew.subprocess_utils import preflight_tools

_GIT = ("git", "-c", "user.name=t", "-c", "user.email=t@example.invalid")

_BASE_FILES = {
    "src/kew/__init__.py": "",
    "src/kew/config.py": "import kew.settings\n",
    "src/kew/settings.py": "VALUE = 1\n",
    "src/kew/a.py": "def run():\n    from kew import b\n    return b.VALUE\n",
    "src/kew/b.py": "VALUE = 1\n",
    "src/kew/c.py": "VALUE = 3\n",
    "tests/conftest.py": "import kew.config\n",
    "tests/support/__init__.py": "",
    "tests/support/helpers.py": "from kew.a import run\n",
    "tests/test_a.py": "from kew.a import run\n\n\ndef test_a():\n    assert run()\n",
    "tests/test_via_support.py": (
        "from support.helpers import run\n\n\ndef test_s():\n    assert run()\n"
    ),
    "tests/test_dynamic.py": (
        "import subprocess\nimport sys\n\n\ndef test_d():\n"
        '    subprocess.run([sys.executable, "-m", "kew", "--help"])\n'
    ),
    "tests/test_policy.py": (
        "import pytest\n\nimport kew.c\n\n\n@pytest.mark.policy\n"
        "def test_p():\n    assert kew.c.VALUE\n"
    ),
    "tests/test_plain.py": "def test_plain():\n    assert True\n",
    **{
        f"tests/test_c{i}.py": (
            "import pytest\n\nfrom kew.c import VALUE\n\n\n"
            '@pytest.mark.parametrize("n", [1, 2, 3])\n'
            "def test_c(n):\n    assert VALUE\n"
        )
        for i in range(6)
    },
}

#: Small projects for one rule each: six tests import ``kew.z`` alone, so a
#: selection stays far below the 70% rule.
_FILLER = {
    "src/kew/__init__.py": "",
    "src/kew/z.py": "",
    **{
        f"tests/test_f{i}.py": "import kew.z\n\n\ndef test_f():\n    assert 1\n"
        for i in range(6)
    },
}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [*_GIT, *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _write(repo: Path, files: dict[str, str | None]) -> str:
    for rel, text in files.items():
        path = repo / rel
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "change")
    return _git(repo, "rev-parse", "HEAD")


def _project(
    root: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str | None]
) -> Path:
    """A project on ``main`` holding *files*, with ``candidate`` checked out."""
    _git(root, "init", "--quiet", "-b", "main", ".")
    _write(root, dict(files))
    _git(root, "checkout", "--quiet", "-b", "candidate")
    monkeypatch.setenv("KEW_PROJECT", str(root))
    monkeypatch.chdir(root)
    monkeypatch.delenv("GATE_RE", raising=False)
    return root


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _project(tmp_path, monkeypatch, _BASE_FILES)


def _explain(ref: str = "candidate") -> str:
    result = CliRunner().invoke(app, ["verify", "--explain-selection", ref])
    assert result.exit_code == 0, result.output
    return result.output


def _report(last: int) -> list[str]:
    args = ["verify", "--selection-report", "--last", str(last)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    return result.output.splitlines()


def test_a_change_selects_each_importer_with_its_import_chain(repo: Path) -> None:
    _write(repo, {"src/kew/b.py": "VALUE = 2\n"})

    out = _explain()

    assert "result: 5 of 11 test files, 5 of 23 tests" in out
    assert "tests/test_a.py  tests/test_a.py → kew.a → kew.b [changed]\n" in out
    assert (
        "tests/test_via_support.py  tests/test_via_support.py → "
        "tests/support/helpers.py → kew.a → kew.b [changed]\n"
    ) in out
    assert "tests/test_dynamic.py  always: runs or loads kew dynamically\n" in out
    assert "tests/test_policy.py  always: marked policy\n" in out
    assert "tests/test_plain.py  always: imports no kew module\n" in out
    assert "tests/test_c0.py" not in out


def test_the_same_inputs_give_a_byte_identical_explanation(repo: Path) -> None:
    _write(repo, {"src/kew/b.py": "VALUE = 2\n"})

    first, second = _explain(), _explain()

    assert first == second
    digest = first.rstrip().rsplit("\n", 1)[1]
    assert digest.startswith("digest sha256:")
    body = first.rstrip().rsplit("\n", 1)[0] + "\n"
    assert digest == f"digest sha256:{hashlib.sha256(body.encode()).hexdigest()}"


def test_the_working_tree_does_not_change_the_answer(repo: Path) -> None:
    _write(repo, {"src/kew/b.py": "VALUE = 2\n"})
    before = _explain()

    (repo / "src/kew/c.py").write_text("import kew.b\n", encoding="utf-8")
    (repo / "tests/conftest.py").write_text("", encoding="utf-8")

    assert _explain() == before


def test_the_merge_gate_paths_come_from_the_committed_kew_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed = '[epic]\nmerge_gate_paths = ["src/kew/b.py"]\n'
    root = _project(tmp_path, monkeypatch, {**_BASE_FILES, "kew.toml": committed})
    _write(root, {"src/kew/b.py": "VALUE = 2\n"})
    before = _explain()

    (root / "kew.toml").write_text("[epic]\nmerge_gate_paths = []\n", encoding="utf-8")

    assert "rule: a merge gate path changed: src/kew/b.py\n" in before
    assert _explain() == before


def test_gate_re_is_named_in_the_header_and_the_digest(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(repo, {"src/kew/b.py": "VALUE = 2\n"})
    unset = _explain()
    monkeypatch.setenv("GATE_RE", r"^nothing/")

    out = _explain()

    assert "GATE_RE: unset\n" in unset
    assert "GATE_RE: ^nothing/\n" in out
    assert out.splitlines()[-1] != unset.splitlines()[-1]


def test_a_changed_test_file_is_always_selected(repo: Path) -> None:
    _write(repo, {"tests/test_c3.py": "def test_new():\n    assert 1\n"})

    out = _explain()

    assert "tests/test_c3.py  always: changed by the candidate\n" in out
    assert "tests/test_c0.py" not in out


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({"tests/conftest.py": "import kew.config\nX = 1\n"}, "tests/conftest.py"),
        ({"tests/support/helpers.py": "X = 1\n"}, "tests/support/helpers.py"),
        ({"uv.lock": "version = 1\n"}, "uv.lock"),
    ],
    ids=["conftest", "support", "uv-lock"],
)
def test_a_change_to_what_every_test_runs_under_answers_full(
    repo: Path, change: dict[str, str | None], rule: str
) -> None:
    _write(repo, change)

    out = _explain()

    assert "result: full suite (11 test files)\n" in out
    expected = {
        "tests/conftest.py": "rule: tests/conftest.py changed: tests/conftest.py",
        "tests/support/helpers.py": (
            "rule: tests/support changed: tests/support/helpers.py"
        ),
        "uv.lock": "rule: uv.lock changed: uv.lock",
    }[rule]
    assert f"{expected}\n" in out


def test_a_module_conftest_imports_answers_full(repo: Path) -> None:
    _write(repo, {"src/kew/settings.py": "VALUE = 2\n"})

    out = _explain()

    assert "rule: tests/conftest.py imports it: kew.settings\n" in out


@pytest.mark.parametrize(
    "conftest",
    [
        "from support import plug\n",
        (
            "import importlib\n\nNAMES = ('kew.c',)\n\n\n"
            "def pytest_configure(config):\n"
            "    for name in NAMES:\n        importlib.import_module(name)\n"
        ),
    ],
    ids=["through-tests-support", "importlib-over-a-tuple"],
)
def test_a_module_conftest_loads_another_way_answers_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conftest: str
) -> None:
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "src/kew/c.py": "X = 1\n",
            "tests/conftest.py": conftest,
            "tests/support/__init__.py": "",
            "tests/support/plug.py": "import kew.c\n",
        },
    )
    _write(root, {"src/kew/c.py": "raise ImportError('every test breaks')\n"})

    assert "rule: tests/conftest.py imports it: kew.c\n" in _explain()


def test_a_non_python_file_under_src_answers_full(repo: Path) -> None:
    _write(repo, {"src/kew/_templates/x.j2": "hi\n"})

    assert "rule: a non-.py file under src/ changed: src/kew/_templates/x.j2\n" in (
        _explain()
    )


@pytest.mark.parametrize(
    "path",
    ["evals/triage/run_eval.py", "create-kew/index.js"],
)
def test_a_path_the_graph_cannot_trace_answers_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    reader = (
        "from pathlib import Path\n\nimport kew.z\n\n\ndef test_reads():\n"
        f"    assert (Path(__file__).parents[1] / {path!r}).read_text()\n"
    )
    root = _project(
        tmp_path,
        monkeypatch,
        {**_FILLER, path: "before\n", "tests/test_reads.py": reader},
    )
    _write(root, {path: "after\n"})

    assert f"rule: an untraced path changed: {path}\n" in _explain()


def test_a_changed_tests_file_nothing_imports_answers_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, monkeypatch, {**_FILLER, "tests/sample_input.py": "1\n"})
    _write(root, {"tests/sample_input.py": "2\n"})

    out = _explain()

    assert "rule: a tests/ file nothing imports changed: tests/sample_input.py\n" in out


def test_a_tests_package_init_selects_the_tests_in_its_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "tests/pkg/__init__.py": "",
            "tests/pkg/test_inside.py": "import kew.z\n\n\ndef test_i():\n    pass\n",
        },
    )
    _write(root, {"tests/pkg/__init__.py": "import os\n"})

    out = _explain()

    assert (
        "tests/pkg/test_inside.py  tests/pkg/test_inside.py → "
        "tests/pkg/__init__.py [changed]\n"
    ) in out


def test_a_file_that_does_not_parse_answers_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "src/kew/c.py": "X = 1\n",
            "tests/test_new.py": "import kew.c\n\n\ndef test_n(:\n    pass\n",
        },
    )
    _write(root, {"src/kew/c.py": "X = 0\n"})

    out = _explain()

    assert "rule: a Python file could not be parsed: tests/test_new.py\n" in out


def test_gate_re_from_the_environment_answers_full(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(repo, {"docs/page.md": "x\n"})
    monkeypatch.setenv("GATE_RE", r"^docs/")

    assert "rule: GATE_RE matched: docs/page.md\n" in _explain()


def test_an_unusable_gate_re_exits_2(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(repo, {"src/kew/b.py": "VALUE = 2\n"})
    monkeypatch.setenv("GATE_RE", "(")

    result = CliRunner().invoke(app, ["verify", "--explain-selection", "candidate"])

    assert result.exit_code == 2
    assert "GATE_RE is not a regular expression" in result.output


@pytest.mark.parametrize("pattern", ["src/kew/b.py", "src/kew/*.py", "**/b.py"])
def test_a_merge_gate_path_answers_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pattern: str
) -> None:
    toml = f"[epic]\nmerge_gate_paths = [{pattern!r}]\n"
    root = _project(tmp_path, monkeypatch, {**_BASE_FILES, "kew.toml": toml})
    _write(root, {"src/kew/b.py": "VALUE = 2\n"})

    assert "rule: a merge gate path changed: src/kew/b.py\n" in _explain()


def test_a_merge_gate_path_that_matches_nothing_leaves_the_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toml = '[epic]\nmerge_gate_paths = ["[", "nowhere/"]\n'
    root = _project(tmp_path, monkeypatch, {**_BASE_FILES, "kew.toml": toml})
    _write(root, {"src/kew/b.py": "VALUE = 2\n"})

    out = _explain()

    assert "rule:" not in out
    assert "tests/test_a.py → kew.a → kew.b [changed]" in out


def test_an_unreadable_kew_toml_answers_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toml = '[epic]\nmerge_gate_paths = "scripts/"\n'
    root = _project(tmp_path, monkeypatch, {**_BASE_FILES, "kew.toml": toml})
    _write(root, {"src/kew/b.py": "VALUE = 2\n"})

    out = _explain()

    assert "rule: kew.toml could not be read: kew.toml: " in out


def test_a_selection_of_most_test_files_answers_full(repo: Path) -> None:
    _write(repo, {"src/kew/c.py": "VALUE = 4\n"})

    out = _explain()

    assert "rule: the selection reached 70% of test files: 9 of 11 test files\n" in out


def test_a_removed_import_still_selects_its_old_importers(repo: Path) -> None:
    _write(
        repo,
        {"src/kew/a.py": "def run():\n    return 1\n", "src/kew/b.py": None},
    )

    out = _explain()

    assert "tests/test_a.py  tests/test_a.py → kew.a [changed]\n" in out
    assert "tests/test_via_support.py  " in out


def test_a_deleted_module_selects_the_unchanged_files_that_imported_it(
    repo: Path,
) -> None:
    """Only ``b.py`` goes; ``a.py`` still says ``from kew import b`` inside a
    function. The candidate's tree alone has no ``kew.b`` to reach; the base's
    edge is what selects ``test_a``."""
    _write(repo, {"src/kew/b.py": None})

    out = _explain()

    assert "tests/test_a.py  tests/test_a.py → kew.a → kew.b [changed]\n" in out


@pytest.mark.parametrize(
    "source",
    [
        (
            "import importlib\n\nimport kew.z\n\n\ndef test_d():\n    name = 'c'\n"
            "    assert importlib.import_module(f'kew.{name}').X\n"
        ),
        (
            "import kew.z\n\n\ndef test_d():\n    name = 'kew.c'\n"
            "    assert __import__(name)\n"
        ),
        (
            "import subprocess\nimport sys\n\nimport kew.z\n\n"
            "SCRIPT = 'import kew.c'\n\n\n"
            "def test_d():\n    subprocess.run([sys.executable, '-c', SCRIPT])\n"
        ),
        (
            "import importlib.util\n\nimport kew.z\n\n\ndef _load(name):\n"
            "    spec = importlib.util.spec_from_file_location(name, f'{name}.py')\n"
            "    return spec\n\n\ndef test_d():\n    assert _load('tool')\n"
        ),
    ],
    ids=[
        "import-module-f-string",
        "dunder-import-of-a-name",
        "python-c",
        "load-f-string",
    ],
)
def test_a_test_loading_kew_by_a_name_it_builds_is_always_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    root = _project(
        tmp_path,
        monkeypatch,
        {**_FILLER, "src/kew/c.py": "X = 1\n", "tests/test_dyn.py": source},
    )
    _write(root, {"src/kew/c.py": "X = 0\n"})

    assert "tests/test_dyn.py  always: runs or loads kew dynamically\n" in _explain()


@pytest.mark.parametrize(
    ("files", "chain"),
    [
        (
            {
                "tests/test_imp.py": (
                    "import pytest\n\n\ndef test_i():\n"
                    "    assert pytest.importorskip('kew.c').X\n"
                )
            },
            "tests/test_imp.py  tests/test_imp.py → kew.c [changed]",
        ),
        (
            {
                "tests/test_imp.py": (
                    "def test_i():\n    assert __import__('kew.c', fromlist=['X'])\n"
                )
            },
            "tests/test_imp.py  tests/test_imp.py → kew.c [changed]",
        ),
        (
            {
                "scripts_dir/tool.py": "import kew.c\nVALUE = kew.c.X\n",
                "tests/test_imp.py": (
                    "import importlib.util\nfrom pathlib import Path\n\n"
                    "SCRIPT = Path(__file__).parents[1] / 'scripts_dir' / 'tool.py'\n"
                    "_spec = importlib.util.spec_from_file_location('tool', SCRIPT)\n"
                    "\n\ndef test_i():\n    assert _spec\n"
                ),
            },
            "tests/test_imp.py  tests/test_imp.py → scripts_dir/tool.py → kew.c "
            "[changed]",
        ),
        (
            {
                "tests/pkg/__init__.py": "",
                "tests/pkg/helper.py": "import kew.c\n",
                "tests/pkg/test_imp.py": (
                    "from .helper import kew\n\n\ndef test_i():\n    assert kew.c.X\n"
                ),
            },
            "tests/pkg/test_imp.py  tests/pkg/test_imp.py → tests/pkg/helper.py → "
            "kew.c [changed]",
        ),
    ],
    ids=["importorskip", "dunder-import", "script-loaded-by-path", "relative-import"],
)
def test_an_import_the_test_spells_out_selects_it_with_its_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, str | None],
    chain: str,
) -> None:
    root = _project(
        tmp_path, monkeypatch, {**_FILLER, "src/kew/c.py": "X = 1\n", **files}
    )
    _write(root, {"src/kew/c.py": "X = 0\n"})

    assert f"{chain}\n" in _explain()


def test_a_module_importing_by_an_f_string_reaches_every_module_it_could(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "src/kew/plugins/__init__.py": "",
            "src/kew/plugins/one.py": "X = 1\n",
            "src/kew/loader.py": (
                "import importlib\n\n\ndef load(name):\n"
                "    return importlib.import_module(f'kew.plugins.{name}')\n"
            ),
            "tests/test_loader.py": (
                "from kew.loader import load\n\n\ndef test_l():\n"
                "    assert load('one').X\n"
            ),
        },
    )
    _write(root, {"src/kew/plugins/one.py": "X = 0\n"})

    assert (
        "tests/test_loader.py  tests/test_loader.py → kew.loader → "
        "kew.plugins.one [changed]\n"
    ) in _explain()


def test_a_merged_commit_replays_its_own_diff(repo: Path) -> None:
    _git(repo, "checkout", "--quiet", "main")
    merged = _write(repo, {"src/kew/b.py": "VALUE = 2\n"})

    assert "tests/test_a.py → kew.a → kew.b [changed]" in _explain(merged)


def test_an_unknown_ref_exits_2(repo: Path) -> None:
    result = CliRunner().invoke(app, ["verify", "--explain-selection", "nope"])

    assert result.exit_code == 2
    assert "kew verify:" in result.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            ["--explain-selection", "--selection-report"],
            "--explain-selection and --selection-report ask different questions",
        ),
        (["--last", "3"], "--last is the number of commits --selection-report"),
        (
            ["--explain-selection", "--host", "local"],
            "--explain-selection runs nothing, so it would ignore --host",
        ),
        (
            ["--explain-selection", "--shards", "2"],
            "--explain-selection runs nothing, so it would ignore --shards",
        ),
        (
            ["--explain-selection", "--item", "5"],
            "--explain-selection runs nothing, so it would ignore --item",
        ),
        (
            ["--explain-selection", "--epic", "x"],
            "--explain-selection runs nothing, so it would ignore --epic",
        ),
        (
            ["--explain-selection", "--pr-body", "body.md"],
            "--explain-selection runs nothing, so it would ignore --pr-body",
        ),
        (
            ["--explain-selection", "--last", "3"],
            "--explain-selection runs nothing, so it would ignore --last",
        ),
        (
            ["--selection-report", "candidate"],
            "--selection-report runs nothing, so it would ignore REF",
        ),
        (
            ["--selection-report", "--host", "gcp"],
            "--selection-report runs nothing, so it would ignore --host",
        ),
    ],
    ids=[
        "both-questions",
        "last-alone",
        "explain-host",
        "explain-shards",
        "explain-item",
        "explain-epic",
        "explain-pr-body",
        "explain-last",
        "report-ref",
        "report-host",
    ],
)
def test_an_option_the_question_would_ignore_exits_2(
    repo: Path, args: list[str], message: str
) -> None:
    (repo / "body.md").write_text("body\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["verify", *args])

    assert result.exit_code == 2, result.output
    assert message in result.output


def test_the_selection_report_prints_median_and_p75_for_three_graphs(
    repo: Path,
) -> None:
    _git(repo, "checkout", "--quiet", "main")
    _write(repo, {"src/kew/b.py": "VALUE = 2\n"})
    _write(repo, {"uv.lock": "version = 1\n"})
    _write(repo, {"tests/test_c3.py": "def test_new():\n    assert 1\n"})

    lines = _report(3)

    assert lines[0] == "selection report: 3 merged commits, by test count"
    # Shares by test count: 5/23, 100% (full), and 4/21 once test_c3 shrank.
    assert lines[1:4] == [
        "today: median 21.7%, p75 60.9%",
        "thin kew.cli: median 21.7%, p75 60.9%",
        "core cycle cut: median 21.7%, p75 60.9%",
    ]
    # Without the uv.lock commit, a path rule forced: 5/23 and 4/21.
    assert lines[4:8] == [
        "no path rule fired: 2 of 3 commits",
        "today, no path rule: median 20.4%, p75 21.1%",
        "thin kew.cli, no path rule: median 20.4%, p75 21.1%",
        "core cycle cut, no path rule: median 20.4%, p75 21.1%",
    ]
    for graph in ("today", "thin kew.cli", "core cycle cut"):
        assert f"{graph}, uv.lock changed: 1" in lines
        assert f"{graph}, selected: 2" in lines
    assert lines[-1].startswith("slowest replay: ")


# ── the what-if graphs ──────────────────────────────────────────────────────

_CLI_PROJECT = {
    **_FILLER,
    "src/kew/cli/__init__.py": (
        "import typer\n\n"
        "from kew.cli._apps import epic_app\n"
        "from kew.heavy import crunch\n\n"
        "app = typer.Typer()\n"
        'app.add_typer(epic_app, name="epic")\n\n\n'
        "@app.callback()\ndef _main():\n    pass\n\n\n"
        "@app.command()\ndef verify():\n    from kew.verifier import check\n\n"
        "    check()\n\n\n"
        "@app.command()\ndef heavy():\n    crunch()\n\n\n"
        "from kew.cli import epic as _epic  # noqa: E402\n"
    ),
    "src/kew/cli/_apps.py": "import typer\n\nepic_app = typer.Typer()\n",
    "src/kew/cli/epic.py": (
        "from kew import cli as _c\nfrom kew.cli._apps import epic_app\n\n\n"
        "@epic_app.command()\ndef status():\n    _c.crunch()\n"
    ),
    "src/kew/verifier.py": "def check():\n    return 1\n",
    "src/kew/heavy.py": "def crunch():\n    return 1\n",
    **{
        f"tests/test_{name}_cli.py": (
            "from typer.testing import CliRunner\n\nfrom kew.cli import app\n\n\n"
            f"def test_{name}():\n"
            f"    assert CliRunner().invoke(app, {argv!r}).exit_code == 0\n"
        )
        for name, argv in (
            ("verify", ["verify"]),
            ("heavy", ["heavy"]),
            ("epic", ["epic", "status"]),
        )
    },
}


def _selected(root: Path, transform: verify_selection.Transform | None) -> set[str]:
    preflight_tools(required=("git",))
    cache = verify_selection.FactCache(root)
    inputs = verify_selection.read_inputs(root, "main", "candidate", cache)
    picked = verify_selection.select(inputs, transform=transform)
    assert picked.full_rule is None, picked.render()
    return {path for path, _ in picked.selected}


_CLI_TESTS = {
    "tests/test_verify_cli.py",
    "tests/test_heavy_cli.py",
    "tests/test_epic_cli.py",
}


@pytest.mark.parametrize(
    ("change", "thin"),
    [
        (
            {"src/kew/verifier.py": "def check():\n    return 2\n"},
            {"tests/test_verify_cli.py"},
        ),
        (
            {"src/kew/heavy.py": "def crunch():\n    return 2\n"},
            {"tests/test_heavy_cli.py", "tests/test_epic_cli.py"},
        ),
    ],
    ids=["a-command-s-lazy-import", "a-name-a-sub-app-command-uses"],
)
def test_a_thin_kew_cli_selects_only_the_tests_driving_a_command_that_reaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, str | None],
    thin: set[str],
) -> None:
    root = _project(tmp_path, monkeypatch, _CLI_PROJECT)
    _write(root, change)
    what_if = verify_selection_report.ThinCli(root)

    assert _selected(root, None) == _CLI_TESTS
    assert _selected(root, what_if) == thin


def test_a_thin_kew_cli_counts_an_edited_command_as_its_own_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, monkeypatch, _CLI_PROJECT)
    source = str(_CLI_PROJECT["src/kew/cli/__init__.py"])
    _write(
        root, {"src/kew/cli/__init__.py": source.replace("    crunch()", "    pass")}
    )
    what_if = verify_selection_report.ThinCli(root)

    assert _selected(root, None) == _CLI_TESTS
    assert _selected(root, what_if) == {"tests/test_heavy_cli.py"}


def test_a_thin_kew_cli_keeps_every_command_when_the_assembly_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, monkeypatch, _CLI_PROJECT)
    source = str(_CLI_PROJECT["src/kew/cli/__init__.py"])
    _write(root, {"src/kew/cli/__init__.py": source.replace('name="epic"', 'name="e"')})
    what_if = verify_selection_report.ThinCli(root)

    assert _selected(root, what_if) == _CLI_TESTS


_CYCLE_PROJECT = {
    **_FILLER,
    "src/kew/x.py": "import kew.y\n",
    "src/kew/y.py": "def f():\n    import kew.x\n\n    return kew.x\n",
    "tests/test_x.py": "import kew.x\n\n\ndef test_x():\n    pass\n",
    "tests/test_y.py": "import kew.y\n\n\ndef test_y():\n    pass\n",
}


def test_cutting_the_core_cycle_drops_what_only_a_lazy_import_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, monkeypatch, _CYCLE_PROJECT)
    _write(root, {"src/kew/x.py": "import kew.y\n\nX = 1\n"})

    today = _selected(root, None)
    cut = _selected(root, verify_selection_report.cut_core_cycle)

    assert today == {"tests/test_x.py", "tests/test_y.py"}
    assert cut == {"tests/test_x.py"}


@pytest.mark.parametrize(
    ("files", "change", "spread"),
    [
        (
            _CLI_PROJECT,
            {"src/kew/verifier.py": "def check():\n    return 2\n"},
            {
                "today": "median 33.3%, p75 33.3%",
                "thin kew.cli": "median 11.1%, p75 11.1%",
                "core cycle cut": "median 33.3%, p75 33.3%",
            },
        ),
        (
            _CYCLE_PROJECT,
            {"src/kew/x.py": "import kew.y\n\nX = 1\n"},
            {
                "today": "median 25.0%, p75 25.0%",
                "thin kew.cli": "median 25.0%, p75 25.0%",
                "core cycle cut": "median 12.5%, p75 12.5%",
            },
        ),
    ],
    ids=["thin-cli", "core-cycle-cut"],
)
def test_the_selection_report_measures_each_what_if(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, str | None],
    change: dict[str, str | None],
    spread: dict[str, str],
) -> None:
    root = _project(tmp_path, monkeypatch, files)
    _git(root, "checkout", "--quiet", "main")
    _write(root, change)

    lines = _report(1)

    for graph, numbers in spread.items():
        assert f"{graph}: {numbers}" in lines
        assert f"{graph}, no path rule: {numbers}" in lines


@pytest.mark.policy
def test_the_selector_reads_this_repository_s_own_conftest() -> None:
    """On this repository's own HEAD, the full-suite rule starts from the kew
    modules ``tests/conftest.py`` really imports — the ones it names to
    ``importlib`` too — and every one resolves."""
    preflight_tools(required=("git",))
    root = Path(__file__).resolve().parents[1]
    snap = verify_selection.snapshot(root, "HEAD", verify_selection.FactCache(root))
    graph = verify_selection.edges(snap)

    conftest = f"{snap.prefix}tests/conftest.py"
    starts = {t for t in graph[conftest] if "/" not in t}

    assert {"kew.config", "kew.repo_host", "kew.subprocess_utils"} <= starts
    assert {"kew.dispatch_core", "kew.review", "kew.ci_fix", "kew.resolve"} <= starts
    assert starts <= set(snap.module_paths)


# ── the files a test reads (#3211) ──────────────────────────────────────────

_READS_DIR = (
    "from pathlib import Path\n\nimport kew.z\n\n"
    "REPO_ROOT = Path(__file__).resolve().parents[1]\n\n\n"
    "def test_nav():\n"
    '    for page in (REPO_ROOT / "docs" / "web").rglob("*.md"):\n'
    "        assert page.read_text()\n"
)


@pytest.mark.parametrize(
    ("path", "reader", "shown"),
    [
        (
            "docs/security.md",
            'Path(__file__).parents[1] / "docs/security.md"',
            "docs/security.md",
        ),
        (
            "frontend/src/lib/run-request.ts",
            'Path(__file__).parent.parent.joinpath("frontend", "src", "lib")',
            "frontend/src/lib/",
        ),
        (
            "frontend/fixtures/factory/dashboard.json",
            'os.path.join(os.path.dirname(__file__), "..", "frontend/fixtures")',
            "frontend/fixtures/",
        ),
        (
            "tests/fixtures/run.json",
            'Path(__file__).parent / "fixtures" / "run.json"',
            "tests/fixtures/run.json",
        ),
        # Tests run from the project root, so ``../`` names the repository.
        ("docs/x.md", 'Path("../docs/x.md")', "docs/x.md"),
    ],
    ids=["docs-file", "frontend-dir", "os-path-join", "tests-fixture", "cwd-relative"],
)
def test_a_changed_file_a_test_reads_selects_that_test_naming_the_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    reader: str,
    shown: str,
) -> None:
    test = (
        "import os\nfrom pathlib import Path\n\nimport kew.z\n\n\n"
        f"def test_reads():\n    assert {reader}\n"
    )
    root = _project(
        tmp_path,
        monkeypatch,
        {**_FILLER, path: "before\n", "tests/test_reads.py": test},
    )
    _write(root, {path: "after\n"})

    out = _explain()

    assert out.startswith("selector v3\n")
    assert "result: 1 of 7 test files, 1 of 7 tests\n" in out
    assert (
        f"tests/test_reads.py  tests/test_reads.py ← reads {shown} [changed {path}]\n"
    ) in out


@pytest.mark.parametrize(
    "reader",
    [
        'Path(f"{Path(__file__).parents[1]}/docs/a.md")',
        'os.path.dirname(__file__) + "/../docs/a.md"',
        'os.path.dirname(__file__) + "/.." + "/docs/a.md"',
        "f\"{os.path.dirname(__file__)}/../docs/{'a.md'}\"",
    ],
    ids=["f-string", "concatenation", "concatenations", "f-string-literal-tail"],
)
def test_a_path_spelled_as_a_string_off_file_selects_its_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str
) -> None:
    test = (
        "import os\nfrom pathlib import Path\n\nimport kew.z\n\n\n"
        f"def test_reads():\n    assert {reader}\n"
    )
    root = _project(
        tmp_path,
        monkeypatch,
        {**_FILLER, "docs/a.md": "before\n", "tests/test_reads.py": test},
    )
    _write(root, {"docs/a.md": "after\n"})

    out = _explain()

    assert "result: 1 of 7 test files, 1 of 7 tests\n" in out
    assert "tests/test_reads.py  tests/test_reads.py ← reads docs/" in out


@pytest.mark.parametrize(
    "reader",
    [
        'Path(f"{Path(__file__).parents[1]}/{name}")',
        "os.path.dirname(__file__) + name",
        'os.path.dirname(__file__) + "/../" + name',
    ],
    ids=["f-string", "concatenation", "concatenation-after-up"],
)
def test_a_string_path_off_file_with_a_variable_runs_on_every_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str
) -> None:
    test = (
        "import os\nfrom pathlib import Path\n\nimport kew.z\n\n\n"
        f'def test_reads():\n    name = os.environ["PAGE"]\n    assert {reader}\n'
    )
    root = _project(
        tmp_path,
        monkeypatch,
        {**_FILLER, "docs/a.md": "before\n", "tests/test_reads.py": test},
    )
    _write(root, {"docs/a.md": "after\n"})

    out = _explain()

    assert "result: 1 of 7 test files, 1 of 7 tests\n" in out
    assert "tests/test_reads.py  always: builds a repo path from a variable\n" in out


def test_a_docs_change_selects_the_test_reading_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape of #3037: only a docs page and frontend fixtures changed, and
    the test that reads the page has no import chain to either."""
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "docs/web/index.md": "before\n",
            "frontend/fixtures/nav.json": "{}\n",
            # Like the real file: one other test in it is marked policy.
            "tests/test_docs_nav.py": (
                f"{_READS_DIR}\n\n@pytest.mark.policy\ndef test_lint():\n    assert 1\n"
            ).replace("import kew.z", "import pytest\n\nimport kew.z"),
        },
    )
    _write(root, {"docs/web/index.md": "after\n", "frontend/fixtures/nav.json": "[]\n"})

    out = _explain()

    assert "result: 1 of 7 test files, 2 of 8 tests\n" in out
    assert (
        "tests/test_docs_nav.py  tests/test_docs_nav.py ← reads docs/web/ "
        "[changed docs/web/index.md]\n"
    ) in out


def test_a_path_a_support_module_reads_selects_its_importers_through_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "docs/cli/verify.md": "before\n",
            "tests/support/__init__.py": "",
            "tests/support/pages.py": (
                "from pathlib import Path\n\n"
                'DOCS = Path(__file__).resolve().parents[2] / "docs"\n\n\n'
                "def page(name):\n    return (DOCS / name).read_text()\n"
            ),
            "tests/test_page.py": (
                "import kew.z\nfrom support.pages import page\n\n\n"
                'def test_page():\n    assert page("cli/verify.md")\n'
            ),
        },
    )
    _write(root, {"docs/cli/verify.md": "after\n"})

    out = _explain()

    assert "result: 1 of 7 test files, 1 of 7 tests\n" in out
    assert (
        "tests/test_page.py  tests/test_page.py → tests/support/pages.py "
        "← reads docs/ [changed docs/cli/verify.md]\n"
    ) in out


def test_a_readable_path_no_test_reads_selects_no_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(
        tmp_path,
        monkeypatch,
        {**_FILLER, "docs/other.md": "before\n", "tests/test_docs_nav.py": _READS_DIR},
    )
    _write(root, {"docs/other.md": "after\n"})

    out = _explain()

    assert "result: 0 of 7 test files, 0 of 7 tests\n" in out


def test_a_test_reading_a_path_built_from_a_variable_always_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variable = (
        "from pathlib import Path\n\nimport pytest\n\nimport kew.z\n\n"
        "ROOT = Path(__file__).parents[1]\n\n\n"
        '@pytest.mark.parametrize("rel", ["docs/a.md"])\n'
        "def test_page(rel):\n    assert (ROOT / rel).read_text()\n"
    )
    scratch = (
        "import kew.y\n\n\ndef test_scratch(tmp_path):\n"
        '    name = "x.md"\n    (tmp_path / name).write_text("x")\n'
    )
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "src/kew/y.py": "",
            "tests/test_variable.py": variable,
            "tests/test_scratch.py": scratch,
        },
    )
    _write(root, {"src/kew/y.py": "VALUE = 1\n"})

    out = _explain()

    assert "result: 2 of 8 test files, 2 of 8 tests\n" in out
    assert "tests/test_variable.py  always: builds a repo path from a variable\n" in out
    assert "tests/test_scratch.py  tests/test_scratch.py → kew.y [changed]\n" in out


def test_a_project_in_a_subdirectory_reads_paths_from_the_repository_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run from ``kew/``, the trees and the diff name the same paths: a module
    change traces through the graph, a root docs change to the test reading it."""
    reader = (
        "from pathlib import Path\n\nimport kew.z\n\n\ndef test_doc():\n"
        '    assert (Path(__file__).parents[2] / "docs" / "a.md").read_text()\n'
    )
    files = {f"kew/{rel}": text for rel, text in _FILLER.items()}
    _project(
        tmp_path,
        monkeypatch,
        {
            **files,
            "docs/a.md": "before\n",
            "kew/src/kew/y.py": "",
            "kew/tests/test_doc.py": reader,
            "kew/tests/test_y.py": "import kew.y\n\n\ndef test_y():\n    assert 1\n",
        },
    )
    _write(tmp_path, {"docs/a.md": "after\n", "kew/src/kew/y.py": "VALUE = 1\n"})
    preflight_tools(required=("git",))

    out = verify_selection.explain(tmp_path / "kew", "candidate").render()

    assert (
        "tests/test_doc.py  tests/test_doc.py ← reads docs/a.md [changed docs/a.md]\n"
    ) in out
    assert "tests/test_y.py  tests/test_y.py → kew.y [changed]\n" in out
    assert "result: 2 of 8 test files, 2 of 8 tests\n" in out


def test_the_selection_report_prints_the_share_before_and_after_the_read_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(
        tmp_path,
        monkeypatch,
        {**_FILLER, "docs/web/index.md": "before\n", "tests/test_nav.py": _READS_DIR},
    )
    _git(root, "checkout", "--quiet", "main")
    _write(root, {"docs/web/index.md": "after\n"})

    lines = _report(1)

    # Before, a docs change answered the full suite; now 1 of 7 tests.
    assert "before the file-read rule: median 100.0%, p75 100.0%" in lines
    assert "after the file-read rule: median 14.3%, p75 14.3%" in lines
    # The slowest replay times four selections, not three.
    assert lines[-1].startswith("slowest replay: ")
    assert lines[-1].endswith(
        "s for the three graphs and the selection before the file-read rule"
    )


# ── what the file-read rule cannot see runs on every change ─────────────────
#
# A selector false negative reddens main once the verify gates on selection,
# so a path the selector cannot name runs its reader rather than nothing.

_HEADER = (
    "import os\nimport subprocess\nfrom pathlib import Path\n\nimport pytest\n\n"
    "import kew.z\n\nROOT = Path(__file__).resolve().parents[1]\n\n\n"
)


def _reader(body: str, module: str = "") -> str:
    """A test file under ``tests/`` whose one test runs *body*, with *module*
    code before it."""
    return f"{_HEADER}{module}def test_reads():\n" + "".join(
        f"    {line}\n" for line in body.splitlines()
    )


@pytest.mark.parametrize(
    "body",
    [
        'subprocess.run(["bash", "scripts/check-docs.sh"], check=False)',
        'subprocess.run(["npm", "run", "ui-check"], cwd="frontend", check=False)',
        'subprocess.run(["node", str(ROOT / "scripts" / "gen.mjs")], check=False)',
        'os.system("bash scripts/check-docs.sh")',
        'command = ["bash", os.environ["GATE"]]\nsubprocess.run(command, check=False)',
        # ``-e`` is errexit to a shell, not inline code: the script still runs.
        'subprocess.run(["bash", "-e", "scripts/check-docs.sh"], check=False)',
        'subprocess.run(["sh", "-o", "errexit", "scripts/x.sh"], check=False)',
    ],
    ids=[
        "bash-script",
        "npm-run",
        "node-path",
        "shell-string",
        "built-elsewhere",
        "shell-option-then-script",
        "shell-option-argument-then-script",
    ],
)
def test_a_test_that_runs_a_script_runs_on_every_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """Like ``tests/test_skills_catalog.py``, which runs ``check-docs.sh``: the
    script greps the whole tree, and the selector cannot see what it reads."""
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "docs/notes/old.md": "before\n",
            "tests/test_gate.py": _reader(body),
        },
    )
    _write(root, {"docs/notes/old.md": "after\n"})

    out = _explain()

    assert "result: 1 of 7 test files, 1 of 7 tests\n" in out
    assert "tests/test_gate.py  always: runs a script\n" in out


def test_a_script_run_through_a_support_module_runs_its_importers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "docs/a.md": "before\n",
            "tests/support/__init__.py": "",
            "tests/support/gates.py": (
                "import subprocess\n\n\ndef run_gate():\n"
                '    return subprocess.run(["bash", "scripts/check-docs.sh"])\n'
            ),
            "tests/test_gate.py": (
                "import kew.z\nfrom support.gates import run_gate\n\n\n"
                "def test_gate():\n    assert run_gate()\n"
            ),
        },
    )
    _write(root, {"docs/a.md": "after\n"})

    out = _explain()

    assert "tests/test_gate.py  always: runs a script\n" in out


@pytest.mark.parametrize(
    "command",
    [
        '["bash", "-c", "exit 0"]',
        # GitHub's default bash shell, as the workflow tests run a step.
        '["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", "exit 0"]',
        '["bash", "--noprofile", "--norc", "-e", "-c", "exit 0"]',
        '["sh", "-o", "errexit", "-xc", "exit 0"]',
    ],
    ids=["bash-c", "github-shell", "errexit-then-c", "option-argument-then-c"],
)
def test_inline_shell_code_is_not_a_script_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    body = f"subprocess.run({command}, check=False)"
    root = _project(
        tmp_path,
        monkeypatch,
        {**_FILLER, "docs/a.md": "before\n", "tests/test_gate.py": _reader(body)},
    )
    _write(root, {"docs/a.md": "after\n"})

    assert "result: 0 of 7 test files, 0 of 7 tests\n" in _explain()


@pytest.mark.parametrize("path", ["README.md", "www/src/app.html"])
def test_a_root_file_or_a_www_file_answers_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """A script a test runs may read it unseen, as ``check-docs.sh`` reads
    ``README.md`` and ``www/``: the full suite runs, the gate test with it."""
    body = (
        'assert (ROOT / "README.md").read_text()\n'
        'subprocess.run(["bash", "scripts/check-docs.sh"], check=False)'
    )
    root = _project(
        tmp_path,
        monkeypatch,
        {**_FILLER, path: "before\n", "tests/test_skills_catalog.py": _reader(body)},
    )
    _write(root, {path: "See docs/notes/old.md for history.\n"})

    out = _explain()

    assert "result: full suite (7 test files)\n" in out
    assert f"rule: an untraced path changed: {path}\n" in out


@pytest.mark.parametrize(
    "reader",
    [
        '((ROOT / rel).parent / "page.md")',
        "os.path.dirname(os.path.join(ROOT, rel))",
        '(ROOT / rel).parents[0] / "page.md"',
        '(ROOT / "docs" / rel).parent.parent / "page.md"',
        'ROOT.parents[depth] / "docs"',
    ],
    ids=["parent", "dirname", "parents-n", "parents-past-a-literal", "variable-index"],
)
def test_a_variable_path_under_a_step_up_runs_on_every_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str
) -> None:
    body = (
        'listed = subprocess.run(["git", "ls-files"], capture_output=True, text=True)\n'
        "depth = len(listed.stdout)\n"
        "for rel in listed.stdout.split():\n"
        f"    assert os.path.exists({reader})"
    )
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "docs/probe/page.md": "before\n",
            "tests/test_pages.py": _reader(body),
        },
    )
    _write(root, {"docs/probe/page.md": "after\n"})

    out = _explain()

    assert "result: 1 of 7 test files, 1 of 7 tests\n" in out
    assert "tests/test_pages.py  always: builds a repo path from a variable\n" in out


@pytest.mark.parametrize(
    "test",
    [
        (
            "from pathlib import Path\n\nimport kew.z\n\n\ndef test_page():\n"
            "    root = Path(__file__).resolve().parents[1]\n"
            '    assert (root / os.environ["PAGE"]).read_text()\n'
        ),
        (
            "from pathlib import Path\n\nimport pytest\n\nimport kew.z\n\n\n"
            "@pytest.fixture\ndef repo_root():\n"
            "    return Path(__file__).resolve().parents[1]\n\n\n"
            '@pytest.mark.parametrize("rel", ["docs/a.md"])\n'
            "def test_page(repo_root, rel):\n    assert (repo_root / rel).read_text()\n"
        ),
        (
            "from pathlib import Path\n\nimport kew.z\n\n"
            "ROOT = Path(__file__).resolve().parents[1]\n\n\ndef test_links():\n"
            '    for page in sorted((ROOT / "docs").rglob("*.md")):\n'
            "        for link in page.read_text().split():\n"
            "            assert (page.parent / link).exists()\n"
        ),
        (
            "from pathlib import Path\n\nimport kew.z\n\n\ndef _root():\n"
            "    return Path(__file__).resolve().parents[1]\n\n\n"
            'def test_page():\n    assert (_root() / os.environ["PAGE"]).read_text()\n'
        ),
    ],
    ids=["function-local", "fixture-parameter", "loop-variable", "returned-root"],
)
def test_a_root_bound_inside_a_function_with_a_variable_after_it_runs_always(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test: str
) -> None:
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "docs/a.md": "x\n",
            "frontend/src/app.ts": "before\n",
            "tests/test_page.py": f"import os\n{test}",
        },
    )
    _write(root, {"frontend/src/app.ts": "after\n"})

    out = _explain()

    assert "result: 1 of 7 test files, 1 of 7 tests\n" in out
    assert "tests/test_page.py  always: builds a repo path from a variable\n" in out


def test_a_root_bound_inside_a_function_reads_what_its_literals_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (
        'here = Path(__file__).parent\nassert (here / "fixtures" / "run.json").exists()'
    )
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "tests/fixtures/run.json": "{}\n",
            "tests/test_run.py": _reader(body),
        },
    )
    _write(root, {"tests/fixtures/run.json": "[]\n"})

    out = _explain()

    assert "result: 1 of 7 test files, 1 of 7 tests\n" in out
    assert (
        "tests/test_run.py  tests/test_run.py ← reads tests/fixtures/run.json "
        "[changed tests/fixtures/run.json]\n"
    ) in out


_DATA = 'DATA = Path(__file__).resolve().parent / "data.json"\n\n\n'


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (
            'assert (DATA.parents[1] / os.environ["P"]).exists()',
            "always: builds a repo path from a variable",
        ),
        (
            'assert list(DATA.parents[1].joinpath("tests", "fixtures").rglob("*"))',
            "tests/test_run.py ← reads tests/fixtures/ "
            "[changed tests/fixtures/run.json]",
        ),
    ],
    ids=["then-a-variable", "then-a-walk"],
)
def test_parents_past_a_literal_segment_climb_from_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, why: str
) -> None:
    """``DATA.parents[1]`` drops ``data.json`` and climbs one step more, to
    the project root, as ``.parent.parent`` does."""
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "tests/fixtures/run.json": "{}\n",
            "tests/test_run.py": _reader(body, _DATA),
        },
    )
    _write(root, {"tests/fixtures/run.json": "[]\n"})

    out = _explain()

    assert "result: 1 of 7 test files, 1 of 7 tests\n" in out
    assert f"tests/test_run.py  {why}\n" in out


_ROUTE = "frontend/src/routes/(factory)/+page.svelte"


@pytest.mark.parametrize(
    "reader",
    [
        f'Path("{_ROUTE}")',
        'ROOT / "frontend" / "src" / "routes" / "(factory)" / "+page.svelte"',
        "_page(Path.cwd())",
    ],
    ids=["lone-string", "chain", "unknown-root"],
)
def test_a_sveltekit_route_path_selects_its_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str
) -> None:
    helper = f'def _page(base):\n    return base / "{_ROUTE}"\n\n\n'
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            _ROUTE: "before\n",
            "tests/test_route.py": _reader(f"assert {reader}", helper),
        },
    )
    _write(root, {_ROUTE: "after\n"})

    out = _explain()

    assert "result: 1 of 7 test files, 1 of 7 tests\n" in out
    assert (
        f"tests/test_route.py  tests/test_route.py ← reads {_ROUTE} "
        f"[changed {_ROUTE}]\n"
    ) in out


@pytest.mark.parametrize(
    "reader",
    ['TESTS_DIR / "fixtures" / "run.json"', 'DOCS / "cli" / "verify.md"'],
    ids=["tests-dir", "docs-dir"],
)
def test_a_path_under_a_root_a_support_module_exports_always_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str
) -> None:
    """Where the exported root sits is the support module's to say, and the
    importer cannot: ``TESTS_DIR`` is ``tests/``, ``DOCS`` is ``docs/``."""
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "tests/fixtures/run.json": "{}\n",
            "tests/support/__init__.py": "",
            "tests/support/roots.py": (
                "from pathlib import Path\n\n"
                "TESTS_DIR = Path(__file__).resolve().parents[1]\n"
                'DOCS = Path(__file__).resolve().parents[2] / "docs"\n'
            ),
            "tests/test_roots.py": (
                "import kew.z\nfrom support.roots import DOCS, TESTS_DIR\n\n\n"
                f"def test_roots():\n    assert {reader}.read_text()\n"
            ),
        },
    )
    _write(root, {"tests/fixtures/run.json": "[]\n"})

    out = _explain()

    assert "tests/test_roots.py  always: builds a repo path from a variable\n" in out


def test_a_root_a_support_function_hands_out_runs_its_importers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "frontend/src/app.ts": "before\n",
            "tests/support/__init__.py": "",
            "tests/support/roots.py": (
                "from pathlib import Path\n\n\ndef repo_root():\n"
                "    return Path(__file__).resolve().parents[2]\n"
            ),
            "tests/test_roots.py": (
                "import os\n\nimport kew.z\nfrom support.roots import repo_root\n\n\n"
                "def test_roots():\n"
                '    assert (repo_root() / os.environ["P"]).exists()\n'
            ),
        },
    )
    _write(root, {"frontend/src/app.ts": "after\n"})

    out = _explain()

    assert "tests/test_roots.py  always: builds a repo path from a variable\n" in out


#: The shape of ``tests/test_prompts_fixture_parity.py``: a helper searches up
#: from ``__file__`` for one directory, and a caller climbs out of it.
_CLIMBING_HELPER = (
    "import json\nfrom pathlib import Path\n\nimport pytest\n\nimport kew.z\n\n"
    '_DIR = "frontend/fixtures/factory"\n\n\n'
    "def _fixture(name):\n"
    "    for parent in Path(__file__).resolve().parents:\n"
    "        candidate = parent / _DIR / name\n"
    "        if candidate.is_file():\n"
    '            return json.loads(candidate.read_text(encoding="utf-8"))\n'
    '    pytest.fail(f"{_DIR}/{name} is missing")\n\n\n'
)


@pytest.mark.parametrize(
    ("path", "shown"),
    [
        (
            "frontend/fixtures/scenarios/prompts-outcomes.json",
            "frontend/fixtures/scenarios/prompts-outcomes.json",
        ),
        (
            "frontend/fixtures/factory/prompts-dispatch.json",
            "frontend/fixtures/factory/",
        ),
    ],
    ids=["climbed-to", "the-helper-s-directory"],
)
def test_a_literal_a_helper_climbs_out_of_its_directory_with_selects_its_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, shown: str
) -> None:
    """``_fixture("../scenarios/x.json")`` reads ``frontend/fixtures/scenarios``,
    not the ``frontend/fixtures/factory`` the helper names; the f-string call
    still reads anything in the helper's own directory."""
    test = _CLIMBING_HELPER + (
        "def test_catalog():\n"
        '    assert _fixture("prompts.json")\n'
        '    for role in ("dispatch",):\n'
        '        assert _fixture(f"prompts-{role}.json")\n\n\n'
        "def test_outcomes():\n"
        '    assert _fixture("../scenarios/prompts-outcomes.json")\n'
    )
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "frontend/fixtures/factory/prompts.json": "[]\n",
            "frontend/fixtures/factory/prompts-dispatch.json": "{}\n",
            "frontend/fixtures/scenarios/prompts-outcomes.json": "{}\n",
            "tests/test_prompts.py": test,
        },
    )
    _write(root, {path: "{}\n\n"})

    out = _explain()

    assert "result: 1 of 7 test files, 2 of 8 tests\n" in out
    reader = "tests/test_prompts.py"
    assert f"{reader}  {reader} ← reads {shown} [changed {path}]\n" in out


def test_a_directory_a_test_module_s_helper_reads_for_its_importer_selects_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another test file imports the helper and passes its own name: the
    helper's directory read is not narrowed to the names its own file
    passes, so a fixture only the importer asks for selects both."""
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "frontend/fixtures/factory/prompts.json": "[]\n",
            "frontend/fixtures/factory/other.json": "{}\n",
            "tests/test_a.py": (
                "import json\nfrom pathlib import Path\n\nimport kew.z\n\n"
                "ROOT = Path(__file__).resolve().parents[1]\n\n\n"
                "def _fixture(name):\n"
                '    path = ROOT / "frontend" / "fixtures" / "factory" / name\n'
                "    return json.loads(path.read_text())\n\n\n"
                'def test_catalog():\n    assert _fixture("prompts.json")\n'
            ),
            "tests/test_b.py": (
                "import kew.z\nfrom test_a import _fixture\n\n\n"
                'def test_other():\n    assert _fixture("other.json")\n'
            ),
        },
    )
    _write(root, {"frontend/fixtures/factory/other.json": "{}\n\n"})

    out = _explain()

    assert "result: 2 of 8 test files, 2 of 8 tests\n" in out
    changed = "[changed frontend/fixtures/factory/other.json]"
    assert (
        f"tests/test_a.py  tests/test_a.py ← reads frontend/fixtures/factory/ {changed}"
    ) in out
    assert "tests/test_b.py  tests/test_b.py → tests/test_a.py ← reads" in out


def test_a_helper_whose_importer_sorts_first_still_reads_for_the_importer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The importing file sorts before the module holding the helper: the
    helper's directory read still widens to the importer's name."""
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "frontend/fixtures/factory/prompts.json": "[]\n",
            "frontend/fixtures/factory/other.json": "{}\n",
            "tests/test_a.py": (
                "import kew.z\nfrom test_b import _fixture\n\n\n"
                'def test_other():\n    assert _fixture("other.json")\n'
            ),
            "tests/test_b.py": (
                "import json\nfrom pathlib import Path\n\nimport kew.z\n\n"
                "ROOT = Path(__file__).resolve().parents[1]\n\n\n"
                "def _fixture(name):\n"
                '    path = ROOT / "frontend" / "fixtures" / "factory" / name\n'
                "    return json.loads(path.read_text())\n\n\n"
                'def test_catalog():\n    assert _fixture("prompts.json")\n'
            ),
        },
    )
    _write(root, {"frontend/fixtures/factory/other.json": "{}\n\n"})

    out = _explain()

    assert "result: 2 of 8 test files, 2 of 8 tests\n" in out
    assert "tests/test_a.py  tests/test_a.py → tests/test_b.py ← reads" in out


_CLIMB_LINE = "always: a path climbs out of a directory it reads with ..\n"


@pytest.mark.parametrize(
    "caller",
    [
        # A parametrized name: the helper's argument is no literal at the call.
        '@pytest.mark.parametrize("name", ["../scenarios/a.json"])\n'
        "def test_scenario(name):\n    assert _fixture(name)\n",
        # A value read out of a module constant.
        '_SCENARIOS = {"a": "../scenarios/a.json"}\n\n\n'
        "def test_scenario():\n    assert _fixture(_SCENARIOS['a'])\n",
    ],
    ids=["parametrized", "subscript"],
)
def test_a_climb_handed_to_a_helper_through_a_variable_always_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller: str
) -> None:
    """The ``..`` reaches the helper as a value the selector cannot follow:
    where the path lands is unknown, so the test runs on every change."""
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "frontend/fixtures/scenarios/a.json": "{}\n",
            "tests/test_prompts.py": _CLIMBING_HELPER + caller,
        },
    )
    _write(root, {"frontend/fixtures/scenarios/a.json": "[]\n"})

    out = _explain()

    assert "result: 1 of 7 test files, 1 of 7 tests\n" in out
    assert f"tests/test_prompts.py  {_CLIMB_LINE}" in out


def test_a_climb_into_a_support_helper_s_directory_always_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper reading a directory under a variable is a support module's;
    the ``..`` its caller passes it is the test's."""
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            "frontend/fixtures/scenarios/a.json": "{}\n",
            "tests/support/__init__.py": "",
            "tests/support/fixtures.py": (
                "import json\nfrom pathlib import Path\n\n"
                "ROOT = Path(__file__).resolve().parents[2]\n\n\n"
                "def fixture(name):\n"
                '    path = ROOT / "frontend/fixtures/factory" / name\n'
                "    return json.loads(path.read_text())\n"
            ),
            "tests/test_prompts.py": (
                "import kew.z\nfrom support.fixtures import fixture\n\n\n"
                'def test_scenario():\n    assert fixture("../scenarios/a.json")\n'
            ),
        },
    )
    _write(root, {"frontend/fixtures/scenarios/a.json": "[]\n"})

    out = _explain()

    assert "result: 1 of 7 test files, 1 of 7 tests\n" in out
    assert f"tests/test_prompts.py  {_CLIMB_LINE}" in out


def test_a_variable_after_an_upward_search_from_file_always_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``for parent in Path(__file__).parents: parent / name`` may name any
    file in the repository."""
    test = (
        "import os\nfrom pathlib import Path\n\nimport kew.z\n\n\n"
        "def test_any():\n"
        '    name = os.environ["PAGE"]\n'
        "    for parent in Path(__file__).resolve().parents:\n"
        "        assert (parent / name).exists()\n"
    )
    root = _project(
        tmp_path,
        monkeypatch,
        {**_FILLER, "docs/a.md": "before\n", "tests/test_any.py": test},
    )
    _write(root, {"docs/a.md": "after\n"})

    out = _explain()

    assert "result: 1 of 7 test files, 1 of 7 tests\n" in out
    assert "tests/test_any.py  always: builds a repo path from a variable\n" in out


def test_a_repo_file_a_kew_module_reads_selects_the_tests_importing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like ``report_export`` reading ``frontend/static/favicon.svg``."""
    logo = "frontend/static/favicon.svg"
    root = _project(
        tmp_path,
        monkeypatch,
        {
            **_FILLER,
            logo: "<svg/>\n",
            "src/kew/brand.py": (
                "from pathlib import Path\n\n\ndef logo_path():\n"
                "    return (\n        Path(__file__).resolve().parents[2]\n"
                '        / "frontend" / "static" / "favicon.svg"\n    )\n'
            ),
            "tests/test_brand.py": (
                "from kew.brand import logo_path\n\n\n"
                "def test_logo():\n    assert logo_path().read_text()\n"
            ),
        },
    )
    _write(root, {logo: "<svg></svg>\n"})

    out = _explain()

    assert "result: 1 of 7 test files, 1 of 7 tests\n" in out
    assert (
        f"tests/test_brand.py  tests/test_brand.py → kew.brand ← reads {logo} "
        f"[changed {logo}]\n"
    ) in out


@pytest.mark.parametrize(
    "reader",
    [
        '"%s/docs/p/page.md" % ROOT',
        '"{}/docs/p/page.md".format(ROOT)',
        '(ROOT / "docs/p/page").with_suffix(".md")',
        '"%s/docs/p/%s" % (ROOT, name)',
    ],
    ids=["percent", "format", "with-suffix", "percent-variable"],
)
def test_a_path_formatted_or_renamed_off_a_root_selects_its_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str
) -> None:
    body = f'name = os.environ["PAGE"]\nassert Path({reader}).read_text()'
    root = _project(
        tmp_path,
        monkeypatch,
        {**_FILLER, "docs/p/page.md": "before\n", "tests/test_reads.py": _reader(body)},
    )
    _write(root, {"docs/p/page.md": "after\n"})

    out = _explain()

    assert "result: 1 of 7 test files, 1 of 7 tests\n" in out
    assert "tests/test_reads.py  tests/test_reads.py ← reads docs/p/" in out


@pytest.mark.parametrize(
    "reads",
    [
        'for rel in os.environ.get("PAGES", "").split():\n    (ROOT / rel).read_text()',
        '(ROOT / "docs" / "a.md").read_text()',
    ],
    ids=["by-variable", "by-literal"],
)
def test_a_file_the_root_conftest_reads_answers_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reads: str
) -> None:
    """Every test loads ``tests/conftest.py``: a file it reads, or any file
    when it reads by a path it builds, is every test's input."""
    conftest = (
        "import os\nfrom pathlib import Path\n\n"
        "ROOT = Path(__file__).resolve().parents[1]\n\n\n"
        "def pytest_configure(config):\n"
        + "".join(f"    {line}\n" for line in reads.splitlines())
    )
    root = _project(
        tmp_path,
        monkeypatch,
        {**_FILLER, "docs/a.md": "before\n", "tests/conftest.py": conftest},
    )
    _write(root, {"docs/a.md": "after\n"})

    out = _explain()

    assert "result: full suite (6 test files)\n" in out
    assert "rule: tests/conftest.py reads it: docs/a.md\n" in out


# The real catches of the testing-strategy study, each replayed as its shape:
# what the PR changed and how the test that failed reaches it. #3037's shape
# is test_a_docs_change_selects_the_test_reading_its_directory.
_CATCHES = {
    # #2791: an image input under src/ that is not Python.
    "non-python-under-src": (
        {"src/kew/_docker/agent.Dockerfile": "FROM a\n"},
        {"src/kew/_docker/agent.Dockerfile": "FROM b\n"},
        "tests/test_sandbox.py",
        "full suite",
    ),
    # #2815 head 1, #2907, #2940, #2986, #3040: the failing test imports the
    # changed module, directly or through another.
    "imports-the-change": (
        {
            "src/kew/acceptance_author.py": "VALUE = 1\n",
            "src/kew/dispatch_core.py": "from kew import acceptance_author\n",
            "tests/test_acceptance_author_dispatch.py": (
                "from kew.dispatch_core import acceptance_author\n\n\n"
                "def test_launch():\n    assert acceptance_author.VALUE\n"
            ),
        },
        {"src/kew/acceptance_author.py": "VALUE = 2\n"},
        "tests/test_acceptance_author_dispatch.py",
        "tests/test_acceptance_author_dispatch.py → kew.dispatch_core → "
        "kew.acceptance_author [changed]",
    ),
    # #2815 head 2, #3015 (both heads): the failing test is one the PR changed.
    "changed-by-the-pr": (
        {
            "tests/test_acceptance_author.py": (
                "import kew.z\n\n\ndef test_a():\n    assert 1\n"
            )
        },
        {
            "tests/test_acceptance_author.py": (
                "import kew.z\n\n\ndef test_a():\n    assert 1\n\n\n"
                "def test_refused():\n    assert 0\n"
            )
        },
        "tests/test_acceptance_author.py",
        "always: changed by the candidate",
    ),
    # #2907's containment test: it runs kew in a subprocess.
    "runs-kew": (
        {
            "src/kew/sandbox.py": "VALUE = 1\n",
            "tests/test_sandbox_containment.py": (
                "import subprocess\nimport sys\n\n\ndef test_contained():\n"
                '    subprocess.run([sys.executable, "-m", "kew", "dispatch"])\n'
            ),
        },
        {"src/kew/sandbox.py": "VALUE = 2\n"},
        "tests/test_sandbox_containment.py",
        "always: runs or loads kew dynamically",
    ),
    # #2838, #3099: the failing test is marked policy.
    "marked-policy": (
        {
            "src/kew/ids.py": "VALUE = 1\n",
            "tests/test_id_sweep_gate.py": (
                "import pytest\n\nimport kew.z\n\n\n@pytest.mark.policy\n"
                "def test_sweep():\n    assert 1\n"
            ),
        },
        {"src/kew/ids.py": "VALUE = 2\n"},
        "tests/test_id_sweep_gate.py",
        "always: marked policy",
    ),
}


@pytest.mark.parametrize("shape", sorted(_CATCHES))
def test_each_real_catch_still_selects_the_test_that_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    before, after, failing, why = _CATCHES[shape]
    root = _project(tmp_path, monkeypatch, {**_FILLER, **before})
    _write(root, dict(after))

    out = _explain()

    if why == "full suite":
        assert "result: full suite" in out
    else:
        assert f"{failing}  {why}\n" in out

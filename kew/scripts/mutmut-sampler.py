#!/usr/bin/env python3
"""
Sample a random subset of [tool.mutmut] paths_to_mutate so that the weekly
mutation-testing job finishes within its 120-minute timeout instead of camping
the shared self-hosted runner.

Uses the ISO week number as the random seed so the same sample is selected
for all re-runs within a week, but changes each Monday.  The sample size is
max(3, ceil(len(paths) * 0.5)) — at least 3 (or all if fewer than 6) to keep
the score meaningful.

mutmut 3 reads paths_to_mutate as a TOML list; a mutmut-2-style colon-joined
string is iterated one character at a time (#2479), so this refuses one.

Also counts surviving mutants from ``mutmut results`` output on stdin, so the
workflow's fail step reads mutmut 3's real ``<mutant>: survived`` lines rather
than grepping a case-sensitive word that mutmut 3 never prints.

With ``--diff-scope BASE --cap SECONDS`` it is merge verify's mutation phase
(#2495): it selects only the mutants on the ``src/`` lines changed since BASE
(:func:`select_mutants`, numbered exactly as mutmut 3 numbers them), runs
mutmut on just those under the time cap, and prints one ``kew-mutation:`` line
per survivor (file, line, mutation) and one summary line naming the cap. A run
the cap stops still reports what it finished. A clean baseline that fails
under mutants/ (#2804) has its failing tests deselected and mutmut re-run, and
a run that still stops names mutmut's own reason. It only reports: it exits 0
whatever mutmut answered.

Usage:  uv run python scripts/mutmut-sampler.py
        uv run mutmut results | uv run python scripts/mutmut-sampler.py \
            --count-survivors
        uv run python scripts/mutmut-sampler.py --diff-scope BASE --cap 600
        (runs from the kew/ working directory)
"""

import math
import os
import random
import re
import signal
import subprocess  # nosec B404 - argv lists only, never a shell
import sys
import time
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

PYPROJECT = Path("pyproject.toml")
KEY = "paths_to_mutate"
MIN_PATHS = 3
SAMPLE_RATIO = 0.5


def _list_re(key: str) -> re.Pattern[str]:
    """Matches  <key> = [ ... ]  (the list may span lines)."""
    return re.compile(rf"^{re.escape(key)}\s*=\s*\[[^\]]*\]", re.MULTILINE)


_LIST_RE = _list_re(KEY)

# One `mutmut results` line:  "    kew.breaker.x_f__mutmut_1: survived"
_RESULT_RE = re.compile(r"^\s*(\S+): (.+?)\s*$")


def count_survivors(results: str) -> int:
    """Count the mutants `mutmut results` reports with status ``survived``."""
    count = 0
    for line in results.splitlines():
        m = _RESULT_RE.match(line)
        if m and m.group(2) == "survived":
            count += 1
    return count


def mutmut_paths(text: str) -> list[str]:
    """Return [tool.mutmut] paths_to_mutate, refusing anything but a list of strings."""
    value = tomllib.loads(text).get("tool", {}).get("mutmut", {}).get(KEY)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise SystemExit(
            f"[tool.mutmut] {KEY} must be a TOML list of strings, got {value!r}"
        )
    return value


def main() -> None:
    text = PYPROJECT.read_text()

    all_paths = mutmut_paths(text)
    if not all_paths or not _LIST_RE.search(text):
        print(f"::warning::{KEY} not found in {PYPROJECT} — skipping sampling")
        return

    total = len(all_paths)

    if total <= MIN_PATHS:
        print(f"Only {total} path(s), no sampling needed")
        return

    # Deterministic seed based on ISO week — changes every Monday
    today = date.today()
    seed = today.isocalendar().week + today.year * 100
    rng = random.Random(seed)

    sample_size = max(MIN_PATHS, math.ceil(total * SAMPLE_RATIO))
    sampled = rng.sample(all_paths, min(sample_size, total))

    new_value = ", ".join(f'"{p}"' for p in sampled)
    new_text = _LIST_RE.sub(lambda _: f"{KEY} = [{new_value}]", text, count=1)
    if mutmut_paths(new_text) != sampled:
        raise SystemExit(f"rewriting {KEY} in {PYPROJECT} did not round-trip")
    PYPROJECT.write_text(new_text)

    skipped = [p for p in all_paths if p not in sampled]
    print(f"Sampled {len(sampled)}/{total} paths (seed={seed}):")
    for p in sampled:
        print(f"  + {p}")
    for p in skipped:
        print(f"  - {p} (skipped this week)")


# --- Diff-scoped mutation report (merge verify, #2495) -----------------------

#: What every report line starts with; merge verify reads the phase off the
#: job log by it (``kew.merge_verify.mutation_report``).
MARK = "kew-mutation:"

#: Seconds mutmut gets to stop on SIGINT, saving what it finished, before the
#: cap kills it.
GRACE_SECONDS = 30

#: mutmut as the phase runs it, from kew/ where verify.sh synced the venv.
MUTMUT = ("uv", "run", "mutmut")

#: Where mutmut's own (very chatty) output goes: beside the checkout, not in it.
MUTMUT_LOG = Path("../.kew-mutation.log")

#: mutmut runs the tests from kew/mutants/, so tests that find the repo root
#: from their own file land on kew/; the weekly job links these there (#2479).
ROOT_LINKS = (".claude", "docker", "docs", "kew.toml")

#: How much of a mutation's code a report line quotes.
_QUOTE = 60

#: How many baseline tests the phase deselects before it gives up (#2804):
#: pytest runs the baseline with ``-x``, so each re-run names one more.
MAX_DESELECTED = 5

#: How much of mutmut's reason a report line carries.
_REASON = 300

# What mutmut 3 prints when the clean baseline run of the selected tests fails.
_STATS_FAILED = "failed to collect stats"

# A pytest short-summary line naming one failing test.
_FAILING_TEST_RE = re.compile(r"^(?:FAILED|ERROR) (tests/\S+::\S+)", re.MULTILINE)

# One `git diff -U0` hunk header; the new side's start line and length.
_HUNK_RE = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")

_TEST_FILE_RE = re.compile(r"tests/(?:.+/)?test_[^/]*\.py")


@dataclass(frozen=True)
class Mutant:
    """One mutmut 3 mutant, located on its source line."""

    #: The name mutmut 3 gives it (``kew.x.x_f__mutmut_3``).
    name: str
    #: The source file, relative to kew/.
    path: str
    #: The line the mutated node starts on.
    line: int
    #: What it changes, as ``'old' -> 'new'``.
    mutation: str


def changed_lines(diff: str) -> dict[str, set[int]]:
    """The new-side lines each file of a ``git diff -U0`` adds or rewrites.

    A hunk that only deletes covers no new line, so it selects nothing: there
    is no code left on it to mutate.
    """
    lines: dict[str, set[int]] = {}
    path: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            path = None if target == "/dev/null" else target.removeprefix("b/")
            continue
        hunk = _HUNK_RE.match(line)
        if hunk and path is not None:
            start = int(hunk.group(1))
            count = 1 if hunk.group(2) is None else int(hunk.group(2))
            lines.setdefault(path, set()).update(range(start, start + count))
    return {path: covered for path, covered in lines.items() if covered}


def _quote(code: str) -> str:
    text = " ".join(code.split())
    return repr(text if len(text) <= _QUOTE else text[: _QUOTE - 3] + "...")


def _mutated_functions(module: object) -> Iterable[tuple[object, str | None]]:
    """Each function mutmut 3 mutates, with its class, in mutmut's own order.

    The walk ``mutmut.file_mutation.combine_mutations_to_source`` makes: the
    module's top-level functions, and the methods of its indented classes.
    """
    import libcst as cst

    if not isinstance(module, cst.Module):
        return
    for statement in module.body:
        if isinstance(statement, cst.FunctionDef):
            yield statement, None
        elif isinstance(statement, cst.ClassDef) and isinstance(
            statement.body, cst.IndentedBlock
        ):
            for method in statement.body.body:
                if isinstance(method, cst.FunctionDef):
                    yield method, statement.name.value


def select_mutants(path: str, source: str, lines: set[int]) -> list[Mutant]:
    """The mutants mutmut 3 makes of *source* that sit on one of *lines*.

    Every mutant of the file is generated and numbered first, exactly as
    ``mutmut run`` numbers them, and only then filtered by line: generating on
    the changed lines alone would renumber them, and a name that points at a
    different mutant than the one reported is worse than no report.
    """
    from libcst.metadata import MetadataWrapper, PositionProvider
    from mutmut.__main__ import get_mutant_name
    from mutmut.file_mutation import create_mutations
    from mutmut.trampoline_templates import mangle_function_name

    module, mutations = create_mutations(source)
    positions = MetadataWrapper(module, unsafe_skip_copy=True).resolve(PositionProvider)
    by_function: dict[int, list] = {}
    for mutation in mutations:
        function = mutation.contained_by_top_level_function
        if function is not None:
            by_function.setdefault(id(function), []).append(mutation)
    selected = []
    for function, class_name in _mutated_functions(module):
        mangled = mangle_function_name(name=function.name.value, class_name=class_name)
        for index, mutation in enumerate(by_function.get(id(function), ()), start=1):
            position = positions.get(mutation.original_node)
            if position is None or position.start.line not in lines:
                continue
            old = module.code_for_node(mutation.original_node)
            new = module.code_for_node(mutation.mutated_node)
            selected.append(
                Mutant(
                    name=get_mutant_name(Path(path), f"{mangled}__mutmut_{index}"),
                    path=path,
                    line=position.start.line,
                    mutation=f"{_quote(old)} -> {_quote(new)}",
                )
            )
    return selected


def scoped_tests(
    changed: Iterable[str], modules: Iterable[str], root: Path
) -> list[str]:
    """The tests the phase runs: the PR's own test files, and each module's suite.

    The question the phase answers is whether the change's tests kill the
    change's mutants, so it runs the test files the change touched, plus
    ``tests/test_<module>.py`` / ``tests/test_<module>_*.py`` for each changed
    module — never the whole tree, which mutmut 3 cannot run from mutants/.
    """
    tests = {
        path
        for path in changed
        if _TEST_FILE_RE.fullmatch(path) and (root / path).is_file()
    }
    for module in modules:
        stem = Path(module).stem
        for pattern in (f"test_{stem}.py", f"test_{stem}_*.py"):
            tests.update(
                path.relative_to(root).as_posix()
                for path in (root / "tests").glob(pattern)
            )
    return sorted(tests)


def _set_list(text: str, key: str, values: Sequence[str]) -> str:
    """*text* with ``[tool.mutmut] <key>`` set to *values*, checked by re-reading.

    A key the table does not have yet is added at its top.
    """
    pattern = _list_re(key)
    joined = ", ".join(f'"{value}"' for value in values)
    if pattern.search(text):
        new_text = pattern.sub(lambda _: f"{key} = [{joined}]", text, count=1)
    else:
        new_text = re.sub(
            r"^\[tool\.mutmut\]\s*$",
            lambda header: f"{header.group(0)}\n{key} = [{joined}]",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    mutmut = tomllib.loads(new_text).get("tool", {}).get("mutmut", {})
    if mutmut.get(key) != list(values):
        raise ValueError(f"rewriting {key} in {PYPROJECT} did not round-trip")
    return new_text


def run_capped(
    argv: Sequence[str],
    cap: float,
    *,
    log: Path,
    cwd: Path = Path("."),
    grace: float = GRACE_SECONDS,
) -> tuple[int | None, bool]:
    """Run *argv* for at most *cap* seconds: its exit code, and whether capped.

    At the cap the whole process group gets SIGINT, which mutmut 3 handles by
    stopping its workers and keeping every result it already saved; one that
    is still running *grace* seconds later is killed.
    """
    with log.open("w") as out:
        process = subprocess.Popen(  # nosec B603 - fixed argv, no shell
            list(argv),
            cwd=cwd,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return process.wait(timeout=cap), False
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        return process.returncode, True


def failing_tests(output: str) -> list[str]:
    """The tests a pytest run in mutmut's *output* reports failing or erroring."""
    return list(dict.fromkeys(_FAILING_TEST_RE.findall(output)))


def mutmut_reason(output: str) -> str:
    """Why mutmut stopped, in one line: the failing baseline tests, its last words."""
    failing = failing_tests(output)
    last = next((line for line in reversed(output.splitlines()) if line.strip()), "")
    reason = " ".join(last.split()) or "mutmut printed nothing"
    if failing:
        reason = f"baseline test failed under mutants/: {' '.join(failing)}; {reason}"
    return reason if len(reason) <= _REASON else reason[: _REASON - 3] + "..."


def _read_log(log: Path) -> str:
    return log.read_text(errors="replace") if log.is_file() else ""


def run_mutmut(
    argv: Sequence[str],
    cap: float,
    *,
    root: Path,
    log: Path,
    grace: float,
) -> tuple[int | None, bool, list[str]]:
    """Run mutmut under *cap*: exit code, whether capped, and the tests deselected.

    A baseline (stats) run that fails on a test — one that runs ``kew`` in a
    child process, say, which inherits ``MUTANT_UNDER_TEST=stats`` but not
    mutmut's config (#2804) — has that test deselected and mutmut re-run in
    what is left of the cap, so one test that cannot run under mutants/ no
    longer leaves every mutant unchecked.
    """
    started = time.monotonic()
    deselected: list[str] = []
    while True:
        remaining = max(cap - (time.monotonic() - started), 1)
        exit_code, capped = run_capped(
            argv, remaining, log=root / log, cwd=root, grace=grace
        )
        output = _read_log(root / log)
        new = [test for test in failing_tests(output) if test not in deselected]
        stopped = exit_code != 0 and not capped and _STATS_FAILED in output
        if not stopped or not new or len(deselected) + len(new) > MAX_DESELECTED:
            return exit_code, capped, deselected
        deselected += new
        pyproject = root / PYPROJECT
        args = [f"--deselect={test}" for test in deselected]
        pyproject.write_text(
            _set_list(pyproject.read_text(), "pytest_add_cli_args", args)
        )


def read_statuses(results: str) -> dict[str, str]:
    """Each mutant's status, from ``mutmut results --all true``."""
    statuses = {}
    for line in results.splitlines():
        match = _RESULT_RE.match(line)
        if match:
            statuses[match.group(1)] = match.group(2)
    return statuses


def report_lines(
    selected: Sequence[Mutant],
    statuses: dict[str, str],
    *,
    exit_code: int | None,
    capped: bool,
    cap: float,
    seconds: float,
) -> list[str]:
    """The phase's report: each survivor located, then one line of totals.

    A mutant the run never reached — the cap stopped it first — counts as
    ``not_checked``; it is neither killed nor survived.
    """
    counts = {"killed": 0, "survived": 0, "not_checked": 0, "other": 0}
    lines = []
    for mutant in selected:
        status = statuses.get(mutant.name, "not checked")
        if status == "survived":
            lines.append(
                f"{MARK} survived {mutant.path}:{mutant.line} {mutant.name} "
                f"{mutant.mutation}"
            )
        key = status.replace(" ", "_")
        counts[key if key in counts else "other"] += 1
    if capped:
        lines.append(
            f"{MARK} capped the run hit the {cap:g}s cap; these results are partial"
        )
    totals = " ".join(f"{key}={value}" for key, value in counts.items())
    code = "none" if exit_code is None else str(exit_code)
    lines.append(
        f"{MARK} done exit={code} capped={'yes' if capped else 'no'} cap={cap:g}s "
        f"seconds={round(seconds)} selected={len(selected)} {totals}"
    )
    return lines


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # nosec B603 B607 - fixed git argv, no shell
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def _link_repo_root(root: Path) -> None:
    for entry in ROOT_LINKS:
        target = root.resolve().parent / entry
        link = root / entry
        if target.exists() and not link.exists() and not link.is_symlink():
            link.symlink_to(Path("..") / entry)


def _select(root: Path, base: str) -> tuple[list[Mutant], list[str], str | None]:
    """The selected mutants and their tests, or why there is nothing to run."""
    diff = _git(
        root,
        "diff",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--relative",
        base,
        "HEAD",
        "--",
        "src/",
    )
    lines = {
        path: covered
        for path, covered in changed_lines(diff).items()
        if path.endswith(".py")
    }
    if not lines:
        return [], [], "no src/ python lines changed"
    selected = [
        mutant
        for path, covered in sorted(lines.items())
        for mutant in select_mutants(path, (root / path).read_text(), covered)
    ]
    if not selected:
        return [], [], "no mutants on the changed lines"
    changed = _git(
        root,
        "diff",
        "--name-only",
        "--relative",
        "--diff-filter=AMR",
        base,
        "HEAD",
        "--",
        "tests/",
    ).split()
    tests = scoped_tests(changed, lines, root)
    if not tests:
        return selected, [], "no tests changed or named for the changed modules"
    return selected, tests, None


def diff_scope(
    base: str,
    cap: float,
    *,
    root: Path = Path("."),
    mutmut: Sequence[str] = MUTMUT,
    grace: float = GRACE_SECONDS,
    log: Path = MUTMUT_LOG,
) -> list[str]:
    """Merge verify's mutation phase, as the report lines it prints."""
    started = time.monotonic()
    selected, tests, skipped = _select(root, base)
    if skipped:
        return [f"{MARK} skipped {skipped}"]
    files = sorted({mutant.path for mutant in selected})
    lines = [
        f"{MARK} selected {len(selected)} mutants in {len(files)} files "
        f"against {len(tests)} test files"
    ]
    pyproject = root / PYPROJECT
    text = _set_list(pyproject.read_text(), KEY, files)
    pyproject.write_text(_set_list(text, "tests_dir", tests))
    _link_repo_root(root)
    names = [mutant.name for mutant in selected]
    exit_code, capped, deselected = run_mutmut(
        [*mutmut, "run", *names], cap, root=root, log=log, grace=grace
    )
    lines += [
        f"{MARK} deselected {test} failed the baseline run under mutants/"
        for test in deselected
    ]
    if exit_code != 0 and not capped:
        lines.append(f"{MARK} reason {mutmut_reason(_read_log(root / log))}")
    results = subprocess.run(  # nosec B603 - fixed argv, no shell
        [*mutmut, "results", "--all", "true"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return lines + report_lines(
        selected,
        read_statuses(results),
        exit_code=exit_code,
        capped=capped,
        cap=cap,
        seconds=time.monotonic() - started,
    )


def _diff_scope_main(argv: list[str]) -> None:
    """``--diff-scope BASE --cap SECONDS``: print the report; never fail the job."""
    try:
        base = argv[argv.index("--diff-scope") + 1]
        cap = float(argv[argv.index("--cap") + 1])
        lines = diff_scope(base, cap)
    except Exception as exc:  # report only: the phase never fails the gate
        lines = [f"{MARK} error {' '.join(str(exc).split())[:200]}"]
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--count-survivors"]:
        print(count_survivors(sys.stdin.read()))
    elif "--diff-scope" in sys.argv[1:]:
        _diff_scope_main(sys.argv[1:])
    else:
        main()

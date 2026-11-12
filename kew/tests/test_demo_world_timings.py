"""Demo world timings: coarse aggregates of report frontmatter, nothing per run (#3141).

The demo site is public, so everything this script writes is real history
leaving the machine. The tests hold it to that: no statistic on fewer than
``MIN_RUNS`` runs, every figure coarsened, and an output that is an allowlist
of keys, types and run kinds.
"""

import copy
import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any

import pytest

KEW = Path(__file__).resolve().parents[1]
SCRIPTS = KEW / "scripts"
COMMITTED = KEW.parent / "frontend" / "demo" / "world" / "timings.json"


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


demo_world_timings = _load("demo_world_timings")
demo_world_fake_corpus = _load("demo_world_fake_corpus")
MIN_RUNS = demo_world_timings.MIN_RUNS


def _report(root: Path, name: str, fields: dict[str, object], body: str = "") -> None:
    lines = ["---", *(f"{k}: {json.dumps(v)}" for k, v in fields.items()), "---", body]
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run(i: int, kind: str = "dispatch", status: str = "success") -> dict[str, object]:
    """One report whose every figure is a non-round, run-unique value."""
    return {
        "run_id": f"secret{kind}{i}",
        "run_kind": kind,
        "model": "claude-opus-5",
        "repo": "someone/private-repo",
        "status": status,
        "cost_usd": round(1.0 + i * 0.137, 6),
        "duration_seconds": 600.37 + i * 61.3,
        "input_tokens": 171 + i * 13,
        "output_tokens": 30_011 + i * 977,
        "cache_read_tokens": 4_100_003 + i * 51_217,
        "total_tokens": 4_130_185 + i * 52_207,
        "review_cycles": i % 3,
        "attempts": 1,
    }


def _corpus(root: Path, counts: dict[str, int], failures: int = 0) -> None:
    for kind, n in counts.items():
        for i in range(n):
            status = "failure" if i < failures else "success"
            _report(
                root,
                f"acme--ledger/run-{kind}-{i}.md",
                _run(i, kind, status),
                body="## Notes\n\nprompt text that must never be read",
            )


def _timings(tmp_path: Path, counts: dict[str, int], failures: int = 0) -> Any:
    corpus = tmp_path / "runs"
    _corpus(corpus, counts, failures)
    out = tmp_path / "timings.json"
    assert demo_world_timings.main(["--corpus", str(corpus), "--out", str(out)]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def test_a_group_of_nineteen_runs_publishes_nothing(tmp_path: Path) -> None:
    timings = _timings(tmp_path, {"dispatch": MIN_RUNS, "ci_fix": MIN_RUNS - 1})

    assert MIN_RUNS == 20
    assert set(timings["by_run_kind"]) == {"dispatch"}
    assert "ci_fix" not in json.dumps(timings)


def test_a_group_of_twenty_runs_publishes_its_statistics(tmp_path: Path) -> None:
    timings = _timings(tmp_path, {"dispatch": MIN_RUNS}, failures=3)

    dispatch = timings["by_run_kind"]["dispatch"]
    assert dispatch["runs"] == MIN_RUNS
    assert dispatch["success_rate"] == 0.85
    for field in demo_world_timings.SPREAD_FIELDS:
        assert set(dispatch[field]) == {"median", "p90"}
        assert dispatch[field]["median"] <= dispatch[field]["p90"]


def test_a_field_fewer_than_twenty_runs_report_publishes_no_spread(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "runs"
    for i in range(MIN_RUNS):
        fields = _run(i)
        if i > 0:
            del fields["cache_read_tokens"]
        _report(corpus, f"run-{i}.md", fields)
    out = tmp_path / "timings.json"
    demo_world_timings.main(["--corpus", str(corpus), "--out", str(out)])

    dispatch = json.loads(out.read_text(encoding="utf-8"))["by_run_kind"]["dispatch"]
    assert dispatch["cache_read_tokens"] is None
    assert dispatch["input_tokens"] is not None


def test_no_group_is_pooled_with_another_to_reach_the_floor(tmp_path: Path) -> None:
    timings = _timings(tmp_path, {"ci_fix": 12, "resolve": 12, "dispatch": MIN_RUNS})

    assert set(timings["by_run_kind"]) == {"dispatch"}
    assert timings["by_run_kind"]["dispatch"]["runs"] == MIN_RUNS
    assert set(timings) == {"source", "min_runs", "by_run_kind"}


def test_the_floor_counts_distinct_runs_not_reports(tmp_path: Path) -> None:
    corpus = tmp_path / "runs"
    _corpus(corpus, {"dispatch": MIN_RUNS - 1})
    # The same run reported again (another repository slug), and a report
    # naming no run at all: neither is a twentieth run.
    _report(corpus, "elsewhere--repo/run-dup.md", _run(0))
    _report(corpus, "elsewhere--repo/run-anon.md", {**_run(99), "run_id": ""})
    out = tmp_path / "timings.json"

    assert demo_world_timings.main(["--corpus", str(corpus), "--out", str(out)]) == 1
    assert not out.exists()

    _report(corpus, "acme--ledger/run-twentieth.md", _run(19))
    assert demo_world_timings.main(["--corpus", str(corpus), "--out", str(out)]) == 0
    dispatch = json.loads(out.read_text(encoding="utf-8"))["by_run_kind"]["dispatch"]
    assert dispatch["runs"] == MIN_RUNS
    assert "secret" not in out.read_text(encoding="utf-8")


def test_an_odd_count_of_small_token_figures_publishes_no_runs_own_value(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "runs"
    n = 21
    small = [41 + 2 * i for i in range(n)]  # 41..81: no run's value is a round ten
    for i, value in enumerate(small):
        _report(corpus, f"run-{i}.md", {**_run(i), "input_tokens": value})
    out = tmp_path / "timings.json"
    demo_world_timings.main(["--corpus", str(corpus), "--out", str(out)])

    spread = json.loads(out.read_text(encoding="utf-8"))["by_run_kind"]["dispatch"][
        "input_tokens"
    ]
    # With n odd the median is one run's value, and at n=21 so is the p90:
    # only the rounding keeps either from being published.
    assert spread == {"median": 60, "p90": 80}
    for value in spread.values():
        assert value not in small


def test_the_success_rate_is_published_in_steps_of_a_twentieth(
    tmp_path: Path,
) -> None:
    timings = _timings(tmp_path, {"dispatch": 21}, failures=3)  # 18/21 = 0.857

    assert timings["by_run_kind"]["dispatch"]["success_rate"] == 0.85


def test_declares_the_corpus_it_was_built_from(tmp_path: Path) -> None:
    corpus = tmp_path / "runs"
    _corpus(corpus, {"dispatch": MIN_RUNS})
    real, fake = tmp_path / "real.json", tmp_path / "fake.json"
    demo_world_timings.main(["--corpus", str(corpus), "--out", str(real)])
    demo_world_timings.main(
        ["--corpus", str(corpus), "--out", str(fake), "--source", "fake"]
    )

    assert json.loads(real.read_text(encoding="utf-8"))["source"] == "corpus"
    assert json.loads(fake.read_text(encoding="utf-8"))["source"] == "fake"


def _sig2(value: float) -> bool:
    """Whether *value* has at most two significant figures, and no finer than tens."""
    if value == 0:
        return True
    if abs(value) < 1000:
        return value % 10 == 0
    digits = 10 ** (math.floor(math.log10(abs(value))) - 1)
    return math.isclose(value / digits, round(value / digits))


def test_every_figure_is_coarsened_and_no_single_run_value_survives(
    tmp_path: Path,
) -> None:
    timings = _timings(tmp_path, {"dispatch": 25, "adversarial_review": 31})
    exact = {
        float(v)
        for i in range(31)
        for v in _run(i).values()
        if isinstance(v, int | float) and not isinstance(v, bool)
    } | {float(_run(i)["duration_seconds"]) / 60 for i in range(31)}  # type: ignore[arg-type]

    for stats in timings["by_run_kind"].values():
        for field in demo_world_timings.SPREAD_FIELDS:
            for value in stats[field].values():
                assert float(value) not in exact, (field, value)
                if field == "cost_usd":
                    assert math.isclose(value * 10, round(value * 10)), value
                elif field == "duration_minutes":
                    assert isinstance(value, int), value
                else:
                    assert isinstance(value, int) and _sig2(value), (field, value)


def test_publishes_input_output_and_cache_reads_as_three_aggregates(
    tmp_path: Path,
) -> None:
    dispatch = _timings(tmp_path, {"dispatch": MIN_RUNS})["by_run_kind"]["dispatch"]

    # Fresh input is hundreds, output tens of thousands, cache reads millions:
    # three figures, so no total that is mostly cache reads reads as fresh input.
    assert 100 <= dispatch["input_tokens"]["median"] < 1_000
    assert 10_000 <= dispatch["output_tokens"]["median"] < 100_000
    assert dispatch["cache_read_tokens"]["median"] >= 1_000_000
    assert "total_tokens" not in dispatch
    assert "review_rounds" not in json.dumps(_timings(tmp_path / "b", {"dispatch": 20}))


def test_output_holds_no_per_run_value_or_free_text(tmp_path: Path) -> None:
    corpus = tmp_path / "runs"
    _corpus(corpus, {"dispatch": MIN_RUNS})
    out = tmp_path / "timings.json"
    demo_world_timings.main(["--corpus", str(corpus), "--out", str(out)])

    text = out.read_text(encoding="utf-8")
    for leaked in (
        "secret",
        "claude-opus-5",
        "someone",
        "private-repo",
        "acme",
        "prompt",
    ):
        assert leaked not in text


def test_a_rerun_is_byte_identical(tmp_path: Path) -> None:
    corpus = tmp_path / "runs"
    _corpus(corpus, {"dispatch": MIN_RUNS})
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    demo_world_timings.main(["--corpus", str(corpus), "--out", str(first)])
    demo_world_timings.main(["--corpus", str(corpus), "--out", str(second)])

    assert first.read_bytes() == second.read_bytes()


def test_skips_files_without_frontmatter_or_run_kind(tmp_path: Path) -> None:
    corpus = tmp_path / "runs"
    _corpus(corpus, {"dispatch": MIN_RUNS})
    (corpus / "README.md").write_text("# not a report\n", encoding="utf-8")
    (corpus / "broken.md").write_text(
        "---\nrun_kind: [unclosed\n---\n", encoding="utf-8"
    )
    _report(corpus, "nokind.md", {"cost_usd": 99.0, "status": "success"})
    out = tmp_path / "timings.json"
    demo_world_timings.main(["--corpus", str(corpus), "--out", str(out)])

    timings = json.loads(out.read_text(encoding="utf-8"))
    assert timings["by_run_kind"]["dispatch"]["runs"] == MIN_RUNS


def test_a_run_kind_kew_does_not_know_is_never_published(tmp_path: Path) -> None:
    timings = _timings(tmp_path, {"dispatch": MIN_RUNS, "someones-secret-kind": 40})

    assert set(timings["by_run_kind"]) == {"dispatch"}


def test_an_empty_corpus_writes_nothing_and_fails(tmp_path: Path) -> None:
    out = tmp_path / "timings.json"

    assert demo_world_timings.main(["--corpus", str(tmp_path), "--out", str(out)]) == 1
    assert not out.exists()


def test_a_corpus_with_no_group_over_the_floor_writes_nothing_and_fails(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "runs"
    _corpus(corpus, {"dispatch": MIN_RUNS - 1})
    out = tmp_path / "timings.json"

    assert demo_world_timings.main(["--corpus", str(corpus), "--out", str(out)]) == 1
    assert not out.exists()


def test_reads_the_configured_sink_path_when_no_corpus_is_named(tmp_path: Path) -> None:
    corpus = tmp_path / "elsewhere"
    _corpus(corpus, {"review_remediation": MIN_RUNS})
    config = tmp_path / "kew.toml"
    config.write_text(f'[run_reports]\nsink_path = "{corpus}"\n', encoding="utf-8")
    out = tmp_path / "timings.json"

    assert demo_world_timings.main(["--config", str(config), "--out", str(out)]) == 0
    timings = json.loads(out.read_text(encoding="utf-8"))
    assert set(timings["by_run_kind"]) == {"review_remediation"}


# --- the allowlist ---------------------------------------------------------


def _valid(tmp_path: Path) -> Any:
    return _timings(tmp_path, {"dispatch": MIN_RUNS, "adversarial_review": MIN_RUNS})


def _break(timings: Any, how: str) -> Any:
    broken = copy.deepcopy(timings)
    dispatch = broken["by_run_kind"]["dispatch"]
    if how == "extra top-level key":
        broken["runs"] = 40
    elif how == "extra kind key":
        dispatch["model"] = 1
    elif how == "extra spread key":
        dispatch["cost_usd"]["max"] = 9.9
    elif how == "a string value":
        dispatch["cost_usd"]["median"] = "3.3"
    elif how == "a bool value":
        dispatch["runs"] = True
    elif how == "a float token count":
        dispatch["input_tokens"]["median"] = 170.5
    elif how == "a list value":
        dispatch["duration_minutes"] = [1, 2]
    elif how == "an unknown run kind":
        broken["by_run_kind"]["someone/private-repo"] = dispatch
    elif how == "a group under the floor":
        dispatch["runs"] = MIN_RUNS - 1
    elif how == "an uncoarsened cost":
        dispatch["cost_usd"]["median"] = 3.317
    elif how == "a missing key":
        del dispatch["success_rate"]
    elif how == "a rate past one":
        dispatch["success_rate"] = 1.5
    elif how == "a rate finer than a twentieth":
        dispatch["success_rate"] = 0.87
    elif how == "a token count finer than tens":
        dispatch["input_tokens"]["median"] = 41
    elif how == "no source":
        del broken["source"]
    elif how == "an unknown source":
        broken["source"] = "laptop"
    return broken


BREAKS = (
    "extra top-level key",
    "extra kind key",
    "extra spread key",
    "a string value",
    "a bool value",
    "a float token count",
    "a list value",
    "an unknown run kind",
    "a group under the floor",
    "an uncoarsened cost",
    "a missing key",
    "a rate past one",
    "a rate finer than a twentieth",
    "a token count finer than tens",
    "no source",
    "an unknown source",
)


def test_the_written_file_passes_the_allowlist(tmp_path: Path) -> None:
    assert demo_world_timings.problems(_valid(tmp_path)) == []


@pytest.mark.parametrize("how", BREAKS)
def test_the_allowlist_refuses(tmp_path: Path, how: str) -> None:
    assert demo_world_timings.problems(_break(_valid(tmp_path), how)) != []


def test_the_committed_timings_pass_the_allowlist() -> None:
    timings = json.loads(COMMITTED.read_text(encoding="utf-8"))

    assert demo_world_timings.problems(timings) == []


def _fake_timings(tmp_path: Path) -> Path:
    corpus = tmp_path / "fake"
    demo_world_fake_corpus.main([str(corpus)])
    out = tmp_path / "timings.json"
    assert (
        demo_world_timings.main(
            ["--corpus", str(corpus), "--out", str(out), "--source", "fake"]
        )
        == 0
    )
    return out


def test_the_fake_corpus_aggregates_clean_and_shows_the_floor(tmp_path: Path) -> None:
    first = _fake_timings(tmp_path / "a").read_bytes()
    second = _fake_timings(tmp_path / "b").read_bytes()
    timings = json.loads(first)

    assert first == second
    assert demo_world_timings.problems(timings) == []
    assert timings["source"] == "fake"
    # ci_fix has fewer than MIN_RUNS fake runs: the floor drops it.
    assert "ci_fix" not in timings["by_run_kind"]
    assert {"dispatch", "adversarial_review", "review_remediation"} <= set(
        timings["by_run_kind"]
    )


def test_the_committed_timings_match_the_source_they_declare(tmp_path: Path) -> None:
    """A ``fake`` file is the fake corpus's aggregate, byte for byte. A
    ``corpus`` file — the operator's release regeneration, which edits no
    test — is held by the allowlist, floor and rounding checks above, and
    must not be the fake aggregate relabelled."""
    committed = COMMITTED.read_text(encoding="utf-8")
    fake = _fake_timings(tmp_path).read_text(encoding="utf-8")
    source = json.loads(committed)["source"]

    assert source in demo_world_timings.SOURCES
    if source == "fake":
        assert committed == fake
    else:
        relabelled = json.loads(fake) | {"source": "corpus"}
        assert json.loads(committed) != relabelled

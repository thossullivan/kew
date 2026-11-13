"""Aggregate run timings for the synthetic demo world (#3141).

The demo console shows a synthetic factory, but its durations, costs and
token counts are shaped by how real runs behaved. This script reads the
run-report corpus and writes coarse AGGREGATE statistics only to
``frontend/demo/world/timings.json``. The demo site is public, so every figure
written here is real history leaving the machine, and the output is held to
three rules:

- **A floor.** A run kind with fewer than ``MIN_RUNS`` distinct runs (by
  ``run_id``, read for that count and nothing else; a report without one is
  not counted) publishes no statistic at all — it is dropped, never pooled
  with another kind — and a field fewer than ``MIN_RUNS`` of a kind's runs
  carry publishes none.
- **Coarse figures.** Rounding is the protection: with an odd count the
  median IS one run's value, and so is the p90 when it lands on a rank, so
  every figure is coarsened before it is written — costs to $0.10, durations
  to whole minutes, token counts to two significant figures but never finer
  than tens, and success rates to steps of 0.05. A coarse figure can still
  equal some run's raw value when that value already sits on the grid; that
  is accepted (2026-10-24): with the floor, the figure says only which grid
  cell holds the middle value, never which run, and refusing such a figure
  would itself reveal that a run had it.
- **An allowlist.** ``SCHEMA`` names every key, type and run kind the file may
  hold; ``problems`` refuses anything else, and nothing is written unless it
  returns none. No run id, item, repository, model, path or free text is read
  past the frontmatter fields in ``FIELDS``.

Fresh input, output and cache reads are three aggregates: a real total is
mostly cache reads, and one total drawn as fresh tokens would misstate it.

The corpus is ``[run_reports] sink_path`` from the kew.toml in force (``--config``
names one; otherwise kew's own discovery), whose default is
``~/.local/share/kew/runs``; ``--corpus`` overrides it.
``frontend/demo/world/generate.mjs`` builds the world from the output with a
fixed seed. Regenerate both on the operator host, from ``kew/``::

    uv run python scripts/demo_world_timings.py
    node ../frontend/demo/world/generate.mjs

The file declares its ``source``: ``corpus`` (the default) or ``fake``, for
the committed file built from the fake corpus
(``scripts/demo_world_fake_corpus.py``; see that script) with
``--source fake``. Tests pin a ``fake`` file to the fake corpus's aggregate;
a ``corpus`` file is held to the allowlist, the floor and the rounding.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from kew.config import load_config
from kew.run_store import KNOWN_RUN_KINDS

#: The frontmatter fields read; nothing else in a report is opened.
FIELDS = (
    "run_id",
    "run_kind",
    "status",
    "cost_usd",
    "duration_seconds",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
)

#: No statistic is computed on fewer distinct runs than this.
MIN_RUNS = 20

#: What a timings file may say it was built from.
SOURCES = ("corpus", "fake")

DEFAULT_OUT = (
    Path(__file__).resolve().parents[2] / "frontend" / "demo" / "world" / "timings.json"
)


def _cost(value: float) -> float:
    return round(value, 1)


def _minutes(value: float) -> int:
    return int(round(value))


def _tokens(value: float) -> int:
    """*value* to two significant figures, but never finer than tens.

    Two significant figures alone would publish 41 as 41: a small count is
    then one run's exact value whenever the median lands on it.
    """
    if abs(value) < 1000:
        return int(round(value, -1))
    return int(round(value, 1 - math.floor(math.log10(abs(value)))))


def _rate(value: float) -> float:
    """A share in steps of 0.05."""
    return round(round(value * 20) / 20, 2)


#: Each published spread: the frontmatter field it reads, the scale it is read
#: in, and how it is coarsened.
SPREAD_FIELDS: dict[str, tuple[str, float, Callable[[float], float]]] = {
    "cost_usd": ("cost_usd", 1.0, _cost),
    "duration_minutes": ("duration_seconds", 1 / 60, _minutes),
    "input_tokens": ("input_tokens", 1.0, _tokens),
    "output_tokens": ("output_tokens", 1.0, _tokens),
    "cache_read_tokens": ("cache_read_tokens", 1.0, _tokens),
}

#: The whole output: every key, and the type each value must have.
SCHEMA: dict[str, Any] = {
    "source": SOURCES,
    "min_runs": int,
    "by_run_kind": {
        "<run kind>": {
            "runs": int,
            "success_rate": float,
            **{
                field: {"median": "<coarse>", "p90": "<coarse>"}
                for field in SPREAD_FIELDS
            },
        }
    },
}


def _fenced(text: str) -> str | None:
    """The text between a report's two ``---`` fences, or None without them."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "\n".join(lines[1:index])
    return None


def read_frontmatter(path: Path) -> dict[str, Any] | None:
    """The FIELDS of one report's frontmatter, or None when it has none."""
    try:
        fenced = _fenced(path.read_text(encoding="utf-8"))
        data = None if fenced is None else yaml.safe_load(fenced)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    return {key: data[key] for key in FIELDS if key in data}


def read_corpus(root: Path) -> list[dict[str, Any]]:
    """Every report under *root* that names its run kind, in path order."""
    reports = (read_frontmatter(path) for path in sorted(root.rglob("*.md")))
    return [r for r in reports if r is not None and isinstance(r.get("run_kind"), str)]


def _numbers(reports: list[dict[str, Any]], key: str) -> list[float]:
    values = (r.get(key) for r in reports)
    return [
        float(v)
        for v in values
        if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)
    ]


def _spread(
    values: list[float], coarse: Callable[[float], float]
) -> dict[str, float] | None:
    """Median and interpolated p90, coarsened; None under the floor."""
    if len(values) < MIN_RUNS:
        return None
    p90 = statistics.quantiles(values, n=10, method="inclusive")[8]
    return {"median": coarse(statistics.median(values)), "p90": coarse(p90)}


def _kind_stats(reports: list[dict[str, Any]]) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "runs": len(reports),
        "success_rate": _rate(
            [r.get("status") for r in reports].count("success") / len(reports)
        ),
    }
    for field, (source, scale, coarse) in SPREAD_FIELDS.items():
        values = [v * scale for v in _numbers(reports, source)]
        stats[field] = _spread(values, coarse)
    return stats


def distinct_runs(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One report per ``run_id``, the first in path order, without the id.

    A run reported twice (a report rewritten under another repository slug)
    counts once; a report naming no run id cannot be counted and is dropped.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for report in reports:
        run_id = report.get("run_id")
        if not isinstance(run_id, str) or run_id == "" or run_id in seen:
            continue
        seen.add(run_id)
        out.append({k: v for k, v in report.items() if k != "run_id"})
    return out


def aggregate(reports: list[dict[str, Any]], source: str = "corpus") -> dict[str, Any]:
    """The statistics the demo world is built from, and nothing per run.

    Only kew's own run kinds with at least ``MIN_RUNS`` distinct runs appear.
    """
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for report in distinct_runs(reports):
        if report["run_kind"] in KNOWN_RUN_KINDS:
            by_kind.setdefault(report["run_kind"], []).append(report)
    return {
        "source": source,
        "min_runs": MIN_RUNS,
        "by_run_kind": {
            kind: _kind_stats(group)
            for kind, group in sorted(by_kind.items())
            if len(group) >= MIN_RUNS
        },
    }


def _keys(where: str, value: Any, expected: set[str]) -> list[str]:
    if not isinstance(value, dict):
        return [f"{where} is not an object"]
    found = set(value)
    return [
        f"{where} has an unexpected key {k!r}" for k in sorted(found - expected)
    ] + [f"{where} lacks {k!r}" for k in sorted(expected - found)]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _coarse_problem(field: str, value: Any) -> str | None:
    """Why *value* is not a coarsened figure of *field*, or None."""
    if not _is_int(value) and not (field == "cost_usd" and isinstance(value, float)):
        return "is not a number of the right type"
    if value < 0 or not math.isfinite(value):
        return "is out of range"
    coarse = SPREAD_FIELDS[field][2]
    return None if coarse(value) == value else "is not coarsened"


def _kind_problems(kind: str, stats: Any) -> list[str]:
    where = f"by_run_kind.{kind}"
    expected = set(SCHEMA["by_run_kind"]["<run kind>"])
    out = _keys(where, stats, expected)
    if not isinstance(stats, dict):
        return out
    runs, rate = stats.get("runs"), stats.get("success_rate")
    if not _is_int(runs) or runs < MIN_RUNS:
        out.append(f"{where}.runs is not a count of at least {MIN_RUNS}")
    if (
        not isinstance(rate, int | float)
        or isinstance(rate, bool)
        or not 0 <= rate <= 1
    ):
        out.append(f"{where}.success_rate is not a rate")
    elif _rate(rate) != rate:
        out.append(f"{where}.success_rate is not coarsened to steps of 0.05")
    for field in SPREAD_FIELDS:
        spread = stats.get(field)
        if spread is None:
            continue
        out += _keys(f"{where}.{field}", spread, {"median", "p90"})
        if not isinstance(spread, dict):
            continue
        for name, value in spread.items():
            why = _coarse_problem(field, value)
            if why is not None:
                out.append(f"{where}.{field}.{name} {why}")
    return out


def problems(timings: Any) -> list[str]:
    """Everything in *timings* that ``SCHEMA`` does not allow; empty when clean."""
    out = _keys("timings", timings, set(SCHEMA))
    if not isinstance(timings, dict):
        return out
    if timings.get("source") not in SOURCES:
        out.append(f"timings.source is not one of {SOURCES}")
    if timings.get("min_runs") != MIN_RUNS or not _is_int(timings.get("min_runs")):
        out.append(f"timings.min_runs is not {MIN_RUNS}")
    by_kind = timings.get("by_run_kind")
    if not isinstance(by_kind, dict):
        return [*out, "timings.by_run_kind is not an object"]
    for kind, stats in by_kind.items():
        if kind not in KNOWN_RUN_KINDS:
            out.append(f"by_run_kind names {kind!r}, which is not a kew run kind")
            continue
        out += _kind_problems(kind, stats)
    return out


def configured_corpus(config: Path | None) -> Path:
    """``[run_reports] sink_path`` from the kew.toml in force."""
    return Path(load_config(config).run_reports.sink_path).expanduser()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--corpus", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--source", choices=SOURCES, default="corpus")
    args = parser.parse_args(argv)
    corpus = args.corpus if args.corpus is not None else configured_corpus(args.config)
    timings = aggregate(read_corpus(corpus), args.source)
    if not timings["by_run_kind"]:
        print(
            f"no run kind under {corpus} has {MIN_RUNS} runs; nothing written",
            file=sys.stderr,
        )
        return 1
    refused = problems(timings)
    if refused:
        print("\n".join(refused), file=sys.stderr)
        return 1
    args.out.write_text(
        json.dumps(timings, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    kinds = len(timings["by_run_kind"])
    print(f"{kinds} run kind(s) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

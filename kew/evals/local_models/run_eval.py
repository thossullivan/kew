#!/usr/bin/env python3
"""Local-model eval harness for the kew Lemonade runner.

Answers the only question that matters for local post-training/serving in a
coding-agent context: *which locally-served models can actually close a real
task?* For each (model, task) it runs the ``kew-lemonade`` loop against an
OpenAI-compatible endpoint (AMD Lemonade, llama-server, LM Studio, vLLM,
Ollama ``/v1``), then runs a deterministic check and records success, wall
time, tool-steps, and token usage.

Cost is deliberately *not* reported in dollars: a local model has no per-token
API price. The honest axis is watts and wall-time on hardware you own - so the
harness reports latency and step-count, and you multiply by your own measured
node power to get tokens-per-joule if you want it.

Usage
-----
    python run_eval.py \
        --base-url http://localhost:13305/v1 \
        --models-file models.json \
        --tasks-file tasks.json \
        --repeats 1 \
        --out-dir results

Deps: standard library only. Requires the ``kew-lemonade`` console script on
PATH (``uv pip install -e .`` in the kew package) and ``python3`` for checks.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess  # nosec B404 - runs the local agent CLI and deterministic checks
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TaskResult:
    model: str
    task_id: str
    repeat: int
    passed: bool
    wall_seconds: float
    steps: int
    input_tokens: int
    output_tokens: int
    stop_reason: str
    error: str = ""


@dataclass
class ModelSummary:
    model: str
    runs: int = 0
    passes: int = 0
    wall_seconds: list[float] = field(default_factory=list)
    steps: list[int] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def pass_rate(self) -> float:
        return self.passes / self.runs if self.runs else 0.0

    def median_wall(self) -> float:
        return statistics.median(self.wall_seconds) if self.wall_seconds else 0.0

    def median_steps(self) -> float:
        return statistics.median(self.steps) if self.steps else 0.0


def _parse_stream(stdout: str) -> tuple[int, int, int, str]:
    """Return (steps, input_tokens, output_tokens, final_stop_reason).

    Mirrors the kew-lemonade event schema: ``tool_execution_start`` marks a
    step; ``message_end`` carries usage and the terminal ``stopReason``.
    """
    steps = 0
    in_tok = 0
    out_tok = 0
    stop_reason = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "tool_execution_start":
            steps += 1
        elif etype == "message_end":
            msg = event.get("message", {})
            if isinstance(msg, dict):
                usage = msg.get("usage", {})
                if isinstance(usage, dict):
                    in_tok += int(usage.get("input") or 0)
                    out_tok += int(usage.get("output") or 0)
                sr = msg.get("stopReason")
                if isinstance(sr, str) and sr:
                    stop_reason = sr
    return steps, in_tok, out_tok, stop_reason


def _write_setup(workdir: Path, setup: dict[str, str]) -> None:
    """Materialize the task's starting files into a fresh working directory.

    Setup paths come from (trusted) tasks.json, but we still reject absolute
    paths and ``..`` traversal so a task can only write inside its worktree.
    """
    root = workdir.resolve()
    for rel, content in setup.items():
        path = (workdir / rel).resolve()
        if path != root and root not in path.parents:
            raise ValueError(f"setup path escapes the work dir: {rel!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _run_check(workdir: Path, check: str, timeout: float = 60.0) -> tuple[bool, str]:
    """Run the task's deterministic check; exit 0 == pass."""
    try:
        proc = subprocess.run(  # nosec B602 - check command is trusted task config
            check,
            shell=True,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "check timed out"
    except OSError as exc:
        return False, f"check error: {exc}"
    ok = proc.returncode == 0
    detail = "" if ok else (proc.stdout + proc.stderr)[-500:]
    return ok, detail


def run_one(
    *,
    model: str,
    task: dict[str, Any],
    repeat: int,
    base_url: str,
    max_steps: int,
    agent_timeout: float,
) -> TaskResult:
    task_id = str(task["id"])
    with tempfile.TemporaryDirectory(prefix=f"kew-eval-{task_id}-") as tmp:
        workdir = Path(tmp)
        _write_setup(workdir, task.get("setup", {}))

        cmd = [
            "kew-lemonade",
            "--base-url",
            base_url,
            "--model",
            model,
            "--max-steps",
            str(max_steps),
            "--cwd",
            str(workdir),
            "-p",
            str(task["prompt"]),
        ]
        start = time.monotonic()
        try:
            proc = subprocess.run(  # nosec B603 - fixed CLI, config args
                cmd,
                capture_output=True,
                text=True,
                timeout=agent_timeout,
            )
            stdout = proc.stdout
            agent_error = "" if proc.returncode == 0 else f"exit {proc.returncode}"
        except FileNotFoundError:
            return TaskResult(
                model=model,
                task_id=task_id,
                repeat=repeat,
                passed=False,
                wall_seconds=0.0,
                steps=0,
                input_tokens=0,
                output_tokens=0,
                stop_reason="",
                error="kew-lemonade not found on PATH (uv pip install -e .)",
            )
        except subprocess.TimeoutExpired:
            stdout = ""
            agent_error = "agent timed out"
        wall = time.monotonic() - start

        steps, in_tok, out_tok, stop_reason = _parse_stream(stdout)
        passed, check_detail = _run_check(workdir, str(task["check"]))
        error = agent_error or ("" if passed else f"check failed: {check_detail}")

        return TaskResult(
            model=model,
            task_id=task_id,
            repeat=repeat,
            passed=passed,
            wall_seconds=round(wall, 2),
            steps=steps,
            input_tokens=in_tok,
            output_tokens=out_tok,
            stop_reason=stop_reason,
            error=error,
        )


def summarize(results: list[TaskResult]) -> list[ModelSummary]:
    by_model: dict[str, ModelSummary] = {}
    for r in results:
        s = by_model.setdefault(r.model, ModelSummary(model=r.model))
        s.runs += 1
        s.passes += int(r.passed)
        s.wall_seconds.append(r.wall_seconds)
        s.steps.append(r.steps)
        s.input_tokens += r.input_tokens
        s.output_tokens += r.output_tokens
    return list(by_model.values())


def render_markdown(summaries: list[ModelSummary]) -> str:
    lines = [
        "# Local-model coding eval (kew-lemonade)",
        "",
        "Cost is ~$0 in API dollars (local inference). The real axis is "
        "wall-time and watts on owned hardware.",
        "",
        "| Model | Runs | Pass rate | Median steps | Median wall (s) | "
        "In tok | Out tok |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in sorted(summaries, key=lambda m: m.pass_rate, reverse=True):
        lines.append(
            f"| {s.model} | {s.runs} | {s.pass_rate:.0%} | "
            f"{s.median_steps():.0f} | {s.median_wall():.1f} | "
            f"{s.input_tokens} | {s.output_tokens} |"
        )
    return "\n".join(lines) + "\n"


def load_models(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):  # allow {"models": [...]}
        data = data.get("models", [])
    return [str(m) for m in data]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="kew-lemonade local-model eval")
    parser.add_argument("--base-url", default="http://localhost:13305/v1")
    parser.add_argument("--models-file", default="models.json")
    parser.add_argument("--tasks-file", default="tasks.json")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=40)
    parser.add_argument("--agent-timeout", type=float, default=600.0)
    parser.add_argument("--out-dir", default="results")
    args = parser.parse_args(argv)

    if shutil.which("kew-lemonade") is None:
        print("[error] kew-lemonade not on PATH - run `uv pip install -e .` first")
        return 1

    models = load_models(Path(args.models_file))
    tasks = json.loads(Path(args.tasks_file).read_text(encoding="utf-8"))
    if not models:
        print("[error] no models configured in", args.models_file)
        return 1

    results: list[TaskResult] = []
    for model in models:
        for task in tasks:
            for rep in range(args.repeats):
                print(f"[run] {model} :: {task['id']} (repeat {rep + 1})")
                res = run_one(
                    model=model,
                    task=task,
                    repeat=rep + 1,  # 1-based, matching the console line above
                    base_url=args.base_url,
                    max_steps=args.max_steps,
                    agent_timeout=args.agent_timeout,
                )
                status = "PASS" if res.passed else "FAIL"
                print(f"       -> {status} ({res.wall_seconds}s, {res.steps} steps)")
                results.append(res)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8"
    )
    summaries = summarize(results)
    md = render_markdown(summaries)
    (out_dir / "results.md").write_text(md, encoding="utf-8")
    print("\n" + md)
    print(f"[done] wrote {out_dir}/results.json and results.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

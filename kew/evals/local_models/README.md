# Local-model coding eval (kew-lemonade)

The question this answers: **which locally-served models can actually close a real coding task through kew's agent loop?** Not "does it emit plausible code" - does it edit the file, use the tools, and make the check pass.

## What it measures

For each `(model, task)` it runs the `kew-lemonade` loop against your OpenAI-compatible endpoint, then runs a deterministic check:

- **Pass rate** - did the check command exit 0? (the only success signal that matters)
- **Median steps** - tool calls per run; a proxy for tool-loop competence
- **Median wall (s)** - latency on your node
- **Tokens** - input/output totals

Cost is intentionally **not** in dollars. A local model has no per-token API price; the honest axis is wall-time and watts on hardware you own. Multiply median wall by your measured node power for tokens-per-joule.

## Run it

```bash
# 1. Serve models locally (AMD Lemonade on :13305, or llama-server / LM Studio / vLLM / Ollama)
# 2. Install the kew-lemonade console script
cd ../../ && uv pip install -e . && cd evals/local_models
# 3. Point models.json at what your endpoint actually serves (curl /v1/models to list)
# 4. Go
python3 run_eval.py --base-url http://localhost:13305/v1 --repeats 3
```

Outputs `results/results.md` (the table) and `results/results.json` (raw rows).

## Extending

Tasks live in `tasks.json` - each is `{id, prompt, setup (files to seed), check (shell cmd, exit 0 = pass)}`. Add tasks that mirror the *shape* of your real client work (extraction, refactors, bugfixes with a gradeable oracle). The three shipped tasks are deliberately tiny and language-runtime-only (no pytest dep) so the harness runs anywhere.

> **Trusted input only.** `tasks.json` and `models.json` are **locally-authored, trusted** configuration. Each task's `check` runs as a shell command (`_run_check`, `shell=True`) and its `setup` seeds files into a temp worktree — so **never** source these files from untrusted contributions or the internet. Treat them like scripts you run, because they are.

## Honest caveats

- **Small, synthetic tasks.** These three are a smoke test, not a benchmark. The signal is directional; a real eval needs tasks from your domain with real oracles.
- **The check is the oracle.** A model can "pass" by luck or "fail" on a trivial format nit. Read the `results.json` transcripts, don't just trust the table.
- **`temperature=0`, single sample per repeat.** Run `--repeats N` to see variance; local models can be less stable than frontier at low sampling.
- **This tests the loop I wrote**, not a battle-tested harness. A weak result may be the model, the task phrasing, or my tool prompts - separate them before drawing conclusions.
- The point isn't a leaderboard. It's the honest question every "run it local" pitch skips: *good enough to actually do the work, or not?*

# Choosing a Local Model

Running local models with kew's Lemonade runner offers several advantages:
- **$0 per run** — no API costs
- **Nothing leaves the machine** — complete data privacy
- **Wall-clock and watts, not dollars** — the honest cost axis

## Sizing to Your RAM

- ~7–9B models fit comfortably on 16GB RAM
- 30B **MoE** (e.g. Qwen3-Coder-30B-A3B, ~3B active) fits ~32GB and is faster than a dense 30B
- Dense 30B+ models require more RAM

## Pick a Coding-Tuned Model

Tool-calling reliability matters most. Coder-tuned models (Qwen2.5-Coder, Qwen3-Coder) emit well-formed tool calls far more reliably than general models.

## Set an Adequate Context Window

The default context window of 4096 tokens is often too small for kew's prompts and file analysis. See [Local Runner Troubleshooting](runners/troubleshooting.md#context-window-too-small) for how to increase it.

## The Honest Cost Axis

Local inference costs are measured in wall-clock time and watts, not dollars. This makes it easier to reason about performance trade-offs.
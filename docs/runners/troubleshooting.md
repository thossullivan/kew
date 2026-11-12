# Local Runner Troubleshooting

This page covers common issues when running kew against a local OpenAI-compatible endpoint.

## Context window too small

**Symptom:** The run fails with an HTTP 400 `context_length_exceeded` / "exceeds the available context size".

**Fix:** The model was loaded with too small a context (e.g. the default 4096 tokens); reload it with a larger context (16k+), since kew's prompt plus the files the agent reads can exceed 4k quickly.

## No model loaded

**Symptom:** Requests return an empty models list or a model-not-found error.

**Fix:** Load/pull a model into your local server first, and confirm the served model id matches the `lemonade/<model-id>` in `kew.toml`.

## Wrong port / base_url

**Symptom:** Connection refused.

**Fix:** Confirm the server's port and set `[agent.lemonade] base_url` to match (Lemonade defaults to `http://localhost:13305/v1`).

## Slow first token

**Symptom:** Long pause before output.

**Note:** Local inference is wall-clock-bound, not a hang; larger models are slower on CPU/GPU without an NPU.
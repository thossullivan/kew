# Lemonade Runner

!!! warning "Experimental"
    The local tool loop and OpenAI-compatible endpoint integration are implemented.
    This runner cannot be contained: its inference endpoint is on host loopback,
    which a contained network cannot reach (`LemonadeRunner.uncontainable_reason`),
    so every dispatch runs on the host. Metering and recovery are not certified.
    Use it only with supervision and explicit `dangerously_skip_containment` consent.

[Lemonade](https://github.com/lemonade-sdk/lemonade) is kew's own tool-use loop (`kew-lemonade`) over any OpenAI-compatible local endpoint. It supports AMD Lemonade, LM Studio, llama.cpp's `llama-server`, vLLM, Ollama, and any other OpenAI-compatible server. Cost is $0 (local); nothing leaves the machine.

## Prerequisites

1. Install a local OpenAI-compatible server — see [Lemonade's installation guide](https://github.com/lemonade-sdk/lemonade?tab=readme-ov-file#installation).
2. Load a model into your server.

## Selecting a model

Set `model = "lemonade/<model-id>"` in `kew.toml`; the `lemonade/` prefix routes to this runner.

```toml
[agent]
model = "lemonade/llama3"
```

## Configuration

The `[agent.lemonade]` block accepts the following settings:

- `base_url` (default `http://localhost:13305/v1`) — the OpenAI-compatible endpoint to connect to.
- `max_steps` (default `40`) — the maximum number of model turns in a single run. One turn can make several tool calls.

## Examples

A `kew.toml` snippet:

```toml
[agent]
model = "lemonade/llama3"

[agent.lemonade]
base_url = "http://localhost:13305/v1"
max_steps = 15
```

Direct invocation with `kew-lemonade`:

```bash
kew-lemonade --base-url http://localhost:13305/v1 --model llama3 -p "Write a Python script to calculate Fibonacci numbers"
```

## Limitations (v1)

- **Model quality is the model's** — kew does not influence model quality; it's entirely up to the model you choose.
- **Tool-calling reliability varies by model** — some models may struggle with consistent tool calling.
- **`run_bash` runs with the loop's privileges** — since this runner executes locally, `run_bash` commands run with the same privileges as the kew process, so it's recommended to run kew inside a sandbox.

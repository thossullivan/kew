# kew model-eol

```text
Usage: kew model-eol COMMAND [ARGS]...
```

```text
Usage: kew model-eol check [OPTIONS]

Options:
  -m, --model TEXT
```

`kew model-eol` reads a **curated model lifecycle registry** — the
`lifecycle` of each row in the reviewed, version-controlled model catalog
(`kew/src/kew/models.json`) that ships inside the kew wheel. Dispatch,
`kew doctor`, and this command all read the same packaged registry offline and
never fetch. Nothing in kew writes it at run time.
[`kew models refresh --write`](models.md) updates prices and lifecycle, rows
are added by hand, and lifecycle facts reach a release only through a reviewed
pull request.

```bash
kew model-eol check
kew model-eol check --model openrouter/deepseek/deepseek-v4-pro
```

## Checking coverage

`kew model-eol check` is read-only and networkless. It validates the registry
and then checks the model ids your configuration actually names — the agent
model, the pinned reviewer model, every resolved `[routing]` entry, and the
`[pricing]` override ids — plus any extra id passed with `--model`
(repeat the option for more than one).

See the shared [exit-code scheme](exit-codes.md).

Exit codes are exact:

| Exit | Meaning |
| --- | --- |
| `0` | Every checked id has a curated row. Advisories may still have been printed. |
| `1` | At least one configured or `--model` id has **no curated row at all**. Add the row in a reviewed PR. |
| `2` | The registry document itself is invalid — a configuration error, not drift. Printed as the three-line config envelope (see [configuration errors](../configuration.md)) with the registry's reason on the fix line. |

Two advisory lines mean different things and are worth reading carefully:

- `registry gap` — there is no curated row for that id (or the row carries an
  unknown status). kew has nothing reviewed to say about the model. Only this
  state produces exit `1`.
- `lifecycle unverified` — a row exists with status `untracked`: the model is
  known and covered, but no first-party lifecycle documentation has been
  reviewed for it yet. This exits `0`.

Actionable statuses — `watch`, `scheduled`, `retiring`, `retired` — warn with
the shutdown date and replacement when the registry records them, and also exit
`0`. Lifecycle data is advisory: it never refuses a dispatch.

A row's status never blocks anything; a missing row does in one place. A
`[routing]` entry that names a model with no lifecycle row at all is refused
when the configuration loads.

## Where the registry comes from

The packaged registry is the default source and needs no configuration. A
project can substitute its own document with the single retained option:

```toml
[model_eol]
snapshot_path = "model-lifecycle-registry.json"
```

`snapshot_path` is an **explicit override only**. Leave it unset and the
packaged registry is used; set it and that file replaces the packaged one
entirely. An override that does not parse against the strict contract below
fails loudly rather than silently degrading to partial lifecycle data.

## The registry contract

```json
{
  "schema": "kew/model-lifecycle-registry@1",
  "reviewedAt": "2026-08-16",
  "models": [
    {
      "id": "provider/model",
      "provider": "provider",
      "status": "untracked",
      "shutdown": null,
      "replacement": null,
      "evidence": [
        {
          "kind": "router-catalog",
          "url": "https://provider.example/models/model",
          "checkedAt": "2026-08-16"
        }
      ]
    }
  ]
}
```

Validation is strict on purpose: the `schema` string must match exactly,
unknown keys are rejected at both the top level and inside each row, ids must be
unique and sorted, `status` must be one of `ok`, `untracked`, `watch`,
`scheduled`, `retiring`, or `retired`, `shutdown` and `replacement` are nullable
strings, dates are ISO `YYYY-MM-DD` calendar dates, and every row carries a
nonempty `evidence` list whose URLs are HTTPS. Any violation is a registry
error (exit `2`), distinct from a coverage finding (exit `1`).

Evidence is typed, not a free-text note. Each record names what was consulted
(`kind`), where (`url`), and when it was last read (`checkedAt`). The `kind` is
what decides how much the record can prove:

- A **first-party provider lifecycle document or announcement** — for example
  `provider-lifecycle-doc` — is the only evidence that can justify a lifecycle
  claim (`ok`, `watch`, `scheduled`, `retiring`, `retired`).
- A **router or provider catalog listing** — for example `router-catalog` or
  `provider-catalog` — establishes only that a route id exists and is currently
  available. Availability is not lifecycle proof.

Absence of evidence, inference from availability, or a third-party summary all
leave a model at `untracked`, reported as `lifecycle unverified`.

`reviewedAt` and `checkedAt` are deliberately separate: `reviewedAt` moves only
after a complete review of the whole registry, while a row's `checkedAt` moves
when that individual piece of evidence is rechecked.

## Review cadence

The registry is updated by reviewed pull request:

- at a scheduled review **at least quarterly**;
- whenever a routed, reviewer, or priced model id changes;
- whenever a provider publishes a lifecycle notice for a covered model.

Git history is the provenance ledger — who changed a lifecycle fact, when, and
against what evidence is answered by `git log` on the registry file. There is no
self-digest, no snapshot age field, and no runtime or CI freshness nag, because
a curated document's staleness is a review question, not something a timestamp
can attest.

## Current coverage

Every row of the model catalog, `kew/src/kew/models.json`, carries a lifecycle,
so every model id kew prices or routes has a lifecycle row, and so does each
alias a row declares. The catalog's top-level `reviewedAt` is the date the
whole set was last reviewed. Adding a model is a catalog change: its row brings
its price, its routing facts, and its lifecycle together. A `[routing]` entry
that names a model with no lifecycle row is refused when the configuration
loads.

The list below is generated from the catalog by
`kew/scripts/model_catalog_docs.py`; the evidence behind each status is in the
catalog row. Do not edit it by hand: change the catalog, then run
`cd kew && uv run python scripts/model_catalog_docs.py`.

<!-- kew:model-list:begin (generated by kew/scripts/model_catalog_docs.py; do not edit) -->

The catalog lists 24 models, last reviewed 2026-10-08.

| Model | Aliases | Provider | Status | Shutdown | Replacement |
| --- | --- | --- | --- | --- | --- |
| `claude-fable-5` |  | anthropic | untracked |  |  |
| `claude-haiku-4` |  | anthropic | untracked |  |  |
| `claude-haiku-4-5` |  | anthropic | untracked |  |  |
| `claude-opus-4` |  | anthropic | untracked |  |  |
| `claude-opus-4-5` |  | anthropic | untracked |  |  |
| `claude-opus-4-6` |  | anthropic | untracked |  |  |
| `claude-opus-4-7` |  | anthropic | untracked |  |  |
| `claude-opus-4-8` |  | anthropic | untracked |  |  |
| `claude-opus-5` |  | anthropic | ok |  |  |
| `claude-opus-5-5` |  | anthropic | ok |  |  |
| `claude-sonnet-4` |  | anthropic | untracked |  |  |
| `claude-sonnet-4-5` |  | anthropic | untracked |  |  |
| `claude-sonnet-4-6` |  | anthropic | untracked |  |  |
| `claude-sonnet-5` |  | anthropic | untracked |  |  |
| `claude-sonnet-5-5` |  | anthropic | ok |  |  |
| `gpt-5.6-luna` |  | openai | untracked |  |  |
| `gpt-5.6-sol` |  | openai | untracked |  |  |
| `openrouter/deepseek/deepseek-v4-flash` |  | openrouter | untracked |  |  |
| `openrouter/deepseek/deepseek-v4-flash-0731` |  | openrouter | untracked |  |  |
| `openrouter/deepseek/deepseek-v4-pro` |  | openrouter | untracked |  |  |
| `openrouter/google/gemini-3.8-flash` |  | openrouter | untracked |  |  |
| `openrouter/moonshotai/kimi-k2.7-code` |  | openrouter | untracked |  |  |
| `openrouter/qwen/qwen3-coder-next` |  | openrouter | untracked |  |  |
| `openrouter/z-ai/glm-5.2` |  | openrouter | untracked |  |  |

<!-- kew:model-list:end -->

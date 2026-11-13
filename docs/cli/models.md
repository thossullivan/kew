# kew models

```text
Usage: kew models COMMAND [ARGS]...
```

```text
Usage: kew models refresh [OPTIONS]

Options:
  --write
  --report PATH
  --eol-feed TEXT
  --accept-large-change
  --catalog PATH
```

`kew models refresh` brings the model catalog (`kew/src/kew/models.json`)
up to date with two sources:

- **prices** from the OpenRouter models API
  (`https://openrouter.ai/api/v1/models`);
- **lifecycle** (status, shutdown date, replacement, evidence) from each
  model-eol feed passed with `--eol-feed`. A feed is a JSON object
  `{"spec": "model-eol/0.1", "models": [...]}` whose rows have the curated
  registry row shape: `id`, `provider`, `status`, `shutdown`, `replacement`,
  `evidence`.

It prints a Markdown report and, with `--write`, rewrites the catalog. The
report has a table of every price change and every lifecycle change (model,
field, old, new, source URL). Use it as the body of the pull request that
commits the refresh: the catalog changes only through a reviewed PR.

```bash
kew models refresh --eol-feed https://example.org/model-eol.json
kew models refresh --eol-feed https://example.org/model-eol.json \
  --write --report refresh.md
git add kew/src/kew/models.json   # open the PR with refresh.md as its body
```

kew's own repository has a weekly job for this
(`.github/workflows/pricing-freshness.yml`). It runs `kew models refresh --write` without
`--accept-large-change`, regenerates the model-eol list and the www lifecycle
view, and opens or updates one PR from the `models-refresh/weekly` branch,
labeled `model-catalog-refresh` and `needs-human-merge`. A person merges that
PR; no automation does. A refused refresh opens no PR: the job files the
refusal on a `pricing-drift` issue instead.

## Fetched data is untrusted

Prices set cost figures and lifecycle facts reach routing, so every fetched
value a refresh would use is checked before anything is written. If any check
fails, the command names the row and writes nothing, neither the catalog nor
the report:

- a price must be greater than 0 and below $1000/MTok;
- a price more than double, or less than half, the committed one refuses
  unless you pass `--accept-large-change` after checking it;
- lifecycle dates must be ISO `YYYY-MM-DD` dates, statuses and evidence kinds
  must be known, and evidence URLs must be `https://`;
- the merged catalog must pass the same strict validation kew loads it with.

A source sets only a row's price and lifecycle. It never sets `id`,
`display_name`, `provider`, `runner`, `price_source`, `routable`,
`default_lanes` or `aliases`, and it never adds a row. An id a source names
that the catalog lacks is listed under **New ids** in the report for a person
to add. From OpenRouter, only ids under a vendor the catalog already routes
through OpenRouter are listed.

## First-party prices

Each catalog row has a `price_source`:

| `price_source` | What the refresh does with an OpenRouter price |
| --- | --- |
| `first-party` | Keeps the catalog price (the vendor's own list price, kept by hand). A disagreeing OpenRouter value is listed in the report only. |
| `openrouter` | Sets the catalog price from OpenRouter, within the bounds above. |

## Never at dispatch time

Nothing in dispatch, review or `kew doctor` runs a refresh or touches the
network for prices. Governed cost reads the committed catalog, so the same
commit prices a run the same way on every machine.

`kew models refresh` replaces `kew pricing sync openrouter` and its
`pricing.openrouter.toml` overlay; the `[pricing]` keys `snapshot_path`,
`warn_age_days` and `max_age_days` are gone. Prices for OpenRouter models now
live in their catalog rows.

See the shared [exit-code scheme](exit-codes.md).

| Exit | Meaning |
| --- | --- |
| `0` | Refresh written, dry run, or nothing to change. |
| `2` | A source fetch failed or a fetched value was refused; nothing written. |

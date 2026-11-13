# kew export

Export run history to JSON or CSV for backup or analysis, or emit a governed
model-eol inventory of the model identities found in the run ledger.

```text
Usage: kew export [OPTIONS]

Options:
  -f, --format TEXT  Export format: json, csv, or model-inventory [default: json]
  -o, --output PATH  Output file path (default: stdout)
  --since TEXT       Only export records after this date (ISO 8601, e.g. 2025-01-01)
  --purge            After export, delete records older than retention_days
                     (requires retention_days > 0 in kew.toml)
```

**Examples:**

```bash
# Export all runs as JSON to stdout
kew export

# Export as CSV to file
kew export -f csv -o runs.csv

# Export only recent runs
kew export --since 2025-01-01

# Export a model-eol v0.2.0 inventory (read-only over the ledger)
kew export -f model-inventory -o models.json
```

## Model inventory

`kew export --format model-inventory` produces the stdlib-only model-eol
inventory format v0.2.0. Its top-level shape is:

```json
{
  "format": "model-eol",
  "version": "0.2.0",
  "generated_at": "2026-08-03T12:00:00Z",
  "models": [
    {
      "id": "claude-sonnet-4-6",
      "source": "reported",
      "usage": {
        "count": 3,
        "first_seen": "2026-08-01T10:00:00Z",
        "last_seen": "2026-08-03T12:00:00Z"
      }
    }
  ]
}
```

Each run contributes to its reported/served identity when one is recorded;
otherwise it contributes to the requested route model with `source` set to
`requested`. `first_seen` and `last_seen` are UTC RFC3339 timestamps and
`count` is the number of contributing run records. Inventory export never
writes the ledger, and cannot be combined with `--purge`.

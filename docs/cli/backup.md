# kew backup

Snapshot `dispatch.db` and `kew.toml` into a portable, compressed archive using SQLite's online-backup API. Safe to run against a live database — no write lock, no forward migration.

```text
Usage: kew backup [OPTIONS]

Options:
  -o, --output PATH
  --include-logs
```

```bash
kew backup                          # kew-backup-<ts>.tar.gz in logging.dir
kew backup -o /backups/             # write to a different directory
kew backup -o /backups/daily.tar.gz # explicit archive name
kew backup --include-logs           # also bundle per-run .evidence.json sidecars
```

The archive always contains `dispatch.db`, `kew.toml` (if found), and `manifest.json` (schema version, chain head hash, run/event counts, chain integrity flag). Per-run **derived-evidence sidecars** are opt-in via `--include-logs`; raw per-run transcripts and event files are never archived.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--output` / `-o PATH` | `logging.dir` | Archive path or directory. If a directory, the file is named `kew-backup-<ts>.tar.gz`. |
| `--include-logs` | off | Also bundle per-run `dispatch-*.evidence.json` / `sandbox-*.evidence.json` sidecars from `logging.dir`. Raw `*.log` and `*.events.jsonl` files are excluded. |

## Exit codes

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
| --- | --- |
| `0` | Archive written successfully. |
| `2` | No database found at the configured path. |

## See also

[Operations guide](../operations.md) — backup cadence, archive contents, what env-var overrides are not captured, and retention.

[`kew restore`](restore.md) — restore a backup archive.

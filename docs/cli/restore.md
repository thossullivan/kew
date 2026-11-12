# kew restore

Validate and non-destructively restore a kew backup archive into the configured log directory. Checks schema compatibility and (by default) chain integrity before touching any files.

```text
Usage: kew restore [OPTIONS] ARCHIVE

Arguments:
  ARCHIVE

Options:
  --force
  --with-config
  --verify / --no-verify
```

```bash
kew restore kew-backup-20260624T120000Z.tar.gz
kew restore --force kew-backup-20260624T120000Z.tar.gz
kew restore --force --with-config kew-backup-20260624T120000Z.tar.gz
kew restore --no-verify kew-backup-20260624T120000Z.tar.gz
```

**Stop kew before restoring.** The command refuses to overwrite a database held open by another kew process.

If a `dispatch.db` already exists at the target, restore refuses unless `--force` is passed. With `--force`, the existing file (and any `-wal`/`-shm` sidecars) is moved aside to `dispatch.db.bak-<ts>` before the restored copy is placed.

After placing the file, the restored database is forward-migrated to the running kew's schema version.

## Arguments

| Argument | Description |
| --- | --- |
| `ARCHIVE` | Path to a `kew-backup-*.tar.gz` produced by `kew backup`. |

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--force` | off | Allow overwriting an existing `dispatch.db`; the existing file is moved aside before the restored copy is placed. |
| `--with-config` | off | Also restore `kew.toml` from the archive; the existing `kew.toml` is moved aside to `kew.toml.bak-<ts>`. |
| `--verify` / `--no-verify` | `--verify` | Verify that the restored chain head and count match the manifest. Pass `--no-verify` if the archive itself recorded `chain_ok=false`. |

## Exit codes

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
| --- | --- |
| `0` | Restore completed successfully. |
| `1` | Chain mismatch or invalid backup archive. |
| `2` | Schema too new; database locked by another process; existing database without --force; missing archive; configuration failure. |

## See also

[Operations guide](../operations.md) — stop-before-restore requirement, non-destructive guarantees, migration safety, and suggested backup cadence.

[`kew backup`](backup.md) — create a backup archive.

# `kew config`

```text
Usage: kew config COMMAND [ARGS]...
```

```text
Usage: kew config effective [OPTIONS]

Options:
  --json
  --sources
```

Inspect the configuration that a live command will use after applying the
default, `kew.toml`, database override, and environment layers.

```console
kew config effective --sources
```

The source annotation identifies the winning layer for each runtime-control
value. This is especially useful for budget limits: a database override made
through the web cockpit outranks `kew.toml`, while an environment variable
outranks both.

Use `--json` for scripts. Combine it with `--sources` to include the source
annotation in the JSON object.

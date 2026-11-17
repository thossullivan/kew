# `kew config`

`kew config effective` shows the configuration a live command will use, after it
applies the default, `kew.toml`, database override, and environment layers. When
a setting does not behave as you expect, `--sources` tells you which layer won.

```text
Usage: kew config COMMAND [ARGS]...
```

```text
Usage: kew config effective [OPTIONS]

Options:
  --json
  --sources
```

```console
kew config effective --sources
```

The source annotation identifies the winning layer for each runtime-control
value. This is especially useful for budget limits: a database override made
through the web cockpit outranks `kew.toml`, while an environment variable
outranks both.

Use `--json` for scripts. Combine it with `--sources` to include the source
annotation in the JSON object.

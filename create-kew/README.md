# create-kew

Installs [kew](https://kew.dev), the open-source control plane for autonomous
coding agents, and runs `kew init` in the current repository.

```bash
npx create-kew
```

Run it from inside the git repository you want kew to work on.

## What it does

1. Checks that [`uv`](https://docs.astral.sh/uv/) is installed, and prints the
   install command if it is not.
2. Installs the kew CLI with `uv tool install`.
3. Runs `kew init` with every flag you passed.

It is a launcher. `kew init` owns detection, questions and the `kew.toml` it
writes.

## Flags

Every flag except `--help` and `-h` goes to `kew init`. For a run with no prompts:

```bash
npx create-kew --yes --runner claude-code
```

`kew init --help` lists the flags. A script or an agent can follow
[Agents](https://kew.dev/docs/agents/).

## Requirements

Node.js 18 or later for this launcher. kew itself needs Python 3.13, `git`, a
logged-in `gh`, a coding-agent CLI such as `claude`, and, on the default route,
a container runtime.
See [Getting Started](https://kew.dev/docs/getting-started/).

## Links

- [Documentation](https://kew.dev/docs/)
- [Source and issues](https://github.com/thossullivan/kew)

Apache-2.0 licensed.

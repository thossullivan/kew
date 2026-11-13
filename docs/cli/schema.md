# kew schema

Print every visible kew command as JSON: its path, help line, arguments, options, whether it takes `--json`, and its exit-code family.

```text
Usage: kew schema [OPTIONS]
```

**Example**

```bash
kew schema | jq -r '.commands[] | select(.json) | .command'
kew schema | jq '.commands[] | select(.command == "kew status") | .options'
```

## Output

The output is read off the live command tree, so it cannot drift from what ships.
Hidden commands and hidden options are left out.
A group that runs on its own, such as `kew serve`, is listed alongside its subcommands.

```json
{
  "schema_version": 1,
  "commands": [
    {
      "path": ["approve"],
      "command": "kew approve",
      "help": "Release a needs-approval hold (records approval_granted).",
      "arguments": [
        {"name": "ISSUE", "type": "integer", "required": true, "default": null, "multiple": false}
      ],
      "options": [
        {"name": "--reason", "flags": ["--reason"], "type": "text", "required": false,
         "default": "", "is_flag": false, "multiple": false}
      ],
      "json": false,
      "exit_codes": {"family": "approve", "reference": "https://kew.dev/docs/cli/exit-codes/"}
    }
  ]
}
```

| Field | Meaning |
|-------|---------|
| `path` | The words after `kew` that select the command |
| `help` | The first paragraph of the command's help, on one line |
| `arguments` | Positional arguments: name, type, whether required, default |
| `options` | Options: longest flag as `name`, every flag spelling, type, default, whether required |
| `json` | `true` when the command accepts `--json` |
| `exit_codes.family` | The row of the [exit-code scheme](exit-codes.md) the command follows |

`type` is Click's type name: `text`, `integer`, `float`, `boolean`, `path`, `choice` (with a `choices` list), or a range such as `float range`.
A `default` of `null` means the option has no default value.

Its own outcomes are the `schema` row of the [exit-code scheme](exit-codes.md).

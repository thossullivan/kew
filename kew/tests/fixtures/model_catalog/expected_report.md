## Model catalog refresh

Sources:

- `https://openrouter.ai/api/v1/models`
- `https://feeds.example.test/model-eol.json`

### Price changes

| Model | Field | Old | New | Source |
|---|---|---|---|---|
| `openrouter/deepseek/deepseek-v4-flash-0731` | `price.input_per_mtok` | `none` | `0.4` | `https://openrouter.ai/api/v1/models` |
| `openrouter/deepseek/deepseek-v4-flash-0731` | `price.output_per_mtok` | `none` | `1.6` | `https://openrouter.ai/api/v1/models` |
| `openrouter/qwen/qwen3-coder-next` | `price.input_per_mtok` | `0.3` | `0.35` | `https://openrouter.ai/api/v1/models` |
| `openrouter/qwen/qwen3-coder-next` | `price.output_per_mtok` | `1.5` | `1.6` | `https://openrouter.ai/api/v1/models` |

### Lifecycle changes

| Model | Field | Old | New | Source |
|---|---|---|---|---|
| `claude-sonnet-5` | `lifecycle.status` | `untracked` | `scheduled` | `https://feeds.example.test/model-eol.json` |
| `claude-sonnet-5` | `lifecycle.shutdown` | `null` | `2027-04-01` | `https://feeds.example.test/model-eol.json` |
| `claude-sonnet-5` | `lifecycle.replacement` | `null` | `claude-sonnet-5-5` | `https://feeds.example.test/model-eol.json` |
| `claude-sonnet-5` | `lifecycle.evidence` | `https://platform.claude.com/docs/en/about-claude/models/overview` | `https://platform.claude.com/docs/en/about-claude/model-deprecations` | `https://feeds.example.test/model-eol.json` |

### First-party prices OpenRouter disagrees with (not applied)

| Model | Field | Catalog | OpenRouter | Source |
|---|---|---|---|---|
| `claude-sonnet-5` | `price.input_per_mtok` | `2` | `3` | `https://openrouter.ai/api/v1/models` |
| `claude-sonnet-5` | `price.output_per_mtok` | `10` | `15` | `https://openrouter.ai/api/v1/models` |

### New ids (not added; a person adds the row)

| Id | Source |
|---|---|
| `openrouter/qwen/qwen3.5-max` | `https://openrouter.ai/api/v1/models` |
| `claude-sonnet-6` | `https://feeds.example.test/model-eol.json` |

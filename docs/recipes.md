# Recipes

This page collects `justfile` recipes for common kew workflows, plus a pointer
to the dev-tunnel setup for Linear webhooks. Start from the ready-made template
at [`create-kew/templates/justfile-recipes.just`](https://github.com/thossullivan/kew/blob/main/create-kew/templates/justfile-recipes.just),
or paste the snippets below into your own `justfile`.

Example `justfile` snippets for common workflows:

```makefile
# Dispatch: pick next ready issue
dispatch:
    kew run

# Dispatch: drain the queue until empty
dispatch-drain:
    kew run --drain

# Dispatch: parallel drain with 3 workers
dispatch-parallel:
    kew run --drain --workers 3

# Dispatch: queue status
dispatch-status:
    kew status

# Review: preview PR comments without dispatching
review-dry pr:
    kew review {{pr}} --dry-run

# Review: dispatch agent to address PR comments
review pr:
    kew review {{pr}}
```

## Linear webhooks + dev tunnel

Receive Linear events locally during development. See the full recipe in [Linear webhooks — dev-tunnel recipe](linear-webhooks.md#dev-tunnel-recipe).

<script lang="ts">
  import { Card } from '$lib/ui';
  import { SIGNIN_COMMANDS, SIGNIN_HEADING, SIGNIN_BODY } from '$lib/signin';

  let { class: klass = '' }: { class?: string } = $props();

  let copied = $state<string | null>(null);
  async function copy(cmd: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(cmd);
      copied = cmd;
      setTimeout(() => {
        if (copied === cmd) copied = null;
      }, 1200);
    } catch {
      // Clipboard unavailable (no permission / insecure context) — the command
      // stays visible for manual copy, so this is a no-op failure.
    }
  }
</script>

<Card eyebrow="sign in" title={SIGNIN_HEADING} class={klass}>
  <p class="mb-3 max-w-prose text-sm text-muted">{SIGNIN_BODY}</p>
  <div class="flex flex-col gap-2">
    {#each SIGNIN_COMMANDS as { cmd, desc } (cmd)}
      <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
        <button
          type="button"
          class="group flex items-center gap-2 rounded-[var(--radius-sm)] border px-3 py-1.5 font-mono text-xs text-fg hover:border-accent"
          title="Copy command"
          onclick={() => copy(cmd)}
        >
          <span>{cmd}</span>
          <span class="text-[0.65rem] text-faint group-hover:text-accent"
            >{copied === cmd ? 'copied' : 'copy'}</span
          >
        </button>
        <span class="text-xs text-muted">{desc}</span>
      </div>
    {/each}
  </div>
</Card>

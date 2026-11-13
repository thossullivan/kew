<script lang="ts">
  /**
   * FUI-05 — a hairline. The factory surfaces separate with a rule where the
   * old cards enclosed with a border, so this is the only horizontal divider
   * they draw.
   */
  import { ruleClasses, type Hairline } from '$lib/factory/classes';

  let { tone = 'hair', class: klass = '' }: { tone?: Hairline; class?: string } = $props();
</script>

<hr aria-hidden="true" class={ruleClasses(tone, klass)} />

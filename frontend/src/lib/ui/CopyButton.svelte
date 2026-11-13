<script lang="ts">
  /**
   * #3459 — the Copy control beside a command block (DESIGN.md
   * `command-block`), with the one failure path every copy needs.
   *
   * "Copied" flashes only once the clipboard took the text. When the page
   * cannot reach a clipboard (no permission, an insecure origin, no
   * `navigator.clipboard` at all) the press does not fail silently: a short
   * line says so and points at the command, which is printed beside the
   * button as selectable text.
   *
   * The button and that line are siblings, not wrapped: inside a command
   * block's wrapping flex row the line takes the full width (`basis-full`) and
   * drops under the command rather than squeezing it.
   */
  import Button from './Button.svelte';
  import type { ButtonSize, ButtonVariant } from '$lib/factory/classes';

  let {
    text,
    label = 'Copy',
    // Quiet by default: magenta is the caller's call, made only where the
    // copied command is the person's next move (DESIGN.md button-primary).
    variant = 'outline',
    size = 'sm',
    class: klass = '',
    ...rest
  }: {
    /** What lands on the clipboard. */
    text: string;
    /** The button's accessible name. */
    label?: string;
    variant?: ButtonVariant;
    size?: ButtonSize;
    class?: string;
    [key: string]: unknown;
  } = $props();

  let outcome = $state<'idle' | 'copied' | 'failed'>('idle');
  let flash: ReturnType<typeof setTimeout> | undefined;

  async function copy() {
    clearTimeout(flash);
    try {
      // Absent outside a secure context: the TypeError is a failure too.
      await navigator.clipboard.writeText(text);
    } catch {
      outcome = 'failed';
      return;
    }
    outcome = 'copied';
    flash = setTimeout(() => (outcome = 'idle'), 1500);
  }
</script>

<Button {variant} {size} aria-label={label} onclick={copy} class={klass} {...rest}
  >{outcome === 'copied' ? 'Copied' : 'Copy'}</Button
>
<!-- The live region is always in the DOM so a screen reader announces the
     text when it arrives; it takes no space until the copy fails. -->
<span
  role="status"
  data-copy-failed={outcome === 'failed' ? '' : undefined}
  class={outcome === 'failed' ? 'basis-full text-13 text-warn' : 'sr-only'}
  >{outcome === 'failed' ? 'copy failed: select the command and copy it by hand' : ''}</span
>

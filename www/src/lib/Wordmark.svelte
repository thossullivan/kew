<script lang="ts">
  /**
   * kew's wordmark. The block cursor is the mark's whole idea: a blinking
   * block is the universal signal for "waiting for a human", which is the
   * product thesis compressed into one glyph.
   *
   * It doubles as a status indicator — cyan while agents work, magenta when
   * a person is needed.
   */
  let {
    state = 'working',
    size = '19px'
  }: { state?: 'working' | 'waiting'; size?: string } = $props();
</script>

<span class="mark" style:font-size={size}>
  kew<span class="cursor" data-state={state}></span>
</span>

<style>
  .mark {
    font-family: var(--mono);
    font-weight: 600;
    letter-spacing: -0.045em;
    color: var(--fg);
    display: inline-flex;
    align-items: baseline;
    gap: 1px;
    line-height: 1;
  }

  .cursor {
    display: inline-block;
    width: 0.52em;
    height: 0.92em;
    transform: translateY(0.08em);
    background: var(--accent-2);
    transition: background 420ms ease;
    animation: blink 1.15s steps(1, end) infinite;
  }

  .cursor[data-state='waiting'] {
    background: var(--accent);
  }

  @keyframes blink {
    0%,
    55% {
      opacity: 1;
    }
    56%,
    100% {
      opacity: 0.12;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .cursor {
      animation: none;
    }
  }
</style>

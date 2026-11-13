<script lang="ts">
  import { onMount } from 'svelte';
  import { DEMO_URL, tourOnDesktop } from './data/demo';
  import {
    DEFAULT_FRAME,
    END_MINUTES,
    FRAME_DIR,
    FRAME_HEIGHT,
    FRAME_WIDTH,
    FRAMES,
    PHONE
  } from './data/console-frames';

  /**
   * Real frames of kew's web console: the demo build's epic view, one epic at
   * three moments of its replay and as it stands now (data/console-frames.js,
   * captured by scripts/shoot-console.mjs). Under the frame a time rail holds
   * one stop per frame, placed at the replay clock the frame prints; pressing
   * a stop shows its frame. Nothing advances by itself.
   *
   * Every frame is in the prerendered HTML at the same size, and only the
   * selected one is drawn, so pressing a stop moves nothing on the page. The
   * rail needs the script, so it is drawn only once the script has mounted;
   * without it the live frame shows alone. Below 640px a 1440px frame would
   * be unreadable, so the page shows a phone capture of the live view instead,
   * and no rail; the images that are not drawn are lazy and never load.
   */

  let selected = $state(DEFAULT_FRAME);
  let mounted = $state(false);
  onMount(() => {
    mounted = true;
  });

  const last = FRAMES.length - 1;
  /** Where a stop sits on the rail, from 0 to 100. */
  const at = (minutes: number) => (minutes / END_MINUTES) * 100;
  /** How a stop's label hangs from its dot: the first from its left edge,
   *  the two last from their right edge, so no label runs off the rail. */
  const align = (i: number) => (i === 0 ? 'start' : i >= last - 1 ? 'end' : 'center');
</script>

<figure class="console-figure shell">
  <div class="frame">
    <p class="frame-head">
      <span>The web console &middot; an epic of 13 issues</span>
      <span>demo data</span>
    </p>
    <div class="shots">
      {#each FRAMES as frame, i (frame.file)}
        <img
          class="desk"
          class:on={i === selected}
          class:live={i === DEFAULT_FRAME}
          src="{FRAME_DIR}{frame.file}"
          width={FRAME_WIDTH}
          height={FRAME_HEIGHT}
          loading="lazy"
          decoding="async"
          alt={frame.alt}
        />
      {/each}
      <img
        class="phone"
        src="{FRAME_DIR}{PHONE.file}"
        width={PHONE.clip.width}
        height={PHONE.clip.height}
        loading="lazy"
        decoding="async"
        alt={PHONE.alt}
      />
    </div>
    {#if mounted}
      <div class="rail" role="group" aria-label="Moments of the epic">
        <span class="line" aria-hidden="true"></span>
        {#each FRAMES as frame, i (frame.file)}
          <button
            type="button"
            class="stop {align(i)}"
            class:low={i % 2 === 1}
            style:left="{at(frame.elapsed)}%"
            aria-pressed={i === selected}
            onclick={() => (selected = i)}
          >
            <span class="dot" aria-hidden="true"></span>
            <span class="label">{frame.time} &middot; {frame.moment}</span>
          </button>
        {/each}
      </div>
    {/if}
  </div>
  <figcaption>
    <!-- Each caption is true of what its width draws: the rail of four
         frames, or the one phone capture. -->
    <span class="caption"
      ><span class="wide">The real console on sample data: one epic at three moments of its
        replay, and as it stands now.</span
      ><span class="narrow">The real console on sample data: one epic as it stands now.</span>
      A card in magenta is where a person is the next move.</span
    >
    <a class="demo" href={DEMO_URL} use:tourOnDesktop>Open the live demo&nbsp;&nearr;</a>
  </figcaption>
</figure>

<style>
  .console-figure {
    width: 100%;
    margin-block: 0;
    padding-bottom: 72px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .frame {
    border: 1px solid var(--hair-strong);
    background: var(--ink-raised);
    padding: 8px;
  }
  .frame-head {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 4px 16px;
    margin: 0;
    padding: 6px 8px 12px;
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
  }

  /* Every desktop frame has the same size, so the box never changes height
     when a stop shows another one. Only the selected one is drawn; below
     640px only the phone capture is. An image that is not drawn is lazy and
     never loads. */
  .shots img {
    display: block;
    width: 100%;
    height: auto;
  }
  .shots .desk {
    aspect-ratio: 1440 / 831;
  }
  .shots .desk:not(.on),
  .shots .phone {
    display: none;
  }
  @media (max-width: 639.98px) {
    .shots .desk.on,
    .rail {
      display: none;
    }
    .shots .phone {
      display: block;
      aspect-ratio: 390 / 715;
    }
  }

  /* ── the time rail ─────────────────────────────────── */

  /* One hairline the width of the frame; each stop's dot sits on it at the
     replay clock its frame prints, inside a 22px inset so a 44px hit area
     never leaves the frame. Labels hang in two rows, so neighbours never
     collide. */
  .rail {
    position: relative;
    height: 88px;
    margin: 16px 22px 0;
  }
  .line {
    position: absolute;
    left: -22px;
    right: -22px;
    top: 21px;
    height: 1px;
    background: var(--hair-strong);
  }
  .stop {
    position: absolute;
    top: 0;
    width: 44px;
    height: 44px;
    margin-left: -22px;
    padding: 0;
    border: 0;
    background: none;
    cursor: pointer;
    color: var(--fg-dim);
  }
  .dot {
    position: absolute;
    left: 50%;
    top: 22px;
    width: 11px;
    height: 11px;
    margin: -6px 0 0 -6px;
    border: 1px solid var(--text-3);
    border-radius: 50%;
    background: var(--ink-raised);
    transition:
      background 160ms ease,
      border-color 160ms ease;
  }
  .label {
    position: absolute;
    top: 40px;
    white-space: nowrap;
    font-family: var(--mono);
    font-size: 11px;
    line-height: 1.4;
    color: inherit;
  }
  .stop.low .label {
    top: 64px;
  }
  .stop.start .label {
    left: 16px;
  }
  .stop.center .label {
    left: 50%;
    transform: translateX(-50%);
  }
  .stop.end .label {
    right: 16px;
  }
  @media (min-width: 1080px) {
    .label {
      font-size: 13px;
    }
  }
  .stop:hover {
    color: var(--fg);
  }
  .stop:hover .dot {
    border-color: var(--fg-dim);
  }
  /* The selected stop is cyan: it is the one on screen. */
  .stop[aria-pressed='true'] {
    color: var(--accent-2);
  }
  .stop[aria-pressed='true'] .dot {
    border-color: var(--accent-2);
    background: var(--accent-2);
  }

  figcaption {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px 24px;
  }
  .narrow {
    display: none;
  }
  @media (max-width: 639.98px) {
    .wide {
      display: none;
    }
    .narrow {
      display: inline;
    }
  }
  .caption {
    max-width: 70ch;
    font-size: 13px;
    line-height: 1.5;
    color: var(--muted);
  }
  .demo {
    display: inline-flex;
    align-items: center;
    min-height: 44px;
    padding: 0 20px;
    font-family: var(--mono);
    font-size: 13px;
    color: var(--fg);
    text-decoration: none;
    white-space: nowrap;
    border: 1px solid var(--border-strong);
    transition: border-color 180ms ease;
  }
  .demo:hover {
    border-color: var(--fg-dim);
  }
</style>

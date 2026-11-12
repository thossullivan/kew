<script lang="ts">
  import Hero from '$lib/Hero.svelte';
  import Evidence from '$lib/Evidence.svelte';
  import Loop from '$lib/Loop.svelte';
  import Install from '$lib/Install.svelte';
  import Footer from '$lib/Footer.svelte';
  import type { PageData } from './$types';

  import { onMount, tick } from 'svelte';

  let { data }: { data: PageData } = $props();

  /* On mount the console figure draws its stops, so the page above a deep
     link (kew.dev/#install) grows after the browser has already scrolled to
     it. Once that change is in the DOM and the fonts are in, bring the named
     element back into view, once (design review c9, A2 item 12). */
  onMount(async () => {
    let id = '';
    try {
      id = decodeURIComponent(location.hash.slice(1));
    } catch {
      // A malformed fragment (#%E0) names nothing: scroll nowhere.
      return;
    }
    if (!id) return;
    await tick();
    // The faces swapping in reflow the text above the target too.
    await document.fonts?.ready;
    requestAnimationFrame(() => {
      document.getElementById(id)?.scrollIntoView({ block: 'start', behavior: 'instant' });
    });
  });

  const TITLE = 'kew: offload the work, keep the judgment';
  const DESCRIPTION =
    'The open-source control plane for autonomous coding agents. kew treats every agent as an untrusted author, and a change never defines its own proof.';

  // Absolute by necessity: OpenGraph crawlers do not resolve relative URLs.
  // Regenerate the card with `npm run og` when its wording changes.
  const ORIGIN = 'https://kew.dev';
  const OG_IMAGE = `${ORIGIN}/og.png`;
</script>

<svelte:head>
  <title>{TITLE}</title>
  <meta name="description" content={DESCRIPTION} />
  <link rel="canonical" href={ORIGIN} />

  <meta property="og:type" content="website" />
  <meta property="og:site_name" content="kew" />
  <meta property="og:title" content={TITLE} />
  <meta property="og:description" content={DESCRIPTION} />
  <meta property="og:url" content={ORIGIN} />
  <meta property="og:image" content={OG_IMAGE} />
  <meta property="og:image:width" content="1200" />
  <meta property="og:image:height" content="630" />
  <meta property="og:image:alt" content="kew: offload the work, keep the judgment" />

  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content={TITLE} />
  <meta name="twitter:description" content={DESCRIPTION} />
  <meta name="twitter:image" content={OG_IMAGE} />
</svelte:head>

<main>
  <Hero />
  <Evidence anchors={data.anchors} />
  <Loop />
  <Install />
</main>
<Footer />

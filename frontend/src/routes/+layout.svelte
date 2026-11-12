<script lang="ts">
  import '../app.css';
  import { theme } from '$lib/theme.svelte';
  import { pickUpSession } from '$lib/auth';
  import type { Snippet } from 'svelte';

  let { children }: { children: Snippet } = $props();

  // FUI-26 — the cutover. Every page of the app lives in the `(factory)` group
  // now, and that group's layout brings the 56px header, so this layout has no
  // shell of its own left to draw: the left Rail is deleted and the route-id
  // test that chose between the two shells went with it.
  //
  // What still renders here bare is the handful of routes that are pure
  // redirects — `/`, `/inbox`, `/dashboard` — which paint one line of text and
  // leave. A 404 has no matched route to take a shell from, so `+error.svelte`
  // draws the factory header itself (design review c9 X7).
  $effect(() => {
    theme.init();
  });

  // Token pickup lives at the root so any route reached via `#token=…` signs
  // in and strips the fragment — not just the URL the serve banner prints.
  // The redirects above are what carry a fragment to the page that keeps it.
  $effect(() => {
    void pickUpSession();
  });
</script>

{@render children()}

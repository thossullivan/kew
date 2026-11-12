<script lang="ts">
  import { fetchSession } from '$lib/api';
  import { inboxLanding } from '$lib/auth';
  import SignInCard from '$lib/SignInCard.svelte';

  // `/inbox` is a legacy path AND where being signed out comes to rest.
  //
  // The legacy half: the judgment surface is the Work screen's needs-you filter
  // now (`/work?filter=needs-you`, FUI-26 — before the cutover it was the
  // console's approvals view at `/?filter=approvals`), so a bookmark redirects
  // there, preserving any `#token=…` fragment so a stale `/inbox#token=…` link
  // still signs in — the root layout picks the token up on arrival. A full
  // document replace (not a client-side `goto`) is what carries the fragment and
  // forces the layout to re-run its pickup.
  //
  // The other half is why this page has a screen at all. Every 401 in the app
  // sends the browser here, and after the cutover the redirect above lands
  // inside the `(factory)` group, whose shared poll signs out on its own 401 —
  // so a signed-out visitor redirected onward would bounce back and reload the
  // document forever. `inboxLanding` is the rule (auth.ts): a session, or a
  // token about to become one, redirects; anything else stays, on the sign-in
  // card, which names the two `kew serve` commands that print a fresh link. That
  // card is the only in-app way back, so this is the one screen the app
  // deliberately draws with no shell around it.
  let signedOut = $state(false);

  $effect(() => {
    // Read the hash before awaiting anything: the root layout's `pickUpSession`
    // strips the fragment as soon as it has exchanged the token, and this page's
    // reading of "is a sign-in in flight" must not depend on which of the two
    // effects the browser ran first.
    const loc = { hash: window.location.hash };
    // Asked with no session, the answer is the token's alone — so a sign-in in
    // flight is forwarded at once rather than behind a session read that is
    // certain to say "no" anyway.
    const now = inboxLanding(loc, false);
    if (now !== null) {
      window.location.replace(now);
      return;
    }
    void (async () => {
      let live = false;
      try {
        live = (await fetchSession()).authenticated;
      } catch {
        // An unreadable session is a session the operator cannot act with, and
        // the card is the honest answer to it.
        live = false;
      }
      const onward = inboxLanding(loc, live);
      if (onward !== null) window.location.replace(onward);
      else signedOut = true;
    })();
  });
</script>

<div class="mx-auto max-w-[1000px] px-6 py-7">
  {#if signedOut}
    <SignInCard />
  {:else}
    <p class="font-mono text-xs text-muted">Redirecting…</p>
  {/if}
</div>

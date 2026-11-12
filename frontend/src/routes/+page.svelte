<script lang="ts">
  // FUI-26, the navigation cutover: `/` is no longer the judgment console. The
  // factory is the UI now, and Work is its home tab, so the root forwards to
  // `/work`. The console moved to `/console` and, once every action it had
  // found a home on a factory screen, was removed (#3454); that URL forwards to
  // Work's needs-you view.
  //
  // A full-document replace, not a client-side `goto`: the serve banner prints
  // `http://127.0.0.1:8765/#token=…`, and a fragment is the one part of a URL a
  // client-side navigation does not carry. Replacing the document takes the
  // token to `/work`, where the root layout's `pickUpSession` exchanges it for
  // the session cookie exactly as it would have here. `factoryHomeHref` is what
  // keeps the query and the fragment (auth.ts); the reload it costs is paid once
  // per visit to a URL nothing in the app links to any more.
  import { factoryHomeHref } from '$lib/auth';

  $effect(() => {
    window.location.replace(factoryHomeHref());
  });
</script>

<div class="mx-auto max-w-[1000px] px-6 py-7">
  <p class="font-mono text-xs text-muted">Redirecting…</p>
</div>

<script lang="ts">
  import { REPO_URL } from './data/install';

  /**
   * One line under the page: who made it, where the source is, and the
   * licence (design review c9, V4). Nothing else personal. The links are
   * underlined, so they read as links, and 32px tall (R14).
   */
</script>

<footer class="shell">
  <p>
    <span class="by">Built by <a href="https://tomsullivan.dev">Tom Sullivan&nbsp;&nearr;</a></span>
    <!-- Each separator opens the item after it, so a dot never ends a line. -->
    <span class="item"
      ><span class="sep" aria-hidden="true">&middot;</span><a href={REPO_URL}
        >GitHub&nbsp;&nearr;</a
      ></span
    >
    <span class="item"><span class="sep" aria-hidden="true">&middot;</span><span>Apache-2.0</span></span>
  </p>
</footer>

<style>
  footer {
    border-top: 1px solid var(--line-soft);
  }
  p {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0 12px;
    margin: 0;
    padding-block: 16px;
    font-family: var(--mono);
    font-size: 13px;
    color: var(--muted);
  }
  .by,
  .item {
    display: inline-flex;
    align-items: center;
    gap: 1ch;
    white-space: nowrap;
  }
  a {
    display: inline-flex;
    align-items: center;
    min-height: 32px;
    color: var(--fg-dim);
    text-decoration: underline;
    text-underline-offset: 3px;
  }
  a:hover {
    color: var(--fg);
  }
</style>

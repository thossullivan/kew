// Build freshness badge derived from /api/bundle. Pure + vitest-covered so the
// factory header stays a thin render of this (it was the left rail's footer
// until FUI-26 deleted the rail). Absent bundles produce no badge;
// a present but unstamped bundle is labeled explicitly, and a stale bundle flags
// a forgotten rebuild.
import type { Bundle } from '$lib/api';

export type BundleBadge = {
  label: string;
  title: string;
  stale: boolean;
};

const isoDay = (builtAt: number): string =>
  builtAt > 0 ? new Date(builtAt * 1000).toISOString().slice(0, 10) : '';

export function bundleBadge(bundle: Bundle | null): BundleBadge | null {
  if (!bundle || !bundle.present) return null;
  const sha = bundle.git_sha || 'unstamped';
  const day = isoDay(bundle.built_at);
  if (bundle.stale) {
    return {
      label: `build ${sha} · stale`,
      title: `Deployed bundle ${sha}${day ? ` (${day})` : ''} is stale — frontend source is newer. Rebuild: bash scripts/build_web.sh`,
      stale: true
    };
  }
  return {
    label: `build ${sha}`,
    title: `Deployed bundle ${sha}${day ? ` · built ${day}` : ''}`,
    stale: false
  };
}

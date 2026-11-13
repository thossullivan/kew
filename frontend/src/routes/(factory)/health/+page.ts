import {
  fetchEffectiveConfig,
  fetchHealth,
  fetchHealthEnv,
  fetchReadiness,
  type EffectiveConfig,
  type Health,
  type HealthEnv,
  type Readiness
} from '$lib/api';

export const ssr = false;

/**
 * The four reads the Health page draws, each settling independently. The
 * readiness read (`kew doctor`'s checks) carries the build command a missing
 * image row prints (D41, #3470).
 *
 * Independent on purpose: a `kew serve` that does not serve one of them yet
 * must still render the other two, and the page's job is to say what is wrong
 * with the factory — so it cannot be the surface that goes blank first. `null`
 * is "this read did not arrive", which each section renders as its own sentence
 * rather than as an empty box.
 */
export async function load({ fetch }: { fetch: typeof globalThis.fetch }): Promise<{
  health: Health | null;
  env: HealthEnv | null;
  config: EffectiveConfig | null;
  readiness: Readiness | null;
}> {
  const [health, env, config, readiness] = await Promise.all([
    fetchHealth(fetch).catch(() => null),
    fetchHealthEnv(fetch).catch(() => null),
    fetchEffectiveConfig(fetch).catch(() => null),
    fetchReadiness(fetch).catch(() => null)
  ]);
  return { health, env, config, readiness };
}

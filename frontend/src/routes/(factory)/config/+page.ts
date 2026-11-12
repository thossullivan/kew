import { redirect } from '@sveltejs/kit';
import { fetchModels, fetchOverridable, type CatalogModel, type OverridableMap } from '$lib/api';

export const ssr = false;

export async function load({
  fetch
}: {
  fetch: typeof globalThis.fetch;
}): Promise<{ overridable: OverridableMap | null; models: CatalogModel[] | null }> {
  // The catalog is its own read: a failure leaves the model controls saying
  // so, and the rest of the page still renders.
  const models = fetchModels(fetch).then(
    (payload) => payload.models,
    () => null
  );
  try {
    const overridable = await fetchOverridable(fetch);
    return { overridable, models: await models };
  } catch (e) {
    if (e instanceof Error && e.message.includes('-> 401')) {
      throw redirect(302, '/inbox');
    }
    return { overridable: null, models: await models };
  }
}

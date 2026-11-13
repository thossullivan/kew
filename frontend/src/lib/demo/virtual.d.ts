// The fixture set `demo/fixture-bundle.ts` compiles into the demo build (#3139).
// Only the demo config resolves this module; see `src/lib/demo/install.ts`.
declare module 'virtual:kew-demo-fixtures' {
  const set: { files: Record<string, unknown>; epoch: string; source: string };
  export default set;
}

// The tours `demo/tour-bundle.ts` compiles into the demo build (#3145), each
// step's words already read out of its docs passage.
declare module 'virtual:kew-demo-tours' {
  const tours: import('./tours/types').BundledTour[];
  export default tours;
}

// What the page foot knows about the build history `demo/provenance-bundle.ts`
// ships (#3144), before the file arrives: the routes it names, and whether it
// is the real export or the committed samples.
declare module 'virtual:kew-demo-provenance-meta' {
  const meta: { routes: string[]; source: 'real' | 'sample' };
  export default meta;
}

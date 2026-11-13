/**
 * The demo tours' shapes (#3145): what a tour file names, and what the demo
 * build hands the console once `demo/tour-bundle.ts` has filled each step in
 * from its docs passage. Types only, so the console's type graph never reaches
 * the extractor's Node code.
 */

/** One run of a passage's text: plain, a code span, or bold. */
export type Segment = { text: string; code?: true; strong?: true };

/** A step as its tour file names it. */
export type TourStepSpec = {
  /** The route the step is on: a path, no query. */
  route: string;
  /**
   * The query the step opens its route with, key by key (`{ mode: 'replay',
   * t: '0' }` for a replay moment, #3147), or absent for the route as the
   * visitor has it. Never `tour` or `step`: those are the tour's own link.
   */
  query?: Record<string, string>;
  /** The value of the `data-tour` attribute it highlights. */
  target: string;
  /**
   * A graph card inside the target, by node id: the step highlights that card
   * (the epic graph's `.svelte-flow__node[data-id]`) instead of the whole
   * target, so a step about one node points at that node.
   */
  card?: string;
  /**
   * Press the epic graph's own Fit all before the step shows, so the whole
   * graph is on the canvas on the step's first frame.
   */
  fit?: true;
  /**
   * The `data-tour` value of a region the step's sentence compares across
   * (the epic graph, for a step that says a card reads the most): the popover
   * keeps clear of it where the window has room, never at the cost of the
   * target.
   */
  clear?: string;
  /** Its docs page, as a path under `docs/`. */
  docs: string;
  /** The anchor on that page it links to. */
  anchor: string;
  /** Its passage id, `<tour>/<n>`. */
  passage: string;
};

/** A tour as its file holds it. */
export type TourSpec = { id: string; title: string; start: string; steps: TourStepSpec[] };

/** A step as the demo reads it: the spec, its passage's words and its docs link. */
export type BundledStep = TourStepSpec & { title: string; body: Segment[][]; href: string };

/** A tour as the demo reads it. */
export type BundledTour = { id: string; title: string; start: string; steps: BundledStep[] };

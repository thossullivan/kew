export type Destination = {
  label: string;
  href: string;
};

/**
 * The three factory tabs (FUI-09). kew ships Work, Monitor and Epics —
 * the artboards' fourth tab, Evals, is not part of kew, and a tab full of
 * placeholders would claim a capability kew does not have.
 */
export const FACTORY_TABS: Destination[] = [
  { label: 'Work', href: '/work' },
  { label: 'Monitor', href: '/monitor' },
  { label: 'Epics', href: '/epics' }
];

export type DestinationGroup = {
  label: string;
  items: Destination[];
};

/**
 * Everything the header tucks into its "More" menu (FUI-26), in the three
 * groups the menu draws (D40, #3470): Factory says how the machine is, History
 * what it did, Settings what it runs with.
 *
 * This list replaced `DESTINATIONS`, the eight destinations of the left rail:
 * the cutover moved every page into the `(factory)` group, so the rail has no
 * route left to render on and is deleted. The judgment console it opened on
 * moved to `/console` and is gone (#3454): every action it offered has a home
 * on a factory screen, and `/console` forwards to Work's needs-you view.
 *
 * Written out rather than derived from the tabs: the order is the menu's own,
 * and a page in neither list has no way into the UI at all, which is what
 * `routes/urls.test.ts` checks.
 */
export const MORE_GROUPS: DestinationGroup[] = [
  {
    label: 'Factory',
    items: [
      { label: 'Health', href: '/health' },
      { label: 'Loops', href: '/loops' }
    ]
  },
  {
    label: 'History',
    items: [
      { label: 'Runs', href: '/runs' },
      { label: 'Scorecard', href: '/scorecard' },
      { label: 'Audit', href: '/audit' }
    ]
  },
  {
    label: 'Settings',
    items: [
      { label: 'Budget', href: '/budget' },
      { label: 'Config', href: '/config' },
      { label: 'Prompts', href: '/prompts' }
    ]
  }
];

/** The More menu's destinations in the menu's own order, groups flattened. */
export const MORE_DESTINATIONS: Destination[] = MORE_GROUPS.flatMap((g) => g.items);

/**
 * True when `pathname` is exactly `href` or a child path of it.
 * Trailing slashes are normalised before comparison.
 */
export function isActive(pathname: string, href: string): boolean {
  const norm = (p: string) => p.replace(/\/+$/, '') || '/';
  const a = norm(pathname);
  const b = norm(href);
  // Root matches exactly — every path is a "child" of '/', and no destination
  // may light up on every page. Nothing in the nav points at '/' any more (it
  // is a redirect to /work), but the rule is cheap to keep correct.
  if (b === '/') return a === '/';
  return a === b || a.startsWith(b + '/');
}

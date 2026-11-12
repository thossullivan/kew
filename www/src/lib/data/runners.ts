/**
 * The runners kew ships, by name, for the install section's runner row.
 *
 * Source of truth for the shipped set is kew/src/kew/runners/registry.py
 * (`_get_runner_factories`); a kew test holds this list equal to it. What each
 * runner executes, reaches and is contained by lives in the docs
 * (docs/runners/), which the row links.
 */

export type Runner = {
  id: string;
  /** Features that remain subject to change while their guarantees mature. */
  experimental?: boolean;
};

export const RUNNERS: Runner[] = [
  { id: 'claude-code' },
  { id: 'opencode' },
  { id: 'codex' },
  { id: 'pi' },
  { id: 'hermes' },
  { id: 'lemonade', experimental: true }
];

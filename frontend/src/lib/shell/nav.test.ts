import { describe, expect, it } from 'vitest';
import { FACTORY_TABS, isActive, MORE_DESTINATIONS, MORE_GROUPS } from './nav';

describe('FACTORY_TABS', () => {
  it('is Work, Monitor, Epics in that order — no Evals', () => {
    expect(FACTORY_TABS.map((t) => t.label)).toEqual(['Work', 'Monitor', 'Epics']);
  });

  it('maps each tab to its own top-level URL', () => {
    expect(FACTORY_TABS.map((t) => t.href)).toEqual(['/work', '/monitor', '/epics']);
  });

  it('lights the Work tab on an item detail URL', () => {
    const work = FACTORY_TABS[0].href;
    expect(isActive('/work', work)).toBe(true);
    expect(isActive('/work/2055', work)).toBe(true);
    expect(isActive('/work/2055/evidence', work)).toBe(true);
    expect(isActive('/workspace', work)).toBe(false);
  });

  it('lights the Epics tab on an epic detail URL', () => {
    expect(isActive('/epics/launch-fixes', '/epics')).toBe(true);
  });

  it('only one tab is ever active for a given path', () => {
    for (const pathname of ['/work/2055', '/monitor', '/epics/launch-fixes']) {
      const active = FACTORY_TABS.filter((t) => isActive(pathname, t.href));
      expect(active, pathname).toHaveLength(1);
    }
  });
});

describe('MORE_DESTINATIONS', () => {
  // The cutover (FUI-26) retired the eight-destination left rail: its legacy
  // pages are now the header's "More" menu. The menu draws them in three
  // groups (D40, #3470) — Factory, History, Settings — and this flat list is
  // those groups in the menu's order.
  it('is the three groups in order, with no legacy console', () => {
    expect(MORE_GROUPS.map((g) => g.label)).toEqual(['Factory', 'History', 'Settings']);
    expect(MORE_GROUPS.map((g) => g.items.map((d) => d.label))).toEqual([
      ['Health', 'Loops'],
      ['Runs', 'Scorecard', 'Audit'],
      ['Budget', 'Config', 'Prompts']
    ]);
    expect(MORE_DESTINATIONS.map((d) => d.label)).toEqual([
      'Health',
      'Loops',
      'Runs',
      'Scorecard',
      'Audit',
      'Budget',
      'Config',
      'Prompts'
    ]);
  });

  it('each destination has a leading-slash href', () => {
    for (const d of MORE_DESTINATIONS) {
      expect(d.href).toMatch(/^\//);
    }
  });

  it('offers no way to /console, which only forwards to Work now', () => {
    expect(MORE_DESTINATIONS.map((d) => d.href)).not.toContain('/console');
  });

  it('never repeats a tab, so no destination has two places to be', () => {
    const tabs = new Set(FACTORY_TABS.map((t) => t.href));
    for (const d of MORE_DESTINATIONS) {
      expect(tabs.has(d.href), d.href).toBe(false);
    }
  });

  it('has no root href: nothing in the nav routes through the / redirect', () => {
    for (const d of [...FACTORY_TABS, ...MORE_DESTINATIONS]) {
      expect(d.href, d.label).not.toBe('/');
    }
  });

  it('lights a More destination on its own detail URL', () => {
    expect(isActive('/runs/abc123', '/runs')).toBe(true);
  });
});

describe('isActive', () => {
  it('exact match returns true', () => {
    expect(isActive('/runs', '/runs')).toBe(true);
  });

  it('child path returns true', () => {
    expect(isActive('/runs/abc123', '/runs')).toBe(true);
  });

  it('sibling prefix does not match', () => {
    expect(isActive('/runs-extra', '/runs')).toBe(false);
  });

  it('root href matches only the root, never child pages', () => {
    expect(isActive('/', '/')).toBe(true);
    expect(isActive('/runs', '/')).toBe(false);
    expect(isActive('/config', '/')).toBe(false);
  });

  it('unrelated path returns false', () => {
    expect(isActive('/inbox', '/runs')).toBe(false);
  });

  it('trailing slash on pathname is normalised', () => {
    expect(isActive('/runs/', '/runs')).toBe(true);
  });

  it('trailing slash on href is normalised', () => {
    expect(isActive('/runs', '/runs/')).toBe(true);
  });
});

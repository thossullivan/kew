// @vitest-environment jsdom
/**
 * #2512 — the console's one key layer.
 *
 * jsdom, because the promises here are about where a press LANDED (a field,
 * the body) and about the one window listener, which a pure model cannot
 * show. The registry imports no Svelte, so it needs nothing else from the
 * client project.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  bindShortcuts,
  claimReserved,
  isTyping,
  keyGlyph,
  legendOf,
  registeredShortcuts,
  registerShortcuts,
  reservedPress,
  resetShortcuts,
  shortcutsSuspended,
  suspendShortcuts,
  type Shortcut
} from './registry';
import { KEY_LEGEND, WORK_KEYS, workShortcuts, type WorkIntent } from '$lib/work/keys';
import { DAG_KEYS } from '$lib/epic/dag-keys';
import { EPIC_KEYS, TIMELINE_KEYS } from '$lib/epic/timeline';
import { BRUSH_KEYS } from '$lib/monitor/brush';
import { OUTCOME_KEYS } from '$lib/report/outcomes';

/** Press `key` on `target` (the body by default), bubbling to the window the
 * way a real press does. Returns the event, to read `defaultPrevented`. */
function press(
  key: string,
  init: KeyboardEventInit = {},
  target: EventTarget = document.body
): KeyboardEvent {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init });
  target.dispatchEvent(event);
  return event;
}

const shortcut = (key: string, handler = vi.fn(), scope = 'test'): Shortcut => ({
  key,
  description: key,
  scope,
  handler
});

/** A handler that claims every press it is given. */
const claiming = () => vi.fn((e: KeyboardEvent) => e.preventDefault());

afterEach(() => {
  resetShortcuts();
  document.body.innerHTML = '';
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe('dispatch', () => {
  it('answers a registered key through one window listener', () => {
    const handler = claiming();
    registerShortcuts([shortcut('j', handler)]);
    const event = press('j');
    expect(handler).toHaveBeenCalledOnce();
    expect(event.defaultPrevented).toBe(true);
    expect(press('k').defaultPrevented).toBe(false);
    expect(handler).toHaveBeenCalledOnce();
  });

  it('installs one listener however many surfaces register, and drops it with the last', () => {
    const add = vi.spyOn(window, 'addEventListener');
    const remove = vi.spyOn(window, 'removeEventListener');
    const offA = registerShortcuts([shortcut('a')]);
    const offB = registerShortcuts([shortcut('b', vi.fn(), 'other')]);
    expect(add.mock.calls.filter(([type]) => type === 'keydown')).toHaveLength(1);
    offA();
    expect(remove).not.toHaveBeenCalled();
    offB();
    expect(remove.mock.calls.filter(([type]) => type === 'keydown')).toHaveLength(1);
  });

  it('forgets a surface once it unregisters', () => {
    const handler = claiming();
    const off = registerShortcuts([shortcut('j', handler)]);
    off();
    expect(press('j').defaultPrevented).toBe(false);
    expect(handler).not.toHaveBeenCalled();
    expect(registeredShortcuts()).toEqual([]);
  });

  it('offers a press newest first, and stops at the surface that claims it', () => {
    const older = claiming();
    const newer = vi.fn();
    registerShortcuts([shortcut('f', older, 'dag')]);
    registerShortcuts([shortcut('f', newer, 'timeline')]);
    press('f');
    // The newer surface looked and declined, so the older one was asked.
    expect(newer).toHaveBeenCalledOnce();
    expect(older).toHaveBeenCalledOnce();

    const first = claiming();
    registerShortcuts([shortcut('f', first, 'top')]);
    older.mockClear();
    press('f');
    expect(first).toHaveBeenCalledOnce();
    expect(older).not.toHaveBeenCalled();
  });

  it('leaves every platform modifier to the browser, but not Shift', () => {
    const handler = claiming();
    registerShortcuts([shortcut('j', handler), shortcut('T', handler)]);
    for (const mod of ['metaKey', 'ctrlKey', 'altKey'] as const) {
      expect(press('j', { [mod]: true }).defaultPrevented, mod).toBe(false);
    }
    expect(handler).not.toHaveBeenCalled();
    expect(press('T', { shiftKey: true }).defaultPrevented).toBe(true);
  });

  it('never traps Tab: one nothing claims is left to move the focus', () => {
    const declines = vi.fn();
    registerShortcuts([shortcut('Tab', declines)]);
    expect(press('Tab').defaultPrevented).toBe(false);
    expect(press('Tab', { shiftKey: true }).defaultPrevented).toBe(false);
    expect(declines).toHaveBeenCalledTimes(2);
  });
});

describe('a press in a field is the field’s', () => {
  const fields = (): [string, HTMLElement][] => {
    const input = document.createElement('input');
    const search = Object.assign(document.createElement('input'), { type: 'search' });
    const textarea = document.createElement('textarea');
    const editable = document.createElement('div');
    editable.contentEditable = 'true';
    // jsdom does not compute `isContentEditable`; a browser does.
    Object.defineProperty(editable, 'isContentEditable', { value: true });
    const all: [string, HTMLElement][] = [
      ['input', input],
      ['search', search],
      ['textarea', textarea],
      ['contenteditable', editable]
    ];
    for (const [, el] of all) document.body.append(el);
    return all;
  };

  it('fires no shortcut from an input, a textarea or an editable element', () => {
    const handler = claiming();
    const keys = ['j', 'k', 'Enter', 'Escape', 'f', 'ArrowDown', '/', 't', 'Tab'];
    registerShortcuts(keys.map((k) => shortcut(k, handler)));
    for (const [name, el] of fields()) {
      for (const key of keys) {
        expect(press(key, {}, el).defaultPrevented, `${key} in ${name}`).toBe(false);
      }
    }
    expect(handler).not.toHaveBeenCalled();
  });

  it('keeps `?` in a field, but lets the palette chord out of one, claimed', () => {
    // #2781 decided this on purpose: ⌘K / ^K types nothing, and from the
    // palette's own field it is how the palette closes.
    const palette = vi.fn();
    const help = claiming();
    claimReserved('palette', palette);
    claimReserved('help', help);
    for (const [name, el] of fields()) {
      expect(press('k', { metaKey: true }, el).defaultPrevented, `⌘K in ${name}`).toBe(true);
      expect(press('k', { ctrlKey: true }, el).defaultPrevented, `^K in ${name}`).toBe(true);
      expect(press('?', { shiftKey: true }, el).defaultPrevented, `? in ${name}`).toBe(false);
    }
    expect(palette).toHaveBeenCalledTimes(8);
    expect(help).not.toHaveBeenCalled();
  });

  it('counts a focused <select> as typing: its letters and arrows pick an option', () => {
    const handler = claiming();
    registerShortcuts([shortcut('j', handler), shortcut('ArrowDown', handler)]);
    const select = document.createElement('select');
    select.append(new Option('one'), new Option('two'));
    document.body.append(select);
    expect(isTyping(select)).toBe(true);
    expect(press('j', {}, select).defaultPrevented).toBe(false);
    expect(press('ArrowDown', {}, select).defaultPrevented).toBe(false);
    expect(handler).not.toHaveBeenCalled();
  });

  it('still answers the same key off the field', () => {
    const handler = claiming();
    registerShortcuts([shortcut('j', handler)]);
    const [[, input]] = fields();
    press('j', {}, input);
    expect(handler).not.toHaveBeenCalled();
    const button = document.createElement('button');
    document.body.append(button);
    press('j', {}, button);
    press('j');
    expect(handler).toHaveBeenCalledTimes(2);
  });

  it('reads a button, a link and the body as not typing', () => {
    expect(isTyping(document.body)).toBe(false);
    expect(isTyping(document.createElement('button'))).toBe(false);
    expect(isTyping(document.createElement('a'))).toBe(false);
    expect(isTyping(window)).toBe(false);
    expect(isTyping(null)).toBe(false);
  });

  it('offers a field only the shortcuts that ask for it (`inFields`)', () => {
    const esc = claiming();
    const j = claiming();
    registerShortcuts([{ ...shortcut('Escape', esc), inFields: true }, shortcut('j', j)]);
    for (const [name, el] of fields()) {
      expect(press('Escape', {}, el).defaultPrevented, `Escape in ${name}`).toBe(true);
      expect(press('j', {}, el).defaultPrevented, `j in ${name}`).toBe(false);
    }
    expect(esc).toHaveBeenCalledTimes(4);
    expect(j).not.toHaveBeenCalled();
  });

  it('still closes the epic page from the replay scrubber, but leaves T to a field', () => {
    // Review r1 (#2554): before the key layer, the page's own window handler
    // ran Esc with no typing guard; the scrubber is an <input type=range>.
    const handler = vi.fn();
    registerShortcuts(bindShortcuts('epic', EPIC_KEYS, handler));
    const scrubber = Object.assign(document.createElement('input'), { type: 'range' });
    document.body.append(scrubber);
    press('Escape', {}, scrubber);
    expect(handler).toHaveBeenCalledTimes(1);
    press('t', {}, scrubber);
    press('T', { shiftKey: true }, scrubber);
    expect(handler).toHaveBeenCalledTimes(1);
  });
});

describe('the reserved presses', () => {
  it('reads ⌘K and ^K as the palette and a bare ? as help', () => {
    const p = (key: string, mods: Partial<Record<'metaKey' | 'ctrlKey' | 'altKey', boolean>> = {}) =>
      reservedPress({ key, metaKey: false, ctrlKey: false, altKey: false, ...mods });
    expect(p('k', { metaKey: true })).toBe('palette');
    expect(p('k', { ctrlKey: true })).toBe('palette');
    expect(p('K', { metaKey: true })).toBe('palette');
    expect(p('k', { metaKey: true, altKey: true })).toBeNull();
    expect(p('k')).toBeNull();
    expect(p('?')).toBe('help');
    expect(p('?', { ctrlKey: true })).toBeNull();
  });

  it.each(['Meta+K', 'Ctrl+K', 'Meta+k', 'ctrl+k', '?'])(
    'throws in development when a surface registers %s',
    (key) => {
      expect(() => registerShortcuts([shortcut(key)])).toThrow(/reserved/);
      expect(registeredShortcuts()).toEqual([]);
    }
  );

  it('throws for any other chord too: the palette is the only one', () => {
    for (const key of ['Meta+J', 'Alt+ArrowLeft', 'Control+S']) {
      expect(() => registerShortcuts([shortcut(key)]), key).toThrow(/only one/);
    }
  });

  it('refuses one key twice in one scope, but lets two scopes share it', () => {
    expect(() => registerShortcuts([shortcut('f'), shortcut('f')])).toThrow(/twice/);
    registerShortcuts([shortcut('f', vi.fn(), 'dag')]);
    expect(() => registerShortcuts([shortcut('f', vi.fn(), 'timeline')])).not.toThrow();
  });

  it('in production says so and registers nothing, rather than taking the page down', () => {
    vi.stubEnv('DEV', false);
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    const handler = claiming();
    expect(() => registerShortcuts([shortcut('?', handler), shortcut('j', handler)])).not.toThrow();
    expect(error).toHaveBeenCalledOnce();
    expect(registeredShortcuts().map((s) => s.key)).toEqual(['j']);
    press('?', { shiftKey: true });
    expect(handler).not.toHaveBeenCalled();
  });

  it('goes to its claimant, claimed, and never to a surface', () => {
    const surface = claiming();
    registerShortcuts([shortcut('k', surface)]);
    const palette = vi.fn();
    const help = vi.fn();
    claimReserved('palette', palette);
    claimReserved('help', help);
    expect(press('k', { metaKey: true }).defaultPrevented).toBe(true);
    expect(press('k', { ctrlKey: true }).defaultPrevented).toBe(true);
    expect(press('?', { shiftKey: true }).defaultPrevented).toBe(true);
    expect(palette).toHaveBeenCalledTimes(2);
    expect(help).toHaveBeenCalledOnce();
    expect(surface).not.toHaveBeenCalled();
  });

  it('is left to the browser while nothing has claimed it', () => {
    expect(press('k', { metaKey: true }).defaultPrevented).toBe(false);
  });

  it('can be claimed once', () => {
    claimReserved('palette', vi.fn());
    expect(() => claimReserved('palette', vi.fn())).toThrow(/already claimed/);
  });
});

describe('an open modal suspends the page', () => {
  it('drops every page shortcut while suspended, and answers again on resume', () => {
    const handler = claiming();
    registerShortcuts([shortcut('j', handler), shortcut('Escape', handler)]);
    const resume = suspendShortcuts();
    expect(shortcutsSuspended()).toBe(true);
    expect(press('j').defaultPrevented).toBe(false);
    expect(press('Escape').defaultPrevented).toBe(false);
    expect(handler).not.toHaveBeenCalled();
    resume();
    expect(shortcutsSuspended()).toBe(false);
    expect(press('j').defaultPrevented).toBe(true);
    expect(handler).toHaveBeenCalledOnce();
  });

  it('still hands the reserved presses to their claimants, so ⌘K closes the palette', () => {
    const palette = vi.fn();
    const help = vi.fn();
    claimReserved('palette', palette);
    claimReserved('help', help);
    suspendShortcuts();
    expect(press('k', { metaKey: true }).defaultPrevented).toBe(true);
    expect(press('?', { shiftKey: true }).defaultPrevented).toBe(true);
    expect(palette).toHaveBeenCalledOnce();
    expect(help).toHaveBeenCalledOnce();
  });

  it('stays suspended until every modal has let go, and a resume counts once', () => {
    const handler = claiming();
    registerShortcuts([shortcut('j', handler)]);
    const drawer = suspendShortcuts();
    const palette = suspendShortcuts();
    palette();
    palette();
    expect(press('j').defaultPrevented).toBe(false);
    drawer();
    expect(press('j').defaultPrevented).toBe(true);
    expect(handler).toHaveBeenCalledOnce();
  });
});

describe('legends read the registry', () => {
  it('draws one hint per description, keys in order, the wide ones last', () => {
    expect(
      legendOf([
        { key: 'Tab', description: 'nodes', legend: true },
        { key: '0', description: 'zoom', legend: 'wide' },
        { key: 'ArrowLeft', description: 'lineage', legend: true },
        { key: 'ArrowUp', description: 'up' },
        { key: 'ArrowRight', description: 'lineage', legend: true }
      ])
    ).toEqual([
      { keys: ['Tab'], label: 'nodes' },
      { keys: ['ArrowLeft', 'ArrowRight'], label: 'lineage' },
      { keys: ['0'], label: 'zoom', wide: true }
    ]);
  });

  it('draws the Work legend from exactly what the page registers', () => {
    registerShortcuts(workShortcuts(() => {}));
    expect(legendOf(registeredShortcuts('work'))).toEqual(KEY_LEGEND);
  });

  it('draws the DAG legend from keys the graph or the page registers', () => {
    registerShortcuts(bindShortcuts('dag', DAG_KEYS, () => {}));
    registerShortcuts(bindShortcuts('epic', EPIC_KEYS, () => {}));
    const answered = new Set(registeredShortcuts().map((s) => s.key));
    const legend = legendOf([...DAG_KEYS, ...EPIC_KEYS]);
    expect(legend.map((h) => h.label)).toEqual([
      'nodes',
      'step along an edge',
      'popover',
      'lineage',
      'close',
      'zoom to node'
    ]);
    for (const key of legend.flatMap((h) => h.keys)) expect(answered.has(key), key).toBe(true);
    expect(legend.flatMap((h) => h.keys).map(keyGlyph)).toEqual(['tab', '←', '→', '↵', 'L', 'esc', '0']);
  });

  it('holds every surface table to the registry’s own rules', () => {
    for (const [scope, defs] of [
      ['dag', DAG_KEYS],
      ['epic', EPIC_KEYS],
      ['timeline', TIMELINE_KEYS],
      ['work', WORK_KEYS],
      ['brush', BRUSH_KEYS],
      ['outcomes', OUTCOME_KEYS]
    ] as const) {
      expect(() => registerShortcuts(bindShortcuts(scope, defs, () => {})), scope).not.toThrow();
    }
  });
});

describe('the Work screen, through the registry', () => {
  it('carries every key in its table to the intent `workIntent` names', () => {
    const run = vi.fn<(intent: WorkIntent, e: KeyboardEvent) => void>();
    registerShortcuts(workShortcuts(run));
    for (const { key, intent } of WORK_KEYS) {
      run.mockClear();
      press(key);
      expect(run, key).toHaveBeenCalledOnce();
      expect(run.mock.calls[0][0], key).toBe(intent);
    }
  });

  it('answers none of them from the search box', () => {
    const run = vi.fn();
    registerShortcuts(workShortcuts(run));
    const box = Object.assign(document.createElement('input'), { type: 'search' });
    document.body.append(box);
    for (const { key } of WORK_KEYS) press(key, {}, box);
    expect(run).not.toHaveBeenCalled();
  });
});

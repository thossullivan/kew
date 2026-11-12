# Factory UI reference artboards

These are the direction-D artboards for the kew Factory UI. They are the
visual source of truth for the Factory UI work. They are design
references, not app code: nothing in this folder is imported by the frontend or
shipped in the `kew serve` bundle.

| File | View |
| --- | --- |
| `DWork.html` | Work |
| `DMonitor.html` | Monitor |
| `DEpic.html` | Epics |
| `DMonitor.events.json` | The five audit events DMonitor's event stream draws |

`DMonitor.events.json` is data, not an export: the events from 15:23:18 to
15:46:58 that DMonitor's stream is drawn from, held here so
`src/lib/monitor/stream.test.ts` can check the strip against the artboard's
positions. The fixture API serves a different stream (`fixtures/factory/audit.json`),
written near the fixture set's epoch so its events fall inside the live window
(#2847).

## How to view

Open a file in a browser. Each artboard is a fixed 1440×960 frame. The nav
links between the three files work locally. The fonts load from Google Fonts,
so the typography needs a network connection.

## Provenance

- Source: a private claude.ai design canvas, owner ruling #2056.
- Exported: 2026-09-24.
- The committed files are the canvas exports with the canvas runtime stripped
  by `strip-canvas.mjs`. Inline styles are unchanged.

sha256 of each original `.dc.html` export:

| Source | sha256 |
| --- | --- |
| `DEpic.dc.html` | `8ab137cad49567bc58b9db6930cc61453bc3517718b43d439b7f22fe4b372aa0` |
| `DWork.dc.html` | `0468a9df090c1e925d2e165e58bcb30c89348c4e5232d9627e8e238f555d88ea` |
| `DMonitor.dc.html` | `4db9f4021fa883661bbe19c240bff0d9a55d7db099d0e92a8e74d2aeb53f28c1` |

## Updating

Replace the files wholesale with `strip-canvas.mjs`. Never hand-edit them.

```sh
node strip-canvas.mjs <dir-with-the-.dc.html-exports> .
```

Then update the sha256 table above. `src/lib/design-refs.test.ts` checks that
no canvas runtime survives, that each root frame is 1440×960, and that every
relative link resolves to a file in this folder.

## ui-ux artboards (2026-10-02)

The `UX*.html` files are the visual targets for the ui-ux epic (#2251). They
are hand-written HTML, not canvas exports, so `strip-canvas.mjs` and the sha256
table above do not apply to them, and they may be edited by hand. Each is a
1440×900 frame, built from live API data with the console's bugs fixed. Tom
approved them on 2026-10-02. Each file's top comment lists the brief sections
it implements and Tom's decisions.

| File | View | Issue |
| --- | --- | --- |
| `UXWork.html` | Work: select in place, entry detail pane | #2246 |
| `UXMonitor.html` | Monitor: range presets, brush, filters, paging | #2248 |
| `UXEpic.html` | Epics: Svelte Flow canvas, dagre layout, node popover | #2250 |

Compare the app against one with `npm run shoot -- <route> UXEpic`.

## Evals

The Evals artboard (`DEvals`) is not part of kew and is not committed here.
The Evals tab in the nav of these files is plain text with the title
"Evals is not part of kew", so no link points at a missing file.

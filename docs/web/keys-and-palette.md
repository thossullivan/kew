# Keys and the palette

The console is keyboard-first. Every shortcut a page answers is held in one
key layer, the registry in `frontend/src/lib/keys/registry.ts`. A page registers
its shortcuts there, and the page's key legend and the <kbd>?</kbd> overlay are
drawn from that same table. So the keys you are told about are the keys the page
answers to.

## Two keys work everywhere

| Key | What it does |
|---|---|
| <kbd>⌘</kbd> <kbd>K</kbd> (macOS) / <kbd>Ctrl</kbd> <kbd>K</kbd> | Opens the [palette](#the-palette). |
| <kbd>?</kbd> | Opens help for the page you are on: the question the page answers, a link to its docs, and every shortcut the page has registered. <kbd>Esc</kbd> closes it and returns the focus to where it was. |

These two presses are reserved. A page cannot register either of them, and
they are the only chords. ⌥⌘K and <kbd>?</kbd> with a modifier are left to the
browser.

## Rules every shortcut follows

The registry enforces these rules for every page:

- **A key you type into a field goes to the field.** A shortcut never fires
  from an input, a textarea, a dropdown or editable text, so <kbd>j</kbd> in
  the search box types a `j`. The one exception is the epic page's
  <kbd>Esc</kbd>, which still closes from the replay scrubber. <kbd>?</kbd>
  does not open help from a field, because there it is a character.
  <kbd>⌘</kbd> <kbd>K</kbd> / <kbd>Ctrl</kbd> <kbd>K</kbd> does open the
  palette from a field, because it types nothing; in the palette's own search
  field it closes the palette.
- **Nothing reaches the page behind an open box.** While the palette, the
  <kbd>?</kbd> overlay, the dispatch launchpad's drawer or a Config dialog is
  open, the page's shortcuts are switched off, so a <kbd>j</kbd> pressed in
  the box never moves the list behind it. <kbd>⌘</kbd> <kbd>K</kbd> and
  <kbd>?</kbd> still work.
- **Browser modifiers stay with the browser.** ⌘R, ^W and ⌥← never reach a
  page. Shift is not a modifier here, because you need it to type <kbd>T</kbd>
  and <kbd>?</kbd>.
- **Tab is never trapped.** If no page handles a <kbd>Tab</kbd>, the focus
  moves on as usual.

## Shortcuts per surface

Each table lists every key the surface registers, with the description the
legend and the <kbd>?</kbd> overlay show for it. Keys appear as the overlay
draws them: <kbd>↵</kbd> is Enter, <kbd>esc</kbd> is Escape, <kbd>tab</kbd> is
Tab, and the arrows are the arrow keys. A surface's keys work only while that
surface is on screen.

### Work list (`work`)

On `/work`.

| Key | Does |
|---|---|
| <kbd>j</kbd> | move |
| <kbd>k</kbd> | move |
| <kbd>↑</kbd> | move up |
| <kbd>↓</kbd> | move down |
| <kbd>↵</kbd> | open |
| <kbd>esc</kbd> | clear |
| <kbd>c</kbd> | copy command |
| <kbd>f</kbd> | needs you |
| <kbd>/</kbd> | search |

<kbd>j</kbd>/<kbd>k</kbd> and the arrows do not wrap past either end of the
list, and they skip only rows that are not on screen. <kbd>c</kbd> copies the
selected item's command. On the [first-run card](index.md#first-run) it copies
the card's primary command.

### Epic page (`epic`)

On `/epics/<ref>`, in both the graph and the timeline view.

| Key | Does |
|---|---|
| <kbd>esc</kbd> | close |
| <kbd>t</kbd> | graph / timeline |
| <kbd>T</kbd> | graph / timeline |

### Epic graph (`dag`)

On the epic page's graph view.

| Key | Does |
|---|---|
| <kbd>tab</kbd> | nodes |
| <kbd>←</kbd> | step along an edge |
| <kbd>→</kbd> | step along an edge |
| <kbd>↑</kbd> | layer up |
| <kbd>↓</kbd> | layer down |
| <kbd>↵</kbd> | popover |
| <kbd>0</kbd> | zoom to node |
| <kbd>l</kbd> | lineage |
| <kbd>L</kbd> | lineage |
| <kbd>f</kbd> | fit all |
| <kbd>F</kbd> | fit all |

### Epic timeline (`timeline`)

On the epic page's timeline view.

| Key | Does |
|---|---|
| <kbd>↑</kbd> | previous row |
| <kbd>↓</kbd> | next row |
| <kbd>f</kbd> | fit |
| <kbd>F</kbd> | fit |

The graph and the timeline both use <kbd>F</kbd>. The press goes to whichever
of the two is on screen.

### Monitor overview brush (`brush`)

On `/monitor`, while the event overview track above the stream has the focus.

| Key | Does |
|---|---|
| <kbd>←</kbd> | pan the window older |
| <kbd>→</kbd> | pan the window newer |
| <kbd>+</kbd> | zoom in |
| <kbd>=</kbd> | zoom in |
| <kbd>-</kbd> | zoom out |
| <kbd>_</kbd> | zoom out |
| <kbd>l</kbd> | back to live |
| <kbd>L</kbd> | back to live |

With ⌘, Ctrl or ⌥ held, these keys are the browser's: ⌘= and ⌘− zoom the page
and ⌘← goes back.

### Scorecard outcome charts (`outcomes`)

On `/scorecard`, while the weekly outcome charts have the focus.

| Key | Does |
|---|---|
| <kbd>←</kbd> | previous week |
| <kbd>→</kbd> | next week |
| <kbd>Home</kbd> | first week |
| <kbd>End</kbd> | last week |
| <kbd>esc</kbd> | clear the crosshair |

## The palette

Press <kbd>⌘</kbd> <kbd>K</kbd> or <kbd>Ctrl</kbd> <kbd>K</kbd> to open the
palette, then type to search. It searches four kinds of place:

- **pages**: every tab, plus everything in the **More** menu;
- **epics**: matched by ref;
- **items**: matched by number. The palette lists the same rows as the Work
  list, so opening one selects that row on `/work`;
- **runs**: the newest runs, matched by run id or item number.

Inside the palette:

| Key | Does |
|---|---|
| <kbd>↑</kbd> / <kbd>↓</kbd> | Move the cursor. It wraps at both ends. |
| <kbd>Enter</kbd> | Go to the place under the cursor. On an action, start the action's confirm step. |
| <kbd>→</kbd> | On an item, list the actions that item offers. |
| <kbd>Esc</kbd> | Leave an item's action list, then close the palette. During a confirm step, cancel the step without writing anything. |

### Palette actions and their confirm steps

The palette can also run three kinds of action:

| Action | Where it comes from | Confirm step |
|---|---|---|
| **An item's offered action** | The actions the server offers for that item. These are the same actions its row offers on `/work`. The palette has no actions of its own. | Shows what will happen. You must press **Confirm**, and enter a reason first if the action needs one. The action is checked against the item as it was when it was offered. If the item has changed since then, the palette loads it again and asks you to choose again. It never retries on its own. |
| **Copy dispatch command** | The `kew dispatch` command that the server builds for that item (`GET /api/dispatch/resolve`). Offered only where dispatch is the item's next move: the item is in the ready queue, or its safest action is to dispatch it. An item in review or on hold, and a needs-you row that is not an item (a stalled epic), have none. | None. It only copies text to your clipboard and changes nothing. |
| **Stop the fleet** | Always available. Kills every running agent and refuses new dispatches until the fleet is resumed. | You must enter a reason and press **Confirm**. |

No request is sent until you confirm. The server checks every write against
its approver rule. If you are not an approver, the palette says so, as the
rows on `/work` do.

!!! important "The palette never dispatches"
    The palette can take you somewhere, copy a command, run an action the
    server offered after you confirm it, or stop the fleet. It cannot start
    work. To dispatch an item, copy the `kew dispatch` command and run it in
    your terminal, or press **Run** on the item so the running `kew drive`
    runs it ([Run](index.md#run)). A run starts only from the CLI.

### Ask mode

Ask mode lets you type a question into the palette. It is off by default
because each ask is a paid model call. To turn it on, set `palette_ask = true`
under [`[serve]`](../configuration.md#serve). When it is on, the field's
placeholder says so:

| Key | Does |
|---|---|
| <kbd>Enter</kbd> | On a query that ends in `?`, ask. |
| <kbd>Tab</kbd> | On any query, ask. |

The server sends the model your query and the label of each entry the
palette is showing, and nothing else. The answer is up to five of **those**
entries, best first, each with a one-line reason. The palette never asks the
same query twice: once a query has an answer, <kbd>Enter</kbd> selects the
entry under the cursor, as it does without ask mode.

Ask mode **only proposes**. Every suggestion is an ordinary palette entry: the
server must offer the action, it goes through the same confirm step described
above, and it never dispatches. Ask mode cannot write anything that the
palette could not write without it.

Asks count toward the daily budget (`[budget] daily_usd`), together with
dispatched runs and other recorded model use. Once today's spend has reached
the cap, the server refuses the ask before calling the model and records
nothing. The palette shows the server's one-sentence refusal, naming the cap,
in place of results. <kbd>Enter</kbd> then does nothing until you change the
query.

## Related

- [Web UI overview](index.md): the cockpit shell, its tabs and the first-run
  card.
- [Epic DAG live view](epics.md): what the graph and timeline show.
- [`kew dispatch`](../cli/dispatch.md): the command the palette copies.

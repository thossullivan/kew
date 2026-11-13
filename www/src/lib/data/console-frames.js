/**
 * The demo console the front page shows: one epic of the demo's sample
 * repository (acme/ledger), `ledger-v2`, at three moments of its replay and
 * in its live view, plus a phone capture of the live view and the "needs
 * you" block the loop section shows beside stage 05.
 *
 * scripts/shoot-console.mjs captures each `path` from a demo build into
 * static/console/<file>, with no tour open and a node's popover closed,
 * clipped to `clip` so the demo's own banner and its build-history foot are
 * not in the picture. The page draws them and the browser check reads the
 * same list. Each label and each alt was written from its screenshot and
 * says only what the frame shows; `header` quotes the header text each
 * relies on. `elapsed` is the replay clock the frame's header prints, in
 * minutes since the epic's first frame; the live view has no clock and sits
 * at the end of the epic's replay (frame 28 of 28, +44h00m). Recapture and
 * reread the frames before changing a label. Plain JavaScript so the Node
 * scripts import the same list.
 */

/**
 * @typedef {{ x: number; y: number; width: number; height: number }} Clip
 * @typedef {{ file: string; path: string; elapsed: number; time: string; moment: string; alt: string; header: string }} Frame
 */

/** The desktop capture: a 1440x900 window, without the demo banner (41 px) and the foot (28 px). */
export const VIEWPORT = { width: 1440, height: 900 };
/** @type {Clip} */
export const CLIP = { x: 0, y: 41, width: 1440, height: 831 };
export const FRAME_WIDTH = CLIP.width;
export const FRAME_HEIGHT = CLIP.height;

/** The epic's replay runs 0 to +44h00m (frame 28 of 28); the live view is its end. */
export const END_MINUTES = 44 * 60;

/** @type {Frame[]} */
export const FRAMES = [
  {
    file: 'epic-t0.png',
    path: '/epics/ledger-v2?mode=replay&t=0&node=101',
    elapsed: 0,
    time: '+0h',
    moment: '#101 starts',
    alt: 'kew’s web console replaying the epic ledger-v2 at its first frame: none of 13 issues done, and #101, selected, in progress',
    header: '0/13 done · todo 12 · in progress 1 · next up 1 · +0h00m · frame 1 / 28 · Centred on #101 · selected'
  },
  {
    file: 'epic-t90600.png',
    path: '/epics/ledger-v2?mode=replay&t=90600&node=',
    elapsed: 25 * 60 + 10,
    time: '+25h',
    moment: '5 of 13 done',
    alt: 'kew’s web console replaying the epic ledger-v2: 5 of 13 issues done, one blocked, and no issue selected',
    header: '5/13 done · todo 7 · done 5 · blocked 1 · next up 5 · +25h10m · frame 18 / 28 · Centred on #113 · next to run'
  },
  {
    file: 'epic-t139380.png',
    path: '/epics/ledger-v2?mode=replay&t=139380&node=108',
    elapsed: 38 * 60 + 43,
    time: '+39h',
    moment: '#108 needs you',
    alt: 'kew’s web console replaying the epic ledger-v2: 5 of 13 issues done, and #108, selected, in magenta because its pull request is open and the decision is a person’s',
    header: '5/13 done · todo 4 · in review 2 · done 5 · failed 1 · blocked 1 · next up 2 · needs you 2 · +38h43m · frame 24 / 28 · Centred on #108 · selected'
  },
  {
    file: 'epic-live.png',
    path: '/epics/ledger-v2',
    elapsed: END_MINUTES,
    time: 'now',
    moment: 'the merge needs you',
    alt: 'kew’s web console on the epic ledger-v2 as it stands: 5 of 13 issues done and three that need a person, #107 and #108 among them as magenta cards; the selected #107’s pipeline, its implement run a success, its publish scan clean, review round 2 ready, and its merge waiting on a person; its evidence 4 of 4 attested; and a block that asks for a merge decision and offers to show the failed merge verify',
    header: '5/13 done · todo 3 · in progress 1 · in review 2 · done 5 · failed 1 · blocked 1 · next up 1 · needs you 3 · Centred on #107 · needs you'
  }
];

/** The frame shown first, and the one shown without script: the live view. */
export const DEFAULT_FRAME = 3;

/** The phone capture: the live view at 390x844, device scale 2, without banner and foot. */
export const PHONE = {
  file: 'epic-live-phone.png',
  path: '/epics/ledger-v2',
  viewport: { width: 390, height: 844 },
  scale: 2,
  /** @type {Clip} */
  clip: { x: 0, y: 88, width: 390, height: 715 },
  alt: 'kew’s web console on a phone, on the epic ledger-v2 as it stands: 5 of 13 issues done, three that need a person, and #107 in magenta, in review, needs you'
};

/** Below this width the page shows the phone capture and no rail. */
export const PHONE_MEDIA = '(max-width: 639.98px)';

/**
 * The live view's "needs you" block, whole, for the loop's stage 05. In a
 * 1440px window the block sits in a pane too short to show all of it, so it
 * is captured where the console lays it out at full height: a 390x844
 * window, device scale 2, the block scrolled into view and clipped to its
 * box with `margin` around it. `width` and `height` are that clip in CSS
 * pixels; the capture refuses a block of any other size, so a change in the
 * console shows up here before it reaches the page.
 */
export const JUDGMENT = {
  file: 'needs-you.png',
  path: '/epics/ledger-v2',
  viewport: { width: 390, height: 844 },
  scale: 2,
  margin: 8,
  width: 366,
  height: 257,
  alt: 'The needs-you block in kew’s web console, headed Needs you and Merge decision. Under Do this it offers a magenta control to see the failed merge verify and a control to open PR #207 to merge, and it says the merge verify failed at 69566afe, that 2 of main’s tests fail on this PR, and that the merge decision is the person’s.'
};

/** Where the frames are served from. */
export const FRAME_DIR = '/console/';

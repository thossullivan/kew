import assert from 'node:assert/strict';
import test from 'node:test';

import { AA, textContrast } from '../scripts/contrast.mjs';

const hex = (s) => ({
  r: parseInt(s.slice(1, 3), 16),
  g: parseInt(s.slice(3, 5), 16),
  b: parseInt(s.slice(5, 7), 16),
  a: 1
});
const CLEAR = { r: 0, g: 0, b: 0, a: 0 };
const BG = hex('#0d0221');
const MUTED = hex('#8b7fa8');

/** A text node's parent, a spine stage at `opacity`, then an opaque body. */
const stage = (opacity) => [
  { background: CLEAR, opacity: 1 },
  { background: CLEAR, opacity },
  { background: BG, opacity: 1 }
];

test('muted small print in a full-opacity stage reads AA', () => {
  const drawn = textContrast(MUTED, stage(1), BG);

  assert.ok(drawn.ratio >= AA, `${drawn.ratio}`);
});

test('an ancestor opacity dims the text as drawn, so a 0.6 stage fails AA', () => {
  const drawn = textContrast(MUTED, stage(0.6), BG);

  assert.ok(drawn.ratio < AA, `${drawn.ratio}`);
  assert.ok(Math.abs(drawn.ratio - 2.6) < 0.1, `${drawn.ratio}`);
});

test('an opacity on the text element itself counts too', () => {
  const chain = [{ background: CLEAR, opacity: 0.5 }, ...stage(1).slice(1)];

  assert.ok(textContrast(MUTED, chain, BG).ratio < AA);
});

test('a background inside the faded group fades with the text', () => {
  const card = hex('#1a0b2e');
  const chain = [
    { background: card, opacity: 0.4 },
    { background: BG, opacity: 1 }
  ];

  const drawn = textContrast(hex('#e0d8ea'), chain, BG);

  assert.ok(drawn.ratio < AA, `${drawn.ratio}`);
});

test('text an opacity of 0 hides is not measured', () => {
  assert.equal(textContrast(MUTED, stage(0), BG), null);
});

test('with no opaque background the page colour sits behind the text', () => {
  const chain = [{ background: CLEAR, opacity: 1 }];

  const drawn = textContrast(MUTED, chain, BG);

  assert.deepEqual(
    [drawn.bg.r, drawn.bg.g, drawn.bg.b].map(Math.round),
    [BG.r, BG.g, BG.b]
  );
  assert.ok(drawn.ratio >= AA);
});

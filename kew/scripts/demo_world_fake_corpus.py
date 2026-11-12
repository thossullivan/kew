"""Write the FAKE run-report corpus the committed demo timings are built from (#3141).

The sandbox that builds the demo world has no real corpus, so the committed
``frontend/demo/world/timings.json`` is aggregated from this one: seeded
reports with invented figures, shaped like real frontmatter (fresh input in
the hundreds, output in the tens of thousands, cache reads in the millions).
``ci_fix`` gets fewer than ``MIN_RUNS`` reports on purpose, so the committed
world exercises the floor: that kind publishes nothing and the generator
draws its fixed synthetic default instead. From ``kew/``::

    uv run python scripts/demo_world_fake_corpus.py "$TMPDIR/fake-runs"
    uv run python scripts/demo_world_timings.py --corpus "$TMPDIR/fake-runs"
    node ../frontend/demo/world/generate.mjs

At release the operator runs ``demo_world_timings.py`` without ``--corpus``,
which reads the real corpus from the configured sink path.
"""

from __future__ import annotations

import argparse
import math
import random
from pathlib import Path

SEED = 3141

#: Per kind: report count, median duration (s), cost ($), input, output and
#: cache-read tokens, and the share that fail. Invented, not measured.
SHAPES: dict[str, tuple[int, float, float, float, float, float, float]] = {
    "dispatch": (60, 1550, 3.3, 260, 41_000, 5_200_000, 0.15),
    "adversarial_review": (50, 1070, 1.3, 120, 14_000, 2_100_000, 0.0),
    "review_remediation": (20, 900, 1.6, 180, 22_000, 2_600_000, 0.05),
    "ci_fix": (10, 860, 1.1, 150, 12_000, 1_700_000, 0.1),
}


def _report(rng: random.Random, kind: str, index: int) -> str:
    _, duration, cost, fresh, output, cached, fail = SHAPES[kind]

    def drawn(median: float) -> float:
        return median * math.exp(0.35 * rng.gauss(0.0, 1.0))

    tokens = [round(drawn(m)) for m in (fresh, output, cached)]
    fields = {
        "run_id": f"fake-{kind}-{index:03d}",
        "run_kind": kind,
        "status": "failure" if rng.random() < fail else "success",
        "cost_usd": round(drawn(cost), 6),
        "duration_seconds": round(drawn(duration), 2),
        "input_tokens": tokens[0],
        "output_tokens": tokens[1],
        "cache_read_tokens": tokens[2],
        "total_tokens": sum(tokens),
    }
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items()) + "---\n"


def write_corpus(root: Path) -> int:
    """Write the fake corpus under *root*; returns the report count."""
    rng = random.Random(SEED)
    count = 0
    for kind, shape in SHAPES.items():
        for index in range(shape[0]):
            path = root / "acme--ledger" / f"fake-{kind}-{index:03d}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_report(rng, kind, index), encoding="utf-8")
            count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dir", type=Path)
    args = parser.parse_args(argv)
    print(f"{write_corpus(args.dir)} fake reports -> {args.dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

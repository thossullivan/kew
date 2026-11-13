# Offloading, not surrender

**The failure mode of AI coding isn't bad code. It's that you stop reading it.**

When an agent hands you a diff that compiles, passes the linter, and looks done, the cheapest thing your brain can do is accept it. Do that enough and something measurable happens: your accuracy starts tracking the model's. When it's right, you're right. When it's wrong — and it will be — you're wrong with it, and you feel just as confident either way.

There's a line here that most tools blur on purpose.

**Cognitive offloading** is delegating the *how* while you keep judgment over the *what*. You hand off a bounded task and stay ready to intervene — the way you offload arithmetic to a calculator without forgetting how numbers work.

**Cognitive surrender** is when you stop constructing an answer at all and adopt the model's. You're not reviewing anymore; you're ratifying. Wharton researchers who [named the term](https://addyosmani.com/blog/cognitive-surrender/) measured the cost: with an AI assistant, accuracy rose 25 points when it was right and fell 15 when it wasn't — and confidence climbed regardless. Addy Osmani calls the residue **comprehension debt**: code living in your system that no human actually understands, compounding quietly until the day it fails.

His sharpest point is the one that matters for a tool: **the posture matters more than the tool.** Two engineers, same model — one offloading, one surrendering — and the only real difference is whether their understanding *grows* or *shrinks* while they ship.

But posture isn't only willpower, because **tools are built to encourage one posture or the other.** Autocomplete you tab through without reading. Agents that merge their own pull requests. "Looks done" as an exit criterion. These optimize for frictionless acceptance — they are, by design, surrender machines. Asking people to summon discipline against a tool engineered for the opposite is a losing bet.

## kew takes the other side of that bet

kew is built so that delegating work produces something you have to look at — and so the looking is the default path, not an act of discipline.

You scope the work. A well-defined issue is the *what*, the part only you can own. Agents do the *how* — under budgets, a security boundary, and an audit log. And what comes back is never a silent merge. It's a **pull request**: a bounded, reviewable artifact you read by hand, or hand to a *fresh* agent to review — one with no stake in having written it.

Everything people might call kew's "overhead" is actually the architecture of *not* surrendering:

- **One scoped issue at a time → a small PR.** Sized to what a human can hold in their head — the opposite of an open-ended session that outruns your comprehension.
- **A PR, not a merge.** The review boundary is structural. The decision to ship stays a decision.
- **Fresh review, opt-in.** The [governed adversarial-review stage](how-it-works.md#governed-adversarial-review) (`[review].adversarial = true`, off by default) puts a fresh-context reviewer on an epic node's PR. [`kew review`](cli/review.md) does something else: it dispatches an agent to address the review comments already on a PR.
- **Approval gates.** Spend or risk past a threshold *holds* for a human. You don't surrender the call to a budget you never saw.
- **A kill switch.** [`kew stop`](cli/stop.md) halts the fleet. Control is never theoretical.
- **A Flight Recorder + a tamper-evident audit log.** Each run's web page shows
  its recorded evidence. The audit log records who acted under which policy.

None of this is bureaucracy. It's the machinery that keeps the human in the decision-making role: *whose cognition does this decision reflect?* With kew, the answer stays **yours**.

**Offload the work. Keep the judgment.**

---

*Further reading: Shaw & Nave, "Thinking—Fast, Slow, and Artificial: How AI is Reshaping Human Reasoning and the Rise of Cognitive Surrender" (The Wharton School); Addy Osmani, ["Cognitive Surrender"](https://addyosmani.com/blog/cognitive-surrender/).*

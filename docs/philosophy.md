# Offloading, not surrender

**Bottom line up front:** the failure mode of AI coding isn't bad code. It's
that you stop reading it. I built kew so that handing work to an agent gives you
something you have to look at, a pull request, and so the looking is the
default path rather than an act of discipline.

When an agent hands you a diff that compiles, passes the linter and looks done,
the cheapest thing your brain can do is accept it. Do that enough and something
measurable happens: your accuracy starts tracking the model's. When it's right,
you're right. When it's wrong (and it will be), you're wrong with it, and you
feel just as confident either way.

Most tools blur a line here, so it's worth naming both sides of it.

**Cognitive offloading** is handing off the *how* while you keep judgment over
the *what*. You delegate a bounded task and stay ready to step in, the way you
offload arithmetic to a calculator without forgetting how numbers work.

**Cognitive surrender** is when you stop constructing an answer at all and adopt
the model's. You're not reviewing any more; you're ratifying. Wharton
researchers who [named the term](https://addyosmani.com/blog/cognitive-surrender/)
measured the cost: with an AI assistant, accuracy rose 25 points when it was
right and fell 15 when it wasn't, and confidence climbed regardless. Addy Osmani
calls the residue **comprehension debt**: code living in your system that no
person actually understands, compounding quietly until the day it fails.

His sharpest point is the one that matters for a tool: **the posture matters
more than the tool.** Take two engineers with the same model, one offloading
and one surrendering. The only real difference is whether their understanding
grows or shrinks while they ship.

But posture isn't only willpower, because **tools are built to encourage one
posture or the other.** Autocomplete you tab through without reading. Agents
that merge their own pull requests. "Looks done" as an exit criterion. These
optimize for frictionless acceptance; by design, they are surrender machines.
Asking people to summon discipline against a tool built for the opposite is a
losing bet.

## kew takes the other side of that bet

I've spent fifteen years building data and AI systems in industries where a
wrong answer costs real money: pharma, consumer finance and insurance. A rule
kew is built on will be familiar to anyone from
that world: the author of a change does not approve it. With kew the author is
an agent, and kew treats it as an untrusted author.

You scope the work. A well-defined issue is the *what*, the part only you can
own. Agents do the *how*, under budgets, a security boundary and an audit log.
What comes back is never a silent merge. It's a **pull request**: a bounded,
reviewable artifact you read by hand, or hand to a *fresh* agent to review (one
with no stake in having written it). On kew's own repository, the reviewer judged
634 of the pull requests that later merged, from July 2026 on, and sent 351 of
them (55.4%) back for changes at least once first.

What people might call kew's overhead is the architecture of not surrendering:

- **One scoped issue at a time, one small PR.** Sized to what a person can hold
  in their head, the opposite of an open-ended session that outruns your
  comprehension.
- **A PR, not a merge.** The review boundary is structural. The decision to
  ship stays a decision.
- **Fresh review, opt-in.** The [governed adversarial-review stage](how-it-works.md#governed-adversarial-review)
  (`[review].adversarial = true`, off by default) puts a fresh-context reviewer
  on an epic node's PR. [`kew review`](cli/review.md) does something else: it dispatches an agent to
  address the review comments already on a PR.
- **Approval gates.** Spend or risk past a threshold *holds* for a person. You
  don't surrender the call to a budget you never saw.
- **A kill switch.** [`kew stop`](cli/stop.md) halts the fleet. Control is never
  theoretical.
- **A Flight Recorder and a tamper-evident audit log.** Each run's web page
  shows its recorded evidence. The audit log records who acted under which
  policy.

None of this is bureaucracy. It's the machinery that keeps a person in the
deciding seat. The test I hold each part of kew to is one question: *whose
cognition does this decision reflect?* With kew, the answer stays **yours**.

**Offload the work. Keep the judgment.**

---

*Further reading: Shaw & Nave, "Thinking—Fast, Slow, and Artificial: How AI is Reshaping Human Reasoning and the Rise of Cognitive Surrender" (The Wharton School); Addy Osmani, ["Cognitive Surrender"](https://addyosmani.com/blog/cognitive-surrender/).*

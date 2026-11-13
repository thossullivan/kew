# Prompt profiles

Every agent kew launches runs a prompt. This page covers which prompt each
role runs, how an operator adds to it, what kew records about it, and why a
contained agent cannot change it.

## Roles and their base prompts

Each agent role has one **base prompt**. It ships with kew and is reviewed
like any other code. `kew prompts list` and the console's **Prompts** page
(`/prompts`) list every role and where its base comes from.

| Role | Run kinds | Base prompt | Profile may apply |
| --- | --- | --- | --- |
| `dispatch` | `dispatch`, `loop`, `delegation` | packaged template `dispatch.md.j2` | yes |
| `adversarial_review` | `adversarial_review` | pinned artifact `adversarial-reviewer` | yes |
| `review_remediation` | `review_remediation`, `review` | packaged template `review.md.j2` | yes |
| `resolve` | `resolve` | packaged template `resolve.md.j2` | yes |
| `ci_fix` | `ci_fix` | packaged template `ci-fix.md.j2` | yes |
| `triage` | `triage` | packaged template `triage.md.j2` | no |
| `planning` | `planning` | packaged template `epic-plan.md.j2` | no |
| `acceptance_author` | `acceptance_author` | built in code | no |
| `loop_continuation` | `loop` | built in code | no |
| `readiness_probe` | `readiness_probe` | built in code | no |

The packaged templates live in `kew/src/kew/_templates/`. The adversarial
reviewer's base is the pinned artifact `kew/src/kew/_skills/adversarial-reviewer/PROMPT.md`.
A project that deploys its own copy of that artifact runs that copy, the same
way it does at review launch.

## A profile is an addendum, never a replacement

A **profile** is text an operator adds to one role's base prompt. It can never
replace the base. A role runs its base plus at most one active addendum. The
addendum renders as one block with this heading:

```text
## Operator instructions — <role> profile v<N>
```

The fixed safety and publication sections render **after** the addendum, and
each one says it takes precedence over it. In the reviewer's prompt, the
host-owned diff boundary and runtime guidance follow the addendum and say the
same.

The body is plain operator instructions. kew refuses a proposal that:

- is empty, or longer than 4000 characters once whitespace and kew's reserved
  fence markers are stripped;
- has a line that could open a Markdown block (a ```` ``` ```` or `~~~` fence,
  or `<` at the line start). The refusal names the line. Inline
  `` `code` `` is allowed, and so is an `<!--` after other text on a line.
  This rule keeps a paste mistake from wrapping the safety rules in a code or
  HTML block.

While a profile is active, a custom `[prompt] template` is refused, because
only the built-in prompt renders the addendum. See
[`[prompt]` and profiles](configuration.md#prompt-config-and-profiles).

## Propose and approve

A profile version moves through these states: `proposed`, then `active` or
`declined`, and an active version is later `retired`. Approving a new version
retires the one it replaces. A proposal changes nothing a role runs until an
approver activates it.

Each proposal and each decision needs an actor on `[approval].approvers`. The
list is empty by default, and while it is empty kew refuses every proposal and
decision: "Set [approval].approvers in kew.toml before deciding prompt
profiles." Releasing a cost hold keeps its own rule: on the kew host an empty
list admits any actor to `kew approve` and `kew deny`, while through `/approve`
comments the `approval.yml` workflow admits a collaborator with repo write access,
and a non-empty list narrows those comment approvals too (see
[`kew approve`](cli/approve.md)). The acting
name is `KEW_ACTOR`, or the OS user if that is unset. kew takes that name as
given, so the list limits who may decide only among people who run kew as
themselves. A refused command writes nothing.

Approve and retire take the role's **generation**: the number you saw when you
read the role. If another decision changed the role since then, kew refuses the
command as stale and changes nothing. Read the role again, then decide again.
Over the API a stale decision answers 409, and the console then locks every
profile write on the page until you reload it, so no control acts on a state
you no longer see.

The console's approver gate is the `kew serve` process's actor (`KEW_ACTOR`,
else the OS user that started it), not each signed-in viewer, so every session
on one server gets the same answer; see the
[single-operator note](web/index.md#security-posture). When that actor is not an
approver, the console hides Approve and Decline on a pending version, still
shows the Propose box with its Propose button disabled and a disabled
"Retire vN" for the active version, and says once beside them why. Approving a version older than the active
one replaces newer text with older, so the console asks you to confirm first:
"v1 is older than the active v2. Approve it anyway?"

From the console, open **Prompts**, pick a role, and use its profile editor to
propose a version, or to approve, decline or retire one. The preview renders
the role's prompt over a sample item, never a real one, with the active profile
in place and, for `dispatch`, the served `[prompt]` configuration. It shows the
shape a launch builds, not the exact prompt: the item, the repo map and the
launch's admission checks (containment, credentials) are the launch's own.
When the launch would refuse, the page says why: a refused profile beside the
base prompt, and a `[prompt] append` or `template` under host publication in
place of a prompt. The line above a preview says which configuration rendered
it: the served `[prompt]`, or none when the base stands in for a refused
profile.

From the CLI ([`kew prompts`](cli/prompts.md)):

```bash
kew prompts list
kew prompts propose dispatch --file dispatch-addendum.md
kew prompts show dispatch                      # read v1 and note the generation
kew prompts approve dispatch 1 --generation 0
kew prompts decline dispatch 2
kew prompts retire dispatch --generation 1
```

## What is recorded

Profiles live only in the operator's `dispatch.db`, in the
`prompt_profile_versions` table. Each row keeps the body and the sha256 it was
proposed with. kew never reads a profile from a checkout, a worktree, an
environment variable or `kew.toml`.

- **Every decision** is a chained audit event: `prompt_profile_proposed`,
  `prompt_profile_activated`, `prompt_profile_declined` or
  `prompt_profile_retired`. The event names the role, the version, the
  body's sha256 and the actor. It commits in the same transaction as the
  state row, and is written first, so a failed write leaves neither. See the
  [audit log](audit.md).
- **Every run** records the profile version it ran under in
  `runs.prompt_profile_version_id`, or null when it ran its bare base.
- **Every adversarial review verdict** records `prompt_profile_version_id` in
  its attested payload, next to `prompt_artifact_sha` (the base artifact) and
  `effective_prompt_sha` (the sha256 of the whole text the reviewer received,
  addendum included). The review round records it too.

At launch, the active body must still match the sha256 it was approved with,
and must still pass the block-opener rule. If either check fails, the launch is
a `prompt_configuration_refused` and the profile never renders. Retire the
profile and propose it again.

## Replacing a base prompt

A profile cannot replace a base. To change a base prompt, open a pull request
that edits the file the role runs. It goes through the same review and merge
as any other change.

- A packaged template: edit it in `kew/src/kew/_templates/`, in kew.
- The adversarial reviewer, when the project deploys no copy of its own: edit
  the pinned artifact `kew/src/kew/_skills/adversarial-reviewer/PROMPT.md` and
  its pin, in kew.
- The adversarial reviewer, when the project deploys
  `.claude/kew/adversarial-reviewer.md` at the top of its Git repository: edit
  that file and merge the change. The path is the repository's top even when
  the kew project sits in a subdirectory of it, and it must be a regular file,
  not a symlink. The reviewer reads the file as committed on the project's
  base branch (`branches.base`, `main` when unset) on `origin`, out of Git:
  the same commit the review diffs the PR against. It never reads
  the working tree, the operator's local base branch or the branch under
  review. A PR cannot change the reviewer that judges it, and an uncommitted
  edit, an unpushed commit, or another branch checked out in the operator's
  checkout, changes nothing until it is merged. The prompt preview reads the
  same branch as last fetched from `origin` (it does not fetch) and records the
  base ref and the commit it was read at. The
  deployed copy wins over the packaged one, so editing the packaged
  artifact changes nothing for that project.

## Why a contained agent cannot change a profile

An agent works in a checkout, and the issue it works from is
attacker-reachable. For an agent in a container, neither can reach a profile:

- kew reads profiles only from the host's `dispatch.db`, never from the
  checkout, the worktree, the environment or `kew.toml`. Nothing an agent
  writes into its workspace is read as a profile.
- Profiles are written only through kew's service layer, behind the approver
  gate. A proposal or decision from an actor outside `[approval].approvers`,
  or made while that list is empty, is refused before anything is written.
- Every write is a chained audit event, so a change made outside the service
  is visible. A body that no longer matches its approved sha256 is refused at
  launch.

Containment is what keeps an agent away from `dispatch.db`. An agent that runs
on the host, uncontained, has the operator's privileges and can edit the
ledger, as [Security](security.md#checkout-binding-boundary) describes. Such
an agent could rewrite an active row's body and sha256 together, and the launch
checks would accept it. The audit chain would still show that no service
decision made the change.

A profile is therefore only as trustworthy as every launch of every
profile-enabled role: each must run `sandboxed` or `native`. The rule is the
posture each launch records in its run and audit events. Any launch that
records `uncontained_flow` or another host posture ran with the operator's
privileges. That can happen in more ways than one setting. Some examples, not
a complete list:

- `[sandbox] enabled = false`. Review remediation and resolve fall back to the
  host even without `dangerously_skip_containment`.
- `allow_uncontained_flows = true`.
- `enabled = "auto"` with a runner that cannot be contained, or with a missing
  runtime, image, credential or certification, for the flows that policy
  permits.

[Security](security.md) describes each route's containment. Before you rely
on profiles, check that the recent runs of every profile-enabled role recorded
`sandboxed` or `native`.

See [Security](security.md#untrusted-content-in-agent-prompts) for how a
profile sits beside the untrusted content in a prompt.

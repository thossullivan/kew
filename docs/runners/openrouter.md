# OpenRouter Models

[OpenRouter](https://openrouter.ai) is a gateway that puts hundreds of models from many vendors (Qwen, DeepSeek, Mistral, Anthropic, OpenAI, Google, …) behind a single API key. Kew dispatches to OpenRouter through the [OpenCode runner](opencode.md), so any model in OpenRouter's catalog is one `model =` line away — no per-vendor accounts or keys.

## Setup

1. Install the OpenCode CLI — see https://opencode.ai/docs/.
2. Create an OpenRouter account at https://openrouter.ai.
3. Create an OpenRouter management key at [Management Keys](https://openrouter.ai/settings/management-keys).
   Set it as `OPENROUTER_PROVISIONING_KEY` in the host environment.
   Every direct OpenRouter run requires it before launch, on the host or in a container.
   Kew uses it to issue [bounded per-run keys](../cli/sandbox.md#bounded-openrouter-credentials).
   A plain `OPENROUTER_API_KEY` or `opencode auth login` does not satisfy this requirement.
   Kew removes an ambient `OPENROUTER_API_KEY` from host and contained child environments.
   Their host boundary is the operator's own machine.
   Missing provisioning causes `OpenRouter provisioning key required for direct routes`, exit 2, and reservation release.
   The run row records `refused`.
   Every container environment removes the ambient `OPENROUTER_API_KEY` by name before explicit bounded-key injection.
   Direct OpenRouter epic and delegation runs also require provisioning in their parent process.
   Switchyard routes, including OpenRouter behind a sidecar, retain their separate upstream credential owner.
   Children receive bounded inference keys without provisioning authority.

4. Configure your OpenRouter account's data policy — see the next section — **before** dispatching real code.

Issued keys use the `issued:openrouter:<hash>` authentication family.
Launch requires a `sandbox_resolved` issuance event for the same run id and attempt.
Its `credential_issuer` identity must contain issuer `openrouter` and the matching `key_hash`.
Missing or mismatched issuance evidence refuses launch with `issued_key_evidence_missing`.
The publication gate checks the same issuance evidence again.
Canonical and checkout publication require revoke-before-publish evidence: a matching revocation event with `credential_issuer.revoked=true`.
Missing or failed revocation holds publication with `issued_key_unrevoked` and the issuer's reason.
Missing revocation evidence uses reason `revocation evidence missing`.
A later retry releases publication after successful revocation evidence arrives.
The publication gate reads evidence; it never calls the issuer.

The host-authenticated issuance record itself is the durable revocation obligation.
Recorded issuance survives a crash before the first DELETE marker.
Before POST, `mint_run_credential` commits an issuance INTENT in the existing meta rows.
The `run_key_intent:<run_id>` row records run id, deterministic key name, requested limit, expiry, issuing pid, and timestamp.
New deterministic names use `kew-run:<ledger_id>:<run_id>`.
The ledger ID is the canonical UUID bound in the store’s `ledger_identity` table.
Recovery uses the exact name stored before issuance.
The issuer reply's `data.name` must exactly equal the qualified name persisted in the issuance intent.
Missing, unqualified, differently qualified, and foreign-ledger names raise `MalformedIssuerReply` before any credential reaches a caller.
Malformed replies with validated hashes enter durable revocation before the error propagates.
Cleanup records confirmed deletion or absence; unconfirmed outcomes remain pending for hash-based retries.
Issuance without a bound ledger identity is refused with `issuer refused`.
After the response, kew writes the pre-launch marker, records issuance evidence, then settles the intent.
Issuance evidence uses a strict append-and-readback operation before kew yields any issued key.
Readback must match the event ID, run ID, host actor, key hash, bounds, and secret fingerprint.
Missing or mismatched evidence raises `IssuanceEvidenceRefused` before review or producer launch.
The run is refused, the operator receives a safe reason, and the intent remains pending for exact-name recovery.
A crash before the marker leaves the intent open, even when POST never happened.
Recovery waits until the requested expiry before looking up an unresolved issuance.
A pending run row remains protected.
Reconciliation scans one bounded page of intents alongside the lifecycle scan.
Recovery uses paginated management GET requests, includes disabled keys, and matches the exact key name.
Kew does not assume an undocumented idempotency API or replay POST.
Matching keys require DELETE with confirmed deletion or absence before settlement.
Absence settles an intent only after a negative lookup strictly after the requested expiry.
A local inactivity timeout does not cancel server work or prove list consistency.
Before expiry, negative lookups keep the intent pending.
Retry delays grow through 30, 60, 120, 240, and 300 seconds, then remain capped.
Unsupported or failed lookup leaves the intent open and emits one warning naming the run.
Expiry alone never proves deletion.
The issuer rejects use after `expires_at`; a credential materializing after that bound is unusable.
This relies on the issuer's expiration rule, not a client cancellation assumption.
See [OpenRouter's expiration field](https://github.com/OpenRouterTeam/terraform-provider-openrouter/blob/main/docs/data-sources/api_keys.md).

Each reconciliation pass also lists issuer entries with the `kew-run:` prefix, including disabled entries.
The sweep waits 300 seconds after issuer `created_at` before deleting an unowned entry.
Pending intents, matching pre-launch markers, and matching issuance records preserve local ownership.
Ownership requires the listed name to equal the persisted qualified name; matching hashes alone do not establish ownership.
A closed absence intent cannot protect a later-visible entry.
The sweep uses the same durable deletion retries and confirmed revocation events as normal recovery.
Malformed list metadata never authorizes deletion.
Projects may share one management account because each ledger has its own namespace.
Sweeps and intent recovery act only within the current ledger’s namespace.
Foreign, unqualified, and malformed names are skipped before ownership checks.
Each sweep logs one skipped count at debug level without credential names.

Generic children always lose both OpenRouter credentials, including before any bounded scope exists.
This rule covers discovery, control, git, gh, container control, ps, and timeout calls.
Host agent launches also remove ambient OpenRouter credentials.
A timeout wrapper executing that agent belongs to the same launch boundary.
Only explicit issued credentials enter these child environments.
Contained agent and sidecar launchers inject only their explicitly issued inference credential.
Management authority never enters a child environment.
Retained image, smoke, and staleness diagnostics redact credential values before truncation.
Recovery requires a terminal run row or explicit terminal evidence for an absent row.
Absence of a run row is never treated as terminal by itself.
The pre-launch window starts when the parent prepares a key and ends when the child writes its pending row.
Before issuance evidence, `mint_run_credential` commits a durable `run_key_prelaunch` marker in the existing meta rows.
The marker records run id, key hash, issuing process pid, issuance time, and `expires_at`.
The pending row committed by `save` or `try_admit_run` supersedes the marker.
A first terminal row also supersedes the marker and permits reconciliation.
The retained marker protects the key if someone later removes its run row without terminal evidence.
Driver cleanup closes prepared-but-unspawned keys, including Popen and environment-construction failures.
Cleanup writes a pre-launch tombstone before DELETE and records the confirmed outcome.
Successful Popen transfers key ownership to the child; driver exit preserves that key before admission.
For absent rows, recovery requires a reaping tombstone, a pre-launch tombstone, or key expiry.
A hard driver death before Popen leaves the marker protected until expiry.
After five minutes, one operator warning lists the waiting run, key hash, issuing pid, and expiry.
Skipped pre-launch keys also produce debug logs; reconciliation never probes process liveness.
Recovery never revokes a pending run, even after key expiry.
Each DELETE first persists a durable `revocation_pending` marker and a safe reason.
The shared reconciler handles foreground keys and driver-issued keys.
Driver ticks, driver exit, and delegation watcher passes run the same reconciler.
Foreground credential finalization and publication retries also use this machinery.
An `issued_key_unrevoked` publication retry first reconciles the issued key, then checks the evidence again.
The check covers every producer attempt and ancestor run.
Predecessor publication jobs use the same reconciliation path.
Each pass attempts at most one DELETE per key hash.
Retry delays start at 30 seconds, double, and stop increasing at 300 seconds.
The durable retry counter saturates at 32; retries continue until the issuer confirms deletion.
Repeated failures retain the pending reason without filling the publication evidence window.
An HTTP 200 response must contain `deleted=true`; HTTP 204 also confirms deletion.
HTTP 404 or an explicit key-absence response records `revoked=true` with reason `already absent`.
Timeouts, network errors, HTTP 429, HTTP 5xx, and malformed responses remain pending.
HTTP 401 or 403 records `invalid provisioning credential` and emits one operator warning per key.
Replace `OPENROUTER_PROVISIONING_KEY` in the host process environment, then retry publication or restart the driver.
Other refused responses remain pending with the safe issuer reason.
Key expiry alone never releases publication.
After expiry, confirmed absence uses the same `already absent` path and releases the hold.

Raw ambient `env:` credentials publish only a head the host scanned clean immediately before the push.
See [Publication output scan](../security.md#publication-output-scan).

## Detached credential handoff

The parent passes `--credential-route <digest>` with `--run-id` to each detached child.
The SHA-256 digest covers the resolved runner, model, transport, sidecar flag, and bounded-key decision.
Transport distinguishes direct inference, native authentication, and sidecar inference.
External Switchyard transport retains its separate transport name.
The parent records `credential_route_digest` in the issuance or exclusion event.
The child records the same digest in its admission evidence.
The child checks the digest before runner preflight and at the actual launch resolution.
Changed labels or routing overrides cause `credential route changed`, exit 2, and reservation release before launch.
An excluded handoff keeps only the resolved route's required credential names; the names are part of the route digest.
Excluded handoffs remove all other inference names, the issued-key marker, and management credentials.
Native Claude selects one authentication mode from the parent's environment.
The route evidence records `claude_auth_mode`; its value and credential names enter the digest.
A parent/child mode mismatch causes `credential route changed` before launch.
Only `1` or `true` enables a cloud selector, ignoring case.
Kew refuses conflicting Bedrock and Vertex selectors.

| Claude mode | Retained environment names |
|---|---|
| Anthropic API | `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_BASE_URL` |
| Bedrock | `CLAUDE_CODE_USE_BEDROCK`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION`, `AWS_DEFAULT_REGION`, `AWS_PROFILE`, `AWS_BEARER_TOKEN_BEDROCK`, `ANTHROPIC_BEDROCK_BASE_URL` |
| Vertex | `CLAUDE_CODE_USE_VERTEX`, `GOOGLE_APPLICATION_CREDENTIALS`, `CLOUD_ML_REGION`, `ANTHROPIC_VERTEX_PROJECT_ID`, `GOOGLE_CLOUD_PROJECT`, `ANTHROPIC_VERTEX_BASE_URL` |

These sets follow Claude Code's [environment reference](https://code.claude.com/docs/en/env-vars).
Cloud names follow its [Bedrock](https://code.claude.com/docs/en/amazon-bedrock) and [Vertex](https://code.claude.com/docs/en/google-vertex-ai) guides.
Kew already uses `ANTHROPIC_BASE_URL` for endpoint routing.
No keep set includes management or publication credential names.
These handoffs do not mount cloud credential files into containers.

Native Codex keeps no inference credentials and uses its own login.
Switchyard keeps its resolved upstream `credential_env_names`, which its supervisor validates against the route configuration.
These names can include `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY`.
Credential values are never part of the digest.
A detached direct OpenRouter child requires a verified issued key.
Missing or invalid issued credentials cause `issued credential mismatch` and exit 2.
There is no fallback to the operator's `OPENROUTER_API_KEY`.
The child identifies detached execution from `--credential-route` or `--run-id`.

## Data policy prerequisites

OpenRouter routes each request to one of several **underlying providers** hosting the model you asked for, and those providers have varying data retention and training policies: some retain prompts for a period, some may train on inputs unless routing excludes them, and some offer zero-data-retention (ZDR) endpoints. See OpenRouter's [provider logging documentation](https://openrouter.ai/docs/guides/privacy/provider-logging) for the current per-provider details.

Since dispatched runs send your repository's code as prompt content, review these before pointing kew's fleet at OpenRouter:

1. **Set the account-level training opt-out.** In your OpenRouter [privacy settings](https://openrouter.ai/settings/privacy), disable model training. OpenRouter then restricts routing to providers that honor that policy.
2. **Optionally pin routing to specific providers or ZDR.** OpenRouter accepts per-request provider routing preferences (provider order, disallowing fallbacks, ZDR-only), and OpenCode can pass them through its provider config in `opencode.json`. For example, to pin a model to specific providers:

   ```json
   {
     "provider": {
       "openrouter": {
         "models": {
           "qwen/qwen3-coder-next": {
             "options": {
               "provider": {
                 "order": ["Fireworks"],
                 "allow_fallbacks": false
               }
             }
           }
         }
       }
     }
   }
   ```

   See OpenRouter's [provider routing documentation](https://openrouter.ai/docs/features/provider-routing) for the available fields (including ZDR enforcement) and [OpenCode's config docs](https://opencode.ai/docs/config/) for where `opencode.json` lives.

3. **Enterprise / EU residency.** OpenRouter's enterprise plan offers an EU in-region base URL so requests are processed on in-region infrastructure. If you have data-residency requirements, that option exists — talk to OpenRouter.

None of this is unique to OpenRouter — any hosted model has a data policy — but because OpenRouter fans out across many providers, the account-level settings above are the difference between "one policy you chose" and "whichever provider happened to serve the request".

## Selecting a model

Use the three-segment form `openrouter/<vendor>/<model>` anywhere kew accepts a model — the `openrouter/` prefix routes to the OpenCode runner, and multi-segment paths are supported:

```toml
[agent]
model = "openrouter/qwen/qwen3-coder-next"
```

Or route by complexity tier:

```toml
[routing]
default = "claude-code"
simple  = { runner = "opencode", model = "openrouter/qwen/qwen3-coder-next" }
```

## Cost semantics

OpenRouter pricing is consumption-based and varies **per provider** — the same model can cost more or less depending on which underlying provider serves the request. Kew handles this in two layers:

- **Preflight estimates** are priced from the model catalog kew ships (`kew/src/kew/models.json`), a committed pricing snapshot, so budget gates evaluate against known list prices rather than a live lookup. [`kew models refresh`](../cli/models.md) compares its prices with the OpenRouter models API and prints a report; only `kew models refresh --write` rewrites the catalog, and a refresh never adds a model the catalog lacks.
- **Actual spend** is runner-reported and recorded per run, as with every other runner — see [cost & honesty across runners](index.md#cost-honesty-across-runners).

Governed dispatch of a model that neither the catalog nor a `[pricing]` override prices **fails closed by design**: an unpriceable model does not pass the budget gate. That is deliberate — no spend kew can't bound. To dispatch a model the catalog lacks, price it with a `[pricing]` override (`overrides = { "model-id" = [input, output] }`, dollars per million tokens) — see [Configuration](../configuration.md).

## Next

- [OpenCode runner](opencode.md) — the runner OpenRouter dispatch goes through, including resume support and limitations.
- [Configuration](../configuration.md) — the `[routing]` and `[agent.opencode]` sections.

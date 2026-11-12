/**
 * #2210 — one readable line per audit event.
 *
 * `/api/audit` serves each event's payload as a JSON excerpt
 * (`_summary` in kew/src/kew/service/audit.py, capped at 200 characters), and
 * the Monitor printed it verbatim: rows reading
 * `dispatch_refused · {"reason":"budget_exceeded","scope":"weekly","estimate_usd":…`
 * (owner review, 2026-09-29). The line is now the event kind in words, the
 * epic/item/PR it names, and the one fact worth the row — with what the log
 * actually recorded kept for the row's expand, never dropped.
 */
import { describe, expect, it } from 'vitest';
import auditJson from '../../../design/factory-ui/DMonitor.events.json';
import type { AuditEvent } from '$lib/api';
import { eventLine, eventWords } from '$lib/monitor/event-line';

const event = (over: Partial<AuditEvent> = {}): AuditEvent => ({
  id: 1,
  timestamp: '2026-09-24T15:46:58+00:00',
  event: 'dispatch_completed',
  issue_num: null,
  item_id: null,
  run_id: null,
  actor_type: 'host',
  actor_id: 'kew',
  summary: null,
  epic_ref: null,
  ...over
});

/** The whole line, as the row prints it. */
const line = (over: Partial<AuditEvent> = {}) => eventLine(event(over)).line;

describe('the event kind', () => {
  it('reads in words, not in identifier case', () => {
    expect(eventWords('publication_output_scan')).toBe('publication output scan');
    expect(line({ event: 'dispatch_refused' })).toBe('dispatch refused');
  });

  it('leads with the shared worded name, so a dotted type is not "Review.outcome"', () => {
    expect(eventLine(event({ event: 'review.outcome' })).label).toBe('Review outcome');
    expect(eventLine(event({ event: 'routing_decision' })).label).toBe('Route chosen');
  });
});

describe('what the line names', () => {
  it('names the item the event carries, as the app writes a ref', () => {
    expect(line({ issue_num: 2055 })).toBe('dispatch completed · #2055');
    expect(line({ summary: '{"item_id":"SFD-12"}' })).toBe('dispatch completed · SFD-12');
  });

  it('names the epic, the item and the PR in one order', () => {
    expect(
      line({
        event: 'adversarial_review_verdict',
        issue_num: 2055,
        summary: '{"epic_ref":"go-live","pr_number":2059,"verdict":"READY"}'
      })
    ).toBe('adversarial review verdict · go-live · #2055 · PR #2059 · READY');
  });

  it('reads a PR the payload named only by URL', () => {
    expect(line({ summary: '{"pull_request_url":"https://github.com/o/r/pull/77"}' })).toBe(
      'dispatch completed · PR #77'
    );
  });

  it('prefers the payload item over the event column, and never prints both', () => {
    expect(line({ issue_num: 2055, summary: '{"issue":2055}' })).toBe(
      'dispatch completed · #2055'
    );
  });

  it('names the provider and address for a host lease event', () => {
    const read = eventLine(
      event({
        event: 'host_lease_takeover_unconfirmed',
        summary:
          '{"host":"gcp","address":"agent-expired-1","resource":"gcp/proj/zone/agent-expired-1"}'
      })
    );
    expect(read.label).toBe('Lease takeover not confirmed');
    expect(read.subject).toBe('gcp · agent-expired-1');
  });
});

describe('the printed fact (#2896 R5)', () => {
  it('words the values a status move recorded, and keeps the codes as the fact', () => {
    const read = eventLine(
      event({
        event: 'epic_node_status',
        summary: '{"from":"in_review","to":"adversarial_review"}'
      })
    );
    expect(read.plain).toBe('in review → adversarial review');
    expect(read.fact).toBe('in_review → adversarial_review');
  });

  it('words a bare fact and a named one, but never an identifier', () => {
    const plain = (summary: string) => eventLine(event({ summary })).plain;
    expect(plain('{"reason":"base_integration_failed"}')).toBe('base integration failed');
    expect(plain('{"verdict":"NEEDS_CHANGES"}')).toBe('needs changes');
    expect(plain('{"scope":"weekly_cap"}')).toBe('scope weekly cap');
    expect(plain('{"branch":"agent/issue_2896"}')).toBe('branch agent/issue_2896');
    expect(plain('{"head_sha":"9f2c_e1"}')).toBe('head sha 9f2c_e1');
  });

  it('prints a free-form error, warning or reason exactly, path and all', () => {
    const plain = (summary: string) => eventLine(event({ summary })).plain;
    expect(plain('{"error":"failed to open src/my_module.py"}')).toBe(
      'failed to open src/my_module.py'
    );
    expect(plain('{"warning":"see kew_config.toml"}')).toBe('see kew_config.toml');
    expect(plain('{"reason":"merge_queue.py held"}')).toBe('merge_queue.py held');
    expect(plain('{"from":"in_review","to":"src/a_b.py"}')).toBe('in review → src/a_b.py');
  });

  it('never words an error or a warning, even one shaped like a code', () => {
    const plain = (summary: string) => eventLine(event({ summary })).plain;
    expect(plain('{"error":"classifier_unavailable"}')).toBe('classifier_unavailable');
    expect(plain('{"warning":"cache_cold"}')).toBe('cache_cold');
  });

  it('words a reason only when it is a code kew is known to record', () => {
    const plain = (summary: string) => eventLine(event({ summary })).plain;
    expect(plain('{"reason":"budget_exceeded"}')).toBe('budget exceeded');
    expect(plain('{"reason":"ci_pending"}')).toBe('CI pending');
    expect(plain('{"reason":"quota_probe_skipped"}')).toBe('quota_probe_skipped');
  });

  it('words the reasons kew’s reap, rollback and orphan events record', () => {
    const plain = (summary: string) => eventLine(event({ summary })).plain;
    expect(plain('{"reason":"process_not_live"}')).toBe('process not live');
    expect(plain('{"reason":"pool_submit_failed"}')).toBe('pool submit failed');
    expect(plain('{"reason":"admission_owner_dead"}')).toBe('admission owner dead');
  });

  it('prints prose the log recorded as prose exactly as it was written', () => {
    expect(eventLine(event({ summary: 'dispatch refused · budget_exceeded' })).plain).toBe(
      'dispatch refused · budget_exceeded'
    );
  });
});

describe('the key fact', () => {
  it('is the reason a refusal recorded, in the backend’s own words', () => {
    expect(line({ event: 'dispatch_refused', summary: '{"reason":"budget_exceeded"}' })).toBe(
      'dispatch refused · budget_exceeded'
    );
  });

  it('names the field where the value alone would not say what it is', () => {
    expect(line({ summary: '{"exit_code":1}' })).toBe('dispatch completed · exit code 1');
    expect(line({ summary: '{"cost_usd":0.4}' })).toBe('dispatch completed · cost $0.40');
  });

  it('takes one fact, the first the payload carries in priority order', () => {
    expect(line({ summary: '{"branch":"agent/issue-2210","reason":"stale"}' })).toBe(
      'dispatch completed · stale'
    );
  });

  it('states no fact at all when the payload carries none it can read', () => {
    // The line is honest about it and the expand still holds the payload —
    // guessing at an unknown field is how a stream starts inventing readings.
    const read = eventLine(event({ summary: '{"tree_sha":"9f2c","tokens":51}' }));
    expect(read.line).toBe('dispatch completed');
    expect(read.detail).toContain('"tree_sha": "9f2c"');
  });

  it('still reads an excerpt the 200-character cap cut mid-payload', () => {
    const cut = '{"reason":"budget_exceeded","scope":"weekly","estimate_us…';
    const read = eventLine(event({ event: 'approval_hold', summary: cut }));
    expect(read.line).toBe('approval hold · budget_exceeded');
    // Unparseable, so it is shown exactly as recorded rather than reformatted.
    expect(read.detail).toBe(cut);
  });
});

describe('the containment posture', () => {
  // The two events a reader comes to this stream for. Both payloads are the
  // server's own shape (`publication.py:select_publication_mode`,
  // `sandbox.py:emit_resolved_posture`), not the artboard's prose: the posture
  // is the one field that differs between opposite containment readings, and a
  // line that drops it says the same thing about both.
  it('is the fact on a publication posture row, whichever posture it is', () => {
    const posture = (value: string) =>
      line({ event: 'publication_posture', summary: `{"branch":"agent/x","posture":"${value}"}` });
    expect(posture('canonical-publication')).toBe('publication posture · canonical-publication');
    expect(posture('uncontained-publication')).toBe(
      'publication posture · uncontained-publication'
    );
  });

  it('outranks the attempt number a sandboxed run records with no reason', () => {
    // `reason` is null on the posture that held, so the old reading fell
    // through to `attempt_number` and printed "attempt number 1" either way.
    const resolved = (value: string) =>
      line({
        event: 'sandbox_resolved',
        issue_num: 2055,
        summary: `{"attempt_number":1,"posture":"${value}","runtime":"docker","reason":null}`
      });
    expect(resolved('sandboxed')).toBe('sandbox resolved · #2055 · sandboxed');
    expect(resolved('uncontained')).toBe('sandbox resolved · #2055 · uncontained');
  });

  it('still yields to the verdict of a gate that recorded one', () => {
    // `merge_queue` records `result` beside `posture`; pass/fail is the row.
    expect(
      eventLine(
        event({ event: 'merge_queue_verified', summary: '{"result":"pass","posture":"sandboxed"}' })
      ).fact
    ).toBe('pass');
  });
});

describe('a transition', () => {
  // #2248: `epic_node_status` is the most repeated row in the stream, and the
  // one thing that made each one different used to be behind the expand.
  it('is the fact on an epic node status row', () => {
    expect(
      line({
        event: 'epic_node_status',
        issue_num: 2219,
        epic_ref: 'ui-refine',
        summary: '{"epic_ref":"ui-refine","from":"in_review","to":"done"}'
      })
    ).toBe('epic node status · ui-refine · #2219 · in_review → done');
  });

  it('outranks the single-key facts, so the move is the row', () => {
    expect(
      eventLine(event({ summary: '{"from":"queued","to":"running","reason":"dispatched"}' })).fact
    ).toBe('queued → running');
  });

  it('needs both halves: a half-stated move is no arrow', () => {
    expect(eventLine(event({ summary: '{"from":"queued","reason":"stale"}' })).fact).toBe('stale');
    expect(eventLine(event({ summary: '{"to":"done","reason":"stale"}' })).fact).toBe('stale');
    // A payload recording `from` as an object has no pair here either: the
    // override shape in run_store_publication_jobs.py is not a transition.
    expect(eventLine(event({ summary: '{"from":{"generation":2},"reason":"stale"}' })).fact).toBe(
      'stale'
    );
  });

  it('leaves nothing to expand when the move was the whole payload', () => {
    expect(
      eventLine(
        event({
          event: 'epic_node_status',
          summary: '{"epic_ref":"ui-refine","from":"in_review","to":"done"}'
        })
      ).detail
    ).toBe('');
  });
});

describe('the epic on a row', () => {
  // P8: `_summary` caps the excerpt at 200 characters, and the payloads worth
  // reading are the ones big enough to be cut. A cut excerpt does not parse, so
  // a row that found its epic by parsing one lost it exactly when it mattered.
  it('comes from the event column, not from parsing the summary', () => {
    expect(line({ epic_ref: 'ui-refine', issue_num: 2219 })).toBe(
      'dispatch completed · ui-refine · #2219'
    );
  });

  it('survives an excerpt the cap cut before the epic field', () => {
    const cut =
      '{"acceptance_reason":"the reviewer wrote a long paragraph here, long enough that the' +
      ' excerpt the stream receives stops before it reaches the epic that the run belongs…';
    const read = eventLine(
      event({ event: 'adversarial_review_verdict', epic_ref: 'ui-refine', issue_num: 2219, summary: cut })
    );
    expect(read.subject).toBe('ui-refine · #2219');
    // And what the cap took is still one click away.
    expect(read.detail).toBe(cut);
  });

  it('falls back to the payload for an event the column does not attribute', () => {
    expect(line({ epic_ref: null, summary: '{"epic":"go-live"}' })).toBe(
      'dispatch completed · go-live'
    );
  });
});

describe('the expand', () => {
  it('holds the payload, formatted, where the line read it', () => {
    const read = eventLine(event({ summary: '{"reason":"budget_exceeded","scope":"weekly"}' }));
    expect(read.detail).toBe('{\n  "reason": "budget_exceeded",\n  "scope": "weekly"\n}');
  });

  it('is empty where the line already prints everything recorded', () => {
    // A prose summary IS the fact; an expand repeating the row is a control
    // that promises more and shows the same.
    expect(eventLine(event({ summary: 'canonical' })).detail).toBe('');
    expect(eventLine(event({ summary: null })).detail).toBe('');
  });

  it('is empty for a payload whose every field reached the line', () => {
    // `_summary` serializes a dict on every payload-bearing event, so prose is
    // a fixture's shape and this is production's: a one-field payload the line
    // prints in full must not become a button that opens a repeat of the row.
    expect(eventLine(event({ summary: '{"reason":"budget_exceeded"}' })).detail).toBe('');
    expect(
      eventLine(event({ event: 'dispatch_refused', summary: '{"issue":2055,"reason":"stale"}' }))
        .detail
    ).toBe('');
  });

  it('is kept wherever the payload holds a field the line does not carry', () => {
    // A key the fact lists do not read, a `null`, and a nested object: three
    // ways a payload says more than the line, each of them worth the control.
    const held = (summary: string) => eventLine(event({ summary })).detail;
    expect(held('{"reason":"stale","scope":"weekly"}')).toContain('"scope"');
    expect(held('{"reason":"stale","tree_sha":"9f2c"}')).toContain('"tree_sha"');
    expect(held('{"reason":"stale","image":null}')).toContain('"image"');
    expect(held('{"reason":"stale","config":{"enabled":true}}')).toContain('"enabled"');
    // A PR named twice: the line reads the number, the URL is still unread.
    expect(held('{"pr_number":2059,"pull_request_url":"https://x/pull/2059"}')).toContain(
      '"pull_request_url"'
    );
  });

  it('is kept where the line rounded the one figure recorded', () => {
    // The only value this module rewrites is money, to the cent. The figure in
    // the chain is the evidence, so it stays one click away even though the
    // line carries that field: `$0.46` is not what the log holds.
    const read = eventLine(event({ summary: '{"cost_usd":0.4567}' }));
    expect(read.line).toBe('dispatch completed · cost $0.46');
    expect(read.detail).toContain('0.4567');
  });
});

describe('the artboard fixture', () => {
  it('reads one line per event, kind in words and the fact kept', () => {
    // Every one of them names its epic, because `epic_ref` is a column on the
    // event rather than a field the row has to find in a capped excerpt.
    expect(auditJson.events.slice(0, 5).map((e) => eventLine(e as AuditEvent).line)).toEqual([
      'dispatch completed · launch-fixes · #2055 · run f5dcb6a5 · success · PR #2059',
      'publication output scan · launch-fixes · #2055 · clean · 3670e43f',
      'publication posture · launch-fixes · #2055 · canonical',
      'sandbox resolved · launch-fixes · #2055 · sandboxed',
      'approval hold · launch-fixes · #2055 · dispatch refused · budget_exceeded (weekly)'
    ]);
  });

  it('offers no expand on any of them: the log recorded prose, not a payload', () => {
    expect(auditJson.events.every((e) => eventLine(e as AuditEvent).detail === '')).toBe(true);
  });
});

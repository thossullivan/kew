import { describe, expect, it } from 'vitest';
import {
  APPROVER_ONLY,
  bodyLength,
  COHERENT_READ_ATTEMPTS,
  coherence,
  configRuns,
  decisionSlot,
  EDITOR_ANCHOR,
  olderApproval,
  pendingVersions,
  PROFILE_CHANGED,
  profileState,
  readSequence,
  readWasStale,
  refusedProfile,
  RELOAD_TO_DECIDE,
  WRITE_OUTCOME_UNKNOWN,
  writeNeedsReload,
  writeBlock,
  writeOutcomeUnknown,
  writeRefusal
} from './profile-state';

describe('profileState', () => {
  it('reads no active profile', () => {
    expect(profileState({ profile: 'none' })).toEqual({ kind: 'none' });
  });

  it('names the active version', () => {
    expect(profileState({ runner: 'claude-code', profile: 'v3' })).toEqual({ kind: 'active', version: 'v3' });
  });

  it('carries the reason a refused profile gives', () => {
    expect(profileState({ profile: 'refused: its body does not match the approved digest' })).toEqual({
      kind: 'refused',
      reason: 'its body does not match the approved digest'
    });
  });

  it('reads a role with no profile key as one no profile applies to', () => {
    expect(profileState({})).toEqual({ kind: 'not-applicable' });
  });

  it('keeps a label it does not know as served', () => {
    expect(profileState({ profile: 'pending' })).toEqual({ kind: 'other', label: 'pending' });
  });
});

describe('refusedProfile', () => {
  const DIGEST = 'its body does not match the approved digest; retire the profile and propose it again';
  const TEMPLATE =
    'the ci_fix prompt profile v2 is active and only the built-in prompt renders it; retire the profile or remove [prompt] template from kew.toml';
  const LEAD = 'The active operator profile is refused, so every launch for this role refuses: ';
  const UNCONFIGURED = 'Set [approval].approvers in kew.toml before deciding prompt profiles.';
  const refused = (reason: string) => ({ kind: 'refused' as const, reason });
  const text = (runs: { text: string }[]) => runs.map((r) => r.text).join('');
  const mono = (runs: { text: string; mono: boolean }[]) => runs.filter((r) => r.mono).map((r) => r.text);

  it('is the needs-you block for an approver: the reason once, no DO THIS, one link to the editor', () => {
    const lead = refusedProfile(refused(DIGEST), { canDecide: true, decideRefusal: null });
    expect(lead?.kind).toBe('needs-you');
    if (lead?.kind !== 'needs-you') return;
    expect(lead.block.label).toBe('Profile refused');
    expect(lead.block.summary).toBe(`${LEAD}${DIGEST}.`);
    expect(text(lead.block.summaryRuns ?? [])).toBe(lead.block.summary);
    expect(lead.block.note).toBe('');
    expect(lead.block.command).toBe('');
    expect(lead.block.since).toBe('');
    expect(lead.block.links).toEqual([
      { key: 'editor', label: 'Operator instructions', href: `#${EDITOR_ANCHOR}`, external: false }
    ]);
    expect(lead.block.external).toEqual([]);
  });

  it('sets a custom-template reason’s config names in mono and adds no remedy of its own', () => {
    const lead = refusedProfile(refused(TEMPLATE), { canDecide: true, decideRefusal: null });
    if (lead?.kind !== 'needs-you') throw new Error(`expected the needs-you block, got ${lead?.kind}`);
    expect(lead.block.summary).toBe(`${LEAD}${TEMPLATE}.`);
    expect(mono(lead.block.summaryRuns ?? [])).toEqual(['[prompt] template', 'kew.toml']);
    expect(lead.block.note).toBe('');
  });

  it('is one neutral sentence for a viewer who is not an approver, naming no move of theirs', () => {
    const lead = refusedProfile(refused(TEMPLATE), { canDecide: false, decideRefusal: null });
    expect(lead?.kind).toBe('neutral');
    if (lead?.kind !== 'neutral') return;
    expect(text(lead.runs)).toBe(`${LEAD}${TEMPLATE}. An approver must clear it.`);
    expect(mono(lead.runs)).toEqual(['[prompt] template', 'kew.toml']);
  });

  it('makes the config fix the DO THIS where the server names one, with no editor link', () => {
    const lead = refusedProfile(refused(DIGEST), { canDecide: false, decideRefusal: UNCONFIGURED });
    if (lead?.kind !== 'needs-you') throw new Error(`expected the needs-you block, got ${lead?.kind}`);
    expect(lead.block.summary).toBe(`${LEAD}${DIGEST}.`);
    expect(lead.block.note).toBe(UNCONFIGURED);
    expect(text(lead.block.noteRuns ?? [])).toBe(UNCONFIGURED);
    expect(mono(lead.block.noteRuns ?? [])).toEqual(['[approval].approvers', 'kew.toml']);
    expect(lead.block.links).toEqual([]);
  });

  it('reads an empty decide_refusal as none', () => {
    expect(refusedProfile(refused(DIGEST), { canDecide: false, decideRefusal: '' })?.kind).toBe('neutral');
  });

  it('stays neutral until the history has been read, so it never flashes magenta first', () => {
    const lead = refusedProfile(refused(DIGEST), undefined);
    expect(lead?.kind).toBe('neutral');
    if (lead?.kind !== 'neutral') return;
    expect(text(lead.runs)).toBe(`${LEAD}${DIGEST}. An approver must clear it.`);
  });

  it('does not double a reason’s own closing full stop', () => {
    const lead = refusedProfile(refused('it was retired.'), undefined);
    expect(lead?.kind === 'neutral' && text(lead.runs)).toBe(`${LEAD}it was retired. An approver must clear it.`);
  });

  it.each([
    { kind: 'none' as const },
    { kind: 'active' as const, version: 'v3' },
    { kind: 'not-applicable' as const },
    { kind: 'other' as const, label: 'pending' }
  ])('is null for a $kind profile, which nobody has to act on', (state) => {
    expect(refusedProfile(state, { canDecide: true, decideRefusal: null })).toBeNull();
  });

  it('is null with no preview read', () => {
    expect(refusedProfile(null, { canDecide: true, decideRefusal: null })).toBeNull();
  });
});

describe('writeRefusal', () => {
  it('reads a 403 from the approver gate as the approver sentence', () => {
    expect(writeRefusal('approve', 403, "actor 'bob' is not in [approval].approvers")).toBe(
      'Only an approver can approve a profile.'
    );
  });

  it('names the setting when no approvers are configured, for every write', () => {
    const sentence = 'Set [approval].approvers in kew.toml before deciding prompt profiles.';
    for (const write of ['propose', 'approve', 'decline', 'retire'] as const) {
      expect(writeRefusal(write, 403, sentence)).toBe(sentence);
    }
  });

  it('reads a 403 from the CSRF check as a session problem, not an approver one', () => {
    expect(writeRefusal('approve', 403, 'invalid or missing CSRF token')).toBe(
      'This session cannot change a profile. Sign in again, then retry.'
    );
  });

  it('reads a 409 as the reload sentence for every write', () => {
    for (const write of ['propose', 'approve', 'decline', 'retire'] as const) {
      expect(writeRefusal(write, 409, 'generation 1 is stale')).toBe(
        'This profile changed since you opened it. Reload to see the new version.'
      );
    }
  });

  it("carries a 400's reason and names any other refusal", () => {
    expect(writeRefusal('propose', 400, 'a prompt profile body may not be empty')).toBe(
      'The server refused it: a prompt profile body may not be empty'
    );
    expect(writeRefusal('retire', 404, '')).toBe('The retire was refused (HTTP 404), so nothing changed.');
  });

  it("reads an approver-gate 403 by an actor whose name spells 'origin' or 'csrf' as the approver sentence", () => {
    for (const actor of ['origin-operator', 'csrf-bot', 'Origin Not Allowed']) {
      expect(writeRefusal('approve', 403, `actor '${actor}' is not in [approval].approvers`)).toBe(
        'Only an approver can approve a profile.'
      );
    }
  });

  it("reads require_write's own two refusals, exactly, as a session problem", () => {
    for (const detail of ['invalid or missing CSRF token', 'origin not allowed']) {
      expect(writeRefusal('retire', 403, detail)).toBe(
        'This session cannot change a profile. Sign in again, then retry.'
      );
    }
  });
});

describe('writeOutcomeUnknown', () => {
  it('reads a write with no HTTP response, an unreadable success or a server error as unknown', () => {
    for (const status of [0, 200, 502, 500]) expect(writeOutcomeUnknown(status), String(status)).toBe(true);
  });

  it('reads a refusal the server answered as known: nothing changed', () => {
    for (const status of [400, 401, 403, 404, 409, 422]) expect(writeOutcomeUnknown(status), String(status)).toBe(false);
  });

  it('never says an unknown write changed nothing', () => {
    for (const write of ['propose', 'approve', 'decline', 'retire'] as const) {
      for (const status of [0, 200, 500]) {
        const said = writeRefusal(write, status, 'Failed to fetch');
        expect(said).toBe(WRITE_OUTCOME_UNKNOWN);
        expect(said).not.toMatch(/nothing changed/);
      }
    }
    expect(WRITE_OUTCOME_UNKNOWN).toBe('The result of this request is unknown. Reload to see the current profile.');
  });
});

describe('readWasStale', () => {
  it("reads only the server's 409 as a role that changed during the read", () => {
    expect(readWasStale(new Error('/api/prompts/dispatch/versions -> 409'))).toBe(true);
    expect(readWasStale(new Error('/api/prompts/dispatch/versions -> 404'))).toBe(false);
    expect(readWasStale(new TypeError('Failed to fetch'))).toBe(false);
  });

  it('prints the same sentence a 409 on a write prints', () => {
    expect(writeRefusal('approve', 409, '')).toBe(PROFILE_CHANGED);
  });
});

describe('coherence', () => {
  const at = (preview: number | null | undefined, history: number | undefined, pending = false, attempts = 0) =>
    coherence({ preview, history, pending, attempts });

  it('shows a history from the generation the preview names', () => {
    expect(at(4, 4)).toBe('agree');
  });

  it('has nothing to compare without a preview on screen or a history yet', () => {
    expect(at(undefined, 4)).toBe('agree');
    expect(at(4, undefined)).toBe('agree');
  });

  it('waits while a read is in flight, and re-reads both once they land', () => {
    expect(at(4, 5, true)).toBe('wait');
    expect(at(4, 5, false, 0)).toBe('reread');
    expect(at(4, 5, false, COHERENT_READ_ATTEMPTS - 1)).toBe('reread');
  });

  it('gives up after the bound, so the pane says the profile changed', () => {
    expect(at(4, 5, false, COHERENT_READ_ATTEMPTS)).toBe('diverged');
  });

  it('never matches a preview whose generation did not settle', () => {
    expect(at(null, 0)).toBe('reread');
    expect(at(null, 0, false, COHERENT_READ_ATTEMPTS)).toBe('diverged');
  });
});

describe('readSequence', () => {
  it("drops a slow read for a role the pane has left, and keeps the new role's", () => {
    const reads = readSequence();
    const dispatchReload = reads.begin('dispatch');
    const ciFix = reads.begin('ci_fix');
    // ci_fix's answer lands first; dispatch's slow reload lands after it.
    expect(ciFix('ci_fix')).toBe(true);
    expect(dispatchReload('ci_fix')).toBe(false);
  });

  it('drops a read a later read of the same role has overtaken', () => {
    const reads = readSequence();
    const first = reads.begin('dispatch');
    const second = reads.begin('dispatch');
    expect(first('dispatch')).toBe(false);
    expect(second('dispatch')).toBe(true);
  });

  it('drops the newest read once the pane shows another role', () => {
    const reads = readSequence();
    const only = reads.begin('dispatch');
    expect(only('ci_fix')).toBe(false);
    expect(only('dispatch')).toBe(true);
  });
});

describe('writeNeedsReload', () => {
  it('locks the writes after a 409 and after an unknown outcome', () => {
    for (const status of [409, 0, 200, 503]) expect(writeNeedsReload(status)).toBe(true);
  });

  it('leaves the writes on after a refusal that changed nothing', () => {
    for (const status of [400, 401, 403, 404]) expect(writeNeedsReload(status)).toBe(false);
  });
});

describe('bodyLength', () => {
  it('counts code points, as the server does, not UTF-16 units', () => {
    const body = '😀'.repeat(4000);
    expect(body.length).toBe(8000);
    expect(bodyLength(body)).toBe(4000);
  });

  it('counts the trimmed text', () => {
    expect(bodyLength('  a𝒳b \n')).toBe(3);
  });
});

describe('pendingVersions', () => {
  it('offers every proposed version, newest first, not only the newest', () => {
    const versions = [
      { version: 4, state: 'proposed' },
      { version: 3, state: 'active' },
      { version: 2, state: 'proposed' },
      { version: 1, state: 'retired' }
    ];
    expect(pendingVersions(versions).map((v) => v.version)).toEqual([4, 2]);
  });
});

describe('olderApproval', () => {
  it('asks before approving a version older than the active one, naming both', () => {
    expect(olderApproval(2, 3)).toBe('v2 is older than the active v3. Approve it anyway?');
  });

  it('asks nothing for a newer version or when none is active', () => {
    expect(olderApproval(4, 3)).toBeNull();
    expect(olderApproval(1, null)).toBeNull();
  });
});

describe('decisionSlot', () => {
  it('offers the decision to an approver with nothing to reload', () => {
    expect(decisionSlot({ canDecide: true, mustReload: false })).toBe('controls');
  });

  it('leaves a non-approver\'s block empty: the write area says why, once', () => {
    expect(decisionSlot({ canDecide: false, mustReload: false })).toBe('empty');
    expect(decisionSlot({ canDecide: false, mustReload: true })).toBe('empty');
  });

  it('asks only for a reload in a block after a 409, never the full sentence again', () => {
    expect(decisionSlot({ canDecide: true, mustReload: true })).toBe('reload');
    expect(RELOAD_TO_DECIDE).toBe('Reload to decide.');
    expect(RELOAD_TO_DECIDE).not.toBe(PROFILE_CHANGED);
  });
});

describe('writeBlock', () => {
  it('leaves every write to an approver', () => {
    expect(writeBlock({ canDecide: true, decideRefusal: null })).toBeNull();
  });

  it('tells a non-approver why no write is theirs', () => {
    expect(writeBlock({ canDecide: false, decideRefusal: null })).toBe(APPROVER_ONLY);
    expect(writeBlock({ canDecide: false, decideRefusal: undefined })).toBe(APPROVER_ONLY);
    expect(APPROVER_ONLY).toBe('Only an approver can change a profile.');
  });

  it("prints the server's sentence when no approver is configured", () => {
    const fix = 'Set [approval].approvers in kew.toml before deciding prompt profiles.';
    expect(writeBlock({ canDecide: false, decideRefusal: fix })).toBe(fix);
  });
});

describe('configRuns', () => {
  it('sets the config key and the file in mono, the rest as prose', () => {
    expect(configRuns('Set [approval].approvers in kew.toml before deciding prompt profiles.')).toEqual([
      { text: 'Set ', mono: false },
      { text: '[approval].approvers', mono: true },
      { text: ' in ', mono: false },
      { text: 'kew.toml', mono: true },
      { text: ' before deciding prompt profiles.', mono: false }
    ]);
  });

  it('sets the custom-template refusal’s [prompt] template in mono', () => {
    expect(configRuns('retire the profile or remove [prompt] template from kew.toml')).toEqual([
      { text: 'retire the profile or remove ', mono: false },
      { text: '[prompt] template', mono: true },
      { text: ' from ', mono: false },
      { text: 'kew.toml', mono: true }
    ]);
  });

  it('leaves a sentence with no config name as one prose run', () => {
    expect(configRuns(APPROVER_ONLY)).toEqual([{ text: APPROVER_ONLY, mono: false }]);
  });
});

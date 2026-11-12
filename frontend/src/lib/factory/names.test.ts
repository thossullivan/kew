import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
  ERROR_CATEGORY_LABELS,
  errorCategoryLabel,
  eventLabel,
  modelName,
  plainTitle,
  reasonLabel,
  refusalReasonLabel,
  signalLabel,
  titleSpans
} from './names';

// Read off disk, not through the module under test, so a catalog row the
// import somehow dropped still has to be named.
const catalog = JSON.parse(
  readFileSync(new URL('../../../../kew/src/kew/models.json', import.meta.url), 'utf8')
) as { models: { id: string; display_name: string; aliases: string[] }[] };

describe('modelName', () => {
  it('names every model id in the catalog', () => {
    expect(catalog.models.length).toBeGreaterThan(0);
    for (const row of catalog.models) {
      const name = modelName(row.id);
      expect(name, row.id).toBe(row.display_name);
      expect(name, row.id).not.toBe(row.id);
      for (const alias of row.aliases) expect(modelName(alias), alias).toBe(row.display_name);
    }
  });

  it('names the ids the console shows as the catalog does', () => {
    expect(modelName('claude-opus-5-5')).toBe('Opus 5.5');
    expect(modelName(' claude-opus-5 ')).toBe('Opus 5');
  });

  it('falls back to the id itself for an id the catalog does not carry', () => {
    expect(modelName('acme-llm-9')).toBe('acme-llm-9');
    expect(modelName('constructor')).toBe('constructor');
    expect(modelName('__proto__')).toBe('__proto__');
  });
});

describe('eventLabel and reasonLabel', () => {
  it('word the codes they know', () => {
    expect(eventLabel('routing_decision')).toBe('Route chosen');
    expect(reasonLabel('artifact_diff')).toBe('sealed change set');
  });

  it('print an unknown code as itself', () => {
    expect(eventLabel('brand_new_event')).toBe('brand_new_event');
    expect(eventLabel('toString')).toBe('toString');
    expect(reasonLabel('brand_new_reason')).toBe('brand_new_reason');
  });
});

describe('signalLabel (#2844 P8)', () => {
  it('words every driver signal, and never prints a snake_case code', () => {
    expect(signalLabel('awaiting_merges')).toBe('awaiting merges');
    expect(signalLabel('progressing')).toBe('progressing');
    expect(signalLabel('brand_new_signal')).toBe('brand new signal');
    expect(signalLabel('constructor')).toBe('constructor');
    expect(signalLabel(null)).toBe('unknown');
    expect(signalLabel('  ')).toBe('unknown');
  });
});

describe('errorCategoryLabel', () => {
  it('words a container that could not start, and never prints a snake_case code', () => {
    expect(errorCategoryLabel('container_start_failed')).toBe('Container start failed');
    expect(errorCategoryLabel('brand_new_category')).toBe('brand new category');
    expect(errorCategoryLabel('constructor')).toBe('constructor');
  });

  it('words the verify host categories kew records on an interrupted job (#3466)', () => {
    expect(errorCategoryLabel('host_job_deadline_exceeded')).toBe(
      'The verify host hit its job deadline'
    );
    expect(errorCategoryLabel('host_connection_lost')).toBe(
      'The connection to the verify host was lost'
    );
    expect(errorCategoryLabel('host_interrupted')).toBe('The verify host was interrupted');
    expect(errorCategoryLabel(' github_auth ')).toBe('GitHub authentication failed');
  });

  it('gives every label words of its own, never the code spaced out', () => {
    for (const [code, words] of Object.entries(ERROR_CATEGORY_LABELS)) {
      expect(words, code).not.toContain('_');
      expect(words.trim(), code).toBe(words);
    }
  });
});

describe('refusalReasonLabel', () => {
  it('words the publication refusal codes kew writes (#2953)', () => {
    expect(refusalReasonLabel('issued_key_evidence_missing')).toBe('issued key evidence missing');
    expect(refusalReasonLabel('credential_scan_missing')).toBe('credential scan missing');
    expect(refusalReasonLabel('contained_profile_missing')).toBe('contained profile missing');
    expect(refusalReasonLabel('tls_error')).toBe('TLS error');
  });

  it('leaves a reason it has not met unworded, and words no prototype member', () => {
    expect(refusalReasonLabel('brand_new_reason')).toBeNull();
    expect(refusalReasonLabel('constructor')).toBeNull();
  });
});

describe('titleSpans', () => {
  it('turns a backtick span into an inline mono span without its backticks', () => {
    expect(titleSpans('`kew drive install` as a service')).toEqual([
      { text: 'kew drive install', mono: true },
      { text: ' as a service', mono: false }
    ]);
    expect(plainTitle('`kew drive install` as a service')).toBe('kew drive install as a service');
  });

  it('closes a span only on a run of the same length, so a backtick can sit inside one', () => {
    expect(titleSpans('quote ``a`b`` here')).toEqual([
      { text: 'quote ', mono: false },
      { text: 'a`b', mono: true },
      { text: ' here', mono: false }
    ]);
  });

  it('drops a backtick that opens nothing, so no title prints the character', () => {
    expect(plainTitle('a stray ` tick and `one` span')).toBe('a stray  tick and one span');
    expect(titleSpans('a stray ` tick')).toEqual([{ text: 'a stray  tick', mono: false }]);
  });

  it('leaves a title without backticks as one plain span, and an empty title empty', () => {
    expect(titleSpans('Review gate lows')).toEqual([{ text: 'Review gate lows', mono: false }]);
    expect(titleSpans('')).toEqual([]);
  });
});

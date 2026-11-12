/**
 * The demo's live streams open and stay quiet (#3139): no frame, no error —
 * so no surface starts a retry loop or a 401 probe — while any other URL still
 * gets the real EventSource.
 */
import { describe, expect, it, vi } from 'vitest';
import { isQuietStream, quietEventSource } from './event-source';

const ORIGIN = 'http://127.0.0.1:4173';

class RealSource extends EventTarget {
  static made: string[] = [];
  constructor(readonly url: string | URL) {
    super();
    RealSource.made.push(String(url));
  }
}

const Quiet = quietEventSource(RealSource as unknown as typeof EventSource, ORIGIN);

describe('the demo EventSource', () => {
  it('quiets exactly the two /api streams', () => {
    expect(isQuietStream('/api/runs/f5dcb6a5/events', ORIGIN)).toBe(true);
    expect(isQuietStream('/api/epics/launch-fixes/stream', ORIGIN)).toBe(true);
    expect(isQuietStream('/api/runs', ORIGIN)).toBe(false);
    expect(isQuietStream('https://elsewhere.example/api/epics/x/stream', ORIGIN)).toBe(false);
  });

  it('opens once and never errors', async () => {
    vi.useFakeTimers();
    try {
      const es = new Quiet('/api/epics/launch-fixes/stream');
      const seen: string[] = [];
      es.onopen = () => seen.push('open');
      es.onerror = () => seen.push('error');
      es.onmessage = () => seen.push('message');
      es.addEventListener('error', () => seen.push('error event'));
      expect(es.readyState).toBe(0);
      await vi.advanceTimersByTimeAsync(60_000);
      expect(seen).toEqual(['open']);
      expect(es.readyState).toBe(1);
      es.close();
      expect(es.readyState).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('is the real EventSource for any other URL', () => {
    const es = new Quiet('/somewhere/else');
    expect(es).toBeInstanceOf(RealSource);
    expect(RealSource.made).toEqual(['/somewhere/else']);
  });
});

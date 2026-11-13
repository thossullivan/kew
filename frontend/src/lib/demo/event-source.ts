/**
 * The demo console's live streams: open, and quiet (#3139).
 *
 * The run page tails `/api/runs/{id}/events` and the epic page
 * `/api/epics/{ref}/stream`. With no backend either would fail, and an
 * `EventSource` that fails reconnects in a loop and fires `error`, which is
 * what starts a surface's retry and its 401 probe. So in the demo those two
 * reads get a stream that opens once and then says nothing, ever: no frame,
 * no error, no close until the page closes it. Any other URL gets the real
 * `EventSource`.
 */
import { isStreamPath, ownsPath } from '../../../fixtures/core';

/** Whether `url` is one of this origin's `/api` streams. */
export function isQuietStream(url: string | URL, origin: string): boolean {
  const parsed = new URL(String(url), origin);
  const path = `${parsed.pathname}${parsed.search}`;
  return parsed.origin === new URL(origin).origin && ownsPath(parsed.pathname) && isStreamPath(path);
}

/**
 * An `EventSource` class whose `/api` streams are quiet ones, and which is the
 * real `Real` for every other URL.
 */
export function quietEventSource(Real: typeof EventSource, origin: string): typeof EventSource {
  class QuietEventSource extends EventTarget {
    static readonly CONNECTING = 0;
    static readonly OPEN = 1;
    static readonly CLOSED = 2;
    readonly CONNECTING = 0;
    readonly OPEN = 1;
    readonly CLOSED = 2;
    readonly url: string;
    readonly withCredentials: boolean;
    readyState: number = 0;
    onopen: ((this: EventSource, ev: Event) => unknown) | null = null;
    onmessage: ((this: EventSource, ev: MessageEvent) => unknown) | null = null;
    onerror: ((this: EventSource, ev: Event) => unknown) | null = null;

    constructor(url: string | URL, init?: EventSourceInit) {
      super();
      this.url = new URL(String(url), origin).href;
      this.withCredentials = init?.withCredentials ?? false;
      if (!isQuietStream(url, origin)) return new Real(url, init) as unknown as QuietEventSource;
      // Opened on the next task, as a real stream is: a caller assigns
      // `onopen` after the constructor returns.
      setTimeout(() => {
        if (this.readyState !== 0) return;
        this.readyState = 1;
        const opened = new Event('open');
        this.dispatchEvent(opened);
        this.onopen?.call(this as unknown as EventSource, opened);
      }, 0);
    }

    close(): void {
      this.readyState = 2;
    }
  }
  return QuietEventSource as unknown as typeof EventSource;
}

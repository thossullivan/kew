import { describe, expect, it, vi } from "vitest";
import {
  fetchMeta,
  fetchBundle,
  fetchTimeline,
  fetchDiff,
  streamEvents,
} from "./api";
import { fetchLoop, fetchLoopAudit, fetchLoops } from "./api";
import {
  fetchEpics,
  fetchEpic,
  fetchEpicTicks,
  fetchEpicNode,
  fetchEpicTimeline,
  streamEpic,
  type EpicDetail,
  type EpicFrame,
} from "./api";
import {
  fetchApprovals,
  fetchFleetStatus,
  fetchBudget,
  fetchSession,
  login,
  decideHold,
  stopFleet,
  setBudget,
  revertBudget,
} from "./api";
import { fetchScorecard, fetchSpend } from "./api";
import { fetchFlight, fetchPrompt, fetchRun, fetchRuns } from "./api";
import {
  fetchAudit,
  fetchAuditHistogram,
  type AuditHistogram,
  type AuditList,
} from "./api";

describe("fetchMeta", () => {
  it("fetches account billing context from /api/meta", async () => {
    const dto = {
      billing: "subscription",
      plan_label: "Claude Max",
      plan_monthly_usd: 200,
      version: "1.2.3",
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const meta = await fetchMeta(fakeFetch as unknown as typeof fetch);
    expect(meta.billing).toBe("subscription");
    expect(meta.plan_monthly_usd).toBe(200);
    expect(fakeFetch).toHaveBeenCalledWith("/api/meta");
  });
});

describe("fetchBundle", () => {
  it("fetches the web-bundle stamp from /api/bundle", async () => {
    const dto = {
      present: true,
      git_sha: "cafe123",
      built_at: 9_000_000_000,
      stale: false,
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const bundle = await fetchBundle(fakeFetch as unknown as typeof fetch);
    expect(bundle.git_sha).toBe("cafe123");
    expect(bundle.stale).toBe(false);
    expect(fakeFetch).toHaveBeenCalledWith("/api/bundle");
  });
});

describe("fetchTimeline", () => {
  it("maps the timeline DTO and preserves node order", async () => {
    const dto = {
      run_id: "r1",
      root_id: "r1:0",
      head_id: "r1:1",
      nodes: [
        {
          id: "r1:0",
          parent_id: null,
          seq: 0,
          ts: 1,
          kind: "init",
          label: "start",
          tokens_in: 0,
          tokens_out: 0,
          cost_usd: 0,
          has_diff: false,
        },
        {
          id: "r1:1",
          parent_id: "r1:0",
          seq: 1,
          ts: 2,
          kind: "edit",
          label: "edit a.py",
          tokens_in: 400,
          tokens_out: 100,
          cost_usd: 0.006,
          has_diff: true,
        },
      ],
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const tl = await fetchTimeline("r1", fakeFetch as unknown as typeof fetch);
    expect(tl.nodes.map((n) => n.kind)).toEqual(["init", "edit"]);
    expect(tl.nodes[1].has_diff).toBe(true);
    expect(fakeFetch).toHaveBeenCalledWith("/api/runs/r1/timeline");
  });

  it("throws on a non-ok response", async () => {
    const fakeFetch = vi.fn(async () => new Response("nope", { status: 404 }));
    await expect(
      fetchTimeline("x", fakeFetch as unknown as typeof fetch),
    ).rejects.toThrow("404");
  });
});

describe("fetchLoops", () => {
  it("fetches the loop list", async () => {
    const dto = [
      {
        loop_id: "L1",
        source: "issue",
        issue_num: 281,
        repo: "o/r",
        goal_text: "make CI green",
        runner_name: "claude-code",
        model: "claude-sonnet-4-6",
        drive_mode: "delegated",
        status: "pursuing",
        status_reason: "",
        needs_human: false,
        budget_usd: 5,
        spent_usd: 1.25,
        wall_clock_secs: 1800,
        max_steps: 40,
        step_count: 7,
        created_at: "2026-06-23T00:00:00+00:00",
        updated_at: null,
        finished_at: null,
      },
    ];
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const loops = await fetchLoops(
      undefined,
      fakeFetch as unknown as typeof fetch,
    );
    expect(loops[0].loop_id).toBe("L1");
    expect(loops[0].drive_mode).toBe("delegated");
    expect(loops[0].model).toBe("claude-sonnet-4-6");
    expect(fakeFetch).toHaveBeenCalledWith("/api/loops");
  });

  it("passes a status filter as a query param", async () => {
    const fakeFetch = vi.fn(async () => new Response("[]"));
    await fetchLoops("needs_human", fakeFetch as unknown as typeof fetch);
    expect(fakeFetch).toHaveBeenCalledWith("/api/loops?status=needs_human");
  });
});

describe("fetchLoop", () => {
  it("fetches a loop detail with iterations", async () => {
    const dto = {
      loop_id: "L1",
      source: "issue",
      issue_num: 281,
      repo: "o/r",
      goal_text: "make CI green",
      runner_name: "claude-code",
      model: "claude-sonnet-4-6",
      drive_mode: "delegated",
      status: "needs_human",
      status_reason: "gate_failed: 2 tests red",
      needs_human: true,
      budget_usd: 5,
      spent_usd: 1.25,
      wall_clock_secs: 1800,
      max_steps: 40,
      step_count: 1,
      created_at: "2026-06-23T00:00:00+00:00",
      updated_at: null,
      finished_at: null,
      iterations: [
        {
          iteration_id: "I0",
          idx: 0,
          kind: "assistant_turn",
          status: "pursuing",
          cost_usd: 0.42,
          tokens: 900,
          run_id: "run-abc",
          started_at: "2026-06-23T00:01:00+00:00",
          finished_at: "2026-06-23T00:02:00+00:00",
        },
      ],
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const loop = await fetchLoop("L1", fakeFetch as unknown as typeof fetch);
    expect(loop.needs_human).toBe(true);
    expect(loop.iterations[0].kind).toBe("assistant_turn");
    expect(fakeFetch).toHaveBeenCalledWith("/api/loops/L1");
  });
});

describe("fetchLoopAudit", () => {
  it("fetches the loop audit timeline", async () => {
    const dto = [
      {
        id: 3,
        timestamp: "2026-06-23T00:01:00+00:00",
        event: "verification_checked",
        actor_type: "policy",
        actor_id: null,
        loop_id: "L1",
        summary: "gate failed: 2 tests red",
      },
    ];
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const events = await fetchLoopAudit(
      "L1",
      fakeFetch as unknown as typeof fetch,
    );
    expect(events[0].event).toBe("verification_checked");
    expect(events[0].summary).toBe("gate failed: 2 tests red");
    expect(fakeFetch).toHaveBeenCalledWith("/api/loops/L1/audit");
  });
});

describe("fetchEpics", () => {
  it("fetches the epic summary list from /api/epics", async () => {
    const dto = [
      {
        epic_ref: "epic-1",
        signal: "green",
        counts: { done: 3, todo: 1 },
        drain_total_usd: 1.5,
        last_tick_ts: "2026-07-01T00:00:00+00:00",
        interval_s: 30,
        change_count: 4,
      },
    ];
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const epics = await fetchEpics(fakeFetch as unknown as typeof fetch);
    expect(epics[0].epic_ref).toBe("epic-1");
    expect(epics[0].counts.done).toBe(3);
    expect(fakeFetch).toHaveBeenCalledWith("/api/epics");
  });

  it("aborts the request when its signal aborts", async () => {
    const fakeFetch = vi.fn(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    );
    const controller = new AbortController();
    const read = fetchEpics(fakeFetch as unknown as typeof fetch, controller.signal);
    controller.abort();
    await expect(read).rejects.toThrow("aborted");
  });
});

describe("fetchEpic", () => {
  it("URL-encodes the epic ref", async () => {
    const dto = {
      epic_ref: "org/repo#42",
      snapshot: null,
      heartbeat: null,
      economics: {
        window_start: null,
        drain_total_usd: 0,
        node_drain_usd: {},
        label: "drain spend",
      },
    };
    const fakeFetch = vi.fn(
      async (_url: string) => new Response(JSON.stringify(dto)),
    );
    const detail = await fetchEpic(
      "org/repo#42",
      fakeFetch as unknown as typeof fetch,
    );
    expect(detail.snapshot).toBeNull();
    expect(fakeFetch).toHaveBeenCalledWith(
      `/api/epics/${encodeURIComponent("org/repo#42")}`,
    );
    const calledUrl = fakeFetch.mock.calls[0][0];
    const encodedSegment = calledUrl.slice("/api/epics/".length);
    expect(encodedSegment).not.toContain("#42");
    expect(encodedSegment).not.toContain("/");
  });

  it("carries the needs-you join and the blocked reason on nodes", async () => {
    const dto = {
      epic_ref: "epic-1",
      snapshot: {
        epic_ref: "epic-1",
        tick_id: 9,
        tick_ts: "2026-09-24T00:00:00+00:00",
        signal: "progressing",
        counts: { in_review: 1, blocked: 1 },
        frontier: [],
        nodes: [
          {
            id: "2055",
            title: "issue 2055",
            status: "in_review",
            status_since: "2026-09-24T00:00:00+00:00",
            url: null,
            pr_url: "https://example.invalid/pr/2059",
            external: false,
            stuck: null,
            attention: {
              kind: "merge_decision",
              reason: "PR open for #2055 — awaiting review",
              url: "https://example.invalid/pr/2059",
            },
            blocked_reason: null,
          },
          {
            id: "2053",
            title: "issue 2053",
            status: "blocked",
            status_since: "2026-09-24T00:00:00+00:00",
            url: null,
            pr_url: null,
            external: false,
            stuck: "needs a design decision",
            attention: null,
            blocked_reason: "needs a design decision",
          },
        ],
        edges: [],
        unavailable: [],
      },
      heartbeat: null,
      economics: null,
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const detail: EpicDetail = await fetchEpic(
      "epic-1",
      fakeFetch as unknown as typeof fetch,
    );
    const nodes = detail.snapshot!.nodes;
    expect(nodes[0].attention?.kind).toBe("merge_decision");
    expect(nodes[0].attention?.url).toBe("https://example.invalid/pr/2059");
    expect(nodes[0].blocked_reason).toBeNull();
    expect(nodes[1].attention).toBeNull();
    expect(nodes[1].blocked_reason).toBe("needs a design decision");
    expect(detail.snapshot!.unavailable).toEqual([]);
  });

  it("keeps the reason a null attention could not be composed for", async () => {
    // A null attention beside a named unavailable entry is "we could not tell",
    // not "nobody is waiting" — the two must stay distinguishable client-side.
    const dto = {
      epic_ref: "epic-1",
      snapshot: {
        epic_ref: "epic-1",
        tick_id: 9,
        tick_ts: "2026-09-24T00:00:00+00:00",
        signal: "progressing",
        counts: {},
        frontier: [],
        nodes: [
          {
            id: "2055",
            title: "issue 2055",
            status: "in_review",
            status_since: null,
            url: null,
            pr_url: null,
            external: false,
            stuck: null,
            attention: null,
            blocked_reason: null,
          },
        ],
        edges: [],
        unavailable: ["attention"],
      },
      heartbeat: null,
      economics: null,
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const detail = await fetchEpic(
      "epic-1",
      fakeFetch as unknown as typeof fetch,
    );
    expect(detail.snapshot!.nodes[0].attention).toBeNull();
    expect(detail.snapshot!.unavailable).toEqual(["attention"]);
  });
});

describe("fetchEpicTicks", () => {
  it("builds the ?after= query param", async () => {
    const fakeFetch = vi.fn(async () => new Response("[]"));
    await fetchEpicTicks("epic-1", 17, fakeFetch as unknown as typeof fetch);
    expect(fakeFetch).toHaveBeenCalledWith("/api/epics/epic-1/ticks?after=17");
  });

  it("defaults after to 0 when omitted", async () => {
    const fakeFetch = vi.fn(async () => new Response("[]"));
    await fetchEpicTicks(
      "epic-1",
      undefined,
      fakeFetch as unknown as typeof fetch,
    );
    expect(fakeFetch).toHaveBeenCalledWith("/api/epics/epic-1/ticks?after=0");
  });

  it("maps a tick with nested snapshot and actions", async () => {
    const dto = [
      {
        id: 5,
        tick_ts: "2026-07-01T00:00:01+00:00",
        snapshot: {
          epic_ref: "epic-1",
          tick_id: 5,
          tick_ts: "2026-07-01T00:00:01+00:00",
          signal: "green",
          counts: { done: 1 },
          frontier: ["n2"],
          nodes: [
            {
              id: "n1",
              title: "Node 1",
              status: "done",
              status_since: null,
              url: null,
              pr_url: null,
              external: false,
              stuck: null,
            },
          ],
          edges: [["n1", "n2"]],
        },
        actions: {
          dispatched: ["n2"],
          merged: [{ id: "n1", approved_by: "alice" }],
        },
        economics: null,
      },
    ];
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const ticks = await fetchEpicTicks(
      "epic-1",
      4,
      fakeFetch as unknown as typeof fetch,
    );
    expect(ticks[0].snapshot.nodes[0].id).toBe("n1");
    expect(ticks[0].actions.merged?.[0].approved_by).toBe("alice");
    expect(ticks[0].economics).toBeNull();
  });
});

describe("fetchEpicTimeline", () => {
  it("reads the epic's timeline, with the ref URL-encoded", async () => {
    const dto = {
      epic_ref: "org/repo#42",
      now: "2026-09-29T15:40:00+00:00",
      span: { start: "2026-09-28T08:05:00+00:00", end: "2026-09-29T15:40:00+00:00" },
      nodes: [
        {
          id: "1117",
          title: "Linear launch gate",
          bands: [
            { status: "in_progress", from: "2026-09-28T08:05:00+00:00", to: null },
          ],
          runs: [],
          rounds: [],
          merged_at: null,
        },
      ],
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const t = await fetchEpicTimeline(
      "org/repo#42",
      fakeFetch as unknown as typeof fetch,
    );
    expect(fakeFetch).toHaveBeenCalledWith(
      `/api/epics/${encodeURIComponent("org/repo#42")}/timeline`,
    );
    // An open band arrives as `to: null` — "still in this status", not "ended
    // at the epoch".
    expect(t.nodes[0].bands[0].to).toBeNull();
  });
});

describe("fetchEpicNode", () => {
  it("URL-encodes both the epic ref and node id", async () => {
    const dto = { id: "n1", runs: [], lifetime_usd: 0, drain_usd: 0 };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    await fetchEpicNode(
      "org/repo#42",
      "issue/1",
      fakeFetch as unknown as typeof fetch,
    );
    expect(fakeFetch).toHaveBeenCalledWith(
      `/api/epics/${encodeURIComponent("org/repo#42")}/nodes/${encodeURIComponent("issue/1")}`,
    );
  });
});

/** Minimal class-based EventSource mock: captures the constructed URL and
 * lets tests dispatch fake MessageEvents through onmessage. */
class MockEventSource {
  url: string;
  onmessage: ((ev: { data: string }) => void) | null = null;
  static instances: MockEventSource[] = [];
  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }
  emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) });
  }
  close() {}
}

describe("streamEpic", () => {
  it("URL-encodes the ref and does not embed a token", () => {
    MockEventSource.instances = [];
    (
      globalThis as unknown as { EventSource: typeof MockEventSource }
    ).EventSource = MockEventSource;
    streamEpic("org/repo#42", () => {});
    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0].url).toBe(
      `/api/epics/${encodeURIComponent("org/repo#42")}/stream`,
    );
  });

  it("dispatches typed frames to onFrame by type", () => {
    MockEventSource.instances = [];
    (
      globalThis as unknown as { EventSource: typeof MockEventSource }
    ).EventSource = MockEventSource;
    const frames: EpicFrame[] = [];
    streamEpic("epic-1", (f) => frames.push(f));
    const es = MockEventSource.instances[0];

    es.emit({
      type: "snapshot",
      data: {
        id: 1,
        tick_ts: "2026-07-01T00:00:00+00:00",
        snapshot: {
          epic_ref: "epic-1",
          tick_id: 1,
          tick_ts: "2026-07-01T00:00:00+00:00",
          signal: "green",
          counts: {},
          frontier: [],
          nodes: [],
          edges: [],
        },
        actions: {},
        economics: null,
      },
    });
    es.emit({
      type: "economics",
      data: {
        window_start: null,
        drain_total_usd: 2.5,
        node_drain_usd: {},
        label: "drain spend",
      },
    });
    es.emit({
      type: "heartbeat",
      data: {
        last_tick_started_at: null,
        last_tick_ts: null,
        last_tick_duration_s: null,
        tick_count: 0,
        interval_s: null,
        last_signal: null,
        change_count: 0,
      },
    });

    expect(frames.map((f) => f.type)).toEqual([
      "snapshot",
      "economics",
      "heartbeat",
    ]);
    expect(
      frames[0].type === "snapshot" && frames[0].data.snapshot.epic_ref,
    ).toBe("epic-1");
    expect(
      frames[1].type === "economics" && frames[1].data.drain_total_usd,
    ).toBe(2.5);
  });
});

describe("a stream frame that is not JSON (#2285, C16)", () => {
  /** A fresh stream, with the mock EventSource installed. */
  function stream(): { es: MockEventSource; frames: unknown[] } {
    MockEventSource.instances = [];
    (
      globalThis as unknown as { EventSource: typeof MockEventSource }
    ).EventSource = MockEventSource;
    const frames: unknown[] = [];
    streamEpic("epic-1", (f) => frames.push(f));
    return { es: MockEventSource.instances[0], frames };
  }

  it("is dropped rather than thrown inside the event dispatch", () => {
    const { es, frames } = stream();
    // The regression: `JSON.parse` in an `onmessage` throws where no caller has
    // a `try` — one malformed frame became an uncaught exception per frame for
    // as long as the stream stayed open.
    expect(() => es.onmessage?.({ data: "<html>Gateway</html>" })).not.toThrow();
    expect(frames).toEqual([]);
  });

  it("does not stop the frames after it from landing", () => {
    const { es, frames } = stream();
    es.onmessage?.({ data: "not json" });
    es.emit({ type: "heartbeat", data: { epic_ref: "epic-1" } });
    expect(frames).toHaveLength(1);
  });

  it("is dropped on the run event stream too", () => {
    MockEventSource.instances = [];
    (
      globalThis as unknown as { EventSource: typeof MockEventSource }
    ).EventSource = MockEventSource;
    const events: unknown[] = [];
    streamEvents("run-1", (e) => events.push(e));
    const es = MockEventSource.instances[0];
    expect(() => es.onmessage?.({ data: "" })).not.toThrow();
    expect(events).toEqual([]);
    es.emit({ kind: "stage" });
    expect(events).toEqual([{ kind: "stage" }]);
  });
});

describe("fetchApprovals", () => {
  it("fetches hold list from /api/approvals", async () => {
    const dto = [
      { issue_num: 42, estimate_usd: 1.5, basis: "budget exceeded" },
    ];
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const holds = await fetchApprovals(fakeFetch as unknown as typeof fetch);
    expect(holds[0].issue_num).toBe(42);
    expect(fakeFetch).toHaveBeenCalledWith("/api/approvals");
  });
});

describe("fetchFleetStatus", () => {
  it("fetches fleet status from /api/fleet/status", async () => {
    const dto = { stopped: false, pending: 3 };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const s = await fetchFleetStatus(fakeFetch as unknown as typeof fetch);
    expect(s.pending).toBe(3);
    expect(fakeFetch).toHaveBeenCalledWith("/api/fleet/status");
  });
});

describe("fetchBudget", () => {
  it("fetches budget map from /api/config/budget", async () => {
    const dto = {
      "budget.per_issue_usd": { effective: 5, file_value: 5, override: null },
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const b = await fetchBudget(fakeFetch as unknown as typeof fetch);
    expect(b["budget.per_issue_usd"].effective).toBe(5);
    expect(fakeFetch).toHaveBeenCalledWith("/api/config/budget");
  });
});

describe("fetchSession", () => {
  it("reads session state from /api/auth/session", async () => {
    const fakeFetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ authenticated: true, csrf_token: "c" })),
    );
    const s = await fetchSession(fakeFetch as unknown as typeof fetch);
    expect(s.authenticated).toBe(true);
    expect(fakeFetch).toHaveBeenCalledWith("/api/auth/session");
  });
});

describe("login", () => {
  it("POSTs token in body (never a URL) to /api/auth/login", async () => {
    const fakeFetch = vi.fn(
      async () => new Response(JSON.stringify({ csrf_token: "c" })),
    );
    await login("sekret", fakeFetch as unknown as typeof fetch);
    const [url, opts] = fakeFetch.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(url).toBe("/api/auth/login");
    expect(String(url)).not.toContain("sekret");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body as string)).toEqual({ token: "sekret" });
  });
});

describe("decideHold", () => {
  it("POSTs CSRF header and action to /api/approvals/{issue}", async () => {
    (globalThis as unknown as { document: { cookie: string } }).document = {
      cookie: "kew_csrf=tok123",
    };
    const fakeFetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ status: "approved", issue: 42 })),
    );
    await decideHold(
      "42",
      "approve",
      "looks good",
      fakeFetch as unknown as typeof fetch,
    );
    const [url, opts] = fakeFetch.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(url).toBe("/api/approvals/42");
    expect(opts.method).toBe("POST");
    expect((opts.headers as Record<string, string>)["X-Kew-CSRF"]).toBe(
      "tok123",
    );
    expect(JSON.parse(opts.body as string)).toEqual({
      action: "approve",
      reason: "looks good",
    });
  });
});

describe("stopFleet", () => {
  it("POSTs to /api/fleet/stop", async () => {
    const fakeFetch = vi.fn(
      async () => new Response(JSON.stringify({ runs_killed: [] })),
    );
    await stopFleet("web stop", fakeFetch as unknown as typeof fetch);
    expect(
      (fakeFetch.mock.calls[0] as unknown as [string, RequestInit])[0],
    ).toBe("/api/fleet/stop");
  });

  it("reads the killed runs as objects, the shape the backend sends", async () => {
    // FleetStopResultDTO: stopped/already_stopped plus one object per killed
    // run. The old `string[]` typing could not read any of these fields.
    const body = {
      stopped: true,
      already_stopped: false,
      runs_killed: [
        { run_id: "r-1", issue: 42, item_id: "42", cost_usd: 1.25 },
        { run_id: "r-2", issue: 7, item_id: null, cost_usd: 0 },
      ],
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(body)));

    const result = await stopFleet(
      "web stop",
      fakeFetch as unknown as typeof fetch,
    );

    expect(result.stopped).toBe(true);
    expect(result.already_stopped).toBe(false);
    expect(result.runs_killed.map((r) => r.run_id)).toEqual(["r-1", "r-2"]);
    expect(result.runs_killed[0].cost_usd).toBe(1.25);
    expect(result.runs_killed[1].item_id).toBeNull();
  });
});

describe("setBudget", () => {
  it("POSTs numeric value to /api/config/{field}", async () => {
    const fakeFetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({ field: "budget.daily_usd", effective: 20 }),
        ),
    );
    await setBudget(
      "budget.daily_usd",
      20,
      "raise",
      fakeFetch as unknown as typeof fetch,
    );
    const [url, opts] = fakeFetch.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(url).toBe("/api/config/budget.daily_usd");
    expect(JSON.parse(opts.body as string)).toEqual({
      value: 20,
      reason: "raise",
    });
  });
});

describe("a refused write throws the server's own sentence", () => {
  // #3139: the demo build refuses every write with a sentence, and a real
  // refusal says why; `<path> -> <status>` dropped both on the floor.
  const refusing = (body: string, status: number) =>
    (async () => new Response(body, { status })) as unknown as typeof fetch;

  it("throws the body's detail for a POST and a DELETE", async () => {
    const detail = JSON.stringify({ detail: "Demo: this would stop the fleet." });
    await expect(stopFleet("why", refusing(detail, 405))).rejects.toThrow(
      /^Demo: this would stop the fleet\.$/,
    );
    await expect(
      revertBudget("budget.daily_usd", refusing(detail, 405)),
    ).rejects.toThrow(/^Demo: this would stop the fleet\.$/);
  });

  it("keeps `<path> -> <status>` when the body carries no detail", async () => {
    await expect(stopFleet("why", refusing("no", 500))).rejects.toThrow(
      /^\/api\/fleet\/stop -> 500$/,
    );
    await expect(
      revertBudget("budget.daily_usd", refusing(JSON.stringify({ detail: 3 }), 403)),
    ).rejects.toThrow(/^\/api\/config\/budget\.daily_usd -> 403$/);
  });
});

describe("fetchScorecard", () => {
  it("fetches scorecard with days param", async () => {
    const dto = {
      days: 3,
      rows: [
        {
          model: "lemonade/qwen",
          runs: 5,
          opened: 5,
          merged: 5,
          review_total: 0,
          review_n: 0,
          spend: 0,
          by_complexity: [],
        },
      ],
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const s = await fetchScorecard(3, fakeFetch as unknown as typeof fetch);
    expect(s.rows[0].model).toBe("lemonade/qwen");
    expect(s.rows[0].by_complexity).toEqual([]);
    expect(fakeFetch).toHaveBeenCalledWith("/api/report/models?days=3");
  });
});

describe("fetchSpend", () => {
  it("fetches spend windows from /api/report/spend", async () => {
    const dto = {
      daily: { window_days: 1, spent: 3, cap: 10, agentic: 2, interactive: 1 },
      weekly: {
        window_days: 7,
        spent: 5,
        cap: 50,
        agentic: 3.5,
        interactive: 1.5,
      },
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const s = await fetchSpend(fakeFetch as unknown as typeof fetch);
    expect(s.daily.spent).toBe(3);
    expect(s.daily.spent).toBe(s.daily.agentic + s.daily.interactive);
    expect(s.weekly.cap).toBe(50);
    expect(fakeFetch).toHaveBeenCalledWith("/api/report/spend");
  });
});

describe("no function in api.ts embeds a token in a URL", () => {
  it('never puts "token" in any fetch or EventSource URL', async () => {
    MockEventSource.instances = [];
    (
      globalThis as unknown as { EventSource: typeof MockEventSource }
    ).EventSource = MockEventSource;

    const urls: string[] = [];
    const fakeFetch = vi.fn(async (input: RequestInfo | URL) => {
      urls.push(String(input));
      return new Response("{}");
    });
    const f = fakeFetch as unknown as typeof fetch;

    await fetchMeta(f);
    await fetchTimeline("r1", f);
    await fetchDiff("r1", "n1", f);
    await fetchLoops(undefined, f);
    await fetchLoop("L1", f);
    await fetchLoopAudit("L1", f);
    await fetchEpics(f);
    await fetchEpic("epic-1", f);
    await fetchEpicTicks("epic-1", 0, f);
    await fetchEpicNode("epic-1", "n1", f);
    await fetchEpicTimeline("epic-1", f);
    await fetchApprovals(f);
    await fetchFleetStatus(f);
    await fetchBudget(f);
    await fetchSession(f);
    await login("sekret", f);
    await decideHold("1", "approve", "", f);
    await stopFleet("", f);
    await setBudget("budget.daily_usd", 1, "", f);
    await fetchScorecard(3, f);
    await fetchSpend(f);
    await fetchRun("r1", f);
    await fetchFlight("r1", undefined, f);
    await fetchPrompt("r1", undefined, f);

    streamEvents("r1", () => {});
    streamEpic("epic-1", () => {});
    urls.push(...MockEventSource.instances.map((i) => i.url));

    expect(urls.length).toBeGreaterThan(0);
    for (const url of urls) {
      expect(url.toLowerCase()).not.toMatch(/token/);
    }
  });
});

// --- Canonical Flight contract (I16/I17) ------------------------------------
// The run-detail page consumes these three reads and nothing else. Each one
// mirrors a DTO in kew/src/kew/service/models.py; the prompt read is the only
// payload on the whole surface that may carry exact prompt bytes.

describe("fetchRun", () => {
  it("reads one run row by id", async () => {
    const f = vi.fn(
      async () => new Response(JSON.stringify({ run_id: "r 1" })),
    );
    const r = await fetchRun("r 1", f as unknown as typeof fetch);
    expect(r.run_id).toBe("r 1");
    expect(f).toHaveBeenCalledWith("/api/runs/r%201");
  });
});

// --- The Stage and Posture columns (FUI-13) ----------------------------------
// DMonitor's live-runs table reads both off /api/runs. `stage` and
// `stage_detail` are new; the posture fields were always served and simply not
// declared here, which is how the Posture column had nothing to type against.

describe("a run row's stage and posture", () => {
  const row = {
    run_id: "9c41e2b7",
    stage: "review",
    stage_detail: "round 1 · PR #2059",
    posture: "sandboxed",
    filesystem_boundary: "container",
    network_boundary: "proxied",
    egress_verification_basis: null,
    credential_names: ["ANTHROPIC_API_KEY"],
    command_guard: "allowlist",
    admission: "admitted",
    reason: null,
  };

  it("carries the stage a run is executing and the detail under it", async () => {
    const f = vi.fn(async () => new Response(JSON.stringify([row])));
    const [run] = await fetchRuns(f as unknown as typeof fetch);

    expect(run.stage).toBe("review");
    expect(run.stage_detail).toBe("round 1 · PR #2059");
    expect(f).toHaveBeenCalledWith("/api/runs");
  });

  it("carries the sandbox posture the host recorded for the attempt", async () => {
    const f = vi.fn(async () => new Response(JSON.stringify([row])));
    const [run] = await fetchRuns(f as unknown as typeof fetch);

    expect(run.posture).toBe("sandboxed");
    expect(run.filesystem_boundary).toBe("container");
    expect(run.network_boundary).toBe("proxied");
    expect(run.command_guard).toBe("allowlist");
    expect(run.admission).toBe("admitted");
    expect(run.credential_names).toEqual(["ANTHROPIC_API_KEY"]);
    expect(run.egress_verification_basis).toBeNull();
    expect(run.reason).toBeNull();
  });

  it('reads "unknown" as an answer, not as a missing field', async () => {
    // A kind kew has no stage word for. The client must not fall back to
    // a friendlier stage: an empty detail means nothing qualified it.
    const f = vi.fn(
      async () =>
        new Response(
          JSON.stringify([
            { ...row, stage: "unknown", stage_detail: "resolve" },
          ]),
        ),
    );
    const [run] = await fetchRuns(f as unknown as typeof fetch);

    expect(run.stage).toBe("unknown");
    expect(run.stage_detail).toBe("resolve");
  });
});

describe("fetchFlight", () => {
  it("reads the receipt-backed evidence projection for a run", async () => {
    const dto = {
      run_id: "r1",
      attempt_number: 2,
      item_id: "SFD-12",
      issue_num: null,
      source_tracker: "linear",
      source_id: "SFD-12",
      sealed: true,
      through_seq: 4,
      chain_head: "abc",
      chain_verified: true,
      artifact_node_id: "r1:artifact:2",
      runner_telemetry: "structured",
      events: [],
      gaps: [],
      availability: [],
    };
    const f = vi.fn(async () => new Response(JSON.stringify(dto)));
    const flight = await fetchFlight(
      "r1",
      undefined,
      f as unknown as typeof fetch,
    );
    expect(flight.item_id).toBe("SFD-12");
    expect(flight.issue_num).toBeNull();
    expect(f).toHaveBeenCalledWith("/api/runs/r1/flight");
  });

  it("scopes the projection to one attempt when asked", async () => {
    const f = vi.fn(async () => new Response("{}"));
    await fetchFlight("r1", 3, f as unknown as typeof fetch);
    expect(f).toHaveBeenCalledWith("/api/runs/r1/flight?attempt=3");
  });
});

describe("fetchPrompt", () => {
  it("reads the protected prompt with the browser cache switched off", async () => {
    const dto = {
      run_id: "r1",
      availability: "available",
      text: "EXACT BYTES",
    };
    const f = vi.fn(async () => new Response(JSON.stringify(dto)));
    const prompt = await fetchPrompt(
      "r1",
      undefined,
      f as unknown as typeof fetch,
    );
    expect(prompt.text).toBe("EXACT BYTES");
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/runs/r1/prompt");
    // Bytes the operator asked for exactly once must not survive in a disk
    // cache, a bfcache entry or a back-button replay.
    expect(init.cache).toBe("no-store");
    expect(init.credentials).toBe("same-origin");
  });

  it("surfaces the refusal status so the caller can explain it", async () => {
    const f = vi.fn(async () => new Response("no", { status: 403 }));
    await expect(
      fetchPrompt("r1", undefined, f as unknown as typeof fetch),
    ).rejects.toThrow(/-> 403$/);
  });
});

describe("the prompt route is reachable from exactly one client function", () => {
  it("is never fetched by any other typed reader", async () => {
    const urls: string[] = [];
    const f = vi.fn(async (input: RequestInfo | URL) => {
      urls.push(String(input));
      return new Response("{}");
    }) as unknown as typeof fetch;

    await fetchRuns(f);
    await fetchRun("r1", f);
    await fetchFlight("r1", undefined, f);
    await fetchTimeline("r1", f);
    await fetchDiff("r1", "n1", f);

    for (const url of urls) expect(url).not.toMatch(/prompt/);
  });
});

// --- Canonical work projection + typed actions (I19/I20) ---------------------

import { fetchWorkProjection, fetchWorkView, performWorkAction } from "./api";

const OBS = {
  dimension: "judgment",
  availability: "available",
  provenance: "observed",
  observed_at: "2026-08-20T10:00:00Z",
  detail: null,
};

const VIEW = {
  identity: {
    source_tracker: "linear",
    item_id: "SFD-12",
    item_ref: "SFD-12",
    delivery_repo: "acme/app",
    epic_ref: null,
    node_id: null,
    run_id: "r1",
    attempt_number: 1,
  },
  source: {
    tracker: "linear",
    item_state: "open",
    synchronization: "pending",
    observation: OBS,
  },
  route: {
    runner: "claude-code",
    model: "claude-opus-5",
    reported_model: "claude-opus-5",
    containment: "contained",
    provenance: "declared",
    claim: "honored",
  },
  execution: {
    state: "completed",
    run_status: "awaiting_publication",
    attempts: 1,
    started_at: null,
    finished_at: null,
    error_category: "",
  },
  artifact: {
    state: "preserved",
    publication: "pending",
    branch: "agent/issue-12",
    pr_url: null,
    orphan: false,
  },
  judgment: { state: "not_applicable", observation: OBS, decided_at: null },
  outcome: { state: "in_progress", detail: "" },
  integration: {
    checks: "unknown",
    mergeability: "unknown",
    review_decision: "unknown",
    observation: OBS,
  },
  readiness: {
    state: "unproven",
    fingerprint: null,
    evidence_ref: null,
    detail: "",
    observation: OBS,
  },
  no_change: {
    state: "not_applicable",
    judgment_state: "",
    policy: "",
    reasons: [],
    sync_outstanding: 0,
    sync_failed: 0,
    effects_outstanding: 0,
  },
  review_pressure: {
    delivery_repo: "acme/app",
    engaged: true,
    countable: true,
    used: 2,
    total: 5,
    held: 1,
    oldest_hold_age_seconds: 7200,
    diff_burden_lines: 480,
    saturated: false,
    reasons: [],
    enforcement: "inactive",
    enforcement_detail: "",
    policy_generation: 3,
    observation: OBS,
  },
  exception: {
    state: "awaiting_publication",
    detail: "the implementation is preserved but unpublished",
    blocking: false,
    dimension: "artifact",
  },
  safest_action: {
    action: "finalize_publication",
    reason: "publish the artifact this run already produced",
    blocked_by: null,
  },
  actions: [
    {
      action: "finalize_publication",
      intent: "publish the preserved artifact this run already produced",
      target_kind: "run",
      target_ref: "r1",
      safest: true,
      requires_reason: false,
    },
  ],
  view_revision: "rev-abc",
  composed_at: "2026-08-20T10:00:01Z",
  observations: [OBS],
};

describe("fetchWorkProjection", () => {
  it("reads the snapshot-bound projection from /api/dashboard/work", async () => {
    const dto = {
      views: [VIEW],
      pressure: VIEW.review_pressure,
      projection_revision: "proj-1",
      composed_at: "2026-08-20T10:00:01Z",
      unreadable: ["SFD-99"],
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const p = await fetchWorkProjection(fakeFetch as unknown as typeof fetch);
    expect(fakeFetch).toHaveBeenCalledWith("/api/dashboard/work");
    expect(p.projection_revision).toBe("proj-1");
    expect(p.views[0].identity.item_id).toBe("SFD-12");
    // Items whose facts could not be read stay named — never silently dropped.
    expect(p.unreadable).toEqual(["SFD-99"]);
  });
});

describe("fetchWorkView", () => {
  it("reads one item view, carrying the opaque id through the path", async () => {
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(VIEW)));
    const v = await fetchWorkView(
      "SFD-12",
      fakeFetch as unknown as typeof fetch,
    );
    expect(fakeFetch).toHaveBeenCalledWith("/api/verbs/work/SFD-12");
    expect(v.view_revision).toBe("rev-abc");
    expect(v.actions).toHaveLength(1);
  });

  it("encodes an id that needs it rather than splicing it raw into the path", async () => {
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(VIEW)));
    await fetchWorkView("a/b", fakeFetch as unknown as typeof fetch);
    expect(fakeFetch).toHaveBeenCalledWith("/api/verbs/work/a%2Fb");
  });
});

describe("performWorkAction", () => {
  it("posts the action against the revision it was decided at", async () => {
    const result = {
      action: "finalize_publication",
      target_kind: "run",
      target_ref: "r1",
      applied: true,
      idempotent: false,
      detail: "publication requested",
      expected_revision: "rev-abc",
      view: VIEW,
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(result)));
    const res = await performWorkAction(
      "SFD-12",
      "finalize_publication",
      "rev-abc",
      "",
      fakeFetch as unknown as typeof fetch,
    );
    expect(res.ok).toBe(true);
    if (!res.ok) throw new Error("expected ok");
    expect(res.data.applied).toBe(true);
    const [path, init] = fakeFetch.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(path).toBe("/api/verbs/work/SFD-12/action");
    expect(init.method).toBe("POST");
    expect(
      (init.headers as Record<string, string>)["X-Kew-CSRF"],
    ).toBeDefined();
    expect(JSON.parse(init.body as string)).toEqual({
      action: "finalize_publication",
      expected_revision: "rev-abc",
      reason: "",
    });
  });

  it("returns a stale 409 with its detail instead of throwing", async () => {
    const fakeFetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({ detail: "view_revision is no longer current" }),
          {
            status: 409,
          },
        ),
    );
    const res = await performWorkAction(
      "SFD-12",
      "finalize_publication",
      "stale-rev",
      "",
      fakeFetch as unknown as typeof fetch,
    );
    expect(res.ok).toBe(false);
    if (res.ok) throw new Error("expected failure");
    expect(res.status).toBe(409);
    expect(res.detail).toContain("no longer current");
  });

  it("reports a transport failure as status 0 rather than a server verdict", async () => {
    const fakeFetch = vi.fn(async () => {
      throw new Error("offline");
    });
    const res = await performWorkAction(
      "SFD-12",
      "dispatch",
      "rev-abc",
      "",
      fakeFetch as unknown as typeof fetch,
    );
    expect(res.ok).toBe(false);
    if (res.ok) throw new Error("expected failure");
    expect(res.status).toBe(0);
    expect(res.detail).toBe("offline");
  });

  it("reports a 200 that is not JSON instead of throwing (#2285, C16)", async () => {
    // A proxy's interstitial on a 200. The throw used to escape into
    // `runWorkAction`, which only handles the typed result — so the Console row
    // that asked for the action kept its spinner for good.
    const fakeFetch = vi.fn(async () => new Response("<html>Gateway</html>"));
    const res = await performWorkAction(
      "SFD-12",
      "dispatch",
      "rev-abc",
      "",
      fakeFetch as unknown as typeof fetch,
    );
    expect(res.ok).toBe(false);
    if (res.ok) throw new Error("expected failure");
    expect(res.status).toBe(200);
    expect(res.detail).toContain("not with a readable result");
  });
});

// --- Item pipeline projection (FUI-11) --------------------------------------

import { fetchPipeline, type ItemPipeline } from "./api";

const PIPELINE: ItemPipeline = {
  item_id: "2059",
  lanes: [
    {
      name: "main",
      stages: [
        {
          stage: "implement",
          state: "done",
          label: "run f5dcb6a5",
          detail: "success",
          evidence: { kind: "run", ref: "f5dcb6a5" },
        },
        {
          stage: "publish",
          state: "done",
          label: "scan clean",
          detail: "3670e43f",
          evidence: { kind: "output_scan", ref: "3670e43f" },
        },
        {
          stage: "review",
          state: "working",
          label: "round 1",
          detail: "reviewing",
          evidence: null,
        },
        {
          stage: "merge",
          state: "needs_you",
          label: "needs you",
          detail: "merge decision",
          evidence: {
            kind: "pull_request",
            ref: "https://example.invalid/pull/2059",
          },
        },
      ],
    },
  ],
  unavailable: [],
  composed_at: "2026-09-24T10:00:00Z",
};

describe("fetchPipeline", () => {
  it("reads one item pipeline, carrying the opaque id through the path", async () => {
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(PIPELINE)));
    const p = await fetchPipeline("2059", fakeFetch as unknown as typeof fetch);
    expect(fakeFetch).toHaveBeenCalledWith("/api/work/2059/pipeline");
    expect(p.lanes.map((l) => l.name)).toEqual(["main"]);
    expect(p.lanes[0].stages.map((s) => s.stage)).toEqual([
      "implement",
      "publish",
      "review",
      "merge",
    ]);
    expect(p.lanes[0].stages[3].state).toBe("needs_you");
  });

  it("encodes an id that needs it rather than splicing it raw into the path", async () => {
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(PIPELINE)));
    await fetchPipeline("a/b", fakeFetch as unknown as typeof fetch);
    expect(fakeFetch).toHaveBeenCalledWith("/api/work/a%2Fb/pipeline");
  });

  it("keeps a lane or stage it does not recognise instead of assuming a grid", async () => {
    // The payload is open-ended so a later server can add a Reproduce stage and a
    // backport lane without a client change. Nothing here may hard-code five
    // columns or two rows.
    const extended: ItemPipeline = {
      ...PIPELINE,
      lanes: [
        ...PIPELINE.lanes,
        {
          name: "release/2.0",
          stages: [
            {
              stage: "backport",
              state: "pending",
              label: "awaiting",
              detail: "",
              evidence: null,
            },
          ],
        },
      ],
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(extended)));
    const p = await fetchPipeline("2059", fakeFetch as unknown as typeof fetch);
    expect(p.lanes).toHaveLength(2);
    expect(p.lanes[1].stages[0].stage).toBe("backport");
  });

  it("throws on a 404 — an item with no runs has no pipeline", async () => {
    const fakeFetch = vi.fn(
      async () => new Response('{"detail":"item not found"}', { status: 404 }),
    );
    await expect(
      fetchPipeline("9999", fakeFetch as unknown as typeof fetch),
    ).rejects.toThrow("/api/work/9999/pipeline -> 404");
  });

  it("surfaces the unavailable reasons behind an unknown stage", async () => {
    const degraded: ItemPipeline = {
      ...PIPELINE,
      lanes: [
        {
          name: "main",
          stages: [
            {
              stage: "review",
              state: "unknown",
              label: "unknown",
              detail: "the review rounds could not be read",
              evidence: null,
            },
          ],
        },
      ],
      unavailable: ["pipeline:review_rounds"],
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(degraded)));
    const p = await fetchPipeline("2059", fakeFetch as unknown as typeof fetch);
    expect(p.lanes[0].stages[0].state).toBe("unknown");
    expect(p.unavailable).toEqual(["pipeline:review_rounds"]);
  });
});

// --- Item evidence chain (FUI-12) -------------------------------------------

import { fetchEvidence, type ItemEvidence } from "./api";

const EVIDENCE: ItemEvidence = {
  item_id: "2059",
  rows: [
    {
      n: 1,
      label: "Sandboxed",
      event: "sandbox_resolved",
      summary: "sandboxed",
      ts: "2026-09-24T15:24:15+00:00",
      state: "attested",
    },
    {
      n: 2,
      label: "Canonical publication",
      event: "publication_posture",
      summary: "canonical",
      ts: "2026-09-24T15:46:50+00:00",
      state: "attested",
    },
    {
      n: 3,
      label: "Scan clean",
      event: "publication_output_scan",
      summary: "clean · 3670e43f",
      ts: "2026-09-24T15:46:54+00:00",
      state: "attested",
    },
    {
      n: 4,
      label: "Verdict",
      event: "adversarial_review_verdict",
      summary: "round 1 · reviewing",
      ts: "",
      state: "pending",
    },
  ],
  attested_count: 3,
  total: 4,
  chain_verified: true,
  reason: "",
  unavailable: [],
  composed_at: "2026-09-24T18:12:04+00:00",
};

describe("fetchEvidence", () => {
  it("reads the four guarantees in order, three of four attested", async () => {
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(EVIDENCE)));
    const e = await fetchEvidence("2059", fakeFetch as unknown as typeof fetch);
    expect(fakeFetch).toHaveBeenCalledWith("/api/work/2059/evidence");
    expect(e.rows.map((r) => r.n)).toEqual([1, 2, 3, 4]);
    expect(e.rows.map((r) => r.label)).toEqual([
      "Sandboxed",
      "Canonical publication",
      "Scan clean",
      "Verdict",
    ]);
    expect([e.attested_count, e.total]).toEqual([3, 4]);
    expect(e.rows[3].state).toBe("pending");
    // No row without a recorded fact borrows another row's time.
    expect(e.rows[3].ts).toBe("");
  });

  it("encodes an id that needs it rather than splicing it raw into the path", async () => {
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(EVIDENCE)));
    await fetchEvidence("a/b", fakeFetch as unknown as typeof fetch);
    expect(fakeFetch).toHaveBeenCalledWith("/api/work/a%2Fb/evidence");
  });

  it("carries an unverified chain through, with nothing left attested", async () => {
    // The rule this payload exists to protect: a surface may draw a check only
    // for state === "attested", and a broken chain leaves none.
    const tampered: ItemEvidence = {
      ...EVIDENCE,
      rows: EVIDENCE.rows.map((r) =>
        r.state === "attested" ? { ...r, state: "unverified" } : r,
      ),
      attested_count: 0,
      chain_verified: false,
      reason: "the audit chain did not verify",
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(tampered)));
    const e = await fetchEvidence("2059", fakeFetch as unknown as typeof fetch);
    expect(e.chain_verified).toBe(false);
    expect(e.attested_count).toBe(0);
    expect(e.rows.filter((r) => r.state === "attested")).toEqual([]);
    expect(e.reason).toBe("the audit chain did not verify");
  });

  it("does not 404 an item with no runs — four pending rows and a reason", async () => {
    const absent: ItemEvidence = {
      ...EVIDENCE,
      item_id: "9999",
      rows: EVIDENCE.rows.map((r) => ({
        ...r,
        summary: "",
        ts: "",
        state: "pending",
      })),
      attested_count: 0,
      reason: "no run has been dispatched for this item",
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(absent)));
    const e = await fetchEvidence("9999", fakeFetch as unknown as typeof fetch);
    expect(e.rows.map((r) => r.state)).toEqual([
      "pending",
      "pending",
      "pending",
      "pending",
    ]);
    expect(e.reason).toBe("no run has been dispatched for this item");
  });

  it("keeps a state it does not recognise instead of assuming four words", async () => {
    const extended: ItemEvidence = {
      ...EVIDENCE,
      rows: [{ ...EVIDENCE.rows[0], state: "quarantined" }],
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(extended)));
    const e = await fetchEvidence("2059", fakeFetch as unknown as typeof fetch);
    expect(e.rows[0].state).toBe("quarantined");
  });

  it("surfaces the unavailable reasons behind a row nobody could read", async () => {
    const degraded: ItemEvidence = {
      ...EVIDENCE,
      unavailable: ["evidence:audit"],
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(degraded)));
    const e = await fetchEvidence("2059", fakeFetch as unknown as typeof fetch);
    expect(e.unavailable).toEqual(["evidence:audit"]);
  });
});

// --- Item description (FUI-31) ----------------------------------------------

import { fetchDescription, type ItemDescription } from "./api";

const DESCRIPTION: ItemDescription = {
  item_id: "2055",
  availability: "available",
  provenance: "observed",
  observed_at: "2026-09-24T18:11:00+00:00",
  title: "The Work screen has no description",
  url: "https://github.com/kew-dev/kew/issues/2055",
  description: "The Work screen shows no description at all.",
  expected: "The issue body, split into its sections.",
  actual: "An empty panel.",
  reason: "",
  composed_at: "2026-09-24T18:12:04+00:00",
};

describe("fetchDescription", () => {
  it("reads the body split into description, expected and actual", async () => {
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(DESCRIPTION)));
    const d = await fetchDescription("2055", fakeFetch as unknown as typeof fetch);
    expect(fakeFetch).toHaveBeenCalledWith("/api/work/2055/description");
    expect(d.availability).toBe("available");
    expect(d.actual).toBe("An empty panel.");
    // The reading's own time, not the moment the payload was composed.
    expect(d.observed_at).not.toBe(d.composed_at);
  });

  it("encodes an id that needs it rather than splicing it raw into the path", async () => {
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(DESCRIPTION)));
    await fetchDescription("a/b", fakeFetch as unknown as typeof fetch);
    expect(fakeFetch).toHaveBeenCalledWith("/api/work/a%2Fb/description");
  });

  it("carries a failed read as a reason, never as an empty description", async () => {
    // What the fallback renders: there is no body, and the panel must say why
    // instead of drawing a blank section as though the issue were empty.
    const down: ItemDescription = {
      ...DESCRIPTION,
      availability: "unavailable",
      provenance: "error",
      observed_at: null,
      title: "",
      url: null,
      description: "",
      expected: "",
      actual: "",
      reason: "gh could not read the issue: HTTP 503",
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(down)));
    const d = await fetchDescription("2055", fakeFetch as unknown as typeof fetch);
    expect(d.availability).toBe("unavailable");
    expect(d.description).toBe("");
    expect(d.reason).toContain("HTTP 503");
    // And no URL: `url` is the reading's, so an answer with no reading carries
    // none. A surface must render the reason alone rather than compose a link
    // of its own — `work/evidence.ts` keeps that end of it.
    expect(d.url).toBeNull();
  });

  it("keeps a stale reading and the reason it could not be refreshed", async () => {
    const stale: ItemDescription = {
      ...DESCRIPTION,
      availability: "stale",
      provenance: "cached",
      reason: "gh could not read the issue: connection refused",
    };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(stale)));
    const d = await fetchDescription("2055", fakeFetch as unknown as typeof fetch);
    expect(d.availability).toBe("stale");
    expect(d.description).not.toBe("");
    expect(d.reason).not.toBe("");
  });

  it("leaves expected and actual empty for a body with no such headings", async () => {
    const plain: ItemDescription = { ...DESCRIPTION, expected: "", actual: "" };
    const fakeFetch = vi.fn(async () => new Response(JSON.stringify(plain)));
    const d = await fetchDescription("2055", fakeFetch as unknown as typeof fetch);
    expect([d.expected, d.actual]).toEqual(["", ""]);
    expect(d.description).not.toBe("");
  });
});

describe("the audit reads", () => {
  const EMPTY_LIST: AuditList = { events: [], next_after: null };
  const EMPTY_HISTOGRAM: AuditHistogram = {
    since: "2026-09-24T12:00:00+00:00",
    until: "2026-09-24T13:00:00+00:00",
    bucket_s: 600,
    buckets: [],
    total: 0,
  };

  /** The query string the client actually sent. */
  const sent = (url: unknown) =>
    new URLSearchParams(String(url).split("?")[1] ?? "");

  it("carries the window, the item, the epic and every prefix", async () => {
    const fakeFetch = vi.fn(
      async (_url: string) => new Response(JSON.stringify(EMPTY_LIST)),
    );
    await fetchAudit(
      {
        since: "2026-09-24T12:00:00Z",
        until: "2026-09-24T13:00:00Z",
        item: "SFD-232",
        epic: "ui-refine",
        event_prefix: ["epic_node", "dispatch_"],
        limit: 50,
        after: 9,
      },
      fakeFetch as unknown as typeof fetch,
    );
    const q = sent(fakeFetch.mock.calls[0][0]);
    expect(q.get("since")).toBe("2026-09-24T12:00:00Z");
    expect(q.get("until")).toBe("2026-09-24T13:00:00Z");
    expect(q.get("item")).toBe("SFD-232");
    expect(q.get("epic")).toBe("ui-refine");
    // Repeatable, not comma-joined: the backend ORs the values.
    expect(q.getAll("event_prefix")).toEqual(["epic_node", "dispatch_"]);
    expect(q.get("limit")).toBe("50");
    expect(q.get("after")).toBe("9");
  });

  it("sends no filter it was not given", async () => {
    const fakeFetch = vi.fn(
      async () => new Response(JSON.stringify(EMPTY_LIST)),
    );
    await fetchAudit({}, fakeFetch as unknown as typeof fetch);
    expect(fakeFetch).toHaveBeenCalledWith("/api/audit");
  });

  it("asks the histogram for a window and a bucket width", async () => {
    const fakeFetch = vi.fn(
      async (_url: string) => new Response(JSON.stringify(EMPTY_HISTOGRAM)),
    );
    const histogram = await fetchAuditHistogram(
      {
        since: "2026-09-24T12:00:00Z",
        until: "2026-09-24T13:00:00Z",
        bucket_s: 600,
      },
      { epic: "ui-refine" },
      fakeFetch as unknown as typeof fetch,
    );
    const url = String(fakeFetch.mock.calls[0][0]);
    const q = sent(url);
    expect(url.startsWith("/api/audit/histogram?")).toBe(true);
    expect(q.get("bucket_s")).toBe("600");
    expect(q.get("since")).toBe("2026-09-24T12:00:00Z");
    expect(q.get("until")).toBe("2026-09-24T13:00:00Z");
    expect(q.get("epic")).toBe("ui-refine");
    // `limit`/`after` are the feed's cursor and have no meaning here.
    expect(q.has("limit")).toBe(false);
    expect(q.has("after")).toBe(false);
    expect(histogram.total).toBe(0);
  });
});

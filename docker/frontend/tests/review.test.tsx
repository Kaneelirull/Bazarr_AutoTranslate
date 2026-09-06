import { render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReviewApp } from "../src/review/App";

const item = {
  id: 7, itemId: 4, itemType: "episodes", targetLanguage: "et", status: "needs_attention",
  updatedAt: 1_800_000_000, media: { title: "Example Show", episodeCode: "S01E02" },
  allowedActions: ["recheck", "queue_retry", "dismiss"], failureRules: ["copied_source"], actions: [],
} as const;
const listing = { counts: { needsAttention: 1 }, items: [item], pagination: { page: 1, pageSize: 20, total: 1 }, actionsEnabled: true };
const cueListing = { planId: 7, expectedUpdatedAt: item.updatedAt, sourceHash: "a".repeat(64), candidateHash: "b".repeat(64), approvalRevision: 0, decisionRevision: 0, decisionCounts: { approved: 0, retry: 0, undecided: 0 }, scope: "sonarr:1", sourceLanguage: "en", targetLanguage: "et", items: [], pagination: { page: 1, pageSize: 1, total: 0 }, approvals: [], actionsEnabled: true };

function requestPath(input: RequestInfo | URL) { return typeof input === "string" ? input : input.toString(); }

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/review");
});

describe("ReviewApp", () => {
  it("loads after Strict Mode replays the initial request effect", async () => {
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => new Response(JSON.stringify(requestPath(input).includes("/cues") ? cueListing : listing), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    render(<StrictMode><ReviewApp pollInterval={60_000} /></StrictMode>);

    expect((await screen.findAllByText("Example Show")).length).toBeGreaterThan(0);
    expect(fetchMock.mock.calls.filter(([input]) => !requestPath(input).includes("/cues"))).toHaveLength(2);
  });

  it("loads reviews and submits a guarded retry with the concurrency token", async () => {
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => new Response(JSON.stringify(
      init?.method === "POST" ? { outcome: "queued" } : requestPath(input).includes("/cues") ? cueListing : listing
    ), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<ReviewApp pollInterval={60_000} />);

    await user.click(await screen.findByRole("button", { name: "Retry recovery" }));
    await waitFor(() => expect(screen.getByText("Manual retry queued for scheduler admission.")).toBeInTheDocument());
    const [, init] = fetchMock.mock.calls.find(([, options]) => options?.method === "POST")!;
    expect(init.headers["X-Bazarr-Autotranslate-Action"]).toBe("manual-review");
    expect(JSON.parse(init.body)).toEqual({ action: "queue_retry", expectedUpdatedAt: 1_800_000_000 });
  });

  it("keeps existing data visible when refresh fails", async () => {
    let listCalls = 0;
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      if (requestPath(input).includes("/cues")) return new Response(JSON.stringify(cueListing), { status: 200 });
      listCalls += 1;
      return listCalls === 2 ? new Response(JSON.stringify({ error: { message: "temporarily unavailable" } }), { status: 503 }) : new Response(JSON.stringify(listing), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<ReviewApp pollInterval={60_000} />);
    await screen.findAllByText("Example Show");
    await user.click(screen.getByRole("button", { name: "Refresh now" }));
    expect(await screen.findByText(/Could not refresh manual reviews.*temporarily unavailable/)).toBeInTheDocument();
    expect(screen.getAllByText("Example Show").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Retry recovery" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Refresh now" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry recovery" })).toBeEnabled());
  });

  it("preserves draft filters when a background refresh completes", async () => {
    let listCalls = 0;
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      if (requestPath(input).includes("/cues")) return new Response(JSON.stringify(cueListing), { status: 200 });
      listCalls += 1;
      return new Response(JSON.stringify(listing), { status: 200 });
    }));
    const user = userEvent.setup();
    render(<ReviewApp pollInterval={20} />);
    await screen.findAllByText("Example Show");
    await user.type(screen.getByLabelText("Search"), "unapplied draft");
    await waitFor(() => expect(listCalls).toBeGreaterThan(1));
    expect(screen.getByLabelText("Search")).toHaveValue("unapplied draft");
  });

  it("sanitizes bookmarked filters before the first request", async () => {
    window.history.replaceState(null, "", `/review?page=oops&pageSize=500&status=unknown&itemType=bad&sort=nope&direction=sideways&q=${"x".repeat(120)}&language=${"e".repeat(30)}`);
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => new Response(JSON.stringify(requestPath(input).includes("/cues") ? cueListing : listing), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewApp pollInterval={60_000} />);
    await screen.findAllByText("Example Show");
    const request = requestPath(fetchMock.mock.calls.find(([input]) => requestPath(input).startsWith("/api/manual-reviews?"))![0]);
    const query = new URL(request, "http://example.test").searchParams;
    expect(Object.fromEntries(query)).toMatchObject({ page: "1", pageSize: "20", status: "", itemType: "", sort: "updatedAt", direction: "desc" });
    expect(query.get("q")).toHaveLength(100);
    expect(query.get("language")).toHaveLength(20);
  });

  it("can reset filters after the initial list request fails", async () => {
    window.history.replaceState(null, "", "/review?status=dismissed&q=example&review=7");
    let listCalls = 0;
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      if (requestPath(input).includes("/cues")) return new Response(JSON.stringify(cueListing), { status: 200 });
      listCalls += 1;
      return listCalls === 1
        ? new Response(JSON.stringify({ error: { message: "temporarily unavailable" } }), { status: 503 })
        : new Response(JSON.stringify(listing), { status: 200 });
    }));
    const user = userEvent.setup();
    render(<ReviewApp pollInterval={60_000} />);
    await user.click(await screen.findByRole("button", { name: "Reset filters" }));
    await screen.findAllByText("Example Show");
    expect(window.location.search).not.toContain("status=");
    expect(window.location.search).not.toContain("q=");
    expect(new URLSearchParams(window.location.search).get("review")).toBe("7");
  });

  it("resets cue pagination when finishing advances to the next review", async () => {
    const nextItem = { ...item, id: 8, itemId: 5, updatedAt: item.updatedAt + 1, media: { title: "Next Show", episodeCode: "S01E03" } };
    const cue = { cueNumber: 1, timestamp: "00:00:01,000 --> 00:00:02,000", sourceText: "Source", targetText: "Target", sourceCueHash: "c".repeat(64), targetCueHash: "d".repeat(64), reason: "Review", rules: ["copied_source"], canApproveName: true, canApproveCue: true, decision: "approve", context: [] };
    let finished = false;
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (init?.method === "POST") { finished = true; return new Response(JSON.stringify({ outcome: "queued" }), { status: 202 }); }
      if (path.includes("/cues")) {
        const reviewId = Number(path.split("/")[3]);
        const requestedPage = Number(new URL(path, "http://example.test").searchParams.get("page"));
        const total = reviewId === 7 ? 2 : 1;
        const page = Math.min(requestedPage, total);
        return new Response(JSON.stringify({ ...cueListing, planId: reviewId, expectedUpdatedAt: reviewId === 7 ? item.updatedAt : nextItem.updatedAt, items: [{ ...cue, cueNumber: page }], pagination: { page, pageSize: 1, total }, decisionCounts: { approved: total, retry: 0, undecided: 0 } }), { status: 200 });
      }
      return new Response(JSON.stringify(finished
        ? { ...listing, items: [nextItem], pagination: { page: 1, pageSize: 20, total: 1 } }
        : { ...listing, items: [item, nextItem], pagination: { page: 1, pageSize: 20, total: 2 } }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<ReviewApp pollInterval={60_000} />);
    await user.click(await screen.findByRole("button", { name: "Next issue" }));
    expect(await screen.findByText("2 of 2")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Finish review" }));
    expect(await screen.findByText("1 of 1")).toBeInTheDocument();
    expect(screen.getAllByText("Next Show").length).toBeGreaterThan(0);
    expect(fetchMock.mock.calls.some(([input]) => requestPath(input).includes("/8/cues?page=1&pageSize=1"))).toBe(true);
  });

  it("keeps action controls disabled in read-only mode", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => new Response(JSON.stringify(requestPath(input).includes("/cues") ? { ...cueListing, actionsEnabled: false } : { ...listing, actionsEnabled: false }), { status: 200 })));
    render(<ReviewApp pollInterval={60_000} />);
    expect(await screen.findByRole("button", { name: "Ignore review" })).toBeDisabled();
    expect(screen.getByText(/Manual actions are disabled/)).toBeInTheDocument();
  });

  it("keeps filters and actions interactive during background polling", async () => {
    const pending = new Promise<Response>(() => undefined);
    let listCalls = 0;
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (requestPath(input).includes("/cues")) return Promise.resolve(new Response(JSON.stringify(cueListing), { status: 200 }));
      listCalls += 1; return listCalls === 1 ? Promise.resolve(new Response(JSON.stringify(listing), { status: 200 })) : pending;
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewApp pollInterval={5} />);

    expect((await screen.findAllByText("Example Show")).length).toBeGreaterThan(0);
    await waitFor(() => expect(listCalls).toBe(2));
    expect(screen.getByLabelText("Search")).toBeEnabled();
    expect(screen.getByRole("button", { name: "Retry recovery" })).toBeEnabled();
    expect(screen.getByText("Refreshing review records in the background")).toBeInTheDocument();
  });

  it("keeps mutations single-flight until their protected refresh completes", async () => {
    let finishRefresh!: (response: Response) => void;
    const protectedRefresh = new Promise<Response>((resolve) => { finishRefresh = resolve; });
    let listCalls = 0;
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") return Promise.resolve(new Response(JSON.stringify({ outcome: "resolved" }), { status: 200 }));
      if (requestPath(input).includes("/cues")) return Promise.resolve(new Response(JSON.stringify(cueListing), { status: 200 }));
      listCalls += 1; return listCalls === 1 ? Promise.resolve(new Response(JSON.stringify(listing), { status: 200 })) : protectedRefresh;
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewApp pollInterval={60_000} />);

    await userEvent.click(await screen.findByRole("button", { name: "Recheck files" }));
    await waitFor(() => expect(listCalls).toBe(2));
    expect(screen.getByRole("button", { name: "Recheck files" })).toBeDisabled();
    finishRefresh(new Response(JSON.stringify(listing), { status: 200 }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Recheck files" })).toBeEnabled());
  });

  it("groups secondary controls under More filters", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => new Response(JSON.stringify(requestPath(input).includes("/cues") ? cueListing : listing), { status: 200 })));
    render(<ReviewApp pollInterval={60_000} />);
    await screen.findAllByText("Example Show");
    expect(screen.getByRole("button", { name: "More filters" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByLabelText("Status")).toBeInTheDocument();
  });
});

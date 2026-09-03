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

afterEach(() => vi.unstubAllGlobals());

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
      return listCalls === 1 ? new Response(JSON.stringify(listing), { status: 200 }) : new Response(JSON.stringify({ error: { message: "temporarily unavailable" } }), { status: 503 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<ReviewApp pollInterval={60_000} />);
    await screen.findAllByText("Example Show");
    await user.click(screen.getByRole("button", { name: "Refresh now" }));
    expect(await screen.findByText(/Could not refresh manual reviews.*temporarily unavailable/)).toBeInTheDocument();
    expect(screen.getAllByText("Example Show").length).toBeGreaterThan(0);
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

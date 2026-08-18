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

afterEach(() => vi.unstubAllGlobals());

describe("ReviewApp", () => {
  it("loads after Strict Mode replays the initial request effect", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify(listing), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    render(<StrictMode><ReviewApp pollInterval={60_000} /></StrictMode>);

    expect(await screen.findByText("Example Show")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("loads reviews and submits a guarded retry with the concurrency token", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(listing), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ outcome: "queued" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(listing), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<ReviewApp pollInterval={60_000} />);

    await user.click(await screen.findByRole("button", { name: "Queue manual retry" }));
    await waitFor(() => expect(screen.getByText("Manual retry queued for scheduler admission.")).toBeInTheDocument());
    const [, init] = fetchMock.mock.calls[1];
    expect(init.headers["X-Bazarr-Autotranslate-Action"]).toBe("manual-review");
    expect(JSON.parse(init.body)).toEqual({ action: "queue_retry", expectedUpdatedAt: 1_800_000_000 });
  });

  it("keeps existing data visible when refresh fails", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(listing), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: { message: "temporarily unavailable" } }), { status: 503 }));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<ReviewApp pollInterval={60_000} />);
    await screen.findByText("Example Show");
    await user.click(screen.getByRole("button", { name: "Refresh now" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("temporarily unavailable");
    expect(screen.getByText("Example Show")).toBeInTheDocument();
  });

  it("keeps action controls disabled in read-only mode", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...listing, actionsEnabled: false }), { status: 200 })));
    render(<ReviewApp pollInterval={60_000} />);
    expect(await screen.findByRole("button", { name: "Dismiss" })).toBeDisabled();
    expect(screen.getByText(/Manual actions are disabled/)).toBeInTheDocument();
  });
});

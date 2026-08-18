import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DashboardApp } from "../src/dashboard/App";
import { StatusBadge } from "../src/dashboard/format";
import type { StatusSnapshot } from "../src/dashboard/types";

const snapshot: StatusSnapshot = {
  generatedAt: new Date().toISOString(), service: { phase: "cycle_work", nextCycleAt: null },
  currentCycle: { number: 42, initial: 1, done: 0, remaining: 1, translating: 1 },
  activeJobs: [{ key: "job-1", title: "Example Show", episodeCode: "S01E02", targetLanguage: "et", state: "translating", operation: "translation", workKind: "cycle", startedAt: new Date().toISOString(), estimatedSeconds: 100 }],
  upNext: [], retryPlans: [{ id: 8, manualReview: true }], recentOutcomes: [],
  timing: {}, circuits: [], history: {}, validationObservations: [], maintenance: { activeJobs: [], recentOutcomes: [], history: {} },
};

afterEach(() => vi.unstubAllGlobals());

describe("DashboardApp", () => {
  it("keeps an explanatory status tooltip open when clicked", async () => {
    const user = userEvent.setup();
    render(<StatusBadge state="failed" reason="Provider timed out" />);
    const trigger = screen.getByRole("button", { name: "failed" });

    await user.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("tooltip")).toHaveTextContent("Provider timed out");

    await user.keyboard("{Escape}");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("renders bootstrap status and switches stable work views", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const user = userEvent.setup();
    render(<DashboardApp initialSnapshot={snapshot} />);
    expect(screen.getByRole("heading", { name: "Translation status" })).toBeInTheDocument();
    expect(screen.getByText("Example Show")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Manual review (1)" })).toHaveAttribute("href", "/review");
    await user.click(screen.getByRole("button", { name: "Up next" }));
    expect(screen.getByText("No queued jobs.")).toBeInTheDocument();
  });

  it("refreshes from the production endpoint without replacing the page shell", async () => {
    const next = { ...snapshot, currentCycle: { ...snapshot.currentCycle, done: 1, accepted: 1 } };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(next), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<DashboardApp initialSnapshot={snapshot} />);
    const heading = screen.getByRole("heading", { name: "Translation status" });
    await user.click(screen.getByRole("button", { name: "Refresh now" }));
    await waitFor(() => expect(screen.getByText(/1 of 1 complete/)).toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "Translation status" })).toBe(heading);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/status");
  });

  it("retains current data and reports delayed updates after a failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: "offline" }), { status: 503 })));
    const user = userEvent.setup();
    render(<DashboardApp initialSnapshot={snapshot} />);
    await user.click(screen.getByRole("button", { name: "Refresh now" }));
    expect(await screen.findByText("Update delayed")).toBeInTheDocument();
    expect(screen.getByText("Example Show")).toBeInTheDocument();
  });
});

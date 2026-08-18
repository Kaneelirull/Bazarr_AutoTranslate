import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DashboardApp } from "../src/dashboard/App";
import { attentionReasons } from "../src/dashboard/HealthHistory";
import { StatusBadge } from "../src/dashboard/format";
import { mergeRecentOutcomes } from "../src/dashboard/Work";
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
  it("merges translation and maintenance outcomes newest first", () => {
    const rows = mergeRecentOutcomes(
      [{ title: "Translation", timestamp: "2026-08-18T08:00:00Z" }],
      [{ title: "Maintenance", timestamp: "2026-08-18T09:00:00Z" }],
    );
    expect(rows.map((row) => row.title)).toEqual(["Maintenance", "Translation"]);
    expect(rows.map((row) => row.workKind)).toEqual(["maintenance", "cycle"]);
  });

  it("treats recent failed maintenance as health attention", () => {
    expect(attentionReasons({}, [], {}, [], { recentOutcomes: [{ outcome: "failed" }] })).toContain("maintenance failures");
  });

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

  it("uses Recent as the idle Auto view", () => {
    vi.stubGlobal("fetch", vi.fn());
    render(<DashboardApp initialSnapshot={{
      ...snapshot,
      activeJobs: [],
      recentOutcomes: [{ key: "recent-1", title: "Finished Show", targetLanguage: "et", operation: "translation", outcome: "accepted", timestamp: "2026-08-18T09:00:00Z" }],
      currentCycle: { ...snapshot.currentCycle, translating: 0 },
    }} />);
    expect(screen.getByText("Finished Show")).toBeInTheDocument();
    expect(screen.getByText(/Recent · 1 completed/)).toBeInTheDocument();
  });

  it("opens Health & history when a refresh introduces attention", async () => {
    const next = { ...snapshot, currentCycle: { ...snapshot.currentCycle, failed: 1 } };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(next), { status: 200 })));
    const user = userEvent.setup();
    render(<DashboardApp initialSnapshot={snapshot} />);
    const disclosure = screen.getByText("Health & history").closest("details");
    expect(disclosure).not.toHaveAttribute("open");
    await user.click(screen.getByRole("button", { name: "Refresh now" }));
    await waitFor(() => expect(disclosure).toHaveAttribute("open"));
    expect(screen.getByText("1 area needs attention")).toBeInTheDocument();
  });

  it("switches history type and window inside the consolidated panel", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const user = userEvent.setup();
    render(<DashboardApp initialSnapshot={{ ...snapshot, history: { "7d": { accepted: 12 } } }} />);
    await user.click(screen.getByText("Health & history"));
    await user.click(screen.getByRole("tab", { name: "History" }));
    await user.selectOptions(screen.getByLabelText("History window"), "7d");
    expect(screen.getByText("12")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Maintenance" }));
    expect(screen.getByRole("button", { name: "Maintenance" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("tab", { name: "Health" }));
    await user.click(screen.getByRole("tab", { name: "History" }));
    expect(screen.getByLabelText("History window")).toHaveValue("7d");
    await user.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "Observations 0" })).toHaveAttribute("aria-selected", "true");
  });
});

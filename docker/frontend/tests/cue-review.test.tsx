import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { CueReview } from "../src/review/CueReview";

const item = { id: 1, updatedAt: 9, media: { title: "Example Show" } };
const cue = { cueNumber: 12, timestamp: "00:01:00,000 --> 00:01:02,000", sourceText: "<i>Alex Martin</i>\nA-L-E-X", targetText: "<script>alert(1)</script>\nAlex Martin", targetCueHash: "d".repeat(64), reason: "Possible unchanged name", rules: ["ambiguous_copied_source"], canApproveName: true, context: [{ cueNumber: 11, sourceText: "Before", targetText: "Före" }] };
const data = { planId: 1, expectedUpdatedAt: 9, sourceHash: "a".repeat(64), candidateHash: "b".repeat(64), approvalRevision: 3, scope: "sonarr:42", sourceLanguage: "en", targetLanguage: "sv", items: [cue], pagination: { page: 1, pageSize: 20, total: 1 }, approvals: [{ id: 5, sourceText: "Remembered name", targetText: "Remembered name" }], actionsEnabled: true, candidateAvailable: true };
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
afterEach(() => vi.unstubAllGlobals());

it("preserves text, escapes markup and submits only identifiers and revisions using the keyboard", async () => {
  const fetch = vi.fn().mockImplementation(async (_url, init) => response(init?.method ? { outcome: "queued" } : data));
  vi.stubGlobal("fetch", fetch);
  const mutation = vi.fn();
  const user = userEvent.setup();
  const { container } = render(<CueReview item={item} disabled={false} onMutation={mutation} />);
  const accept = await screen.findByRole("button", { name: "Accept as name and remember" });
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector(".cue-text")?.textContent).toBe(cue.sourceText);
  expect(screen.getByText(/sonarr:42/)).toBeInTheDocument();
  accept.focus(); await user.keyboard("{Enter}");
  await waitFor(() => expect(mutation).toHaveBeenCalledWith(false, expect.stringContaining("Recovery is queued")));
  const request = fetch.mock.calls.find(([, init]) => init?.method === "POST")![1];
  expect(JSON.parse(request.body)).toEqual({ action: "approve_name", expectedUpdatedAt: 9, approvalRevision: 3, sourceHash: data.sourceHash, candidateHash: data.candidateHash, cueNumber: 12, targetCueHash: cue.targetCueHash });
});

it("blocks stale decisions until evidence refresh succeeds", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (_url, init) => init?.method ? response({ error: { message: "Review changed" } }, 409) : response(data)));
  const user = userEvent.setup();
  render(<CueReview item={item} disabled={false} onMutation={vi.fn()} />);
  await user.click(await screen.findByRole("button", { name: "Accept as name and remember" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Review changed");
  expect(screen.getByRole("button", { name: "Accept as name and remember" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Refresh cue comparison" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Accept as name and remember" })).toBeEnabled());
});

it("shows unavailable artifacts while allowing scoped revocation", async () => {
  const missing = { ...data, items: [], candidateAvailable: false, actionsEnabled: false, unavailableReason: "Candidate unavailable" };
  const fetch = vi.fn().mockImplementation(async (_url, init) => response(init?.method ? { outcome: "revoked" } : missing));
  vi.stubGlobal("fetch", fetch);
  const user = userEvent.setup();
  render(<CueReview item={item} disabled={false} onMutation={vi.fn()} />);
  await user.click(await screen.findByRole("button", { name: "Forget approval" }));
  expect(JSON.parse(fetch.mock.calls.find(([, init]) => init?.method === "POST")![1].body)).toEqual({ action: "revoke_name", expectedUpdatedAt: 9, approvalRevision: 3, approvalId: 5 });
  expect(screen.queryByText("No unresolved cue findings in this candidate.")).toBeNull();
});

it("disables approval and revocation with global actions disabled", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(data)));
  render(<CueReview item={item} disabled onMutation={vi.fn()} />);
  expect(await screen.findByRole("button", { name: "Accept as name and remember" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Forget approval" })).toBeDisabled();
});

import { expect, test, type Page } from "@playwright/test";

const NOW = new Date("2026-08-18T12:00:00Z");
const baseStatus = {
  generatedAt: NOW.toISOString(),
  service: { phase: "cycle_work", nextCycleAt: "2026-08-18T12:20:00Z", recoveryDiagnostics: { providerHealth: { malformed_response: 0 } } },
  currentCycle: { number: 42, initial: 8, done: 3, remaining: 5, queued: 3, translating: 1, validating: 1, accepted: 3 },
  completedCycle: 41,
  activeJobs: [{ key: "job-1", title: "The Last of Us", episodeCode: "S02E04", episodeTitle: "Day One", itemType: "episodes", targetLanguage: "et", state: "translating", operation: "translation", workKind: "cycle", startedAt: "2026-08-18T11:58:00Z", estimatedSeconds: 420 }],
  upNext: [{ key: "job-2", title: "Severance", episodeCode: "S02E02", itemType: "episodes", targetLanguage: "sv", queuedAt: "2026-08-18T11:55:00Z" }],
  recentOutcomes: [{ key: "recent-1", title: "Slow Horses", episodeCode: "S04E01", itemType: "episodes", targetLanguage: "et", operation: "translation", outcome: "accepted", durationSeconds: 386, attempts: 1, timestamp: "2026-08-18T11:45:00Z" }],
  retryPlans: [{ id: 8, itemType: "episodes", itemId: 8, displayTitle: "Foundation", targetLanguage: "et", state: "regeneration_waiting", eligibleCompletedCycle: 42, attemptCount: 1, manualReview: false }, { id: 9, manualReview: true }],
  retryMaxAttempts: 5,
  timing: { file: { secondsPerCue: 1.8, sampleCount: 12 }, repair: { secondsPerCue: 3.4, sampleCount: 6 } },
  circuits: [],
  validationObservations: [],
  history: { "24h": { accepted: 18, repaired: 3, failed: 1 }, "7d": { accepted: 84, repaired: 12, failed: 4 } },
  maintenance: { activeJobs: [], recentOutcomes: [{ key: "maintenance-1", title: "Library scan", workKind: "maintenance", operation: "library_scan", outcome: "accepted", durationSeconds: 42, timestamp: "2026-08-18T11:30:00Z" }], history: { "24h": { repaired: 4, pruned: 2 }, "7d": { repaired: 20, pruned: 8 } }, lastScan: { timestamp: "2026-08-18T11:30:00Z", metrics: { repaired: 4, pruned: 2 } } },
};

const warningStatus = {
  ...baseStatus,
  currentCycle: { ...baseStatus.currentCycle, failed: 1, quarantined: 1 },
  validationObservations: [{ itemType: "episodes", itemId: 4, title: "Silo", episodeCode: "S02E01", targetLanguage: "et", cueNumber: 91, classification: "ambiguous", reason: "Copied-source repair was suppressed after validation.", timestamp: "2026-08-18T11:50:00Z", evidence: { similarity: 0.94, tokenCount: 12, cueLanguage: "en" } }],
};

const reviewPayload = {
  counts: { needsAttention: 2, manuallyQueued: 1, resolved: 7, dismissed: 3 }, actionsEnabled: true,
  pagination: { page: 1, pageSize: 20, total: 2 },
  items: [{ id: 7, itemId: 4, itemType: "episodes", targetLanguage: "et", status: "needs_attention", updatedAt: 1787053500, media: { title: "Example Show With A Longer Name", episodeCode: "S01E02" }, allowedActions: ["recheck", "queue_retry", "dismiss"], failureRules: ["copied_source"], actions: [] }, { id: 8, itemId: 9, itemType: "movies", targetLanguage: "sv", status: "manually_queued", updatedAt: 1787053000, media: { title: "Example Movie" }, allowedActions: [], failureRules: ["undersized"], actions: [] }],
};

async function prepare(page: Page) {
  await page.clock.setFixedTime(NOW);
  await page.route("**/api/manual-reviews?**", (route) => route.fulfill({ json: reviewPayload }));
  await page.route("**/api/logs?**", (route) => route.fulfill({ json: { lines: ["2026-08-18 11:59:58 [INFO] Cycle 42 started", "2026-08-18 11:59:59 [WARNING] Retry queued for Foundation", "2026-08-18 12:00:00 [INFO] Translation active for The Last of Us"], nextCursor: null, sanitized: true } }));
}

async function screenshot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await expect(page).toHaveScreenshot(`${name}.png`, { fullPage: true });
}

test.beforeEach(async ({ page }) => { await prepare(page); });

test("healthy status", async ({ page }) => {
  await page.route("**/api/status", (route) => route.fulfill({ json: baseStatus }));
  await page.goto("/");
  await page.getByRole("button", { name: "Refresh now" }).click();
  await expect(page.getByText("All systems healthy")).toBeVisible();
  await screenshot(page, "status-healthy");
});

test("warning status", async ({ page }) => {
  await page.route("**/api/status", (route) => route.fulfill({ json: warningStatus }));
  await page.goto("/");
  await page.getByRole("button", { name: "Refresh now" }).click();
  await expect(page.locator(".health-history")).toHaveAttribute("open", "");
  await screenshot(page, "status-warning");
});

test("manual review", async ({ page }) => {
  await page.goto("/review");
  await expect(page.getByText("Example Show With A Longer Name")).toBeVisible();
  await screenshot(page, "manual-review");
});

test("name review comparisons and keyboard approval", async ({ page }, testInfo) => {
  const source = "Alexandra Martin.\nA-L-E-X-A-N-D-R-A. <i>Original markup</i>";
  const translated = "Alexandra Martin.\nA-L-E-X-A-N-D-R-A. <script>escaped markup</script>";
  await page.route("**/api/manual-reviews/7/cues?**", (route) => route.fulfill({ json: {
    planId: 7, expectedUpdatedAt: 1787053500, sourceHash: "a".repeat(64), candidateHash: "b".repeat(64),
    approvalRevision: 2, scope: "sonarr:42", sourceLanguage: "en", targetLanguage: "et", candidateAvailable: true,
    actionsEnabled: true, pagination: { page: 1, pageSize: 20, total: 1 }, approvals: [],
    items: [{ cueNumber: 623, timestamp: "00:23:46,000 --> 00:23:48,000", sourceText: source, targetText: translated,
      targetCueHash: "c".repeat(64), reason: "Possible unchanged name needs review.", rules: ["ambiguous_copied_source"], canApproveName: true,
      context: [{ cueNumber: 622, sourceText: "An adjacent line with enough text to check wrapping and comparison across the two languages.", targetText: "Kõrval olev rida pikema tekstiga, et võrrelda ridu mõlemas keeles." }] }],
  } }));
  let request: Record<string, unknown> | undefined;
  await page.route("**/api/manual-reviews/7/actions", async (route) => {
    request = route.request().postDataJSON();
    await route.fulfill({ json: { outcome: "queued" }, status: 202 });
  });
  await page.goto("/review");
  await page.getByText("Recovery details", { exact: true }).first().click();
  const comparison = page.locator(".cue-comparison").first();
  await expect(comparison).toContainText(source);
  await comparison.getByText("Adjacent cues", { exact: true }).click();
  await expect(comparison.locator("script")).toHaveCount(0);
  const columns = comparison.locator(".cue-text-grid").first().locator(":scope > div");
  const left = await columns.nth(0).boundingBox();
  const right = await columns.nth(1).boundingBox();
  if (testInfo.project.name.startsWith("mobile")) expect(right!.y).toBeGreaterThan(left!.y);
  else expect(right!.x).toBeGreaterThan(left!.x);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await screenshot(page, "name-review");
  await comparison.getByRole("button", { name: "Accept as name and remember" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("Name remembered. Recovery is queued; the full file still needs validation.")).toBeVisible();
  expect(request).toMatchObject({ action: "approve_name", approvalRevision: 2, cueNumber: 623 });
  expect(request).not.toHaveProperty("sourceText");
});

test("logs", async ({ page }) => {
  await page.goto("/logs");
  await expect(page.getByText(/Cycle 42 started/)).toBeVisible();
  await screenshot(page, "logs");
});

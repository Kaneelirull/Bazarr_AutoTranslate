import { defineConfig } from "@playwright/test";

const variants = [
  ["desktop-dark", { width: 1600, height: 1000 }, "dark"],
  ["desktop-light", { width: 1600, height: 1000 }, "light"],
  ["mobile-dark", { width: 390, height: 844 }, "dark"],
  ["mobile-light", { width: 390, height: 844 }, "light"],
] as const;

export default defineConfig({
  testDir: "./tests/visual",
  snapshotPathTemplate: "{testDir}/__screenshots__/{testFileName}/{projectName}/{arg}{ext}",
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "list",
  expect: {
    toHaveScreenshot: {
      animations: "disabled",
      maxDiffPixelRatio: 0.005,
    },
  },
  use: {
    baseURL: "http://127.0.0.1:8876",
    browserName: "chromium",
    reducedMotion: "reduce",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "python ../../scripts/ui_preview.py",
    cwd: import.meta.dirname,
    port: 8876,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
  projects: variants.map(([name, viewport, colorScheme]) => ({ name, use: { viewport, colorScheme } })),
});

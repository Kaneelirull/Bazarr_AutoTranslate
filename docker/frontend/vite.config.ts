import { resolve } from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  base: "/assets/app/",
  plugins: [react()],
  build: {
    outDir: "../static/app",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      input: {
        dashboard: resolve(import.meta.dirname, "src/dashboard/main.tsx"),
        review: resolve(import.meta.dirname, "src/review/main.tsx"),
        logs: resolve(import.meta.dirname, "src/logs/main.tsx"),
      },
      output: {
        entryFileNames: "[name].js",
        chunkFileNames: "chunks/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./tests/setup.ts",
    restoreMocks: true,
    clearMocks: true,
  },
});

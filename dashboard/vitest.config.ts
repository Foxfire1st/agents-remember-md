import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// Standalone Vitest config so vite.config.ts keeps pure Vite types: the project's Vite and
// Vitest's bundled Vite are distinct instances, and sharing one `defineConfig` clashes on
// plugin types. Direct Vitest runs are supported as fast diagnostic unit/component loops; only
// the pinned Dagger graph can certify acceptance. Playwright and the Python acceptance suite keep
// their Dagger attestation boundary. No React plugin is needed here.
export default defineConfig({
  define: {
    __AR_DASHBOARD_BUILD__: JSON.stringify("test-dashboard-build"),
  },
  resolve: {
    alias: {
      // Under vitest, the package's `node` export condition resolves to an "edge-light" build
      // that skips layout effects — panels then never receive their initial layout and the
      // imperative collapse/expand API asserts. Alias to the browser development build: the
      // code path the real app runs (FEUI-L1: the sessions view drives collapse via commands).
      "react-resizable-panels": fileURLToPath(
        new URL(
          "./node_modules/react-resizable-panels/dist/react-resizable-panels.browser.development.js",
          import.meta.url,
        ),
      ),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    // Unbounded forks saturated this shared host and drove it into swap; keep every
    // config-backed run at the measured safe ceiling.
    maxWorkers: 2,
    setupFiles: ["./src/test/setup.ts"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.test.{ts,tsx}",
        "src/**/*.test-utils.{ts,tsx}",
        "src/test/**",
        "src/dev/**",
        "src/types/**",
        "src/vite-env.d.ts",
      ],
      // Coverage is diagnostic; percentages never fail delivery.
      reporter: ["text", "json", "html"],
      reportsDirectory: "coverage",
    },
    // Vitest owns logic tests under src/; the e2e/ Playwright specs (which import
    // @playwright/test) are run by `npm run e2e`, never collected here.
    include: ["src/**/*.{test,spec}.{ts,tsx}", "scripts/**/*.test.mjs"],
  },
});

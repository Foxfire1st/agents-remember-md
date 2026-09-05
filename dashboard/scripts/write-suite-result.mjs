// Persist one real, deterministic dashboard-suite result JSON into {reports}
// after a successful vitest coverage run (CCR-L29 Gate-2 suite-result artifact).
//
// The runner exports {reports} to the host; the reports directory is injected
// into this process through AR_QUALITY_PROGRESS_REPORT (set by the executor to
// {reports}/quality-progress.json), exactly like every other rail report. The
// suite "passed" fact is truthful:  chains this script
// after , so it only executes when vitest exited 0. Coverage facts
// come from the coverage JSON vitest just wrote.
import { dirname, join } from "node:path";
import { readFileSync, writeFileSync } from "node:fs";

const progressReport = process.env.AR_QUALITY_PROGRESS_REPORT || "";
const reportsRoot = progressReport ? dirname(progressReport) : ".";
const coveragePath = join(process.cwd(), "coverage", "coverage-final.json");

let totals = {};
try {
  const coverage = JSON.parse(readFileSync(coveragePath, "utf8"));
  if (coverage && coverage.total) {
    totals = Object.fromEntries(
      Object.entries(coverage.total).map(([key, value]) => [
        key,
        Number.isFinite(value?.percent) ? Math.round(value.percent * 1000) / 1000 : null,
      ]),
    );
  }
} catch {
  // A passed run without a readable coverage JSON still records the pass fact.
}

const result = {
  suite: "dashboard-suite",
  schemaVersion: "dashboard-suite-result/v1",
  passed: true,
  coverageJson: "coverage/coverage-final.json",
  totals,
};

writeFileSync(
  join(reportsRoot, "dashboard-suite-result.json"),
  JSON.stringify(result, null, 2) + "\n",
  "utf8",
);

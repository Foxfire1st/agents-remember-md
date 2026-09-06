#!/usr/bin/env node
// Diagnostic changed-line coverage for dashboard/src; no mandatory percentage.
// A changed comment/blank/continuation line carries no executable statement and contributes
// nothing (same accounting as Coverage.py's executed/missing lines). Resolves AR_GATE_DIFF_BASE,
// then GITHUB_BASE_REF, then the upstream, then origin/HEAD, then main, then git's empty tree.
// Requires the v8 coverage JSON emitted by `npm run test:coverage` (coverage/coverage-final.json).
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { requireDaggerTestEnvironment } from "./require-dagger-test-environment.mjs";

/** Lines v8 records as executable statements: every line inside a statement range. */
export function executableStatementLines(entry) {
  const lines = new Set();
  for (const statement of Object.values(entry.statementMap ?? {})) {
    for (let line = statement.start.line; line <= statement.end.line; line += 1) {
      lines.add(line);
    }
  }
  return lines;
}

/** Lines inside statement ranges whose execution count is positive. */
export function coveredStatementLines(entry) {
  const lines = new Set();
  for (const [id, count] of Object.entries(entry.s ?? {})) {
    if (count <= 0) continue;
    const statement = entry.statementMap?.[id];
    if (!statement) continue;
    for (let line = statement.start.line; line <= statement.end.line; line += 1) {
      lines.add(line);
    }
  }
  return lines;
}

/**
 * Score changed lines against the v8 report. Denominator: changed lines v8 records as executable
 * statements (comments/blanks/continuations contribute nothing); numerator: those covered.
 * Files with no v8 entry record no executable statements, so they contribute nothing.
 */
export function measureDiffCoverage(changedLines, coverageByRelative) {
  let changedTotal = 0;
  let coveredTotal = 0;
  const missing = [];
  for (const [file, lines] of changedLines) {
    const key = file.startsWith("dashboard/")
      ? file.slice("dashboard/".length)
      : file;
    if (!key.startsWith("src/")) continue;
    if (
      key.endsWith(".test.ts") ||
      key.endsWith(".test.tsx") ||
      key.startsWith("src/test/") ||
      key.startsWith("src/dev/") ||
      key.startsWith("src/types/")
    ) {
      continue;
    }
    const entry = coverageByRelative.get(key);
    if (!entry) continue;
    const executable = executableStatementLines(entry);
    const covered = coveredStatementLines(entry);
    for (const line of lines) {
      if (!executable.has(line)) continue;
      changedTotal += 1;
      if (covered.has(line)) {
        coveredTotal += 1;
      } else {
        missing.push(`${key}:${line}`);
      }
    }
  }
  return { covered: coveredTotal, total: changedTotal, missing };
}

function main() {
  requireDaggerTestEnvironment("changed-lines coverage");
  const dashboardRoot = fileURLToPath(new URL("..", import.meta.url));
  const emptyTree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904";
  const git = (args) =>
    execFileSync("git", args, {
      cwd: dashboardRoot,
      encoding: "utf8",
      // The diff against a series fork point can be large; never let the default 1 MiB pipe
      // buffer turn a legitimate diff into a spawn failure.
      maxBuffer: 256 * 1024 * 1024,
    }).trim();
  const revisionExists = (revision) => {
    try {
      git(["rev-parse", "--verify", revision]);
      return true;
    } catch {
      return false;
    }
  };
  const mergeBaseWith = (revision) => {
    try {
      return git(["merge-base", revision, "HEAD"]);
    } catch {
      return null;
    }
  };
  // Mirror the Python resolver's candidate order: AR_GATE_DIFF_BASE, then the pull-request
  // base (GITHUB_BASE_REF), then the branch's upstream, then origin/HEAD, then main. The first
  // resolvable merge base wins; otherwise the diff falls back to the empty tree.
  const resolveBase = () => {
    const explicit = (process.env.AR_GATE_DIFF_BASE ?? "").trim();
    const baseRef = (process.env.GITHUB_BASE_REF ?? "").trim();
    const candidates = [];
    if (explicit) candidates.push([explicit, "AR_GATE_DIFF_BASE"]);
    if (baseRef) {
      candidates.push([`origin/${baseRef}`, "GITHUB_BASE_REF (pull request base)"]);
      candidates.push([baseRef, "GITHUB_BASE_REF (pull request base)"]);
    }
    candidates.push(["@{upstream}", "the branch's configured upstream"]);
    candidates.push(["origin/HEAD", "the remote's default branch"]);
    candidates.push(["main", "the local default branch"]);
    for (const [revision, origin] of candidates) {
      if (!revisionExists(revision)) continue;
      const base = mergeBaseWith(revision);
      if (base !== null) {
        console.log(`[diff-coverage] base=${base} (${origin})`);
        return base;
      }
    }
    console.log(`[diff-coverage] base=${emptyTree} (no merge base resolvable)`);
    return emptyTree;
  };

  const base = resolveBase();
  console.log(`[diff-coverage] base=${base}`);

  const diff = git([
    "diff",
    "--unified=0",
    base,
    "--",
    "src/**/*.ts",
    "src/**/*.tsx",
  ]);

  const changedLines = new Map();
  let currentFile = null;
  let hunkStart = 0;
  for (const line of diff.split("\n")) {
    const fileMatch = line.match(/^\+\+\+ b\/(.+)$/);
    if (fileMatch) {
      currentFile = fileMatch[1];
      if (!changedLines.has(currentFile)) {
        changedLines.set(currentFile, new Set());
      }
      continue;
    }
    const hunkMatch = line.match(/^@@ -\S+ \+(\d+)(?:,(\d+))? @@/);
    if (hunkMatch) {
      hunkStart = Number(hunkMatch[1]);
      continue;
    }
    if (!currentFile) continue;
    if (line.startsWith("+")) {
      changedLines.get(currentFile)?.add(hunkStart);
      hunkStart += 1;
    } else if (line.startsWith("-")) {
      // Removed lines have no new-file counterpart to cover; skip.
    } else if (line.startsWith(" ")) {
      hunkStart += 1;
    }
  }

  let coverage;
  try {
    coverage = JSON.parse(
      readFileSync(join(dashboardRoot, "coverage", "coverage-final.json"), "utf8"),
    );
  } catch {
    console.error(
      "[diff-coverage] FAIL: coverage/coverage-final.json missing; run `npm run test:coverage` first",
    );
    process.exit(1);
  }

  // v8 emits absolute source paths; normalize to the same relative keys git emits.
  const coverageByRelative = new Map();
  for (const [absolutePath, entry] of Object.entries(coverage)) {
    const srcIndex = absolutePath.lastIndexOf("/src/");
    coverageByRelative.set(
      srcIndex >= 0 ? absolutePath.slice(srcIndex + 1) : absolutePath,
      entry,
    );
  }

  const tally = measureDiffCoverage(changedLines, coverageByRelative);
  const percent = tally.total === 0 ? 100 : (tally.covered / tally.total) * 100;
  console.log(
    `[diff-coverage] ${tally.covered}/${tally.total} changed lines covered (${percent.toFixed(1)}%, diagnostic only)`,
  );
  if (tally.missing.length > 0) {
    console.log(
      `[diff-coverage] uncovered changed lines:\n${tally.missing.join("\n")}`,
    );
  }
  console.log("[diff-coverage] REPORT: uncovered lines inform behavior-focused review");
}

// Keep the git/coverage side effects out of imports: the contract test imports the pure scoring
// functions, and only a direct CLI invocation runs the gate.
if (
  process.argv[1] &&
  fileURLToPath(import.meta.url) === resolve(process.argv[1])
) {
  main();
}

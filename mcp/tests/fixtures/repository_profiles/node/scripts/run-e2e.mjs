import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

const [resultPath] = process.argv.slice(2);
const outcome = spawnSync(process.execPath, ["--test", "test/e2e.test.mjs"], {
  stdio: "inherit",
});
if (outcome.status !== 0) {
  process.exit(outcome.status ?? 1);
}
fs.mkdirSync(path.dirname(resultPath), { recursive: true });
fs.writeFileSync(resultPath, `${JSON.stringify({ status: "passed", tool: "node:test" })}\n`);

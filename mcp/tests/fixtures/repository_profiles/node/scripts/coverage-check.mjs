import fs from "node:fs";

const [coveragePath] = process.argv.slice(2);
const result = JSON.parse(fs.readFileSync(coveragePath, "utf8"));
if (result.statementCoverage !== 100 || result.suiteResult !== "node-suite.json") {
  throw new Error("post-suite coverage proof is incomplete");
}

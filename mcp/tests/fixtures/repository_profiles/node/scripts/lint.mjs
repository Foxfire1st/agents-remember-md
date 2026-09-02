import fs from "node:fs";

for (const path of ["src/math.mjs", "test/unit.test.mjs", "test/e2e.test.mjs"]) {
  const source = fs.readFileSync(path, "utf8");
  if (source.includes("\t") || source.includes("var ")) {
    throw new Error(`static quality failed for ${path}`);
  }
}

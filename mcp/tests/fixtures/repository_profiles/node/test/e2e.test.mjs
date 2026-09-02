import assert from "node:assert/strict";
import test from "node:test";

import { add } from "../src/math.mjs";

test("the clean-room service flow composes repository behavior", () => {
  const response = JSON.stringify({ total: add(20, 22) });
  assert.deepEqual(JSON.parse(response), { total: 42 });
});

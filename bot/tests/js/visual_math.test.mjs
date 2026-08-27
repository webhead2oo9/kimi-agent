import assert from "node:assert/strict";
import test from "node:test";

import {
  buildAxisScale,
  formatScaleLabel,
  layoutOverlapBadge,
} from "../../web_browser/visual_math.mjs";

test("symlog keeps a finite span for a high constant domain", () => {
  const scale = buildAxisScale([1e15], 105, 1160, "symlog", false);
  const position = scale.position(1e15);
  assert.ok(Number.isFinite(position));
  assert.ok(position > 105 && position < 1160);
  assert.ok(scale.ticks.every(Number.isFinite));
  assert.ok(new Set(scale.ticks).size > 1);
});

test("mixed-sign symlog ticks include exact zero", () => {
  const scale = buildAxisScale([-1e6, -0.001, 0.002, 1e6], 520, 76, "symlog", true);
  assert.ok(scale.ticks.includes(0));
  assert.ok(scale.ticks.some((value) => value < 0));
  assert.ok(scale.ticks.some((value) => value > 0));
  assert.equal(scale.position(0), 298);
});

test("tiny linear domains retain their ticks and zero baseline", () => {
  const positive = buildAxisScale([0, 1e-13], 520, 76, "linear", true);
  assert.equal(positive.ticks.length, 6);
  assert.ok(positive.ticks.includes(0));

  const signed = buildAxisScale([-1e-13, 1e-13], 520, 76, "linear", true);
  assert.equal(signed.ticks.length, 7);
  assert.ok(signed.ticks.includes(0));
});

test("symlog label truncation preserves Unicode code points", () => {
  const label = `${"a".repeat(83)}😀${"b".repeat(30)}`;
  const formatted = formatScaleLabel(label, "symlog");
  assert.ok(formatted.includes("😀"));
  assert.ok(!formatted.includes("�"));
  assert.ok(Array.from(formatted).length <= 100);
  assert.equal(formatScaleLabel(label, "linear"), label);
});

test("overlap badges stay separate from labels near either vertical edge", () => {
  const ordinary = layoutOverlapBadge(300, 200, 105, 1160, 76, 520, 32, true);
  assert.ok(ordinary.y > 200);
  assert.equal(ordinary.pointLabelY, 190);

  const bottom = layoutOverlapBadge(300, 515, 105, 1160, 76, 520, 32, true);
  assert.ok(bottom.y < 515);
  assert.ok(bottom.pointLabelY < bottom.y);
});

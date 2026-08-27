/** Pure chart-layout helpers shared by the renderer and direct Node tests. */

export function buildAxisScale(values, start, end, mode, includeZero) {
  let minimum = Math.min(...values);
  let maximum = Math.max(...values);
  if (includeZero) {
    minimum = Math.min(0, minimum);
    maximum = Math.max(0, maximum);
  }
  const nonzero = values.map(Math.abs).filter((value) => value > 0);
  const threshold = nonzero.length ? Math.min(...nonzero) : 1;
  const thresholdLog = Math.log10(threshold);
  const transform = mode === "symlog"
    ? (value) => Math.sign(value) * (Math.log10(Math.abs(value) + threshold) - thresholdLog)
    : (value) => value;
  const inverse = mode === "symlog"
    ? (value) => Math.sign(value) * (10 ** (Math.abs(value) + thresholdLog) - threshold)
    : (value) => value;

  let transformedMinimum = transform(minimum);
  let transformedMaximum = transform(maximum);
  if (!(transformedMaximum > transformedMinimum)) {
    const center = transform(values[0]);
    const padding = Math.max(0.1, Math.abs(center) * 0.1);
    transformedMinimum = center - padding;
    transformedMaximum = center + padding;
  }
  const span = transformedMaximum - transformedMinimum;
  const position = (value) => start + ((transform(value) - transformedMinimum) / span) * (end - start);
  const transformedTicks = Array.from(
    { length: 6 },
    (_, index) => transformedMinimum + span * index / 5,
  );
  if (transformedMinimum <= 0 && transformedMaximum >= 0) transformedTicks.push(0);
  transformedTicks.sort((left, right) => left - right);
  const uniqueTicks = transformedTicks.filter(
    (value, index) => index === 0 || value !== transformedTicks[index - 1],
  );
  return { position, ticks: uniqueTicks.map(inverse) };
}

export function formatScaleLabel(label, scale) {
  if (scale !== "symlog") return label;
  const prefix = Array.from(label).slice(0, 84).join("").trimEnd();
  return `${prefix}${prefix ? " " : ""}(symlog scale)`;
}

export function layoutOverlapBadge(px, py, left, right, top, bottom, labelWidth, hasLabel) {
  const height = 19;
  const x = px + 7 + labelWidth <= right ? px + 7 : Math.max(left, px - 7 - labelWidth);
  if (py + 10 + height <= bottom) {
    return { x, y: py + 10, pointLabelY: py - 10 };
  }
  const y = Math.max(top, py - 10 - height);
  return { x, y, pointLabelY: hasLabel ? y - 8 : py - 10 };
}

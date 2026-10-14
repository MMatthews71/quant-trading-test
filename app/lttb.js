// Largest-Triangle-Three-Buckets downsampling.
// Keeps the visual shape of a series while cutting it to `threshold` points.
// Input: array of {x, y} sorted ascending by x. Returns a downsampled array.
function lttb(data, threshold) {
  const n = data.length;
  if (threshold >= n || threshold <= 2) return data.slice();

  const sampled = [data[0]];
  const every = (n - 2) / (threshold - 2);
  let a = 0; // index of the previously selected point

  for (let i = 0; i < threshold - 2; i++) {
    // Average point of the next bucket (the "third" point of the triangle).
    let avgX = 0;
    let avgY = 0;
    let start = Math.floor((i + 1) * every) + 1;
    let end = Math.floor((i + 2) * every) + 1;
    end = end < n ? end : n;
    const span = end - start;
    if (span > 0) {
      for (let j = start; j < end; j++) {
        avgX += data[j].x;
        avgY += data[j].y;
      }
      avgX /= span;
      avgY /= span;
    } else {
      avgX = data[Math.min(start, n - 1)].x;
      avgY = data[Math.min(start, n - 1)].y;
    }

    // Pick the point in the current bucket forming the largest-area triangle.
    const rangeStart = Math.floor(i * every) + 1;
    const rangeEnd = Math.floor((i + 1) * every) + 1;
    const ax = data[a].x;
    const ay = data[a].y;
    let maxArea = -1;
    let next = rangeStart;
    for (let j = rangeStart; j < rangeEnd && j < n; j++) {
      const area = Math.abs(
        (ax - avgX) * (data[j].y - ay) - (ax - data[j].x) * (avgY - ay)
      );
      if (area > maxArea) {
        maxArea = area;
        next = j;
      }
    }
    sampled.push(data[next]);
    a = next;
  }

  sampled.push(data[n - 1]);
  return sampled;
}

module.exports = { lttb };

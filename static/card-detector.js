/* Card detection in the browser, without libraries.
 *
 * Finds one card-shaped outline (a rectangle with the 63:88 shape of a Pokémon card) in an
 * image, returns its four corners, and can cut the card out straightened. Used for the live
 * camera overlay and to straighten photos before they are sent to the server.
 *
 * How: grey scale -> blur -> edge strength (Sobel) -> connected edge regions -> for each large
 * region, its convex hull and the smallest rectangle around it -> keep the one whose size and
 * shape match a card -> the hull points nearest that rectangle's corners are the card corners
 * (so a card photographed at an angle gives a true quadrilateral, not a rectangle).
 */
"use strict";

(function (root) {
  const CARD_RATIO = 63 / 88;                      // width / height
  const READ_AREA = [0.0, 0.86, 0.64, 1.0];        // identifier strip, as fractions of the card

  function gray(img) {
    const { data, width, height } = img;
    const out = new Float32Array(width * height);
    for (let i = 0, j = 0; i < out.length; i++, j += 4) {
      out[i] = 0.299 * data[j] + 0.587 * data[j + 1] + 0.114 * data[j + 2];
    }
    return out;
  }

  function boxBlur(src, w, h) {
    let a = src;
    let b = new Float32Array(w * h);
    for (let pass = 0; pass < 2; pass++) {
      for (let y = 0; y < h; y++) {
        const y0 = Math.max(0, y - 1), y1 = Math.min(h - 1, y + 1);
        for (let x = 0; x < w; x++) {
          const x0 = Math.max(0, x - 1), x1 = Math.min(w - 1, x + 1);
          let s = 0;
          for (let yy = y0; yy <= y1; yy++) for (let xx = x0; xx <= x1; xx++) s += a[yy * w + xx];
          b[y * w + x] = s / ((y1 - y0 + 1) * (x1 - x0 + 1));
        }
      }
      [a, b] = [b, a];
    }
    return a;
  }

  function edgeMap(g, w, h) {
    const mag = new Float32Array(w * h);
    for (let y = 1; y < h - 1; y++) {
      for (let x = 1; x < w - 1; x++) {
        const i = y * w + x;
        const gx = g[i - w + 1] + 2 * g[i + 1] + g[i + w + 1] - g[i - w - 1] - 2 * g[i - 1] - g[i + w - 1];
        const gy = g[i + w - 1] + 2 * g[i + w] + g[i + w + 1] - g[i - w - 1] - 2 * g[i - w] - g[i - w + 1];
        mag[i] = Math.abs(gx) + Math.abs(gy);
      }
    }
    const sample = [];
    for (let i = 0; i < mag.length; i += 7) sample.push(mag[i]);
    sample.sort((p, q) => p - q);
    const threshold = Math.max(40, sample[Math.floor(sample.length * 0.85)]);
    const edges = new Uint8Array(w * h);
    for (let i = 0; i < mag.length; i++) if (mag[i] > threshold) edges[i] = 1;
    const grown = new Uint8Array(w * h);  // thickened 3x3 to close small gaps in the outline
    for (let y = 1; y < h - 1; y++) {
      for (let x = 1; x < w - 1; x++) {
        const i = y * w + x;
        if (edges[i] || edges[i - 1] || edges[i + 1] || edges[i - w] || edges[i + w] ||
            edges[i - w - 1] || edges[i - w + 1] || edges[i + w - 1] || edges[i + w + 1]) grown[i] = 1;
      }
    }
    return { edges, grown };
  }

  /* Fraction of points along the weakest side of `quad` that lie on an edge. A real card has
     edges all the way round; an outline that merged with things on the table does not. */
  function sideSupport(grown, w, h, quad) {
    let weakest = 1;
    for (let s = 0; s < 4; s++) {
      const [ax, ay] = quad[s], [bx, by] = quad[(s + 1) % 4];
      let hits = 0;
      const samples = 40;
      for (let k = 2; k < samples - 2; k++) {  // skip the rounded corners
        const t = k / samples;
        const x = Math.round(ax + (bx - ax) * t), y = Math.round(ay + (by - ay) * t);
        if (x >= 0 && y >= 0 && x < w && y < h && grown[y * w + x]) hits++;
      }
      weakest = Math.min(weakest, hits / (samples - 4));
    }
    return weakest;
  }

  /* Large connected edge regions, each reduced to its left/right-most pixel per row. */
  function regions(edges, w, h, minBoxArea) {
    const seen = new Uint8Array(w * h);
    const stack = new Int32Array(w * h);
    const members = new Int32Array(w * h);
    const out = [];
    for (let start = 0; start < w * h; start++) {
      if (!edges[start] || seen[start]) continue;
      let sp = 0, n = 0;
      let x0 = w, x1 = -1, y0 = h, y1 = -1;
      stack[sp++] = start;
      seen[start] = 1;
      while (sp) {
        const p = stack[--sp];
        members[n++] = p;
        const x = p % w, y = (p - x) / w;
        if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y;
        for (let dy = -1; dy <= 1; dy++) {
          const yy = y + dy;
          if (yy < 0 || yy >= h) continue;
          for (let dx = -1; dx <= 1; dx++) {
            const xx = x + dx;
            if (xx < 0 || xx >= w) continue;
            const q = yy * w + xx;
            if (edges[q] && !seen[q]) { seen[q] = 1; stack[sp++] = q; }
          }
        }
      }
      if ((x1 - x0) * (y1 - y0) < minBoxArea) continue;
      const left = new Int32Array(h).fill(w), right = new Int32Array(h).fill(-1);
      for (let k = 0; k < n; k++) {
        const x = members[k] % w, y = (members[k] - x) / w;
        if (x < left[y]) left[y] = x;
        if (x > right[y]) right[y] = x;
      }
      const points = [];
      for (let y = y0; y <= y1; y++) if (right[y] >= 0) { points.push([left[y], y]); points.push([right[y], y]); }
      out.push({ points, box: [x0, y0, x1, y1] });
    }
    return out;
  }

  function convexHull(points) {
    const pts = points.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const lower = [], upper = [];
    for (const p of pts) {
      while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop();
      lower.push(p);
    }
    for (let i = pts.length - 1; i >= 0; i--) {
      const p = pts[i];
      while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop();
      upper.push(p);
    }
    upper.pop(); lower.pop();
    return lower.concat(upper);
  }

  function inside(hull, [x, y]) {  // point in convex polygon (either winding)
    let sign = 0;
    for (let i = 0; i < hull.length; i++) {
      const [ax, ay] = hull[i], [bx, by] = hull[(i + 1) % hull.length];
      const c = (bx - ax) * (y - ay) - (by - ay) * (x - ax);
      if (c !== 0) {
        if (sign === 0) sign = Math.sign(c);
        else if (Math.sign(c) !== sign) return false;
      }
    }
    return true;
  }

  function polygonArea(poly) {
    let s = 0;
    for (let i = 0; i < poly.length; i++) {
      const [x1, y1] = poly[i], [x2, y2] = poly[(i + 1) % poly.length];
      s += x1 * y2 - x2 * y1;
    }
    return Math.abs(s) / 2;
  }

  /* Smallest rectangle around a convex polygon (rotating one side onto each hull edge). */
  function minAreaRect(hull) {
    let best = null;
    for (let i = 0; i < hull.length; i++) {
      const [ax, ay] = hull[i], [bx, by] = hull[(i + 1) % hull.length];
      const len = Math.hypot(bx - ax, by - ay);
      if (!len) continue;
      const ux = (bx - ax) / len, uy = (by - ay) / len, vx = -uy, vy = ux;
      let minU = Infinity, maxU = -Infinity, minV = Infinity, maxV = -Infinity;
      for (const [x, y] of hull) {
        const u = x * ux + y * uy, v = x * vx + y * vy;
        if (u < minU) minU = u; if (u > maxU) maxU = u;
        if (v < minV) minV = v; if (v > maxV) maxV = v;
      }
      const area = (maxU - minU) * (maxV - minV);
      if (!best || area < best.area) {
        const corner = (u, v) => [u * ux + v * vx, u * uy + v * vy];
        best = { area, sides: [maxU - minU, maxV - minV],
                 corners: [corner(minU, minV), corner(maxU, minV), corner(maxU, maxV), corner(minU, maxV)] };
      }
    }
    return best;
  }

  /* Order four corners as top-left, top-right, bottom-right, bottom-left of an upright card. */
  function orderCorners(c) {
    const d = (p, q) => Math.hypot(p[0] - q[0], p[1] - q[1]);
    // Rotate so that side 0-1 is a short side.
    let pts = d(c[0], c[1]) <= d(c[1], c[2]) ? c.slice() : [c[1], c[2], c[3], c[0]];
    // Of the two short sides (0-1 and 2-3), the one higher in the image is the top.
    if ((pts[0][1] + pts[1][1]) > (pts[2][1] + pts[3][1])) pts = [pts[2], pts[3], pts[0], pts[1]];
    // Top-left is the left one of the top side (clockwise order in image coordinates).
    if (pts[0][0] > pts[1][0]) pts = [pts[1], pts[0], pts[3], pts[2]];
    return pts;
  }

  /**
   * Find one card in an ImageData (ideally ~320 px wide).
   * Returns { corners: [[x,y] x4 tl,tr,br,bl], score } or null.
   */
  function detect(img, debug = null) {
    const { width: w, height: h } = img;
    const { edges, grown } = edgeMap(boxBlur(gray(img), w, h), w, h);
    let best = null;
    const rejected = [];  // closed shapes that are not a card (used to spot a card's art window)
    // Thickened edges join a broken outline; thin edges keep the card apart from things touching it.
    const candidates = regions(grown, w, h, 0.08 * w * h).concat(regions(edges, w, h, 0.08 * w * h));
    for (const region of candidates) {
      const [x0, y0, x1, y1] = region.box;
      if (x0 <= 1 || y0 <= 1 || x1 >= w - 2 || y1 >= h - 2) continue;  // cut off by the frame edge
      const hull = convexHull(region.points);
      if (hull.length < 4) continue;
      const rect = minAreaRect(hull);
      const [s1, s2] = rect.sides;
      const ratio = Math.min(s1, s2) / Math.max(s1, s2);
      const fill = polygonArea(hull) / rect.area;
      const size = rect.area / (w * h);
      if (debug) debug.push({ box: region.box, ratio: +ratio.toFixed(3), fill: +fill.toFixed(3), size: +size.toFixed(3) });
      if (ratio < 0.6 || ratio > 0.82 || fill < 0.85 || size < 0.08 || size > 0.97) {
        if (size >= 0.08) rejected.push({ hull, area: rect.area });
        continue;
      }
      // The art window inside a card is landscape and card-shaped too; a card held upright is portrait.
      const [ra, rb] = [rect.corners[0], rect.corners[1]];
      const longIsFirst = s1 >= s2;
      const angle = Math.abs(Math.atan2(rb[1] - ra[1], rb[0] - ra[0]) * 180 / Math.PI) % 180;
      const longAxisAngle = longIsFirst ? angle : (angle + 90) % 180;
      const landscape = longAxisAngle < 35 || longAxisAngle > 145;
      if (landscape && size < 0.3) {
        rejected.push({ hull, area: rect.area });
        continue;
      }
      const nearest = (p) => hull.reduce((a, q) =>
        (Math.hypot(q[0] - p[0], q[1] - p[1]) < Math.hypot(a[0] - p[0], a[1] - p[1]) ? q : a));
      const corners = orderCorners(rect.corners.map(nearest));
      const support = sideSupport(grown, w, h, corners);
      if (debug) Object.assign(debug[debug.length - 1], { support: +support.toFixed(3), corners });
      if (support < 0.6) continue;
      const score = size * fill * support * (1 - Math.abs(ratio - CARD_RATIO) * 3);
      if (!best || score > best.score) best = { corners, score, area: rect.area };
    }
    // Inside a bigger closed shape that failed the card test? Then it is probably the art window
    // of a card whose outline merged with things on the table: better no outline than a wrong one.
    if (best && rejected.some((r) => r.area > 1.3 * best.area && best.corners.every((p) => inside(r.hull, p)))) {
      return null;
    }
    return best && { corners: best.corners, score: best.score };
  }

  /* 3x3 homography mapping 4 points `from` onto 4 points `to`. */
  function homography(from, to) {
    const A = [], b = [];
    for (let i = 0; i < 4; i++) {
      const [x, y] = from[i], [u, v] = to[i];
      A.push([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.push(u);
      A.push([0, 0, 0, x, y, 1, -v * x, -v * y]); b.push(v);
    }
    for (let c = 0; c < 8; c++) {  // Gaussian elimination with partial pivoting
      let pivot = c;
      for (let r = c + 1; r < 8; r++) if (Math.abs(A[r][c]) > Math.abs(A[pivot][c])) pivot = r;
      [A[c], A[pivot]] = [A[pivot], A[c]]; [b[c], b[pivot]] = [b[pivot], b[c]];
      for (let r = c + 1; r < 8; r++) {
        const f = A[r][c] / A[c][c];
        for (let k = c; k < 8; k++) A[r][k] -= f * A[c][k];
        b[r] -= f * b[c];
      }
    }
    const h = new Array(8);
    for (let r = 7; r >= 0; r--) {
      let s = b[r];
      for (let k = r + 1; k < 8; k++) s -= A[r][k] * h[k];
      h[r] = s / A[r][r];
    }
    return [...h, 1];
  }

  function project(H, x, y) {
    const d = H[6] * x + H[7] * y + H[8];
    return [(H[0] * x + H[1] * y + H[2]) / d, (H[3] * x + H[4] * y + H[5]) / d];
  }

  /* Map points given as fractions of the card (0..1) onto the detected corners. */
  function cardToImage(corners, fractions) {
    const H = homography([[0, 0], [1, 0], [1, 1], [0, 1]], corners);
    return fractions.map(([fx, fy]) => project(H, fx, fy));
  }

  function readArea(corners) {
    const [l, t, r, b] = READ_AREA;
    return cardToImage(corners, [[l, t], [r, t], [r, b], [l, b]]);
  }

  /* Cut the card out of `src` (ImageData) straightened to outW x outH (bilinear sampling). */
  function warp(src, corners, outW, outH) {
    const H = homography([[0, 0], [outW - 1, 0], [outW - 1, outH - 1], [0, outH - 1]], corners);
    const out = new ImageData(outW, outH);
    const { data, width, height } = src;
    const o = out.data;
    for (let y = 0; y < outH; y++) {
      for (let x = 0; x < outW; x++) {
        const d = H[6] * x + H[7] * y + H[8];
        let sx = (H[0] * x + H[1] * y + H[2]) / d, sy = (H[3] * x + H[4] * y + H[5]) / d;
        sx = Math.min(Math.max(sx, 0), width - 1.001);
        sy = Math.min(Math.max(sy, 0), height - 1.001);
        const x0 = sx | 0, y0 = sy | 0, fx = sx - x0, fy = sy - y0;
        const i00 = (y0 * width + x0) * 4, i10 = i00 + 4, i01 = i00 + width * 4, i11 = i01 + 4;
        const j = (y * outW + x) * 4;
        for (let c = 0; c < 3; c++) {
          o[j + c] = (data[i00 + c] * (1 - fx) + data[i10 + c] * fx) * (1 - fy) +
                     (data[i01 + c] * (1 - fx) + data[i11 + c] * fx) * fy;
        }
        o[j + 3] = 255;
      }
    }
    return out;
  }

  /* Output size for a straightened card: keep the detail the photo has (between 15 and 30 px/mm). */
  function cardSize(corners) {
    const d = (p, q) => Math.hypot(p[0] - q[0], p[1] - q[1]);
    const tall = Math.max(d(corners[0], corners[3]), d(corners[1], corners[2]));
    const height = Math.round(Math.min(2640, Math.max(1320, tall)));
    return [Math.round(height * CARD_RATIO), height];
  }

  const api = { detect, warp, readArea, cardSize, homography, project, orderCorners, CARD_RATIO, READ_AREA };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.CardDetector = api;
})(typeof window !== "undefined" ? window : globalThis);

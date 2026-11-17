/* Card detection in the browser, without libraries (same method as app/detect.py).
 *
 * Finds one Pokémon card in an image, returns its four corners, and can cut the card out
 * straightened (with a small margin). Used for the live camera overlay and to straighten
 * photos before they are sent to the server.
 *
 * Line-based, like a document scanner, so it copes with busy backgrounds (shelves, grids,
 * sleeves) where the card's outline touches other edges:
 *  1. edge strength and direction (Sobel) on a small grey copy;
 *  2. Hough transform: long straight lines;
 *  3. two pairs of roughly parallel lines, roughly perpendicular, spaced like a card
 *     (63 x 88 mm) make a candidate;
 *  4. each candidate is scored by how much real edge runs along its four sides *between* its
 *     corners (background lines stop where the card covers them), minus how much the sides
 *     continue past the corners (a real card's sides end there);
 *  5. of near-equal candidates nested around the best, the outer one is kept (the card's
 *     edge rather than a line just inside it).
 */
"use strict";

(function (root) {
  const CARD_RATIO = 63 / 88;   // width / height
  const PAD = 0.06;             // margin kept around a straightened card (must match app/reader.py)
  // Identifier strip in the straightened, padded card (app/reader.py _CARD_REGIONS) ...
  const STRIP = [0.0, 0.79, 0.72, 1.0];
  // ... and the same strip in card coordinates (0..1 inside the card), for the overlay.
  const READ_AREA = STRIP.map((v) => v * (1 + 2 * PAD) - PAD);
  const BIN = 5;                // degrees per direction bin

  function grayBlur(img) {
    const { data, width: w, height: h } = img;
    const g = new Float32Array(w * h);
    for (let i = 0, j = 0; i < g.length; i++, j += 4) g[i] = 0.299 * data[j] + 0.587 * data[j + 1] + 0.114 * data[j + 2];
    const k = [1, 4, 6, 4, 1];  // 5-tap binomial blur, separable
    const tmp = new Float32Array(w * h), out = new Float32Array(w * h);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        let s = 0, n = 0;
        for (let d = -2; d <= 2; d++) { const xx = x + d; if (xx >= 0 && xx < w) { s += k[d + 2] * g[y * w + xx]; n += k[d + 2]; } }
        tmp[y * w + x] = s / n;
      }
    }
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        let s = 0, n = 0;
        for (let d = -2; d <= 2; d++) { const yy = y + d; if (yy >= 0 && yy < h) { s += k[d + 2] * tmp[yy * w + x]; n += k[d + 2]; } }
        out[y * w + x] = s / n;
      }
    }
    return out;
  }

  /* Edge magnitude, edge-normal direction in degrees [0,180), and the strong-edge threshold. */
  function edges(g, w, h) {
    const mag = new Float32Array(w * h), dir = new Float32Array(w * h);
    for (let y = 1; y < h - 1; y++) {
      for (let x = 1; x < w - 1; x++) {
        const i = y * w + x;
        const gx = g[i - w + 1] + 2 * g[i + 1] + g[i + w + 1] - g[i - w - 1] - 2 * g[i - 1] - g[i + w - 1];
        const gy = g[i + w - 1] + 2 * g[i + w] + g[i + w + 1] - g[i - w - 1] - 2 * g[i - w] - g[i - w + 1];
        mag[i] = Math.hypot(gx, gy);
        let a = Math.atan2(gy, gx) * 180 / Math.PI;
        a = ((a % 180) + 180) % 180;
        dir[i] = a;
      }
    }
    const sample = [];
    for (let i = 0; i < mag.length; i += 5) sample.push(mag[i]);
    sample.sort((p, q) => p - q);
    const thresh = Math.max(30, sample[Math.floor(sample.length * 0.8)]);
    return { mag, dir, thresh };
  }

  const angleDiff = (a, b) => { const d = Math.abs(a - b) % 180; return Math.min(d, 180 - d); };

  /* Strong straight lines as [thetaDeg, rho] with x*cos + y*sin = rho. */
  function hough(mag, dir, thresh, w, h, maxLines) {
    const diag = Math.ceil(Math.hypot(w, h)) + 1, cols = 2 * diag + 1;
    const acc = new Float32Array(180 * cols);
    const cos = new Float32Array(180), sin = new Float32Array(180);
    for (let t = 0; t < 180; t++) { cos[t] = Math.cos(t * Math.PI / 180); sin[t] = Math.sin(t * Math.PI / 180); }
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = y * w + x;
        if (mag[i] <= thresh) continue;
        const t0 = Math.round(dir[i]);
        for (let d = -6; d <= 6; d++) {  // vote only near the pixel's own edge direction
          const t = ((t0 + d) % 180 + 180) % 180;
          acc[t * cols + Math.round(x * cos[t] + y * sin[t]) + diag] += 1;
        }
      }
    }
    const minVotes = 0.12 * Math.min(w, h);
    const peaks = [];
    for (let k = 0; k < acc.length; k++) if (acc[k] >= minVotes) peaks.push(k);
    peaks.sort((a, b) => acc[b] - acc[a]);
    const lines = [];
    for (const k of peaks) {
      const t = Math.floor(k / cols), r = (k % cols) - diag;
      const close = lines.some(([lt, lr]) => {
        const dt = Math.abs(lt - t);
        if (dt <= 3) return Math.abs(lr - r) <= 3;
        if (dt >= 177) return Math.abs(lr + r) <= 3;  // the same line seen from the other side
        return false;
      });
      if (close) continue;
      lines.push([t, r]);
      if (lines.length >= maxLines) break;
    }
    return lines;
  }

  function intersect(l1, l2) {
    const a1 = Math.cos(l1[0] * Math.PI / 180), b1 = Math.sin(l1[0] * Math.PI / 180);
    const a2 = Math.cos(l2[0] * Math.PI / 180), b2 = Math.sin(l2[0] * Math.PI / 180);
    const det = a1 * b2 - a2 * b1;
    if (Math.abs(det) < 1e-6) return null;
    return [(l1[1] * b2 - l2[1] * b1) / det, (a1 * l2[1] - a2 * l1[1]) / det];
  }

  /* Fraction of points between p and q (t0..t1 of the way) with an edge running along the side. */
  function support(E, w, h, p, q, samples, t0, t1) {
    const dx = q[0] - p[0], dy = q[1] - p[1];
    if (Math.abs(dx) + Math.abs(dy) < 1) return 0;
    const normal = ((Math.atan2(dx, -dy) * 180 / Math.PI) % 180 + 180) % 180;
    let hits = 0;
    for (let k = 0; k < samples; k++) {
      const t = t0 + (t1 - t0) * (samples === 1 ? 0.5 : k / (samples - 1));
      const cx = Math.round(p[0] + dx * t), cy = Math.round(p[1] + dy * t);
      let hit = false;
      for (let oy = -2; oy <= 2 && !hit; oy++) {
        const yy = cy + oy;
        if (yy < 0 || yy >= h) continue;
        for (let ox = -2; ox <= 2; ox++) {
          const xx = cx + ox;
          if (xx < 0 || xx >= w) continue;
          const i = yy * w + xx;
          if (E.mag[i] > E.thresh && angleDiff(E.dir[i], normal) < 22) { hit = true; break; }
        }
      }
      if (hit) hits++;
    }
    return hits / samples;
  }

  function polygonArea(pts) {
    let s = 0;
    for (let i = 0; i < pts.length; i++) {
      const [x1, y1] = pts[i], [x2, y2] = pts[(i + 1) % pts.length];
      s += x1 * y2 - x2 * y1;
    }
    return Math.abs(s) / 2;
  }

  function inside(quad, [x, y], tol = 2) {
    let sign = 0;
    for (let k = 0; k < 4; k++) {
      const [ax, ay] = quad[k], [bx, by] = quad[(k + 1) % 4];
      const cross = (bx - ax) * (y - ay) - (by - ay) * (x - ax);
      if (Math.abs(cross) / (Math.hypot(bx - ax, by - ay) || 1) <= tol) continue;
      if (sign === 0) sign = Math.sign(cross);
      else if (Math.sign(cross) !== sign) return false;
    }
    return true;
  }

  /* Order four corners as top-left, top-right, bottom-right, bottom-left of an upright card. */
  function orderCorners(c) {
    const d = (p, q) => Math.hypot(p[0] - q[0], p[1] - q[1]);
    let pts = d(c[0], c[1]) <= d(c[1], c[2]) ? c.slice() : [c[1], c[2], c[3], c[0]];
    if ((pts[0][1] + pts[1][1]) > (pts[2][1] + pts[3][1])) pts = [pts[2], pts[3], pts[0], pts[1]];
    if (pts[0][0] > pts[1][0]) pts = [pts[1], pts[0], pts[3], pts[2]];
    return pts;
  }

  /**
   * Find one card in an ImageData (long side ~320-400 px).
   * Returns { corners: [[x,y] x4 tl,tr,br,bl], score } or null.
   */
  function detect(img, options = {}) {
    const { width: w, height: h } = img;
    const maxLines = options.maxLines || 80, maxCandidates = options.maxCandidates || 4000;
    const E = edges(grayBlur(img), w, h);
    const lines = hough(E.mag, E.dir, E.thresh, w, h, maxLines);
    if (lines.length < 4) return null;

    // Pairs of roughly parallel lines at least a small card's width apart.
    const pairs = [];
    for (let i = 0; i < lines.length; i++) {
      for (let j = i + 1; j < lines.length; j++) {
        const [ti, ri] = lines[i], [tj, rj] = lines[j];
        let dth = Math.abs(ti - tj);
        const wrap = dth > 90;
        if (wrap) dth = 180 - dth;
        if (dth > 14) continue;
        const sep = Math.abs(wrap ? ri + rj : ri - rj);
        if (sep < 0.12 * Math.min(w, h)) continue;
        const mid = wrap ? ((ti + tj + 180) / 2) % 180 : (ti + tj) / 2;
        pairs.push({ a: lines[i], b: lines[j], sep, mid });
      }
    }
    // Two pairs, roughly perpendicular, spaced like a card.
    const combos = [];
    for (let i = 0; i < pairs.length; i++) {
      for (let j = i + 1; j < pairs.length; j++) {
        const P = pairs[i], Q = pairs[j];
        if (Math.abs(angleDiff(P.mid, Q.mid) - 90) > 18) continue;
        const ratio = Math.min(P.sep, Q.sep) / Math.max(P.sep, Q.sep);
        const area = P.sep * Q.sep / (w * h);
        if (ratio < 0.6 || ratio > 0.85 || area < 0.06 || area > 0.98) continue;
        combos.push([Math.abs(ratio - CARD_RATIO), P, Q]);
      }
    }
    combos.sort((x, y) => x[0] - y[0]);  // most card-like proportions first

    const candidates = [];
    for (const [, P, Q] of combos.slice(0, maxCandidates)) {
      const pts = [intersect(P.a, Q.a), intersect(P.a, Q.b), intersect(P.b, Q.b), intersect(P.b, Q.a)];
      if (pts.some((p) => !p || p[0] < 2 || p[1] < 2 || p[0] > w - 3 || p[1] > h - 3)) continue;  // whole card in view
      const side = (k) => Math.hypot(pts[k][0] - pts[(k + 1) % 4][0], pts[k][1] - pts[(k + 1) % 4][1]);
      const sA = (side(0) + side(2)) / 2, sB = (side(1) + side(3)) / 2;
      const r = Math.min(sA, sB) / Math.max(sA, sB);
      if (r < 0.6 || r > 0.85) continue;
      const sup = [];
      for (let k = 0; k < 4; k++) {
        sup.push(support(E, w, h, pts[k], pts[(k + 1) % 4], 32, 0.1, 0.9));
        if (sup[k] < 0.55) break;
      }
      if (sup.length < 4 || Math.min(...sup) < 0.55) continue;
      let beyond = 0;
      for (let k = 0; k < 4; k++) {
        const p = pts[k], q = pts[(k + 1) % 4];
        beyond += Math.max(support(E, w, h, p, q, 8, -0.12, -0.03), support(E, w, h, p, q, 8, 1.03, 1.12));
      }
      const frac = polygonArea(pts) / (w * h);
      const meanSup = sup.reduce((s, v) => s + v, 0) / 4;
      const score = (meanSup - 0.5 * beyond / 4) * (1 - 2 * Math.abs(r - CARD_RATIO)) * Math.pow(frac, 0.25);
      candidates.push({ score, frac, pts });
    }
    if (!candidates.length) return null;
    const best = candidates.reduce((a, b) => (b.score > a.score ? b : a));
    const around = candidates.filter((c) => c.score >= best.score - 0.05 && c.frac > best.frac &&
      c.frac <= 1.35 * best.frac && best.pts.every((p) => inside(c.pts, p)));
    const pick = around.length ? around.reduce((a, b) => (b.frac > a.frac ? b : a)) : best;
    return { corners: orderCorners(pick.pts), score: pick.score };
  }

  /* 3x3 homography (flattened, last = 1) mapping 4 points `from` onto 4 points `to`. */
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
    const hm = new Array(8);
    for (let r = 7; r >= 0; r--) {
      let s = b[r];
      for (let k = r + 1; k < 8; k++) s -= A[r][k] * hm[k];
      hm[r] = s / A[r][r];
    }
    return [...hm, 1];
  }

  function project(H, x, y) {
    const d = H[6] * x + H[7] * y + H[8];
    return [(H[0] * x + H[1] * y + H[2]) / d, (H[3] * x + H[4] * y + H[5]) / d];
  }

  /* Points given in card coordinates (0..1 inside the card) mapped onto the detected corners. */
  function cardToImage(corners, points) {
    const H = homography([[0, 0], [1, 0], [1, 1], [0, 1]], corners);
    return points.map(([u, v]) => project(H, u, v));
  }

  /* The identifier strip (where the set code and number are read), as 4 image points. */
  function readArea(corners) {
    const [l, t, r, b] = READ_AREA;
    return cardToImage(corners, [[l, t], [r, t], [r, b], [l, b]]);
  }

  /* Cut the card out of `src` (ImageData) straightened to outW x outH, including the margin. */
  function warp(src, corners, outW, outH) {
    const Hc = homography([[0, 0], [1, 0], [1, 1], [0, 1]], corners);
    const u = (x) => x / (outW - 1) * (1 + 2 * PAD) - PAD, v = (y) => y / (outH - 1) * (1 + 2 * PAD) - PAD;
    const srcCorners = [[0, 0], [outW - 1, 0], [outW - 1, outH - 1], [0, outH - 1]].map(([x, y]) => project(Hc, u(x), v(y)));
    const H = homography([[0, 0], [outW - 1, 0], [outW - 1, outH - 1], [0, outH - 1]], srcCorners);
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

  /* Output size for a straightened card with margin: keep the photo's detail (1320-2640 px tall). */
  function cardSize(corners) {
    const d = (p, q) => Math.hypot(p[0] - q[0], p[1] - q[1]);
    const tall = Math.max(d(corners[0], corners[3]), d(corners[1], corners[2])) * (1 + 2 * PAD);
    const height = Math.round(Math.min(2640, Math.max(1320, tall)));
    return [Math.round(height * CARD_RATIO), height];
  }

  const api = { detect, warp, readArea, cardSize, homography, project, orderCorners, CARD_RATIO, READ_AREA, PAD };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.CardDetector = api;
})(typeof window !== "undefined" ? window : globalThis);

"""Find one Pokémon card in a photo (numpy only, no OpenCV).

Line-based, like a document scanner, so it copes with busy backgrounds (shelves, grids,
sleeves) where the card's outline touches other edges:

1. Edge strength and direction (Sobel) on a small grey copy of the photo.
2. Hough transform: long straight lines.
3. Every two pairs of roughly parallel lines that are roughly perpendicular to each other
   make a candidate quadrilateral. Keep those with a card's shape (63 x 88 mm, allowing for
   perspective) and a sensible size.
4. Score each by how much real edge runs along all four sides, *between* its corners.
   Lines from the background stop where the card covers them, so they score badly.
5. The best candidate's corners, in the original photo's pixels (top-left, top-right,
   bottom-right, bottom-left of the card held upright; possibly upside down).
"""

from __future__ import annotations

import itertools
import math

import numpy as np
from PIL import Image, ImageFilter

WORK_SIZE = 320          # long side of the image the detector works on (same as the browser)
CARD_RATIO = 63 / 88


def find_card_corners(img: Image.Image) -> list[tuple[float, float]] | None:
    scale = WORK_SIZE / max(img.size)
    small = img.convert("L").resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                                    Image.Resampling.BILINEAR)
    corners = detect(np.asarray(small.filter(ImageFilter.GaussianBlur(1.2)), dtype=np.float32))
    if corners is None:
        return None
    return [(x / scale, y / scale) for x, y in corners]


def _sobel(g: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    p = np.pad(g, 1, mode="edge")
    gx = (p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[1:-1, :-2] + p[2:, :-2])
    gy = (p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[:-2, 1:-1] + p[:-2, 2:])
    return gx, gy


def _hough(mag, ang, thresh, h, w, max_lines=60):
    """Strong lines as (theta_deg, rho) with theta in [0, 180): x*cos + y*sin = rho."""
    ys, xs = np.nonzero(mag > thresh)
    if len(xs) == 0:
        return []
    diag = int(math.hypot(h, w)) + 1
    acc = np.zeros((180, 2 * diag + 1), dtype=np.float32)
    phi = (np.degrees(ang[ys, xs]) % 180.0)  # gradient direction = line normal
    for d in range(-6, 7):  # vote only near each pixel's own edge direction
        t = (np.round(phi).astype(int) + d) % 180
        rad = np.radians(t)
        rho = np.round(xs * np.cos(rad) + ys * np.sin(rad)).astype(int) + diag
        np.add.at(acc, (t, rho), 1.0)
    # Non-maximum suppression over (theta, rho) windows.
    lines = []
    min_votes = 0.12 * min(h, w)
    work = acc.copy()
    for _ in range(max_lines):
        t, r = np.unravel_index(np.argmax(work), work.shape)
        if work[t, r] < min_votes:
            break
        lines.append((float(t), float(r - diag)))
        for dt in range(-3, 4):
            tt = (t + dt) % 180
            lo, hi = max(0, r - 3), min(work.shape[1], r + 4)
            if t + dt < 0 or t + dt >= 180:  # wrapped: the same line has rho with the other sign
                rr = 2 * diag - r
                lo, hi = max(0, rr - 3), min(work.shape[1], rr + 4)
            work[tt, lo:hi] = 0
    return lines


def _angle_diff(a, b):
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def _intersect(l1, l2):
    t1, r1 = math.radians(l1[0]), l1[1]
    t2, r2 = math.radians(l2[0]), l2[1]
    a = np.array([[math.cos(t1), math.sin(t1)], [math.cos(t2), math.sin(t2)]])
    det = np.linalg.det(a)
    if abs(det) < 1e-6:
        return None
    x, y = np.linalg.solve(a, [r1, r2])
    return float(x), float(y)


_BIN = 5  # degrees per direction bin


def _direction_maps(mag, ang, thresh):
    """For each line-normal direction (5-degree bins): where an edge runs that way, widened by 2 px."""
    strong = mag > thresh
    ori = np.degrees(ang) % 180.0
    maps = []
    for k in range(180 // _BIN):
        d = np.abs(ori - k * _BIN)
        m = strong & (np.minimum(d, 180.0 - d) < 22)
        grown = m.copy()
        for dy in (-2, -1, 0, 1, 2):
            for dx in (-2, -1, 0, 1, 2):
                if dx or dy:
                    grown |= np.roll(np.roll(m, dy, axis=0), dx, axis=1)
        maps.append(grown)
    return maps


def _side_support(maps, p, q, samples=32, t0=0.1, t1=0.9):
    """Fraction of points between p and q (corners excluded) with an edge along the side.

    t0/t1 outside 0..1 measure the line's continuation beyond the corners instead.
    """
    h, w = maps[0].shape
    dx, dy = q[0] - p[0], q[1] - p[1]
    if abs(dx) + abs(dy) < 1:
        return 0.0
    normal = math.degrees(math.atan2(dx, -dy)) % 180.0
    edge = maps[int(round(normal / _BIN)) % len(maps)]
    ts = np.linspace(t0, t1, samples)
    xs = np.round(p[0] + dx * ts).astype(int)
    ys = np.round(p[1] + dy * ts).astype(int)
    ok = (xs >= 0) & (ys >= 0) & (xs < w) & (ys < h)
    if not ok.any():
        return 0.0
    return float(np.where(ok, edge[np.clip(ys, 0, h - 1), np.clip(xs, 0, w - 1)], False).mean())


def _order(corners):
    """Top-left, top-right, bottom-right, bottom-left of the card held upright (short side on top)."""
    c = [tuple(p) for p in corners]
    dist = lambda a, b: math.hypot(a[0] - b[0], a[1] - b[1])  # noqa: E731
    if dist(c[0], c[1]) > dist(c[1], c[2]):
        c = [c[1], c[2], c[3], c[0]]
    if c[0][1] + c[1][1] > c[2][1] + c[3][1]:
        c = [c[2], c[3], c[0], c[1]]
    if c[0][0] > c[1][0]:
        c = [c[1], c[0], c[3], c[2]]
    return c


def detect(gray: np.ndarray) -> list[tuple[float, float]] | None:
    """Corners of the card in a small grey image (long side ~400 px), or None."""
    h, w = gray.shape
    gx, gy = _sobel(gray)
    mag = np.hypot(gx, gy)
    ang = np.arctan2(gy, gx)
    thresh = max(30.0, float(np.percentile(mag, 80)))
    lines = _hough(mag, ang, thresh, h, w, max_lines=100)
    if len(lines) < 4:
        return None
    maps = _direction_maps(mag, ang, thresh)

    # Pairs of roughly parallel lines at least a small card's width apart.
    th = np.array([l[0] for l in lines])
    rho = np.array([l[1] for l in lines])
    i, j = np.triu_indices(len(lines), 1)
    dth = np.abs(th[i] - th[j])
    wrap = dth > 90
    dth = np.where(wrap, 180 - dth, dth)
    sep = np.abs(np.where(wrap, rho[i] + rho[j], rho[i] - rho[j]))  # distance between the lines
    mid = np.where(wrap, ((th[i] + th[j] + 180) / 2) % 180, (th[i] + th[j]) / 2)
    keep = (dth <= 14) & (sep >= 0.12 * min(h, w))
    pi, pj, psep, pmid = i[keep], j[keep], sep[keep], mid[keep]
    if len(pi) < 2:
        return None

    # Two pairs, roughly perpendicular, whose spacings have a card's proportions.
    a, b = np.triu_indices(len(pi), 1)
    dm = np.abs(pmid[a] - pmid[b])
    dm = np.minimum(dm, 180 - dm)
    ratio = np.minimum(psep[a], psep[b]) / np.maximum(psep[a], psep[b])
    area = psep[a] * psep[b] / (h * w)
    ok = (np.abs(dm - 90) <= 18) & (ratio >= 0.6) & (ratio <= 0.85) & (area >= 0.06) & (area <= 0.98)
    order = np.argsort(np.abs(ratio[ok] - CARD_RATIO))  # most card-like proportions first
    combos = np.stack([a[ok], b[ok]], axis=1)[order][:4000]

    candidates = []
    for ka, kb in combos:
        a1, a2 = lines[pi[ka]], lines[pj[ka]]
        b1, b2 = lines[pi[kb]], lines[pj[kb]]
        pts = [_intersect(a1, b1), _intersect(a1, b2), _intersect(a2, b2), _intersect(a2, b1)]
        if any(p is None for p in pts):
            continue
        if any(not (2 <= x <= w - 3 and 2 <= y <= h - 3) for x, y in pts):  # whole card in view
            continue
        sides = [math.dist(pts[k], pts[(k + 1) % 4]) for k in range(4)]
        s_a, s_b = (sides[0] + sides[2]) / 2, (sides[1] + sides[3]) / 2
        r = min(s_a, s_b) / max(s_a, s_b)
        if not 0.6 <= r <= 0.85:
            continue
        support = []
        for k in range(4):
            support.append(_side_support(maps, pts[k], pts[(k + 1) % 4]))
            if support[-1] < 0.55:
                break
        if len(support) < 4 or min(support) < 0.55:
            continue
        # A real side stops at the card's corners: little edge where it would continue past them.
        beyond = [max(_side_support(maps, pts[k], pts[(k + 1) % 4], 8, -0.12, -0.03),
                      _side_support(maps, pts[k], pts[(k + 1) % 4], 8, 1.03, 1.12)) for k in range(4)]
        frac = 0.5 * abs(sum(pts[k][0] * pts[(k + 1) % 4][1] - pts[(k + 1) % 4][0] * pts[k][1]
                             for k in range(4))) / (h * w)
        score = (sum(support) / 4 - 0.5 * sum(beyond) / 4) * (1 - 2 * abs(r - CARD_RATIO)) * frac ** 0.25
        candidates.append((score, frac, pts))
    if not candidates:
        return None
    best = max(candidates, key=lambda c: c[0])
    # A slightly bigger outline around the best one with almost the same score is the card's outer
    # edge (the best one ran along a line just inside the card, e.g. above the bottom text).
    around = [c for c in candidates if c[0] >= best[0] - 0.05 and best[1] < c[1] <= 1.35 * best[1]
              and all(_inside(c[2], p) for p in best[2])]
    return _order(max(around, key=lambda c: c[1])[2] if around else best[2])


def _inside(quad, point, tol=2.0):
    x, y = point
    sign = 0
    for k in range(4):
        (ax, ay), (bx, by) = quad[k], quad[(k + 1) % 4]
        cross = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
        length = math.hypot(bx - ax, by - ay) or 1
        if abs(cross) / length <= tol:
            continue
        if sign == 0:
            sign = 1 if cross > 0 else -1
        elif (cross > 0) != (sign > 0):
            return False
    return True

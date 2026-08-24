"""
ground_station/graphs/decimate.py
═════════════════════════════════
§13.2 — min/max decimation per pixel column.

The arithmetic from the spec: 10 Hz × 2 vehicles × 13 plotted channels
over one hour is ~940,000 points, "far past viable per-point rendering".
So the full-mission view draws at most two points per pixel column — the
column's minimum and maximum.

Decimation is display-only. Full resolution stays in the log (§12.2) and
in memory to the buffer limit (§13.2).

WHY MIN/MAX AND NOT STRIDE SAMPLING
───────────────────────────────────
Taking every Nth point is cheaper and is wrong here in a way that
matters. A 40 ms acceleration spike at burnout occupies four samples; at
full-mission zoom one pixel column covers several hundred samples, so
stride sampling drops the spike entirely with probability ~99%. The
operator would see a clean trace and conclude nothing happened. Min/max
guarantees both extremes of every column survive, so a transient shows
up as a full-height spike no matter how far out the view is zoomed —
which is exactly what it should look like.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

Point = Tuple[float, float]


def minmax_decimate(xs: Sequence[float], ys: Sequence[float],
                    columns: int) -> Tuple[List[float], List[float]]:
    """Reduce a series to at most 2 points per pixel column.

    Points are emitted in x order, and within a column the min and max
    are emitted in the order they actually occurred. Emitting them
    always-min-then-max would be simpler and would introduce a sawtooth
    that does not exist in the data — visible as false high-frequency
    texture on any zoomed-out trace.
    """
    n = len(xs)
    if n == 0:
        return [], []
    if columns <= 0 or n <= columns * 2:
        return list(xs), list(ys)

    x0, x1 = xs[0], xs[-1]
    span = x1 - x0
    if span <= 0:
        return list(xs), list(ys)

    out_x: List[float] = []
    out_y: List[float] = []

    col_width = span / columns
    i = 0
    for col in range(columns):
        col_end = x0 + col_width * (col + 1)
        start = i
        while i < n and (xs[i] < col_end or col == columns - 1 and i < n):
            i += 1
            if col < columns - 1 and i < n and xs[i] >= col_end:
                break
        if i == start:
            continue

        lo_i = hi_i = start
        for k in range(start, i):
            if ys[k] < ys[lo_i]:
                lo_i = k
            if ys[k] > ys[hi_i]:
                hi_i = k

        if lo_i == hi_i:
            out_x.append(xs[lo_i])
            out_y.append(ys[lo_i])
        else:
            first, second = (lo_i, hi_i) if lo_i < hi_i else (hi_i, lo_i)
            out_x.extend((xs[first], xs[second]))
            out_y.extend((ys[first], ys[second]))

        if i >= n:
            break

    return out_x, out_y


class RingBuffer:
    """Bounded per-channel sample store (§13.2 — "graph buffers are
    bounded", §7.3 — "the data domain grows without bound while telemetry
    arrives").

    Those two are only in tension if the buffer is the dataset. It is
    not: the log is (§12.2). This holds what the graph can draw, and it
    drops the oldest samples once full rather than growing forever.

    Default capacity is 36,000 — one hour per channel at 10 Hz, which
    covers any realistic flight plus a long pre-launch hold with room to
    spare.
    """

    __slots__ = ("_x", "_y", "capacity")

    def __init__(self, capacity: int = 36000):
        self.capacity = capacity
        self._x: List[float] = []
        self._y: List[float] = []

    def append(self, x: float, y: float) -> None:
        self._x.append(x)
        self._y.append(y)
        if len(self._x) > self.capacity:
            # Trim in blocks rather than one element at a time: popping
            # from the front of a list is O(n), so per-sample trimming at
            # 10 Hz on a full buffer would be 36,000 element moves ten
            # times a second, for every channel.
            drop = self.capacity // 10
            del self._x[:drop]
            del self._y[:drop]

    def clear(self) -> None:
        self._x.clear()
        self._y.clear()

    def __len__(self) -> int:
        return len(self._x)

    @property
    def xs(self) -> List[float]:
        return self._x

    @property
    def ys(self) -> List[float]:
        return self._y

    def window(self, x_lo: float, x_hi: float) -> Tuple[List[float], List[float]]:
        """Samples within [x_lo, x_hi], plus one either side.

        The extra point at each end matters: without it the trace stops
        at the viewport edge instead of running off it, which reads as
        the data ending rather than the view being zoomed.
        """
        xs, ys = self._x, self._y
        n = len(xs)
        if n == 0:
            return [], []

        lo = _bisect_left(xs, x_lo)
        hi = _bisect_right(xs, x_hi)
        lo = max(0, lo - 1)
        hi = min(n, hi + 1)
        return xs[lo:hi], ys[lo:hi]

    def y_range(self, x_lo: Optional[float] = None,
                x_hi: Optional[float] = None
                ) -> Tuple[Optional[float], Optional[float]]:
        if x_lo is None or x_hi is None:
            ys = self._y
        else:
            _, ys = self.window(x_lo, x_hi)
        if not ys:
            return None, None
        return min(ys), max(ys)


def _bisect_left(a: Sequence[float], v: float) -> int:
    lo, hi = 0, len(a)
    while lo < hi:
        mid = (lo + hi) // 2
        if a[mid] < v:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _bisect_right(a: Sequence[float], v: float) -> int:
    lo, hi = 0, len(a)
    while lo < hi:
        mid = (lo + hi) // 2
        if v < a[mid]:
            hi = mid
        else:
            lo = mid + 1
    return lo

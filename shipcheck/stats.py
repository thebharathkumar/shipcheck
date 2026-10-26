"""Small-sample statistics, standard library only.

wilson_interval gives the likely range of a pass rate. mcnemar_exact asks
whether two versions differ on the same cases more than chance would explain,
using only the cases where they disagree.
"""

from __future__ import annotations

from fractions import Fraction
from math import comb, sqrt


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for k successes out of n. z=1.96 is about 95%.

    Unlike the plain normal interval it stays inside [0, 1] and behaves at
    k=0 and k=n. With n=0 nothing is known, so the whole range is returned.
    """
    if n < 0 or k < 0 or k > n:
        raise ValueError(f"need 0 <= k <= n, got k={k}, n={n}")
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    lo = 0.0 if k == 0 else max(0.0, center - half)
    hi = 1.0 if k == n else min(1.0, center + half)
    return (lo, hi)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar test on the discordant pairs.

    b is the number of cases that failed before and pass now, c the number that
    passed before and fail now. Under "no real difference" each discordant case
    is a fair coin, so the p-value is a two-sided binomial tail with p = 1/2.
    With no discordant cases there is no evidence of a difference: p = 1.
    """
    if b < 0 or c < 0:
        raise ValueError("counts must be non-negative")
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(Fraction(comb(n, i)) for i in range(min(b, c) + 1)) / Fraction(2**n)
    return float(min(Fraction(1), 2 * tail))

"""RMS helpers.

PHD2 (and, very likely, ASIAIR) report RMS as the standard deviation about the
sample mean, so a constant offset doesn't count. "rms" (about zero) is kept as
an option so the logs can be checked against both.
"""

import math
from typing import Optional, Sequence

METHODS = ("stdev", "rms")


def spread(values: Sequence[float], method: str = "stdev") -> Optional[float]:
    """Population std dev ('stdev') or root-mean-square about zero ('rms'). None if empty."""
    n = len(values)
    if n == 0:
        return None
    if method == "rms":
        return math.sqrt(sum(v * v for v in values) / n)
    if method == "stdev":
        mean = sum(values) / n
        return math.sqrt(sum((v - mean) ** 2 for v in values) / n)
    raise ValueError(f"unknown RMS method {method!r}; use one of {METHODS}")


def axis_rms(ra: Sequence[float], dec: Sequence[float], method: str = "stdev"):
    """Return (ra, dec, total) where total = sqrt(ra^2 + dec^2), as PHD2 does. Nones if no data."""
    r = spread(ra, method)
    d = spread(dec, method)
    if r is None or d is None:
        return None, None, None
    return r, d, math.hypot(r, d)


def r2(x: Optional[float]) -> Optional[float]:
    """Round to 2 dp for publishing, keeping None as None."""
    return None if x is None else round(x, 2)

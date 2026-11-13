"""Sun altitude, good to a fraction of a degree, which is plenty for "is it dawn yet"."""

import math


def _sun(t: float, lat: float, lon: float):
    """(altitude, declination) in degrees at Unix time t, for latitude/longitude in degrees (east +)."""
    d = t / 86400.0 - 10957.5                       # days since J2000.0
    g = math.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    lam = math.radians(q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.00000036 * d)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = math.radians((280.46061837 + 360.98564736629 * d) % 360)
    ha = gmst + math.radians(lon) - ra
    la = math.radians(lat)
    alt = math.asin(math.sin(la) * math.sin(dec) + math.cos(la) * math.cos(dec) * math.cos(ha))
    return math.degrees(alt), math.degrees(dec)


def sun_altitude(t: float, lat: float, lon: float) -> float:
    """Degrees above the horizon at Unix time t, for latitude/longitude in degrees (east +)."""
    return _sun(t, lat, lon)[0]


SHORT_NIGHT_MARGIN = 4.0   # degrees above the night's lowest sun


def is_dawn(t: float, lat: float, lon: float, limit: float) -> bool:
    """Sun above `limit` and rising: the night is over. (Rising only, so an early start at
    dusk isn't treated as the end of the night.)

    Near midsummer in the UK the sun never gets down to `limit` (at 51.5° N it bottoms out
    around -15 in June), so "rising past -18" would be true from about 1 am. On such nights
    dawn counts from SHORT_NIGHT_MARGIN above the night's lowest point instead."""
    alt, dec = _sun(t, lat, lon)
    lowest = abs(lat + dec) - 90.0              # the sun's altitude at lower culmination
    limit = max(limit, lowest + SHORT_NIGHT_MARGIN)
    return alt > limit and sun_altitude(t + 600, lat, lon) > alt

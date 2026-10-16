"""Calculation core: positions, houses, new moons, transits.

Numbers and dates only. No interpretation text of any kind lives here.
Swiss Ephemeris (pyswisseph) with the built-in Moshier ephemeris, no data files,
flags FLG_MOSEPH | FLG_SPEED.

Licensed AGPL-3.0, see LICENSE.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import swisseph as swe

FLAGS = swe.FLG_MOSEPH | swe.FLG_SPEED

SIGNS = ["Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo", "Libra",
         "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces"]
SIGN_ABBR = ["Ari", "Tau", "Gem", "Can", "Leo", "Vir", "Lib", "Sco", "Sag", "Cap", "Aqu", "Pis"]

BODIES = {
    "Sun": swe.SUN, "Moon": swe.MOON, "Mercury": swe.MERCURY, "Venus": swe.VENUS,
    "Mars": swe.MARS, "Jupiter": swe.JUPITER, "Saturn": swe.SATURN,
    "Uranus": swe.URANUS, "Neptune": swe.NEPTUNE, "Pluto": swe.PLUTO,
}
DEFAULT_TRANSITING = ["Mars", "Jupiter", "Saturn", "Uranus", "Neptune", "Pluto"]

# Generous upper bounds on apparent speed, degrees per day, to size scan steps.
MAX_SPEED = {"Sun": 1.1, "Moon": 15.5, "Mercury": 2.3, "Venus": 1.3, "Mars": 0.8,
             "Jupiter": 0.3, "Saturn": 0.15, "Uranus": 0.08, "Neptune": 0.04, "Pluto": 0.06}

ASPECTS = {"conjunction": 0.0, "sextile": 60.0, "square": 90.0, "trine": 120.0, "opposition": 180.0}

HOUSE_SYSTEMS = {"whole_sign": b"W", "placidus": b"P"}

MAX_SAMPLES = 600_000          # ephemeris calls allowed for one request
MAX_RANGE_DAYS = 366 * 40      # longest date range any endpoint accepts


class InputError(ValueError):
    """Bad request data. The message is safe to return to the caller."""


# ----------------------------------------------------------------- time ---

def _utc(dt):
    return dt.astimezone(timezone.utc)


def parse_iso(value, *, end_of_day=False):
    """ISO 8601 string to aware UTC datetime. Naive means UTC. A bare date is
    00:00 UTC, or the start of the next day when end_of_day is set."""
    if not isinstance(value, str):
        raise InputError("date/time must be an ISO 8601 string")
    s = value.strip()
    date_only = len(s) == 10
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise InputError(f"cannot parse date/time: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = _utc(dt)
    if date_only and end_of_day:
        dt += timedelta(days=1)
    return dt


def get_zone(name):
    if name is None:
        return None
    if not isinstance(name, str):
        raise InputError("timezone must be an IANA name such as America/Bogota")
    if name.upper() in ("UTC", "UT", "Z"):
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise InputError(f"unknown timezone: {name!r}")


def parse_when(obj):
    """A moment as an object: {"utc": ISO} or {"date","time","timezone"}.
    timezone defaults to UTC. Local times use the IANA database, so historical
    daylight saving applies. Returns an aware UTC datetime."""
    if isinstance(obj, str):
        return parse_iso(obj)
    if not isinstance(obj, dict):
        raise InputError("a moment must be an object or an ISO string")
    if "utc" in obj:
        return parse_iso(obj["utc"])
    if "date" not in obj or "time" not in obj:
        raise InputError("give either utc, or date and time (with timezone for local time)")
    zone = get_zone(obj.get("timezone", "UTC"))
    try:
        t = obj["time"]
        if not isinstance(t, str):
            raise ValueError
        dt = datetime.fromisoformat(f"{obj['date']}T{t}")
    except (ValueError, TypeError):
        raise InputError("date must be YYYY-MM-DD and time HH:MM or HH:MM:SS")
    if dt.tzinfo is not None:
        raise InputError("put the offset in timezone, not in date/time")
    return _utc(dt.replace(tzinfo=zone))


def to_jd(dt):
    dt = _utc(dt)
    return swe.julday(dt.year, dt.month, dt.day,
                      dt.hour + dt.minute / 60 + (dt.second + dt.microsecond / 1e6) / 3600)


def from_jd(jd):
    y, m, d, h = swe.revjul(jd)
    return datetime(y, m, d, tzinfo=timezone.utc) + timedelta(hours=h)


def stamp(jd, zone=None):
    """Time fields for a Julian day: UTC to the second, plus local if a zone is given."""
    dt = from_jd(jd)
    dt = (dt + timedelta(milliseconds=500)).replace(microsecond=0)
    out = {"utc": dt.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if zone is not None:
        loc = dt.astimezone(zone)
        out["local"] = loc.isoformat()
        out["local_date"] = loc.strftime("%Y-%m-%d")
    return out


# ------------------------------------------------------------ longitudes ---

def wrap(x):
    return (x + 180.0) % 360.0 - 180.0


def sign_fields(lon):
    lon %= 360.0
    i = int(lon // 30)
    within = lon - 30 * i
    return {
        "longitude": round(lon, 8),
        "sign": SIGNS[i],
        "sign_abbr": SIGN_ABBR[i],
        "degree_in_sign": round(within, 8),
        "degree": int(within),
        "minute": int((within % 1) * 60),   # floored, never 60
    }


def body_state(jd, name):
    r = swe.calc_ut(jd, BODIES[name], FLAGS)[0]
    return r[0], r[3]


def bisect(f, a, b, iterations=60):
    """Root of f between a and b, where f changes sign."""
    fa = f(a)
    for _ in range(iterations):
        c = (a + b) / 2
        fc = f(c)
        if (fa < 0) == (fc < 0):
            a, fa = c, fc
        else:
            b = c
    return (a + b) / 2


# ----------------------------------------------------------------- houses ---

def compute_houses(jd, lat, lon, system):
    if system not in HOUSE_SYSTEMS:
        raise InputError(f"house_system must be one of {sorted(HOUSE_SYSTEMS)}")
    try:
        cusps, ascmc = swe.houses(jd, lat, lon, HOUSE_SYSTEMS[system])
    except swe.Error:
        raise InputError(f"{system} houses are undefined at latitude {lat}; use whole_sign")
    return list(cusps), ascmc[0], ascmc[1]


def house_of_longitude(lon, cusps):
    """1-based house for an ecliptic longitude, from 12 cusp longitudes."""
    lon %= 360.0
    for i in range(12):
        start = cusps[i] % 360.0
        width = (cusps[(i + 1) % 12] - cusps[i]) % 360.0
        if (lon - start) % 360.0 < width:
            return i + 1
    return 12  # only reachable through float edge cases on a cusp


def _num(obj, key, lo, hi):
    v = obj.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise InputError(f"{key} must be a number")
    if not lo <= v <= hi:
        raise InputError(f"{key} must be between {lo} and {hi}")
    return float(v)


def chart(req):
    """Natal chart. Input: moment fields (see parse_when), lat, lon (east positive),
    house_system (whole_sign default, or placidus)."""
    if not isinstance(req, dict):
        raise InputError("body must be a JSON object")
    dt = parse_when(req)
    lat = _num(req, "lat", -90, 90)
    lon = _num(req, "lon", -180, 180)
    system = req.get("house_system", "whole_sign")
    jd = to_jd(dt)
    cusps, asc, mc = compute_houses(jd, lat, lon, system)

    bodies = {}
    for name in BODIES:
        x, speed = body_state(jd, name)
        f = sign_fields(x)
        f["speed"] = round(speed, 8)
        f["retrograde"] = speed < 0
        f["house"] = house_of_longitude(x, cusps)
        bodies[name] = f
    return {
        "input": {"utc": dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "jd_ut": round(jd, 8),
                  "lat": lat, "lon": lon, "house_system": system},
        "bodies": bodies,
        "angles": {"Ascendant": {**sign_fields(asc), "house": house_of_longitude(asc, cusps)},
                   "Midheaven": {**sign_fields(mc), "house": house_of_longitude(mc, cusps)}},
        "cusps": [{"house": i + 1, **sign_fields(c)} for i, c in enumerate(cusps)],
        "nodes": lunar_nodes(jd, cusps, req.get("node", "true")),
        "engine": engine_info(),
    }


def lunar_nodes(jd, cusps, method="true"):
    """The Moon's nodes: where the Moon's orbit crosses the ecliptic. The south node is
    exactly opposite the north. "true" (the default since 2026-10-16, as most apps show
    it) wobbles around "mean" by up to about 1.7 degrees. Numbers only, like everything here."""
    if method not in ("true", "mean"):
        raise InputError('node must be "true" or "mean"')
    r = swe.calc_ut(jd, swe.TRUE_NODE if method == "true" else swe.MEAN_NODE, FLAGS)[0]
    north, speed = r[0] % 360, r[3]
    out = {}
    for name, x in (("North Node", north), ("South Node", (north + 180) % 360)):
        out[name] = {**sign_fields(x), "house": house_of_longitude(x, cusps),
                     "speed": round(speed, 8), "retrograde": speed < 0}
    out["method"] = method
    return out


def engine_info():
    return {"library": "pyswisseph", "swisseph_version": swe.version,
            "ephemeris": "moshier", "flags": "FLG_MOSEPH|FLG_SPEED"}


def _lon_of(entry, label):
    if isinstance(entry, dict):
        entry = entry.get("longitude")
    if isinstance(entry, bool) or not isinstance(entry, (int, float)):
        raise InputError(f"{label}: longitude must be a number")
    return float(entry) % 360.0


def resolve_natal(natal):
    """Natal data in any of three shapes:
      - a /chart response (has "bodies"),
      - {"positions": {"Sun": 304.9, "Chiron": 101.2, ...}, "cusps": [12 longitudes]},
      - birth data (has "date"), computed here.
    Returns (points name->longitude, cusps list or None)."""
    if not isinstance(natal, dict):
        raise InputError("natal must be an object")
    if "bodies" in natal:
        points = {n: _lon_of(v, n) for n, v in natal["bodies"].items()}
        for n, v in (natal.get("angles") or {}).items():
            points[n] = _lon_of(v, n)
        cusps = natal.get("cusps")
    elif "positions" in natal:
        pos = natal["positions"]
        if not isinstance(pos, dict) or not pos:
            raise InputError("natal.positions must be a non-empty object of name to longitude")
        points = {n: _lon_of(v, n) for n, v in pos.items()}
        cusps = natal.get("cusps")
    elif "date" in natal or "utc" in natal:
        return resolve_natal(chart(natal))
    else:
        raise InputError("natal must be a /chart response, {positions: {...}}, or birth data")
    if cusps is not None:
        if not isinstance(cusps, list) or len(cusps) != 12:
            raise InputError("natal.cusps must list 12 longitudes")
        cusps = [_lon_of(c, "cusp") for c in cusps]
    if len(points) > 40:
        raise InputError("too many natal points")
    return points, cusps


def house_of(req):
    """Which natal house (and sign) a moment falls in. The moment's longitude is that of
    `body` (default Sun; at a new moon Sun and Moon share it) at `moment`, or a plain
    `longitude`."""
    if not isinstance(req, dict):
        raise InputError("body must be a JSON object")
    points, cusps = resolve_natal(req.get("natal"))
    if cusps is None:
        raise InputError("natal needs house cusps: send a /chart response, birth data, or cusps")
    out = {}
    if "longitude" in req:
        lon = _lon_of(req["longitude"], "longitude")
    else:
        if "moment" not in req:
            raise InputError("give moment (with optional body) or longitude")
        body = req.get("body", "Sun")
        if body not in BODIES:
            raise InputError(f"body must be one of {list(BODIES)}")
        dt = parse_when(req["moment"])
        lon, speed = body_state(to_jd(dt), body)
        out["moment_utc"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        out["body"] = body
    out.update(sign_fields(lon))
    out["house"] = house_of_longitude(lon, cusps)
    return out


# -------------------------------------------------------------- new moons ---

def _range(req, max_days=MAX_RANGE_DAYS):
    if "start" not in req or "end" not in req:
        raise InputError("start and end are required (ISO date or datetime)")
    a = parse_iso(req["start"])
    b = parse_iso(req["end"], end_of_day=True)
    if b <= a:
        raise InputError("end must be after start")
    if (b - a).days > max_days:
        raise InputError(f"range too long, the limit is {max_days} days")
    return a, b


def new_moons(req):
    """New moons with exact time, sign and degree. A bare end date includes that whole day."""
    if not isinstance(req, dict):
        raise InputError("body must be a JSON object")
    a, b = _range(req)
    zone = get_zone(req.get("timezone"))
    j0, j1 = to_jd(a), to_jd(b)

    def elong(jd):
        return wrap(body_state(jd, "Moon")[0] - body_state(jd, "Sun")[0])

    out = []
    step = 0.5
    jd = j0
    prev = elong(jd)
    while jd < j1:
        nxt = min(jd + step, j1)
        cur = elong(nxt)
        # conjunction: elongation rises through zero; |value| < 90 excludes the +-180 wrap
        if prev < 0 <= cur and abs(prev) < 90 and abs(cur) < 90:
            t = bisect(elong, jd, nxt)
            lon, _ = body_state(t, "Moon")
            out.append({**stamp(t, zone), **sign_fields(lon), "jd_ut": round(t, 8)})
        jd, prev = nxt, cur
    return {"range": {"start": a.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": b.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "new_moons": out, "engine": engine_info()}


# --------------------------------------------------------------- transits ---

def _orbs(spec):
    if spec is None:
        spec = 1.0
    if isinstance(spec, bool):
        raise InputError("orb must be a number or an object by aspect")
    if isinstance(spec, (int, float)):
        d = {k: float(spec) for k in ASPECTS}
    elif isinstance(spec, dict):
        d = {k: float(spec.get(k, 1.0)) for k in ASPECTS}
    else:
        raise InputError("orb must be a number or an object by aspect")
    for k, v in d.items():
        if not 0.05 <= v <= 10:
            raise InputError("orb must be between 0.05 and 10 degrees")
    return d


def transits(req):
    """Dated aspects of transiting planets to natal points, with orb windows, plus stations.

    A pass is one time the transiting planet is inside the orb; it lists every exact hit
    inside it (a retrograde loop gives up to three). Both aspect points are scanned
    (natal +/- angle) for sextile, square and trine."""
    if not isinstance(req, dict):
        raise InputError("body must be a JSON object")
    points, _ = resolve_natal(req.get("natal"))
    a, b = _range(req)
    zone = get_zone(req.get("timezone"))
    j0, j1 = to_jd(a), to_jd(b)

    planets = req.get("planets", DEFAULT_TRANSITING)
    if (not isinstance(planets, list) or not planets
            or any(p not in BODIES for p in planets) or len(set(planets)) != len(planets)):
        raise InputError(f"planets must be a list drawn from {list(BODIES)}")
    aspects = req.get("aspects", list(ASPECTS))
    if (not isinstance(aspects, list) or not aspects
            or any(x not in ASPECTS for x in aspects) or len(set(aspects)) != len(aspects)):
        raise InputError(f"aspects must be a list drawn from {list(ASPECTS)}")
    orbs = _orbs(req.get("orb"))
    want_targets = req.get("targets")
    if want_targets is not None:
        missing = [t for t in want_targets if t not in points]
        if missing:
            raise InputError(f"targets not in natal: {missing}")
        points = {k: v for k, v in points.items() if k in want_targets}
    include_stations = req.get("stations", True)

    # Plan the sampling and refuse a request that would cost too much.
    min_orb = min(orbs[x] for x in aspects)
    steps = {}
    total = 0
    for p in planets:
        steps[p] = max(min(0.25, 0.5 * min_orb / MAX_SPEED[p]), 0.002)
        total += int((j1 - j0) / steps[p]) + 2
    if total > MAX_SAMPLES:
        raise InputError("request too heavy: shorten the range, drop fast planets, or use a wider orb")

    aspect_list = []
    stations = []
    for p in planets:
        step = steps[p]
        ts, lons, spds = [], [], []
        jd = j0
        while True:
            x, s = body_state(jd, p)
            ts.append(jd); lons.append(x); spds.append(s)
            if jd >= j1:
                break
            jd = min(jd + step, j1)

        if include_stations and p not in ("Sun", "Moon"):
            for i in range(1, len(ts)):
                if (spds[i] < 0) != (spds[i - 1] < 0):
                    t = bisect(lambda u: body_state(u, p)[1], ts[i - 1], ts[i])
                    x, _ = body_state(t, p)
                    stations.append({"planet": p, "type": "retrograde" if spds[i] < 0 else "direct",
                                     **stamp(t, zone), **sign_fields(x)})

        for nname, nlon in points.items():
            for asp in aspects:
                angle = ASPECTS[asp]
                offs = [angle] if angle in (0.0, 180.0) else [angle, -angle]
                orb = orbs[asp]
                for off in offs:
                    target = (nlon + off) % 360.0
                    passes = _scan(p, ts, lons, target, orb, zone, j0, j1)
                    if passes:
                        aspect_list.append({
                            "transiting": p, "natal": nname, "aspect": asp,
                            "target_longitude": round(target, 8), "orb": orb,
                            "passes": passes,
                        })

    aspect_list.sort(key=lambda r: (r["passes"][0]["enter"]["utc"], r["transiting"], r["natal"]))
    stations.sort(key=lambda r: (r["utc"], r["planet"]))
    return {"range": {"start": a.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": b.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "aspects": aspect_list, "stations": stations, "engine": engine_info()}


def _scan(p, ts, lons, target, orb, zone, j0, j1):
    """Passes of planet p through the orb around target, with exact hits inside each."""
    def dist(jd):
        return wrap(body_state(jd, p)[0] - target)

    def inside(jd):
        return abs(dist(jd)) - orb

    d = [wrap(x - target) for x in lons]
    # Cheap rejection: this target never comes within reach in the range.
    if min(abs(v) for v in d) > orb and not any(
            (d[i] < 0) != (d[i - 1] < 0) and abs(d[i]) < 10 for i in range(1, len(d))):
        return []

    events = []  # (jd, kind, payload)
    was_in = abs(d[0]) <= orb
    if was_in:
        events.append((ts[0], "enter", True))
    for i in range(1, len(ts)):
        now_in = abs(d[i]) <= orb
        if (d[i] < 0) != (d[i - 1] < 0) and abs(d[i]) < 10 and abs(d[i - 1]) < 10:
            t = bisect(dist, ts[i - 1], ts[i])
            events.append((t, "exact", None))
        if now_in and not was_in:
            events.append((bisect(inside, ts[i - 1], ts[i]), "enter", False))
        elif was_in and not now_in:
            events.append((bisect(inside, ts[i - 1], ts[i]), "exit", False))
        was_in = now_in
    if was_in:
        events.append((ts[-1], "exit", True))

    order = {"enter": 0, "exact": 1, "exit": 2}
    events.sort(key=lambda e: (e[0], order[e[1]]))

    passes, cur = [], None
    for t, kind, clipped in events:
        if kind == "enter":
            cur = {"enter": stamp(t, zone), "starts_before_range": bool(clipped), "exact": []}
        elif kind == "exact" and cur is not None:
            x, s = body_state(t, p)
            cur["exact"].append({**stamp(t, zone), "retrograde": s < 0,
                                 "transit_longitude": round(x % 360.0, 8)})
        elif kind == "exit" and cur is not None:
            cur["exit"] = stamp(t, zone)
            cur["continues_after_range"] = bool(clipped)
            passes.append(cur)
            cur = None
    return passes


# ---------------------------------------------------------- progressions ---

TROPICAL_YEAR = 365.24219
PROG_ORBS = {"Sun": 2.5, "Moon": 5.0}          # everything else 1.5 (degrees)
PROG_POINTS = ["Sun", "Moon", "Mercury", "Venus", "Mars", "Ascendant", "Midheaven"]


def _armc(jd, lat, lon):
    return swe.houses_ex(jd, lat, lon, b"P")[1][2]


def _ra_sun(jd):
    return swe.calc_ut(jd, swe.SUN, FLAGS | swe.FLG_EQUATORIAL)[0][0]


def _prog_jd(jd_birth, jd_real):
    """Secondary progression: one day after birth for each year of life."""
    return jd_birth + (jd_real - jd_birth) / TROPICAL_YEAR


def _prog_state(jd_birth, jd_real, lat, lon, armc_birth):
    """Longitudes (and speeds per year of life) of the progressed bodies and angles.
    Angles by the solar arc in right ascension: the birth ARMC moved by how far the
    progressed Sun has travelled in right ascension, houses rebuilt at the birth latitude."""
    jp = _prog_jd(jd_birth, jd_real)
    pts = {}
    for name in BODIES:
        x, speed = body_state(jp, name)
        pts[name] = (x, speed)
    armc = (armc_birth + _ra_sun(jp) - _ra_sun(jd_birth)) % 360
    eps = swe.calc_ut(jp, swe.ECL_NUT)[0][0]
    try:
        _, ascmc = swe.houses_armc(armc, lat, eps, b"P")
        pts["Ascendant"] = (ascmc[0] % 360, None)
    except swe.Error:
        pass
    pts["Midheaven"] = (swe.houses_armc(armc, lat, eps, b"W")[1][1] % 360, None)
    return pts


def progressions(req):
    """Secondary progressions for a birth chart at a moment. Input: the birth fields of
    /chart (date, time, timezone or utc, lat, lon, house_system), "at": a moment
    ({"utc": ...} or date fields) and optional "years" (default 1): the window either
    side of "at" scanned for sign and house changes and stations. Numbers only.

    Returns the progressed positions (natal houses), the aspects from progressed Sun,
    Moon, Mercury, Venus, Mars, Ascendant and Midheaven to natal points within orb
    (Sun 2.5, Moon 5, others 1.5 degrees) with an estimated exact date, and the sign
    and house changes and stations inside the window."""
    if not isinstance(req, dict):
        raise InputError("body must be a JSON object")
    natal = chart(req)
    jd_birth = natal["input"]["jd_ut"]
    lat, lon = natal["input"]["lat"], natal["input"]["lon"]
    if "at" not in req:
        raise InputError('give "at": the moment to progress to, as {"utc": ...}')
    jd_at = to_jd(parse_when(req["at"]))
    if jd_at <= jd_birth:
        raise InputError('"at" must be after birth')
    years = _num(req, "years", 0, 10) if "years" in req else 1.0
    zone = get_zone(req.get("timezone_out"))
    cusps = [c["longitude"] for c in natal["cusps"]]
    armc_b = _armc(jd_birth, lat, lon)

    now = _prog_state(jd_birth, jd_at, lat, lon, armc_b)
    natal_pts = {n: v["longitude"] for n, v in natal["bodies"].items()}
    natal_pts.update({n: v["longitude"] for n, v in natal["angles"].items()})
    if natal.get("nodes"):
        natal_pts["North Node"] = natal["nodes"]["North Node"]["longitude"]

    positions = {}
    for n, (x, speed) in now.items():
        f = sign_fields(x)
        f["natal_house"] = house_of_longitude(x, cusps)
        if speed is not None:
            f["speed_per_year"] = round(speed, 6)
            f["retrograde"] = speed < 0
        positions[n] = f

    # Aspects at the moment, with an estimated exact date from the rate of motion.
    later = _prog_state(jd_birth, jd_at + TROPICAL_YEAR, lat, lon, armc_b)
    aspects = []
    for p in PROG_POINTS:
        if p not in now:
            continue
        x = now[p][0]
        rate = wrap(later[p][0] - x)                    # degrees per year of life
        orb = PROG_ORBS.get(p, 1.5)
        for n, y in natal_pts.items():
            sep = wrap(x - y)
            for asp, ang in ASPECTS.items():
                for target in ({ang, -ang} if ang not in (0, 180) else {ang}):
                    off = wrap(sep - target)
                    if abs(off) <= orb:
                        entry = {"progressed": p, "natal": n, "aspect": asp,
                                 "orb": round(abs(off), 3), "applying": (off * rate) < 0}
                        if rate:
                            yrs = -off / rate
                            if abs(yrs) <= 30:
                                entry["exact"] = stamp(jd_at + yrs * TROPICAL_YEAR, zone)
                        aspects.append(entry)
    aspects.sort(key=lambda a: a["orb"])

    # Sign and house changes, and stations, inside the window.
    changes, stations = [], []
    start, end = jd_at - years * TROPICAL_YEAR, jd_at + years * TROPICAL_YEAR
    step = 7.0                                          # real days; the Moon moves about a quarter degree
    prev = _prog_state(jd_birth, start, lat, lon, armc_b)
    t = start
    while t < end:
        t2 = min(t + step, end)
        cur = _prog_state(jd_birth, t2, lat, lon, armc_b)
        for p in PROG_POINTS:
            if p not in cur or p not in prev:
                continue
            a, b = prev[p][0], cur[p][0]
            if int(a // 30) != int(b // 30):
                changes.append({"progressed": p, "kind": "sign", "from": SIGNS[int(a // 30)],
                                "to": SIGNS[int(b // 30)], **stamp(t2, zone)})
            ha, hb = house_of_longitude(a, cusps), house_of_longitude(b, cusps)
            if ha != hb and p not in ("Ascendant", "Midheaven"):
                changes.append({"progressed": p, "kind": "house", "from": ha, "to": hb, **stamp(t2, zone)})
            sa, sb = prev[p][1], cur[p][1]
            if sa is not None and sb is not None and (sa < 0) != (sb < 0):
                stations.append({"progressed": p, "type": "retrograde" if sb < 0 else "direct",
                                 **sign_fields(b), **stamp(t2, zone)})
        prev, t = cur, t2

    return {
        "input": {**natal["input"], "at": stamp(jd_at)["utc"],
                  "progressed_date": stamp(_prog_jd(jd_birth, jd_at))["utc"], "years": years},
        "positions": positions, "aspects": aspects, "changes": changes, "stations": stations,
        "method": "secondary progressions (a day for a year); angles by solar arc in right ascension",
        "engine": engine_info(),
    }

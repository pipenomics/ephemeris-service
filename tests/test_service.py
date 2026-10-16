"""Checks for the ephemeris service. Run from the service folder:

    ~/.venvs/ephem/bin/python -I -m unittest discover -s tests -v

Starts the real HTTP server on a random local port with a random key. Standard library only.

Where the expected values come from (this folder imports nothing from outside itself):
  * CHART_*   : printed by the verified check_positions.py (swisseph, Moshier), 2026-10-14.
  * TRANSIT_* : printed by the verified transit_dates.py logic, run on the fixture natal
                points below (round invented numbers, not anyone's chart), dates in
                America/Bogota local time as that script prints them.
  * NEW_MOONS : Fred Espenak's Phases of the Moon table, Universal Time, fetched 2026-10-15:
                https://astropixels.com/ephemeris/phasescat/phases2001.html
"""
import json
import math
import os
import secrets
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import swisseph as swe  # noqa: E402
import server  # noqa: E402
import core  # noqa: E402

KEY = secrets.token_urlsafe(32)

CHART_2001 = """\
Sun      Tau  9°13'
Moon     Can 23°19'
Mercury  Tau 16°20'
Venus    Ari  3°03'
Mars     Sag 28°10'
Jupiter  Gem 13°10'
Saturn   Gem  1°02'
Uranus   Aqu 24°28'
Neptune  Aqu  8°44'
Pluto    Sag 14°48' R"""

CHART_2026 = """\
Sun      Lib 21°09'
Moon     Sag  5°35'
Mercury  Sco 16°02'
Venus    Sco  6°02' R
Mars     Leo  9°21'
Jupiter  Leo 21°52'
Saturn   Ari 10°31' R
Uranus   Gem  5°13' R
Neptune  Ari  2°29' R
Pluto    Aqu  3°04' R"""

BOGOTA = {"lat": 4.6, "lon": -74.1}

NATAL = {"Uranus": 278.0, "Moon": 193.0, "Sun": 305.0, "Ascendant": 145.0, "Mercury": 302.5}
# (transiting, natal, aspect, [(local date, retrograde), ...])
TRANSIT_EXACT = [
    ("Saturn", "Uranus", "square", [("2026-04-20", False), ("2026-12-02", True), ("2026-12-19", False)]),
    ("Saturn", "Moon", "opposition", [("2026-06-10", False), ("2026-09-11", True), ("2027-02-28", False)]),
    ("Jupiter", "Ascendant", "conjunction", [("2026-11-06", False), ("2027-01-17", True), ("2027-06-30", False)]),
    ("Jupiter", "Sun", "opposition", [("2026-07-22", False)]),
    ("Pluto", "Mercury", "conjunction", [("2025-02-15", False), ("2025-07-28", True), ("2025-12-24", False)]),
]
STATIONS = [("Saturn", "retrograde", "2026-07-26"), ("Saturn", "direct", "2026-12-10"),
            ("Jupiter", "retrograde", "2026-12-12"), ("Pluto", "direct", "2026-10-15"),
            ("Pluto", "retrograde", "2027-05-08")]

NEW_MOONS = ["2026-10-10T15:50", "2026-11-09T07:02", "2026-12-09T00:52", "2027-01-07T20:24"]


def call(base, path, body=None, key=KEY, method="POST", raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    if key is not None:
        req.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def fmt(b):
    return f"{b['sign_abbr']} {b['degree']:2d}°{b['minute']:02d}'" + (" R" if b["retrograde"] else "")


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = server.make_server("127.0.0.1", 0, KEY)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    # (d) authentication
    def test_rejects_missing_wrong_and_malformed_keys(self):
        for path in ("/chart", "/new-moons", "/transits", "/house-of", "/nope"):
            self.assertEqual(call(self.base, path, {}, key=None)[0], 401, path)
            self.assertEqual(call(self.base, path, {}, key="wrong-key-wrong-key")[0], 401, path)
        self.assertEqual(call(self.base, "/chart", None, key=None, method="GET")[0], 401)
        self.assertEqual(call(self.base, "/chart", {}, key=KEY[:-1])[0], 401)

    def test_bearer_header_accepted(self):
        req = urllib.request.Request(self.base + "/chart", method="POST", data=json.dumps(
            {"date": "2026-10-14", "time": "12:00", **BOGOTA}).encode())
        req.add_header("Authorization", "Bearer " + KEY)
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 200)

    def test_server_refuses_a_short_key(self):
        with self.assertRaises(SystemExit):
            server.make_server("127.0.0.1", 0, "short")

    def test_bad_input_is_400_not_500(self):
        self.assertEqual(call(self.base, "/chart", {"date": "x", "time": "12:00", **BOGOTA})[0], 400)
        self.assertEqual(call(self.base, "/chart", {"date": "2026-10-14", "time": "12:00", "lat": 200, "lon": 0})[0], 400)
        self.assertEqual(call(self.base, "/chart", None, raw=b"{not json")[0], 400)
        self.assertEqual(call(self.base, "/chart", None, method="GET")[0], 405)
        self.assertEqual(call(self.base, "/chart", {"date": "2026-10-14", "time": "12:00", "timezone": "Mars/Base", **BOGOTA})[0], 400)

    # (a) positions
    def _chart_lines(self, date):
        s, r = call(self.base, "/chart", {"date": date, "time": "12:00", "timezone": "UTC", **BOGOTA})
        self.assertEqual(s, 200)
        return [f"{n:8} {fmt(b)}" for n, b in r["bodies"].items()]

    def test_chart_matches_check_positions_2001(self):
        got = [l.rstrip() for l in self._chart_lines("2001-04-29")]
        self.assertEqual(got, [self._norm(l) for l in CHART_2001.splitlines()])

    def test_chart_matches_check_positions_2026(self):
        got = [l.rstrip() for l in self._chart_lines("2026-10-14")]
        self.assertEqual(got, [self._norm(l) for l in CHART_2026.splitlines()])

    @staticmethod
    def _norm(line):
        name, rest = line.split(None, 1)
        return f"{name:8} {rest.strip()}"

    def test_local_time_uses_the_timezone(self):
        s, r = call(self.base, "/chart", {"date": "2026-10-14", "time": "07:00", "timezone": "America/Bogota", **BOGOTA})
        self.assertEqual(r["input"]["utc"], "2026-10-14T12:00:00Z")

    def test_angles_and_cusps_against_the_closed_form(self):
        # Independent of swe.houses: MC and Ascendant from sidereal time and obliquity.
        s, r = call(self.base, "/chart", {"date": "2026-10-14", "time": "12:00", "house_system": "placidus", **BOGOTA})
        jd = swe.julday(2026, 10, 14, 12)
        ramc = math.radians((swe.sidtime(jd) * 15 + BOGOTA["lon"]) % 360)
        eps = math.radians(swe.calc_ut(jd, swe.ECL_NUT)[0][0])
        lat = math.radians(BOGOTA["lat"])
        mc = math.degrees(math.atan2(math.sin(ramc), math.cos(ramc) * math.cos(eps))) % 360
        asc = math.degrees(math.atan2(math.cos(ramc), -(math.sin(ramc) * math.cos(eps) + math.tan(lat) * math.sin(eps)))) % 360
        self.assertLess(abs((r["angles"]["Midheaven"]["longitude"] - mc + 180) % 360 - 180), 0.01)
        self.assertLess(abs((r["angles"]["Ascendant"]["longitude"] - asc + 180) % 360 - 180), 0.01)
        c = r["cusps"]
        self.assertEqual(len(c), 12)
        self.assertAlmostEqual(c[0]["longitude"], r["angles"]["Ascendant"]["longitude"], places=6)
        self.assertAlmostEqual(c[9]["longitude"], r["angles"]["Midheaven"]["longitude"], places=6)

    def test_whole_sign_cusps_start_at_the_ascendant_sign(self):
        s, r = call(self.base, "/chart", {"date": "2026-10-14", "time": "12:00", **BOGOTA})
        first = int(r["angles"]["Ascendant"]["longitude"] // 30)
        self.assertEqual([c["longitude"] for c in r["cusps"]], [((first + i) % 12) * 30.0 for i in range(12)])

    def test_placidus_rejected_where_undefined(self):
        s, r = call(self.base, "/chart", {"date": "2026-10-14", "time": "12:00", "lat": 80, "lon": 10, "house_system": "placidus"})
        self.assertEqual(s, 400)
        self.assertEqual(call(self.base, "/chart", {"date": "2026-10-14", "time": "12:00", "lat": 80, "lon": 10})[0], 200)

    # (b) transits
    def test_transit_exact_dates_match_transit_dates(self):
        s, r = call(self.base, "/transits", {
            "natal": {"positions": NATAL}, "planets": ["Saturn", "Jupiter", "Pluto"],
            "start": "2024-01-01", "end": "2030-12-31", "timezone": "America/Bogota"})
        self.assertEqual(s, 200)
        for planet, natal, aspect, want in TRANSIT_EXACT:
            found = [a for a in r["aspects"] if (a["transiting"], a["natal"], a["aspect"]) == (planet, natal, aspect)
                     and any(p["exact"] for p in a["passes"])]
            self.assertEqual(len(found), 1, (planet, natal, aspect))
            got = [(e["local_date"], e["retrograde"]) for p in found[0]["passes"] for e in p["exact"]]
            self.assertEqual(got, want, (planet, natal, aspect))
        have = {(x["planet"], x["type"], x["local_date"]) for x in r["stations"]}
        for st in STATIONS:
            self.assertIn(st, have)

    def test_orb_windows_contain_their_exact_hits(self):
        s, r = call(self.base, "/transits", {"natal": {"positions": NATAL}, "planets": ["Saturn"],
                                             "start": "2026-01-01", "end": "2027-12-31", "orb": 1.0})
        for a in r["aspects"]:
            for p in a["passes"]:
                for e in p["exact"]:
                    self.assertTrue(p["enter"]["utc"] <= e["utc"] <= p["exit"]["utc"])
                    d = (e["transit_longitude"] - a["target_longitude"] + 180) % 360 - 180
                    self.assertLess(abs(d), 1e-4)

    def test_natal_can_be_a_chart_response_or_birth_data(self):
        birth = {"date": "1990-03-05", "time": "08:30", "timezone": "America/Bogota", **BOGOTA}
        _, chart = call(self.base, "/chart", birth)
        body = {"planets": ["Saturn"], "start": "2026-01-01", "end": "2026-12-31", "aspects": ["conjunction"]}
        _, a = call(self.base, "/transits", {**body, "natal": chart})
        _, b = call(self.base, "/transits", {**body, "natal": birth})
        self.assertEqual(a["aspects"], b["aspects"])

    # (c) new moons
    def test_new_moons_within_a_minute_of_espenak(self):
        s, r = call(self.base, "/new-moons", {"start": "2026-10-01", "end": "2027-01-31"})
        self.assertEqual(s, 200)
        got = [datetime.strptime(m["utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) for m in r["new_moons"]]
        want = [datetime.strptime(w, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc) for w in NEW_MOONS]
        self.assertEqual(len(got), len(want))
        for g, w in zip(got, want):
            # The reference is rounded to the minute, so allow a minute plus that rounding.
            self.assertLessEqual(abs((g - w).total_seconds()), 60, (g, w))

    def test_new_moon_sun_and_moon_coincide(self):
        s, r = call(self.base, "/new-moons", {"start": "2026-10-01", "end": "2026-10-31"})
        m = r["new_moons"][0]
        _, c = call(self.base, "/chart", {"utc": m["utc"], **BOGOTA})
        d = (c["bodies"]["Moon"]["longitude"] - c["bodies"]["Sun"]["longitude"] + 180) % 360 - 180
        self.assertLess(abs(d), 0.01)
        self.assertEqual(m["sign"], c["bodies"]["Sun"]["sign"])

    # house-of
    def test_house_of_whole_sign(self):
        _, chart = call(self.base, "/chart", {"date": "1990-03-05", "time": "08:30", "timezone": "America/Bogota", **BOGOTA})
        s, r = call(self.base, "/house-of", {"moment": {"utc": "2026-10-10T15:50:00Z"}, "natal": chart})
        self.assertEqual(s, 200)
        asc_sign = int(chart["angles"]["Ascendant"]["longitude"] // 30)
        sun_sign = int(r["longitude"] // 30)
        self.assertEqual(r["house"], (sun_sign - asc_sign) % 12 + 1)
        self.assertEqual(r["sign"], "Libra")

    def test_house_of_placidus_boundaries(self):
        birth = {"date": "1990-03-05", "time": "08:30", "timezone": "America/Bogota", "house_system": "placidus", **BOGOTA}
        _, chart = call(self.base, "/chart", birth)
        for i, c in enumerate(chart["cusps"]):
            _, r = call(self.base, "/house-of", {"longitude": (c["longitude"] + 0.01) % 360, "natal": chart})
            self.assertEqual(r["house"], i + 1)
            _, r = call(self.base, "/house-of", {"longitude": (c["longitude"] - 0.01) % 360, "natal": chart})
            self.assertEqual(r["house"], (i - 1) % 12 + 1)


if __name__ == "__main__":
    unittest.main()


class TestNodes(unittest.TestCase):
    def test_mean_node_matches_meeus_at_j2000(self):
        # Meeus, Astronomical Algorithms: mean ascending node = 125.0445479 deg at J2000.0
        c = core.chart({"utc": "2000-01-01T12:00:00Z", "lat": 0, "lon": 0, "node": "mean"})
        north = c["nodes"]["North Node"]["longitude"]
        self.assertAlmostEqual(north, 125.0445479, delta=0.01)
        self.assertAlmostEqual((c["nodes"]["South Node"]["longitude"] - north) % 360, 180, delta=1e-9)
        self.assertEqual(c["nodes"]["method"], "mean")

    def test_true_node_stays_near_the_mean_node(self):
        # The true node wobbles around the mean node by under two degrees.
        for utc in ("1985-06-15T13:30:00Z", "2000-01-01T12:00:00Z", "2026-11-09T07:02:07Z"):
            t = core.chart({"utc": utc, "lat": 0, "lon": 0})["nodes"]["North Node"]["longitude"]
            m = core.chart({"utc": utc, "lat": 0, "lon": 0, "node": "mean"})["nodes"]["North Node"]["longitude"]
            self.assertLess(abs((t - m + 180) % 360 - 180), 2.0, utc)


class TestProgressions(unittest.TestCase):
    # An invented birth, not anyone's chart.
    BIRTH = {"date": "1985-06-15", "time": "09:30", "timezone": "America/New_York",
             "lat": 40.71, "lon": -74.01, "house_system": "placidus"}
    BIRTH_UTC = datetime(1985, 6, 15, 13, 30, tzinfo=timezone.utc)

    def test_progressed_date_is_one_day_per_year(self):
        # 41.40 years after birth progresses to 41.40 days after birth: 26 July 1985.
        r = core.progressions({**self.BIRTH, "at": {"utc": "2026-11-09T07:02:07Z"}})
        self.assertTrue(r["input"]["progressed_date"].startswith("1985-07-26"))

    def test_progressed_sun_matches_the_chart_on_the_progressed_day(self):
        r = core.progressions({**self.BIRTH, "at": {"utc": "2026-11-09T07:02:07Z"}})
        c = core.chart({"utc": r["input"]["progressed_date"], "lat": 40.71, "lon": -74.01})
        self.assertAlmostEqual(r["positions"]["Sun"]["longitude"], c["bodies"]["Sun"]["longitude"], delta=0.01)

    def test_progressed_sun_enters_cancer_at_the_1985_solstice(self):
        # Found directly from the Sun's position: the June 1985 solstice (Sun at 0 Cancer).
        # The progressed Sun reaches it as many years after birth as the solstice was days after it.
        j0 = core.to_jd(self.BIRTH_UTC)
        sol = core.bisect(lambda j: core.wrap(core.body_state(j, "Sun")[0] - 90.0), j0, j0 + 15)
        want = self.BIRTH_UTC + timedelta(days=(sol - j0) * core.TROPICAL_YEAR)
        r = core.progressions({**self.BIRTH, "at": {"utc": want.strftime("%Y-%m-%dT%H:%M:%SZ")}, "years": 1})
        ch = [c for c in r["changes"] if c["progressed"] == "Sun" and c["kind"] == "sign"]
        self.assertEqual(len(ch), 1)
        self.assertEqual(ch[0]["to"], "Cancer")
        got = datetime.fromisoformat(ch[0]["utc"].replace("Z", "+00:00"))
        self.assertLessEqual(abs((got - want).days), 10)

    def test_progressed_angles_are_consistent(self):
        # The progressed Ascendant must be the one the closed-form formula gives for the
        # progressed Midheaven at the birth latitude.
        r = core.progressions({**self.BIRTH, "at": {"utc": "2026-11-09T07:02:07Z"}})
        mc, asc = r["positions"]["Midheaven"]["longitude"], r["positions"]["Ascendant"]["longitude"]
        jd = core.to_jd(datetime.fromisoformat(r["input"]["progressed_date"].replace("Z", "+00:00")))
        eps = math.radians(swe.calc_ut(jd, swe.ECL_NUT)[0][0])
        m = math.radians(mc)
        ramc = math.atan2(math.sin(m) * math.cos(eps), math.cos(m))
        lat = math.radians(self.BIRTH["lat"])
        want = math.degrees(math.atan2(math.cos(ramc), -(math.sin(ramc) * math.cos(eps) + math.tan(lat) * math.sin(eps)))) % 360
        self.assertLess(abs((asc - want + 180) % 360 - 180), 0.05)

# Ephemeris service

A small HTTP service that does astronomical arithmetic and nothing else: planetary
positions, houses, new moons, and dated transits, as JSON numbers and dates.

**It is calculation-only.** It returns no interpretation text, no principle or meaning
tables, no prompts and no prose of any kind. Whatever turns these numbers into a reading
lives in a different program that talks to this one over HTTP and JSON. Nothing here
imports from, or depends on, that program.

## Why this is its own AGPL-3.0 folder

The maths uses [pyswisseph](https://github.com/astrorigin/pyswisseph), the Python binding
of the Swiss Ephemeris, which is distributed under the GNU AGPL (or a paid Astrodienst
professional license). A program that links it carries that obligation, so the part that
calls the library is kept small, separate and open source (AGPL-3.0, see `LICENSE`), and
anything else talks to it only over the network. License terms and the professional
license: <https://www.astro.com/swisseph/swephinfo_e.htm>. Whether that separation is
enough for a given product is Astrodienst's to confirm, not this README's.

## Run it

Python 3.9 or newer. The only dependency is pyswisseph (`requirements.txt`).

    python3 -m venv ~/.venvs/ephem && ~/.venvs/ephem/bin/pip install -r requirements.txt
    export EPHEMERIS_API_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
    ~/.venvs/ephem/bin/python -I server.py            # http://127.0.0.1:8765

    ~/.venvs/ephem/bin/python -I -m unittest discover -s tests -v

- The service will not start without `EPHEMERIS_API_KEY` of at least 16 characters.
- Every request, any method, any path, needs the key as `X-API-Key: <key>` or
  `Authorization: Bearer <key>`. Missing or wrong gives 401 before anything else is read.
- It binds to `127.0.0.1`. `--host` can change that, with a warning: there is no TLS here,
  so put a TLS terminator in front before any network use.
- Requests carry birth data. Bodies are never logged (only method, path and status),
  and responses are `Cache-Control: no-store`.

## Conventions

- Longitudes are ecliptic degrees 0 to 360, tropical. Latitude north and longitude **east**
  are positive.
- Times are UTC. A moment is `{"utc": "2026-10-10T15:50:00Z"}` or
  `{"date": "2001-04-29", "time": "12:00", "timezone": "UTC"}`. A local time uses an IANA
  zone (`America/Bogota`) with historical daylight saving; the response echoes the UTC
  instant it resolved. A skipped or repeated local time at a daylight saving change takes
  the first reading, so send UTC when that matters.
- Date ranges: `start` and `end` are ISO dates or datetimes in UTC. A bare `end` date
  includes that whole day. Pass `"timezone"` to also get `local` and `local_date` on each time.
- Positions carry `sign`, `sign_abbr`, `degree_in_sign` (float), `degree` and `minute`
  (minute is floored, never 60), `longitude`, and for bodies `speed` (degrees per day),
  `retrograde` and `house`.
- Errors are `{"error": "..."}` with status 400 for bad input, 401 for the key.

## Endpoints (all POST)

### /chart

    {"date": "2001-04-29", "time": "12:00", "timezone": "UTC",
     "lat": 4.6, "lon": -74.1, "house_system": "whole_sign"}

With the server running, from a shell that has the same key exported (put your own birth
data in, local time with an IANA zone, longitude east positive):

    curl -s -X POST http://127.0.0.1:8765/chart -H "X-API-Key: $EPHEMERIS_API_KEY" \
      -d '{"date":"YYYY-MM-DD","time":"HH:MM","timezone":"America/Bogota","lat":0.0,"lon":0.0}'

`house_system` is `whole_sign` (default) or `placidus` (refused with 400 where undefined,
roughly above 66 degrees latitude). Returns `bodies` (Sun to Pluto), `angles`
(Ascendant, Midheaven), `cusps` (12) and `engine` info.

### /new-moons

    {"start": "2026-10-01", "end": "2027-01-31", "timezone": "America/Bogota"}

Returns `new_moons`: exact time (to the second), sign and degree.

### /transits

    {"natal": {...}, "start": "2026-10-01", "end": "2027-12-31",
     "planets": ["Mars", "Jupiter", "Saturn", "Uranus", "Neptune", "Pluto"],
     "aspects": ["conjunction", "sextile", "square", "trine", "opposition"],
     "orb": 1.0, "targets": ["Sun", "Moon"], "stations": true}

`natal` is any of: a `/chart` response; `{"positions": {"Sun": 304.9, "Chiron": 101.2},
"cusps": [...]}` (any named points, which is how a point this library cannot compute
reaches the scan); or birth data as for `/chart`. Defaults are Mars to Pluto, all five
aspects, orb 1 degree (a number, or an object by aspect), all natal points including the
Ascendant and Midheaven, stations on.

Returns `aspects`, each a transiting planet, natal point, aspect and `target_longitude`
(sextile, square and trine are scanned on both sides of the natal point), with `passes`.
A pass is one stay inside the orb: `enter`, `exit`, and `exact` hits inside it (a
retrograde loop gives up to three, each with `retrograde` and the transit longitude).
`starts_before_range` and `continues_after_range` mark windows cut by the range edges.
Also returns `stations` (retrograde and direct, with time and position).

Limits: 40 years per range and a cap on ephemeris calls per request (refused with 400 beyond it).
Fast bodies (Sun, Moon, Mercury, Venus) are allowed but sampled finely, so keep ranges short.

### /house-of

    {"moment": {"utc": "2026-10-10T15:50:07Z"}, "body": "Sun",
     "natal": {...a /chart response, or birth data, or positions with cusps...}}

or `{"longitude": 197.36, "natal": {...}}`. Returns the longitude, sign and natal `house`.
`body` defaults to Sun (at a new moon Sun and Moon share a longitude).

### /progressions

    {"date": "1985-06-15", "time": "09:30", "timezone": "America/New_York", "lat": 40.71, "lon": -74.01,
     "house_system": "placidus", "at": {"utc": "2026-11-09T07:02:07Z"}, "years": 2}

Secondary progressions (a day after birth for each year of life). Returns the progressed
positions in the natal houses, the progressed Ascendant and Midheaven (solar arc in right
ascension), aspects from the progressed Sun, Moon, Mercury, Venus, Mars and angles to
natal points within orb (Sun 2.5, Moon 5, others 1.5 degrees) with an estimated exact
date, and the sign and house changes and stations within `years` either side of `at`.

## Accuracy and what was checked

Swiss Ephemeris through pyswisseph 2.10.3.2 with the built-in Moshier ephemeris
(`FLG_MOSEPH | FLG_SPEED`, no data files), the same as the tools this service generalises.
Houses, Ascendant and Midheaven come from `swe.houses`. Moshier is accurate to arcseconds
for the planets over the modern era, which is far finer than anything a monthly report uses.
Chiron is not computed here (it needs a data file). The Moon's mean nodes are, in `nodes` on every `/chart` response (added 2026-10-16), checked against Meeus's formula for the mean node.

`tests/test_service.py` checks: chart positions for 2001-04-29 and 2026-10-14 at 12 UT
against the printed output of a previously verified script; transit exact dates and
stations against that script's logic; new moons against Espenak's published table
(<https://astropixels.com/ephemeris/phasescat/phases2001.html>, within one minute);
Ascendant and Midheaven against the closed-form formula; house membership; the mean node
against Meeus and the true node within two degrees of it; progressions against the Sun's own
solstice crossing and the closed-form Ascendant; and that every request without the right key
is refused. Every birth used in the tests is invented.

## License

AGPL-3.0, see `LICENSE`. If you run a modified version for others over a network, the
AGPL requires you to offer them its source.

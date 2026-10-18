"""Run a private check file (one that calls core.chart and core.progressions directly)
against the deployed service, printing pass or fail counts only.

    EPHEMERIS_URL=https://... EPHEMERIS_API_KEY=... \
        ~/.venvs/ephem/bin/python -I tests/run_private_remote.py /path/to/private_checks.py

core.chart and core.progressions are replaced by HTTP calls to /chart and /progressions,
so every assertion in the check file runs on the hosted numbers. The check file itself
is never read or printed here: this runner holds no birth data and shows no values.

Licensed AGPL-3.0, see LICENSE.
"""
import importlib.util
import json
import os
import sys
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import core  # noqa: E402

url = os.environ.get("EPHEMERIS_URL", "").rstrip("/")
key = os.environ.get("EPHEMERIS_API_KEY")
if not url or not key or len(sys.argv) < 2:
    sys.exit("usage: EPHEMERIS_URL=... EPHEMERIS_API_KEY=... run_private_remote.py <check file>")


def _post(path, body):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                 headers={"X-API-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


core.chart = lambda req: _post("/chart", req)
core.progressions = lambda req: _post("/progressions", req)

spec = importlib.util.spec_from_file_location("private_checks", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
    # Quiet runner: names of tests only, never assertion values.
    result = unittest.TextTestRunner(verbosity=2, stream=open(os.devnull, "w")).run(suite)
    print(f"ran {result.testsRun} tests against {url}: "
          f"{'PASS' if result.wasSuccessful() else 'FAIL'} "
          f"(failures {len(result.failures)}, errors {len(result.errors)})")
    for t, _ in result.failures + result.errors:
        print("  failed:", t.id().rsplit('.', 1)[-1])
    sys.exit(0 if result.wasSuccessful() else 1)

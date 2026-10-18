"""Run tests/test_service.py against a deployed copy of the service instead of a local one.

    EPHEMERIS_URL=https://... EPHEMERIS_API_KEY=... \
        ~/.venvs/ephem/bin/python -I tests/run_remote.py [-v]

ServiceTests normally starts the real server on a random local port; here its base URL is
the hosted one and its key the hosted key, and nothing else in the test file changes.
TestNodes and TestProgressions call core directly and so still run against the local
code, which is the same code the container mounts. test_server_refuses_a_short_key is
local by nature too.

Licensed AGPL-3.0, see LICENSE.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_service  # noqa: E402

url = os.environ.get("EPHEMERIS_URL")
key = os.environ.get("EPHEMERIS_API_KEY")
if not url or not key:
    sys.exit("set EPHEMERIS_URL and EPHEMERIS_API_KEY")

test_service.KEY = key
# call(base, path, body=None, key=KEY, ...) bound the random KEY as a default at import.
test_service.call.__defaults__ = (None, key, "POST", None)


def _remote_setup(cls):
    cls.base = url.rstrip("/")


test_service.ServiceTests.setUpClass = classmethod(_remote_setup)
test_service.ServiceTests.tearDownClass = classmethod(lambda cls: None)

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(test_service)
    result = unittest.TextTestRunner(verbosity=2 if "-v" in sys.argv else 1).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)

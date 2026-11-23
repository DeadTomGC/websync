"""Pytest configuration for the websync test suite.

Loads the local .env file (if present) before tests are collected, so that:
- live tests' skipif conditions (e.g. CAMEO_TWC_BASE_URL) see values from
  .env, and
- anything reading tool settings behaves the same as a local CLI run.

Real environment variables always win (override=False), so CI variables are
unaffected. Unit tests never require .env — no value there is needed for the
default suite.
"""

import pytest

from websync.env import load_env

load_env()


@pytest.fixture
def twc_stub():
    """A running local stub HTTP server for atwc/aiohttp contract tests.

    Point ``CAMEO_TWC_BASE_URL`` at ``twc_stub.url`` and register routes;
    every request is recorded in ``twc_stub.calls``.
    """
    from httpstub import StubHTTPServer

    server = StubHTTPServer().start()
    yield server
    server.stop()

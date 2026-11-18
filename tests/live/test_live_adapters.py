"""Live adapter checks against real internal instances.

Run manually (never in CI):
    pytest -m live tests/live/test_live_adapters.py -v

Requires the tool env vars to be set (see .env.example) and SHOULD point at a
scratch project, not production data. These are the Phase 2 exit-criteria
checks: healthcheck, fetch a few changed items, and print a sample so payload
shapes can be compared against docs/tools/*.md.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("CAMEO_TWC_BASE_URL"), reason="CAMEO_TWC_BASE_URL not set"
    ),
]

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def test_cameo_twc_live():
    from websync.adapters.cameo_twc import CameoTWCAdapter
    from websync.config import ScopeConfig

    scope = ScopeConfig(project=os.environ["CAMEO_TWC_LIVE_PROJECT"])
    adapter = CameoTWCAdapter.from_env(scope)
    adapter.healthcheck()
    items = adapter.fetch_changed(EPOCH)
    print(f"\nTWC: fetched {len(items)} element(s)")
    for item in items[:3]:
        print(f"  {item.native_id} [{item.type}] {item.fields.get('name')!r}")


@pytest.mark.skipif(not os.environ.get("JAMA_BASE_URL"), reason="JAMA_BASE_URL not set")
def test_jama_live():
    from websync.adapters.jama import JamaAdapter
    from websync.config import ScopeConfig

    scope = ScopeConfig(project=int(os.environ["JAMA_LIVE_PROJECT"]))
    adapter = JamaAdapter.from_env(scope)
    adapter.healthcheck()
    items = adapter.fetch_changed(EPOCH)
    print(f"\nJama: fetched {len(items)} item(s)")
    for item in items[:3]:
        print(f"  {item.native_id} [{item.type}] {item.fields.get('name')!r}")
        print(f"    fields: {list(item.fields)}")

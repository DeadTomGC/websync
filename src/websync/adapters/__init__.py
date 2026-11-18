"""Tool adapters.

Each adapter wraps one external tool behind the :class:`~websync.adapters.base.Adapter`
protocol. The sync engine never imports a concrete adapter directly.
"""

from websync.adapters.base import Adapter, SyncError

ADAPTER_REGISTRY: dict[str, str] = {
    # name -> adapter class path; populated as adapters are implemented.
    "cameo_twc": "websync.adapters.cameo_twc.CameoTWCAdapter",
    "jama": "websync.adapters.jama.JamaAdapter",
    "jira": "websync.adapters.jira.JiraAdapter",
}

__all__ = ["ADAPTER_REGISTRY", "Adapter", "SyncError"]

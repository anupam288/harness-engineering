"""
harness/monitoring/adapters/__init__.py

Adapter registry. Adding a new adapter:
1. Create harness/monitoring/adapters/my_adapter.py
2. Subclass BaseLogAdapter, set SOURCE_NAME
3. Register it here in ADAPTER_REGISTRY
4. Add config in monitoring_config.yaml under adapters:
"""

from harness.monitoring.adapters.file_adapter import FileAdapter, StdoutAdapter
from harness.monitoring.adapters.loki_adapter import LokiAdapter
from harness.monitoring.adapters.datadog_adapter import DatadogAdapter
from harness.monitoring.adapters.webhook_adapter import WebhookAdapter
from harness.monitoring.base_adapter import BaseLogAdapter

ADAPTER_REGISTRY: dict[str, type[BaseLogAdapter]] = {
    "file":      FileAdapter,
    "stdout":    StdoutAdapter,
    "loki":      LokiAdapter,
    "datadog":   DatadogAdapter,
    "webhook":   WebhookAdapter,
}


def build_adapter(name: str, config: dict) -> BaseLogAdapter:
    """
    Build an adapter instance from its name and config dict.
    Raises ValueError for unknown adapter names.
    """
    cls = ADAPTER_REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown adapter '{name}'. "
            f"Available: {sorted(ADAPTER_REGISTRY.keys())}. "
            f"To add a new adapter, see harness/monitoring/adapters/__init__.py."
        )
    return cls(config)


def build_adapters_from_config(monitoring_config: dict) -> list[BaseLogAdapter]:
    """
    Build all enabled adapters from the monitoring_config dict.
    Called by LogIngestor on startup.
    """
    adapters = []
    for name, adapter_cfg in monitoring_config.get("adapters", {}).items():
        if not adapter_cfg.get("enabled", True):
            continue
        try:
            adapter = build_adapter(name, adapter_cfg)
            adapters.append(adapter)
        except Exception as exc:
            print(f"  ⚠ Failed to build adapter '{name}': {exc}")
    return adapters


__all__ = [
    "BaseLogAdapter",
    "FileAdapter", "StdoutAdapter",
    "LokiAdapter",
    "DatadogAdapter",
    "WebhookAdapter",
    "ADAPTER_REGISTRY",
    "build_adapter",
    "build_adapters_from_config",
]

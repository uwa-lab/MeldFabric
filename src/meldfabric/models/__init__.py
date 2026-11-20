"""Model provider adapters and model construction."""

from meldfabric.models.registry import (
    DiscoveredModel,
    ModelAdapter,
    ModelAdapterRegistry,
    OpenAICompatibleAdapter,
)

__all__ = [
    "DiscoveredModel",
    "ModelAdapter",
    "ModelAdapterRegistry",
    "OpenAICompatibleAdapter",
]

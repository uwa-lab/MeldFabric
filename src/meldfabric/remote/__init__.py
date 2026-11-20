"""Remote Agent protocol and orchestration."""

from meldfabric.remote.a2a_client import A2ARemoteClient
from meldfabric.remote.client import MeldFabricRemoteClient
from meldfabric.remote.manager import RemoteAgentManager
from meldfabric.remote.models import (
    RemoteAgentDescriptor,
    RemoteCallContext,
    RemoteCancelRequest,
    RemoteInvokeRequest,
    RemoteResumeEntry,
    RemoteResumeRequest,
    RemoteRunResult,
    RemoteTopology,
    TopologyEdge,
    TopologyNode,
    TopologyUnreachable,
)

__all__ = [
    "A2ARemoteClient",
    "MeldFabricRemoteClient",
    "RemoteAgentDescriptor",
    "RemoteAgentManager",
    "RemoteCallContext",
    "RemoteCancelRequest",
    "RemoteInvokeRequest",
    "RemoteResumeEntry",
    "RemoteResumeRequest",
    "RemoteRunResult",
    "RemoteTopology",
    "TopologyEdge",
    "TopologyNode",
    "TopologyUnreachable",
]

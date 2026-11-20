"""System metadata, secrets, Strands state, memory, and mapping persistence."""

from meldfabric.storage.database import AsyncDatabase, resolve_database_url
from meldfabric.storage.manager import StorageManager
from meldfabric.storage.memory import SQLMemoryStore, memory_namespace
from meldfabric.storage.persistence import PersistenceManager
from meldfabric.storage.playground import PlaygroundRepository
from meldfabric.storage.secrets import (
    GenerationSecretRepository,
    SecretReader,
    SecretRepository,
)
from meldfabric.storage.strands import SQLStorage
from meldfabric.storage.system import SystemRepository
from meldfabric.storage.types import (
    RuntimeLease,
    StoredA2ATask,
    StoredAgentRun,
    StoredRemoteInteraction,
    StoredToolExecution,
)

__all__ = [
    "AsyncDatabase",
    "GenerationSecretRepository",
    "PersistenceManager",
    "PlaygroundRepository",
    "RuntimeLease",
    "SQLMemoryStore",
    "SQLStorage",
    "SecretReader",
    "SecretRepository",
    "StorageManager",
    "StoredA2ATask",
    "StoredAgentRun",
    "StoredRemoteInteraction",
    "StoredToolExecution",
    "SystemRepository",
    "memory_namespace",
    "resolve_database_url",
]

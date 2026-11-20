"""Small concurrency primitives used by the Agent runtime."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Generic, TypeVar

KeyT = TypeVar("KeyT", bound=Hashable)


@dataclass(slots=True)
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


class KeyedAsyncLock(Generic[KeyT]):
    """Serialize work for one key while allowing unrelated keys to run concurrently.

    ``users`` counts both the current owner and queued waiters.  This lets the pool
    discard an entry as soon as the final user leaves without racing a waiter that has
    already obtained the entry but has not acquired its lock yet.
    """

    def __init__(self) -> None:
        self._entries: dict[KeyT, _LockEntry] = {}
        self._entries_lock = asyncio.Lock()

    @asynccontextmanager
    async def hold(self, key: KeyT) -> AsyncIterator[None]:
        async with self._entries_lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _LockEntry(lock=asyncio.Lock())
                self._entries[key] = entry
            entry.users += 1

        try:
            async with entry.lock:
                yield
        finally:
            async with self._entries_lock:
                entry.users -= 1
                if entry.users == 0 and self._entries.get(key) is entry:
                    self._entries.pop(key, None)


__all__ = ["KeyedAsyncLock"]

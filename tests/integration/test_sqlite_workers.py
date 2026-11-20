"""Exercise SQLite first-start initialization in independent worker processes."""

from __future__ import annotations

import asyncio
import multiprocessing
import tempfile
from pathlib import Path

from sqlalchemy import text

from meldfabric.config.models import DatabaseConnection
from meldfabric.storage.database import AsyncDatabase
from meldfabric.storage.system import SystemRepository


def worker(directory: str, barrier) -> None:
    async def run():
        for index in range(12):
            await asyncio.to_thread(barrier.wait, 30)
            databases = [
                AsyncDatabase(
                    DatabaseConnection(
                        id="stress",
                        name="stress",
                        engine="sqlite",
                        url=f"sqlite+aiosqlite:///concurrent-{index}.db",
                    ),
                    config_dir=Path(directory),
                )
                for _ in range(3)
            ]
            try:
                await asyncio.gather(*(SystemRepository(db).initialize() for db in databases))
                for database in databases:
                    async with database.engine.connect() as connection:
                        assert await connection.scalar(text("PRAGMA journal_mode")) == "wal"
            finally:
                await asyncio.gather(*(database.close() for database in databases))

    asyncio.run(run())


def main() -> int:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    with tempfile.TemporaryDirectory(prefix="meldfabric-sqlite-workers-") as directory:
        processes = [context.Process(target=worker, args=(directory, barrier)) for _ in range(4)]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=120)
            passed = all(process.exitcode == 0 for process in processes)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=10)
                    if process.is_alive():
                        process.kill()
                        process.join()
    print(f"{'PASS' if passed else 'FAIL'}: 4 workers, 144 connections, 12 fresh SQLite databases")
    return 0 if passed else 1


def test_sqlite_first_start_is_safe_across_workers() -> None:
    assert main() == 0

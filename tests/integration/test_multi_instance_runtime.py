"""Exercise runtime coordination in independent MeldFabric processes."""

from __future__ import annotations

import asyncio
import multiprocessing
import sqlite3
import tempfile
from pathlib import Path

from meldfabric import AgentKit
from meldfabric.errors import AgentKitError


def worker(
    state_dir: str,
    worker_id: int,
    start_barrier,
    lease_barrier,
    release_winner,
    results,
) -> None:
    kit = None
    try:
        start_barrier.wait(30)
        kit = AgentKit(state_dir=state_dir)

        async def run() -> None:
            await kit.storage.initialize(kit.config)
            await kit.agent_runtime.start()
            await asyncio.to_thread(lease_barrier.wait, 30)
            try:
                async with kit.agent_runtime._exclusive_thread(
                    kit.config.workspace.id,
                    "agent-shared",
                    "user-shared",
                    "thread-shared",
                    run_id=f"run-worker-{worker_id}",
                ) as lease:
                    results.put(
                        {
                            "worker": worker_id,
                            "outcome": "acquired",
                            "fencing_token": lease.fencing_token,
                        }
                    )
                    released = await asyncio.to_thread(release_winner.wait, 30)
                    if not released:
                        raise TimeoutError("winner was not released by the parent process")
            except AgentKitError as exc:
                results.put(
                    {
                        "worker": worker_id,
                        "outcome": "rejected",
                        "code": exc.code,
                    }
                )
            finally:
                await kit.agent_runtime.close()
                await kit.persistence.close()
                await kit.storage.close()

        asyncio.run(run())
    except BaseException as exc:
        results.put(
            {
                "worker": worker_id,
                "outcome": "error",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else None,
                "cause": str(exc.__cause__) if exc.__cause__ else None,
            }
        )


def generation_worker(
    state_dir: str,
    role: str,
    follower_ready,
    generation_published,
    results,
) -> None:
    kit = None
    try:
        kit = AgentKit(state_dir=state_dir)

        async def run() -> None:
            await kit.storage.initialize(kit.config)
            kit.agent_runtime.maintenance_interval_seconds = 0.05
            await kit.agent_runtime.start()
            try:
                if role == "follower":
                    initial = kit.runtime_config.config_generation
                    results.put({"role": role, "event": "ready", "generation": initial})
                    follower_ready.set()
                    if not await asyncio.to_thread(generation_published.wait, 30):
                        raise TimeoutError("publisher did not publish a generation")
                    deadline = asyncio.get_running_loop().time() + 15
                    while kit.runtime_config.config_generation == initial:
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError("follower did not adopt the published generation")
                        await asyncio.sleep(0.05)
                    results.put(
                        {
                            "role": role,
                            "event": "adopted",
                            "generation": kit.runtime_config.config_generation,
                        }
                    )
                else:
                    if not await asyncio.to_thread(follower_ready.wait, 30):
                        raise TimeoutError("follower did not become ready")
                    saved = await kit.apatch_config(
                        {"workspace": {"name": "Published by another process"}}
                    )
                    await kit.aapply_latest_config(
                        expected_generation=saved.config.config_generation
                    )
                    results.put(
                        {
                            "role": role,
                            "event": "published",
                            "generation": saved.config.config_generation,
                        }
                    )
                    generation_published.set()
            finally:
                await kit.agent_runtime.close()
                await kit.persistence.close()
                await kit.storage.close()

        asyncio.run(run())
    except BaseException as exc:
        results.put(
            {
                "role": role,
                "event": "error",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        )
        generation_published.set()


def verify_generation_adoption(context) -> tuple[bool, list[dict[str, object]]]:
    follower_ready = context.Event()
    generation_published = context.Event()
    results = context.Queue()
    with tempfile.TemporaryDirectory(prefix="meldfabric-generation-processes-") as directory:
        processes = [
            context.Process(
                target=generation_worker,
                args=(
                    directory,
                    role,
                    follower_ready,
                    generation_published,
                    results,
                ),
            )
            for role in ("follower", "publisher")
        ]
        observations: list[dict[str, object]] = []
        try:
            for process in processes:
                process.start()
            observations = [results.get(timeout=60) for _ in range(3)]
            for process in processes:
                process.join(timeout=60)
        finally:
            generation_published.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=10)
                    if process.is_alive():
                        process.kill()
                        process.join()

    initial = next(
        (item.get("generation") for item in observations if item.get("event") == "ready"),
        None,
    )
    published = next(
        (item.get("generation") for item in observations if item.get("event") == "published"),
        None,
    )
    adopted = next(
        (item.get("generation") for item in observations if item.get("event") == "adopted"),
        None,
    )
    passed = (
        all(process.exitcode == 0 for process in processes)
        and isinstance(initial, int)
        and isinstance(published, int)
        and published > initial
        and adopted == published
    )
    return passed, observations


def main() -> int:
    context = multiprocessing.get_context("spawn")
    start_barrier = context.Barrier(2)
    lease_barrier = context.Barrier(2)
    release_winner = context.Event()
    results = context.Queue()

    with tempfile.TemporaryDirectory(prefix="meldfabric-multi-instance-") as directory:
        processes = [
            context.Process(
                target=worker,
                args=(
                    directory,
                    worker_id,
                    start_barrier,
                    lease_barrier,
                    release_winner,
                    results,
                ),
            )
            for worker_id in range(2)
        ]
        observations: list[dict[str, object]] = []
        try:
            for process in processes:
                process.start()
            observations = [results.get(timeout=60) for _ in processes]
            release_winner.set()
            for process in processes:
                process.join(timeout=60)
        finally:
            release_winner.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=10)
                    if process.is_alive():
                        process.kill()
                        process.join()

        acquired = [item for item in observations if item.get("outcome") == "acquired"]
        rejected = [item for item in observations if item.get("outcome") == "rejected"]
        process_ok = all(process.exitcode == 0 for process in processes)

        database_path = Path(directory) / "control.db"
        with sqlite3.connect(database_path) as connection:
            workspace_count = connection.execute(
                "SELECT COUNT(*) FROM meldfabric_control_workspaces"
            ).fetchone()[0]
            admin_count = connection.execute(
                "SELECT COUNT(*) FROM meldfabric_control_admin_credentials"
            ).fetchone()[0]
            active_lease_count = connection.execute(
                "SELECT COUNT(*) FROM meldfabric_thread_leases"
            ).fetchone()[0]
            instance_count = connection.execute(
                "SELECT COUNT(*) FROM meldfabric_runtime_instances"
            ).fetchone()[0]

        lease_passed = (
            process_ok
            and len(acquired) == 1
            and len(rejected) == 1
            and rejected[0].get("code") == "THREAD_BUSY"
            and acquired[0].get("fencing_token") == 1
            and workspace_count == 1
            and admin_count == 1
            and active_lease_count == 0
            and instance_count == 2
        )

    generation_passed, generation_observations = verify_generation_adoption(context)
    passed = lease_passed and generation_passed

    print(
        f"{'PASS' if passed else 'FAIL'}: two independent processes, "
        "one lease winner, one THREAD_BUSY rejection, and maintenance configuration adoption; "
        f"lease_observations={observations}; "
        f"generation_observations={generation_observations}"
    )
    return 0 if passed else 1


def test_multi_instance_runtime_coordination() -> None:
    assert main() == 0

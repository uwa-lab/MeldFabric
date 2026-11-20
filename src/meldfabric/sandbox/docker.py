"""Docker sandbox provider with per-thread container reuse and hard host isolation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import posixpath
import tarfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from meldfabric.config.models import SandboxProfile
from meldfabric.errors import SandboxExecutionError, SandboxUnavailableError
from meldfabric.sandbox.base import SandboxContext, SandboxStatus, SandboxToolRequest

_BOOTSTRAP = r"""
import asyncio
import base64
import importlib
import inspect
import json
import os

request = json.loads(base64.b64decode(os.environ["MELDFABRIC_AGENT_TOOL_REQUEST"]).decode("utf-8"))
module_name, qualname = request["python_path"].split(":", 1)
target = importlib.import_module(module_name)
for part in qualname.split("."):
    if part == "<locals>":
        raise RuntimeError("locally defined tools cannot be imported inside a sandbox")
    target = getattr(target, part)
result = target(**request["arguments"])
if inspect.isawaitable(result):
    result = asyncio.run(result)
if hasattr(result, "model_dump"):
    result = result.model_dump(mode="json")
print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
""".strip()


@dataclass(slots=True)
class _ContainerLease:
    container: Any
    last_used: float
    profile_fingerprint: str
    ttl_seconds: int


class DockerSandboxProvider:
    name: ClassVar[str] = "docker"
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {"python_tool_execution", "thread_reuse", "network_isolation"}
    )
    config_schema: ClassVar[dict[str, Any]] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Docker 提供方配置",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "image": {
                "type": "string",
                "default": "python:3.11-slim",
                "title": "容器镜像",
                "description": "运行工具与命令的 Docker 镜像。",
            },
            "working_dir": {
                "type": "string",
                "default": "/workspace",
                "title": "工作目录",
            },
            "user": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
                "title": "容器用户",
            },
            "read_only_root": {
                "type": "boolean",
                "default": True,
                "title": "只读根文件系统",
            },
            "max_output_bytes": {
                "type": "integer",
                "minimum": 1024,
                "default": 1_000_000,
                "title": "最大输出字节数",
            },
            "disk_limit_mb": {
                "type": "integer",
                "minimum": 16,
                "default": 256,
                "title": "临时磁盘限额 MB",
            },
            "mounts": {
                "type": "array",
                "title": "目录挂载",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["source", "target"],
                    "properties": {
                        "source": {
                            "type": "string",
                            "title": "宿主机绝对路径",
                        },
                        "target": {
                            "type": "string",
                            "title": "容器绝对路径",
                        },
                        "read_only": {
                            "type": "boolean",
                            "default": True,
                            "title": "只读挂载",
                        },
                    },
                },
                "default": [],
            },
        },
    }

    def __init__(self, client: Any | None = None) -> None:
        self._client = client
        self._leases: dict[str, _ContainerLease] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def dependency_available() -> bool:
        try:
            import docker  # noqa: F401
        except ImportError:
            return False
        return True

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import docker
        except ImportError as exc:
            raise SandboxUnavailableError(
                "Docker sandbox support requires meldfabric[sandbox-docker]"
            ) from exc
        self._client = docker.from_env()
        return self._client

    async def test(self, profile: SandboxProfile) -> SandboxStatus:
        try:
            client = self._get_client()
            await asyncio.to_thread(client.ping)
            image = str(profile.config.get("image", "python:3.11-slim"))
            await asyncio.to_thread(client.images.get, image)
        except Exception as exc:
            return SandboxStatus(
                provider=self.name,
                available=False,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return SandboxStatus(provider=self.name, available=True)

    async def execute_tool(self, request: SandboxToolRequest) -> Any:
        key = self._lease_key(request.context, request.profile)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._collect_expired()
            lease = await self._lease(request.context, request.profile, key)
            payload = base64.b64encode(
                json.dumps(
                    {
                        "python_path": request.python_path,
                        "arguments": request.arguments,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            ).decode("ascii")
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        lease.container.exec_run,
                        ["python", "-c", _BOOTSTRAP],
                        environment={"MELDFABRIC_AGENT_TOOL_REQUEST": payload},
                        demux=True,
                    ),
                    timeout=request.profile.timeout_seconds,
                )
            except TimeoutError as exc:
                await self._discard(key)
                raise SandboxExecutionError(
                    "sandboxed tool execution timed out",
                    details={"timeout_seconds": request.profile.timeout_seconds},
                ) from exc
            lease.last_used = time.monotonic()
            exit_code = int(getattr(result, "exit_code", 1))
            stdout, stderr = getattr(result, "output", (b"", b""))
            stdout_text = (stdout or b"").decode("utf-8", errors="replace").strip()
            stderr_text = (stderr or b"").decode("utf-8", errors="replace").strip()
            if exit_code != 0:
                raise SandboxExecutionError(
                    "sandboxed tool process failed",
                    details={"exit_code": exit_code, "stderr": stderr_text[-4000:]},
                )
            try:
                response = json.loads(stdout_text.splitlines()[-1])
            except (IndexError, json.JSONDecodeError) as exc:
                raise SandboxExecutionError(
                    "sandboxed tool returned an invalid response",
                    details={"stdout": stdout_text[-4000:], "stderr": stderr_text[-4000:]},
                ) from exc
            return response["result"]

    async def create_sandbox(
        self,
        *,
        profile: SandboxProfile,
        context: SandboxContext,
    ) -> Any:
        """Return Strands' native Docker sandbox over the managed thread container."""

        from strands.sandbox.docker import DockerSandbox

        key = self._lease_key(context, profile)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._collect_expired()
            lease = await self._lease(context, profile, key)
            lease.last_used = time.monotonic()
            return DockerSandbox(
                lease.container.id,
                working_dir=str(profile.config.get("working_dir", "/workspace")),
                user=(
                    str(profile.config["user"]) if profile.config.get("user") is not None else None
                ),
            )

    async def execute_command(
        self,
        *,
        context: SandboxContext,
        profile: SandboxProfile,
        command: str,
        timeout: int | None = None,
    ) -> tuple[str, int | None, bool]:
        key = self._lease_key(context, profile)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._collect_expired()
            lease = await self._lease(context, profile, key)
            effective_timeout = profile.timeout_seconds
            if timeout is not None:
                effective_timeout = min(effective_timeout, max(1, timeout))
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        lease.container.exec_run,
                        ["/bin/sh", "-lc", command],
                        demux=True,
                    ),
                    timeout=effective_timeout,
                )
            except TimeoutError as exc:
                await self._discard(key)
                raise SandboxExecutionError(
                    "sandbox command timed out",
                    details={"timeout_seconds": effective_timeout},
                ) from exc
            lease.last_used = time.monotonic()
            exit_code = int(getattr(result, "exit_code", 1))
            stdout, stderr = getattr(result, "output", (b"", b""))
            combined = (stdout or b"") + (stderr or b"")
            limit = int(profile.config.get("max_output_bytes", 1_000_000))
            truncated = len(combined) > limit
            if truncated:
                combined = combined[:limit]
            return combined.decode("utf-8", errors="replace"), exit_code, truncated

    async def upload_files(
        self,
        *,
        context: SandboxContext,
        profile: SandboxProfile,
        files: list[tuple[str, bytes]],
    ) -> list[tuple[str, str | None]]:
        key = self._lease_key(context, profile)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._collect_expired()
            lease = await self._lease(context, profile, key)
            responses: list[tuple[str, str | None]] = []
            for path, content in files:
                try:
                    directory, name = self._split_container_path(path)
                    await asyncio.to_thread(
                        lease.container.exec_run,
                        ["mkdir", "-p", "--", directory],
                    )
                    archive = self._single_file_archive(name, content)
                    written = await asyncio.to_thread(
                        lease.container.put_archive,
                        directory,
                        archive,
                    )
                    if not written:
                        raise RuntimeError("Docker rejected the file archive")
                    responses.append((path, None))
                except Exception as exc:
                    responses.append((path, f"{type(exc).__name__}: {exc}"))
            lease.last_used = time.monotonic()
            return responses

    async def download_files(
        self,
        *,
        context: SandboxContext,
        profile: SandboxProfile,
        paths: list[str],
    ) -> list[tuple[str, bytes | None, str | None]]:
        key = self._lease_key(context, profile)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._collect_expired()
            lease = await self._lease(context, profile, key)
            responses: list[tuple[str, bytes | None, str | None]] = []
            for path in paths:
                try:
                    chunks, _metadata = await asyncio.to_thread(
                        lease.container.get_archive,
                        path,
                    )
                    payload = b"".join(chunks)
                    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
                        members = [item for item in archive.getmembers() if item.isfile()]
                        if len(members) != 1:
                            raise RuntimeError("sandbox path is not a regular file")
                        extracted = archive.extractfile(members[0])
                        if extracted is None:
                            raise RuntimeError("sandbox file could not be read")
                        responses.append((path, extracted.read(), None))
                except Exception as exc:
                    responses.append((path, None, f"{type(exc).__name__}: {exc}"))
            lease.last_used = time.monotonic()
            return responses

    async def _lease(
        self,
        context: SandboxContext,
        profile: SandboxProfile,
        key: str,
    ) -> _ContainerLease:
        fingerprint = self._profile_fingerprint(profile)
        existing = self._leases.get(key)
        if existing is not None and existing.profile_fingerprint == fingerprint:
            try:
                await asyncio.to_thread(existing.container.reload)
                if existing.container.status == "running":
                    return existing
            except Exception:
                pass
            await self._discard(key)
        elif existing is not None:
            await self._discard(key)

        client = self._get_client()
        image = str(profile.config.get("image", "python:3.11-slim"))
        volumes = self._volumes(profile)
        kwargs: dict[str, Any] = {
            "image": image,
            "command": ["python", "-c", "import time; time.sleep(10**9)"],
            "detach": True,
            "read_only": bool(profile.config.get("read_only_root", True)),
            "volumes": volumes,
            "labels": {
                "meldfabric.managed": "true",
                "meldfabric.profile": profile.id,
                "meldfabric.thread": context.thread_id,
            },
            "working_dir": str(profile.config.get("working_dir", "/workspace")),
            "tmpfs": self._tmpfs(profile, volumes),
        }
        if not profile.network_enabled:
            kwargs["network_mode"] = "none"
        if profile.cpu_limit is not None:
            kwargs["nano_cpus"] = int(profile.cpu_limit * 1_000_000_000)
        if profile.memory_limit_mb is not None:
            kwargs["mem_limit"] = f"{profile.memory_limit_mb}m"
        try:
            container = await asyncio.to_thread(client.containers.run, **kwargs)
        except Exception as exc:
            raise SandboxUnavailableError(
                f"could not start Docker sandbox: {type(exc).__name__}: {exc}",
                details={"sandbox_ref": profile.id, "image": image},
            ) from exc
        lease = _ContainerLease(
            container=container,
            last_used=time.monotonic(),
            profile_fingerprint=fingerprint,
            ttl_seconds=profile.ttl_seconds,
        )
        self._leases[key] = lease
        return lease

    @staticmethod
    def _volumes(profile: SandboxProfile) -> dict[str, dict[str, str]]:
        volumes: dict[str, dict[str, str]] = {}
        raw_mounts = profile.config.get("mounts", [])
        if not isinstance(raw_mounts, list):
            raise SandboxUnavailableError("sandbox config.mounts must be a list")
        for raw in raw_mounts:
            if not isinstance(raw, dict):
                raise SandboxUnavailableError("each sandbox mount must be an object")
            source = Path(str(raw.get("source", ""))).expanduser()
            target = str(raw.get("target", ""))
            if not source.is_absolute() or not target.startswith("/"):
                raise SandboxUnavailableError(
                    "sandbox mount source and target must be absolute paths"
                )
            if not source.exists():
                raise SandboxUnavailableError(f"sandbox mount source {source} does not exist")
            volumes[str(source.resolve())] = {
                "bind": target,
                "mode": "ro" if raw.get("read_only", True) else "rw",
            }
        return volumes

    @staticmethod
    def _tmpfs(
        profile: SandboxProfile,
        volumes: dict[str, dict[str, str]],
    ) -> dict[str, str]:
        targets = {item["bind"] for item in volumes.values()}
        result = {"/tmp": "rw,noexec,nosuid,size=64m"}
        working_dir = str(profile.config.get("working_dir", "/workspace"))
        if working_dir not in targets:
            disk_limit_mb = int(profile.config.get("disk_limit_mb", 256))
            result[working_dir] = f"rw,nosuid,size={disk_limit_mb}m"
        return result

    @staticmethod
    def _split_container_path(path: str) -> tuple[str, str]:
        if not path.startswith("/") or ".." in path.split("/"):
            raise ValueError("sandbox file paths must be absolute and cannot contain '..'")
        normalized = posixpath.normpath(path)
        directory, name = posixpath.split(normalized)
        if not name:
            raise ValueError("sandbox file path must identify a file")
        return directory or "/", name

    @staticmethod
    def _single_file_archive(name: str, content: bytes) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(content))
        return buffer.getvalue()

    @staticmethod
    def _profile_fingerprint(profile: SandboxProfile) -> str:
        payload = json.dumps(profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _lease_key(context: SandboxContext, profile: SandboxProfile) -> str:
        from meldfabric.domain.identity import identity_digest

        return identity_digest(
            context.workspace_id,
            context.agent_id,
            context.user_id,
            context.thread_id,
            profile.id,
        )

    async def _collect_expired(self) -> None:
        now = time.monotonic()
        expired = [
            key for key, lease in self._leases.items() if now - lease.last_used > lease.ttl_seconds
        ]
        for key in expired:
            await self._discard(key)

    async def _discard(self, key: str) -> None:
        lease = self._leases.pop(key, None)
        if lease is None:
            return
        with suppress(Exception):
            await asyncio.to_thread(lease.container.remove, force=True)

    async def close(self) -> None:
        for key in list(self._leases):
            await self._discard(key)
        client = self._client
        self._client = None
        if client is not None:
            close = getattr(client, "close", None)
            if close is not None:
                await asyncio.to_thread(close)

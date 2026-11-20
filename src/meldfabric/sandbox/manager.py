"""Sandbox provider registry and execution routing."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from meldfabric.config.models import SandboxProfile
from meldfabric.config.structured import GENERIC_JSON_OBJECT_SCHEMA, validate_json_schema
from meldfabric.errors import SandboxExecutionError, SandboxUnavailableError
from meldfabric.sandbox.base import (
    SandboxContext,
    SandboxProvider,
    SandboxStatus,
    SandboxToolRequest,
)


class SandboxManager:
    def __init__(self) -> None:
        self._providers: dict[str, SandboxProvider] = {}
        self._provider_schemas: dict[str, dict[str, Any]] = {}
        self._availability: dict[tuple[int, str], SandboxStatus] = {}
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._register_builtin_providers()

    def _register_builtin_providers(self) -> None:
        try:
            from meldfabric.sandbox.docker import DockerSandboxProvider
        except ImportError:
            return
        if DockerSandboxProvider.dependency_available():
            self.register(DockerSandboxProvider())

    def register(self, provider: SandboxProvider, *, replace: bool = False) -> None:
        if provider.name in self._providers and not replace:
            raise ValueError(f"sandbox provider {provider.name!r} is already registered")
        schema = getattr(provider, "config_schema", GENERIC_JSON_OBJECT_SCHEMA)
        validate_json_schema(
            schema,
            source=f"sandbox provider {provider.name!r} config",
        )
        self._providers[provider.name] = provider
        self._provider_schemas[provider.name] = dict(schema)

    def provider_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def provider_schemas(self) -> dict[str, dict[str, Any]]:
        return {name: dict(schema) for name, schema in sorted(self._provider_schemas.items())}

    async def ensure_available(
        self,
        profile: SandboxProfile,
        *,
        revision: int,
        refresh: bool = False,
    ) -> SandboxStatus:
        key = (revision, profile.id)
        if not refresh and key in self._availability:
            status = self._availability[key]
        else:
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                if not refresh and key in self._availability:
                    status = self._availability[key]
                else:
                    provider = self._providers.get(profile.provider)
                    if provider is None:
                        status = SandboxStatus(
                            provider=profile.provider,
                            available=False,
                            detail=(
                                f"provider {profile.provider!r} is not installed; "
                                "install the matching MeldFabric extra"
                            ),
                        )
                    else:
                        try:
                            status = await provider.test(profile)
                        except Exception as exc:
                            status = SandboxStatus(
                                provider=profile.provider,
                                available=False,
                                detail=f"{type(exc).__name__}: {exc}",
                            )
                    self._availability[key] = status
        if not status.available:
            raise SandboxUnavailableError(
                f"sandbox profile {profile.id!r} is unavailable",
                details={
                    "sandbox_ref": profile.id,
                    "provider": profile.provider,
                    "reason": status.detail,
                },
            )
        return status

    async def test_profile(
        self,
        profile: SandboxProfile,
        *,
        revision: int,
    ) -> SandboxStatus:
        try:
            return await self.ensure_available(profile, revision=revision, refresh=True)
        except SandboxUnavailableError as exc:
            return SandboxStatus(
                provider=profile.provider,
                available=False,
                detail=str(exc.details.get("reason") or exc.message),
            )

    async def create_strands_sandbox(
        self,
        profile: SandboxProfile,
        *,
        context: SandboxContext,
    ) -> Any:
        """Create the native Strands ``Sandbox`` selected by a profile.

        Providers that only implement the legacy per-tool execution contract are not
        silently adapted: a Strands Agent sandbox owns command, code, and filesystem
        semantics as one shared execution environment.  A provider must therefore
        expose ``create_sandbox(profile=..., context=...)`` explicitly.
        """

        await self.ensure_available(profile, revision=context.config_generation)
        provider = self._providers.get(profile.provider)
        factory = getattr(provider, "create_sandbox", None)
        if not callable(factory):
            raise SandboxUnavailableError(
                f"sandbox provider {profile.provider!r} has no native Strands adapter",
                details={"provider": profile.provider, "sandbox_ref": profile.id},
            )
        sandbox = factory(profile=profile, context=context)
        if inspect.isawaitable(sandbox):
            sandbox = await sandbox
        from strands.sandbox import Sandbox

        if not isinstance(sandbox, Sandbox):
            raise SandboxUnavailableError(
                f"sandbox provider {profile.provider!r} returned an invalid Strands sandbox",
                details={"provider": profile.provider, "sandbox_ref": profile.id},
            )
        return sandbox

    async def execute_tool(
        self,
        *,
        context: SandboxContext,
        profile: SandboxProfile,
        python_path: str,
        arguments: dict[str, Any],
    ) -> Any:
        await self.ensure_available(profile, revision=context.config_generation)
        provider = self._providers.get(profile.provider)
        if provider is None:  # Defensive: ensure_available normally catches this.
            raise SandboxUnavailableError(f"sandbox provider {profile.provider!r} is unavailable")
        try:
            return await provider.execute_tool(
                SandboxToolRequest(
                    context=context,
                    profile=profile,
                    python_path=python_path,
                    arguments=arguments,
                )
            )
        except (SandboxExecutionError, SandboxUnavailableError):
            raise
        except Exception as exc:
            raise SandboxExecutionError(
                f"sandboxed tool execution failed: {type(exc).__name__}: {exc}",
                details={"provider": profile.provider, "sandbox_ref": profile.id},
            ) from exc

    def _provider_with(self, profile: SandboxProfile, method: str) -> Any:
        provider = self._providers.get(profile.provider)
        if provider is None or not callable(getattr(provider, method, None)):
            raise SandboxUnavailableError(
                f"sandbox provider {profile.provider!r} does not support {method}",
                details={"provider": profile.provider, "sandbox_ref": profile.id},
            )
        return provider

    async def execute_command(
        self,
        *,
        context: SandboxContext,
        profile: SandboxProfile,
        command: str,
        timeout: int | None = None,
    ) -> tuple[str, int | None, bool]:
        await self.ensure_available(profile, revision=context.config_generation)
        provider = self._provider_with(profile, "execute_command")
        return await provider.execute_command(
            context=context,
            profile=profile,
            command=command,
            timeout=timeout,
        )

    async def upload_files(
        self,
        *,
        context: SandboxContext,
        profile: SandboxProfile,
        files: list[tuple[str, bytes]],
    ) -> list[tuple[str, str | None]]:
        await self.ensure_available(profile, revision=context.config_generation)
        provider = self._provider_with(profile, "upload_files")
        return await provider.upload_files(context=context, profile=profile, files=files)

    async def download_files(
        self,
        *,
        context: SandboxContext,
        profile: SandboxProfile,
        paths: list[str],
    ) -> list[tuple[str, bytes | None, str | None]]:
        await self.ensure_available(profile, revision=context.config_generation)
        provider = self._provider_with(profile, "download_files")
        return await provider.download_files(context=context, profile=profile, paths=paths)

    def prune(self, active_revisions: set[int]) -> None:
        for key in list(self._availability):
            if key[0] not in active_revisions:
                self._availability.pop(key, None)
                self._locks.pop(key, None)

    async def close(self) -> None:
        providers = list(self._providers.values())
        self._availability.clear()
        self._locks.clear()
        for provider in providers:
            await provider.close()

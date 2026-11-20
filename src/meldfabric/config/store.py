"""Generation-aware configuration service used by the app and management API."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from meldfabric.config.models import WorkspaceConfig
from meldfabric.config.repository import ConfigRepository, LoadedWorkspace
from meldfabric.config.types import EncryptedSecretWrite
from meldfabric.errors import ConfigurationConflictError, ConfigurationError
from meldfabric.resources.capabilities import CapabilityRegistry


def merge_patch(target: Any, patch: Any) -> Any:
    """Apply RFC 7396-style merge patch semantics."""

    if not isinstance(patch, dict):
        return deepcopy(patch)
    if not isinstance(target, dict):
        target = {}
    result = deepcopy(target)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


class ConfigStore:
    def __init__(
        self,
        repository: ConfigRepository,
        *,
        capabilities: CapabilityRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.capabilities = capabilities or CapabilityRegistry()
        self.feature_validator: Callable[[WorkspaceConfig, WorkspaceConfig], None] | None = None
        self.structured_validator: Callable[[WorkspaceConfig], None] | None = None
        self.candidate_processor: Callable[[WorkspaceConfig], WorkspaceConfig] | None = None

    def initialize(self) -> LoadedWorkspace:
        loaded = self.repository.initialize()
        self.capabilities.validate_workspace(loaded.config, allow_unavailable=True)
        return loaded

    def load(self) -> LoadedWorkspace:
        loaded = self.repository.load()
        self.capabilities.validate_workspace(loaded.config, allow_unavailable=True)
        return loaded

    def patch(
        self,
        patch: dict[str, Any],
        *,
        expected_generation: int | None = None,
        base_config: WorkspaceConfig | None = None,
        secret_writes: Sequence[EncryptedSecretWrite] = (),
    ) -> LoadedWorkspace:
        current = self.repository.load_config()
        expected = current.config_generation if expected_generation is None else expected_generation
        if current.config_generation != expected:
            raise ConfigurationConflictError(
                "configuration changed before this update was saved",
                details={
                    "expected_generation": expected,
                    "current_generation": current.config_generation,
                },
            )
        base = base_config or current
        if base.config_generation != current.config_generation:
            raise ConfigurationConflictError(
                "effective configuration does not match the latest stored generation",
                details={
                    "effective_generation": base.config_generation,
                    "current_generation": current.config_generation,
                },
            )
        candidate = self._validate_candidate(base, patch)
        return self.repository.save(
            candidate,
            expected_generation=expected,
            reason="patch",
            secret_writes=secret_writes,
        )

    def preview_patch(
        self,
        patch: dict[str, Any],
        *,
        expected_generation: int | None = None,
        base_config: WorkspaceConfig | None = None,
    ) -> WorkspaceConfig:
        current = self.repository.load_config()
        if expected_generation is not None and current.config_generation != expected_generation:
            raise ConfigurationConflictError(
                "configuration changed before this update was validated",
                details={
                    "expected_generation": expected_generation,
                    "current_generation": current.config_generation,
                },
            )
        base = base_config or current
        if base.config_generation != current.config_generation:
            raise ConfigurationConflictError(
                "effective configuration does not match the latest stored generation"
            )
        return self._validate_candidate(base, patch)

    def _validate_candidate(
        self, current: WorkspaceConfig, patch: dict[str, Any]
    ) -> WorkspaceConfig:
        candidate_payload = merge_patch(current.model_dump(mode="json"), patch)
        candidate_payload["config_generation"] = current.config_generation + 1
        try:
            candidate = WorkspaceConfig.model_validate(candidate_payload)
        except ValidationError as exc:
            raise ConfigurationError(
                "configuration patch is invalid",
                details={"errors": exc.errors(include_url=False)},
            ) from exc
        if candidate.workspace.id != current.workspace.id:
            raise ConfigurationError(
                "workspace identity is immutable",
                details={"field": "workspace.id"},
            )
        if self.candidate_processor is not None:
            candidate = self.candidate_processor(candidate)
        self.capabilities.validate_workspace(candidate, previous=current)
        if self.feature_validator is not None:
            self.feature_validator(current, candidate)
        if self.structured_validator is not None:
            self.structured_validator(candidate)
        return candidate

"""HTTP request models for the management and publication API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, SecretStr, StringConstraints, model_validator

from meldfabric.config.models import ModelProvider, ModelResource
from meldfabric.config.structured import validate_database_options
from meldfabric.errors import ConfigurationError

CatalogDigest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ConfigPatchRequest(BaseModel):
    patch: dict[str, Any] = Field(default_factory=dict)
    expected_generation: int | None = None
    expected_catalog_digest: CatalogDigest


class ConfigApplyRequest(BaseModel):
    expected_generation: int
    expected_catalog_digest: CatalogDigest


class RuntimeSuspendRequest(BaseModel):
    confirm: bool = False


class EntityMutationRequest(BaseModel):
    resource: dict[str, Any]
    expected_generation: int | None = None
    expected_catalog_digest: CatalogDigest


class ToolOverrideRequest(BaseModel):
    enabled: bool | None = None
    display_name: str | None = None
    description: str | None = None
    requires_sandbox: bool | None = None
    parameter_descriptions: dict[str, str] | None = None
    expected_generation: int | None = None
    expected_catalog_digest: CatalogDigest


class CreateEndpointKeyRequest(BaseModel):
    kind: Literal["agent_api", "remote_access"] = "agent_api"
    scopes: list[str] | None = None
    expires_at: datetime | None = None


class KeyStatusRequest(BaseModel):
    status: Literal["active", "disabled", "revoked"]


class SecretRequest(BaseModel):
    value: SecretStr
    expected_generation: int | None = None
    expected_catalog_digest: CatalogDigest


class ProviderPreviewRequest(BaseModel):
    resource: ModelProvider
    api_key: SecretStr | None = None


class ModelPreviewRequest(ProviderPreviewRequest):
    model: ModelResource


class RemoteDiscoveryRequest(BaseModel):
    base_url: str = Field(min_length=1)
    access_key: SecretStr
    timeout_seconds: float = Field(default=60, gt=0)
    protocol: Literal["auto", "meldfabric_remote", "a2a"] = "auto"


class InvokeRequest(BaseModel):
    messages: list[dict[str, Any]] = Field(min_length=1)
    user_id: str | None = None
    thread_id: str = Field(min_length=1)


class PlaygroundConversationCreateRequest(BaseModel):
    endpoint_id: str = Field(min_length=1, max_length=128)


class PlaygroundRunRequest(BaseModel):
    content: str = Field(min_length=1, max_length=200_000)
    message_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )


class PlaygroundResumeRequest(BaseModel):
    interrupt_id: str = Field(min_length=1, max_length=256)
    status: Literal["resolved", "cancelled"] = "resolved"
    payload: Any = None

    @model_validator(mode="after")
    def resolved_payload_is_present(self) -> PlaygroundResumeRequest:
        if self.status == "resolved" and self.payload is None:
            raise ValueError("a resolved interrupt requires a response payload")
        return self


class SnapshotCreateRequest(BaseModel):
    label: str | None = Field(default=None, max_length=256)
    note: str | None = Field(default=None, max_length=4000)
    expected_generation: int | None = None


class SnapshotRestoreRequest(BaseModel):
    confirm: bool = False
    expected_generation: int = Field(ge=1)
    expected_catalog_digest: CatalogDigest


class AdminKeyUpdateRequest(BaseModel):
    confirm: bool = False
    new_key: SecretStr = Field(min_length=16, max_length=1024)


class BackupRequest(BaseModel):
    engine: Literal["sqlite", "postgresql", "mysql"]
    url: SecretStr
    options: dict[str, Any] = Field(default_factory=dict)
    admin_key: SecretStr
    overwrite: bool = False

    @model_validator(mode="after")
    def valid_engine_options(self) -> BackupRequest:
        try:
            options = validate_database_options(self.options, engine=self.engine)
        except ConfigurationError as exc:
            raise ValueError(exc.message) from exc
        object.__setattr__(self, "options", options)
        return self


__all__ = [
    "AdminKeyUpdateRequest",
    "BackupRequest",
    "CatalogDigest",
    "ConfigApplyRequest",
    "ConfigPatchRequest",
    "CreateEndpointKeyRequest",
    "EntityMutationRequest",
    "InvokeRequest",
    "KeyStatusRequest",
    "ModelPreviewRequest",
    "PlaygroundConversationCreateRequest",
    "PlaygroundResumeRequest",
    "PlaygroundRunRequest",
    "ProviderPreviewRequest",
    "RemoteDiscoveryRequest",
    "RuntimeSuspendRequest",
    "SecretRequest",
    "SnapshotCreateRequest",
    "SnapshotRestoreRequest",
    "ToolOverrideRequest",
]

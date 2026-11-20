"""Stable error types used across MeldFabric."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class AgentKitError(Exception):
    """Base class for errors that are safe to map to a stable API error code."""

    code = "AGENT_KIT_ERROR"

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = dict(details or {})


class ConfigurationError(AgentKitError):
    code = "CONFIG_VALIDATION_FAILED"


class ConfigurationConflictError(ConfigurationError):
    code = "CONFIG_GENERATION_CONFLICT"


class CodeCatalogConflictError(ConfigurationConflictError):
    code = "CODE_CATALOG_CONFLICT"


class ControlDatabaseError(AgentKitError):
    code = "CONTROL_DATABASE_ERROR"


class ControlDatabaseUnavailableError(ControlDatabaseError):
    code = "CONTROL_DATABASE_UNAVAILABLE"


class ControlDatabaseCorruptError(ControlDatabaseError):
    code = "CONTROL_DATABASE_CORRUPT"


class ResourceNotFoundError(ConfigurationError):
    code = "RESOURCE_NOT_FOUND"


class ResourceCapabilityError(ConfigurationError):
    code = "RESOURCE_CAPABILITY_MISMATCH"


class ToolRegistrationError(AgentKitError):
    code = "TOOL_REGISTRATION_FAILED"


class CodeRegistrationError(AgentKitError):
    """A trusted Python capability has an invalid or conflicting declaration."""

    code = "CODE_REGISTRATION_FAILED"


class ToolResultError(AgentKitError):
    code = "TOOL_RESULT_INVALID"


class OptionalDependencyError(AgentKitError):
    code = "OPTIONAL_DEPENDENCY_MISSING"


class AgentNotFoundError(AgentKitError):
    code = "AGENT_NOT_FOUND"


class AgentDisabledError(AgentKitError):
    code = "AGENT_DISABLED"


class AgentNotReadyError(AgentKitError):
    code = "AGENT_NOT_READY"


class ServiceSuspendedError(AgentNotReadyError):
    code = "SERVICE_SUSPENDED"


class ConfigurationRevisionUnavailableError(AgentNotReadyError):
    code = "CONFIG_REVISION_UNAVAILABLE"


class ModelNotConfiguredError(AgentNotReadyError):
    code = "MODEL_NOT_CONFIGURED"


class ModelCredentialMissingError(AgentNotReadyError):
    code = "MODEL_CREDENTIAL_MISSING"


class ModelServiceError(AgentKitError):
    code = "MODEL_SERVICE_ERROR"


class ModelInputUnsupportedError(AgentKitError):
    code = "MODEL_INPUT_UNSUPPORTED"


class ModelTimeoutError(ModelServiceError):
    code = "MODEL_TIMEOUT"


class AgentRunError(AgentKitError):
    code = "AGENT_RUN_FAILED"


def public_run_error(exc: Exception) -> AgentKitError:
    """Never expose provider response bodies, credentials or tracebacks to callers."""
    if isinstance(exc, AgentKitError):
        return exc
    from openai import APIConnectionError, APIError, APITimeoutError

    details = {"exception_type": type(exc).__name__}
    if isinstance(exc, APITimeoutError):
        return ModelTimeoutError(
            "The model service timed out; check model timeout settings", details=details
        )
    if isinstance(exc, APIConnectionError):
        return ModelServiceError("Cannot connect to the model service", details=details)
    if isinstance(exc, APIError):
        status = getattr(exc, "status_code", None)
        if status is not None:
            details["upstream_status"] = status
        return ModelServiceError(
            "The model service rejected or failed the request; check model protocol, "
            "parameters and credentials",
            details=details,
        )
    return AgentRunError(
        "Agent execution failed; inspect the service log for details", details=details
    )


class PromptRenderError(AgentKitError):
    code = "PROMPT_RENDER_FAILED"


class IdentityResolutionError(AgentKitError):
    code = "IDENTITY_RESOLUTION_FAILED"


class RemoteAgentError(AgentKitError):
    code = "REMOTE_AGENT_ERROR"


class RemoteAgentUnreachableError(RemoteAgentError):
    code = "REMOTE_AGENT_UNREACHABLE"


class RemoteAgentCycleError(RemoteAgentError):
    code = "REMOTE_AGENT_CYCLE_DETECTED"


class RemoteDelegationDepthError(RemoteAgentError):
    code = "REMOTE_DELEGATION_DEPTH_EXCEEDED"


class RemoteDelegationLimitError(RemoteAgentError):
    code = "REMOTE_DELEGATION_LIMIT_EXCEEDED"


class RemoteInteractionNotSupportedError(RemoteAgentError):
    code = "REMOTE_INTERACTION_NOT_SUPPORTED"


class InterruptPendingError(RemoteAgentError):
    code = "INTERRUPT_PENDING"


class SandboxUnavailableError(AgentNotReadyError):
    code = "SANDBOX_UNAVAILABLE"


class SandboxExecutionError(AgentKitError):
    code = "SANDBOX_EXECUTION_FAILED"


class RunConflictError(AgentKitError):
    code = "RUN_CONFLICT"


class ThreadBusyError(RunConflictError):
    code = "THREAD_BUSY"


class RunAlreadyActiveError(RunConflictError):
    code = "RUN_ALREADY_ACTIVE"


class RunIdAlreadyUsedError(RunConflictError):
    code = "RUN_ID_ALREADY_USED"


class ThreadAwaitingResumeError(RunConflictError):
    code = "THREAD_AWAITING_RESUME"


class RunLeaseLostError(RunConflictError):
    code = "RUN_LEASE_LOST"


class RunNotFoundError(AgentKitError):
    code = "RUN_NOT_FOUND"


class InterruptResumeError(AgentKitError):
    code = "INTERRUPT_RESUME_INVALID"


class RunCancelledError(AgentKitError):
    code = "RUN_CANCELLED"

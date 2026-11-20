"""Pluggable caller identity resolution without imposing a user-management system."""

from __future__ import annotations

import importlib
import inspect
import os
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from meldfabric.errors import ConfigurationError, IdentityResolutionError


@dataclass(frozen=True, slots=True)
class UserIdentity:
    user_id: str
    attributes: dict[str, Any] | None = None


@runtime_checkable
class IdentityResolver(Protocol):
    async def resolve(
        self,
        request: Any,
        *,
        claimed_user_id: str | None,
    ) -> UserIdentity: ...


class ClaimedIdentityResolver:
    """Trust the caller-supplied identity after an upstream service authenticates it."""

    async def resolve(
        self,
        request: Any,
        *,
        claimed_user_id: str | None,
    ) -> UserIdentity:
        del request
        if not claimed_user_id or not claimed_user_id.strip():
            raise IdentityResolutionError(
                "A user identity is required",
                details={
                    "accepted_sources": [
                        "user_id",
                        "X-User-ID",
                        "forwardedProps.userId",
                    ]
                },
            )
        return UserIdentity(user_id=claimed_user_id.strip())


@dataclass(frozen=True, slots=True)
class JWTIdentityResolver:
    """Verify a JWT from a dedicated identity header and use a stable claim as user ID.

    The default header is intentionally separate from ``Authorization``, which MeldFabric Agent
    Kit reserves for the Agent API Key. For asymmetric JWTs, ``key`` is typically a public
    key; symmetric secrets should be supplied by host code from an environment variable.
    """

    key: str
    algorithms: tuple[str, ...] = ("RS256",)
    subject_claim: str = "sub"
    audience: str | tuple[str, ...] | None = None
    issuer: str | None = None
    token_header: str = "X-Identity-Token"
    token_scheme: str = "Bearer"

    @classmethod
    def from_environment(
        cls,
        key_env: str,
        **kwargs: Any,
    ) -> JWTIdentityResolver:
        key = os.getenv(key_env)
        if not key:
            raise ConfigurationError(
                f"JWT verification key environment variable {key_env!r} is not set",
                details={"key_env": key_env},
            )
        return cls(key=key, **kwargs)

    async def resolve(
        self,
        request: Any,
        *,
        claimed_user_id: str | None,
    ) -> UserIdentity:
        try:
            import jwt
        except ImportError as exc:
            raise ConfigurationError(
                "JWT identity resolution is unavailable; reinstall meldfabric with dependencies"
            ) from exc

        raw = request.headers.get(self.token_header)
        if not raw or not raw.strip():
            raise IdentityResolutionError(
                "A JWT identity token is required",
                details={"header": self.token_header},
            )
        token = raw.strip()
        prefix = f"{self.token_scheme} " if self.token_scheme else ""
        if prefix:
            if not token.lower().startswith(prefix.lower()):
                raise IdentityResolutionError(
                    "JWT identity token uses an invalid authentication scheme",
                    details={"header": self.token_header, "expected_scheme": self.token_scheme},
                )
            token = token[len(prefix) :].strip()
        try:
            payload = jwt.decode(
                token,
                self.key,
                algorithms=list(self.algorithms),
                audience=self.audience,
                issuer=self.issuer,
                options={"require": [self.subject_claim]},
            )
        except Exception as exc:
            raise IdentityResolutionError(
                "JWT identity token is invalid",
                details={"reason": type(exc).__name__},
            ) from exc
        subject = payload.get(self.subject_claim)
        if not isinstance(subject, (str, int)) or not str(subject).strip():
            raise IdentityResolutionError(
                f"JWT claim {self.subject_claim!r} must contain a stable user identifier"
            )
        user_id = str(subject).strip()
        if claimed_user_id is not None and claimed_user_id.strip() != user_id:
            raise IdentityResolutionError(
                "The caller-supplied user ID does not match the verified JWT identity"
            )
        return UserIdentity(user_id=user_id, attributes=dict(payload))


def load_identity_resolver(path: str) -> IdentityResolver:
    """Load a trusted resolver instance or zero-argument factory by ``module:attribute``."""

    module_name, separator, attribute_name = path.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ConfigurationError(
            "identity_resolver_path must use the form 'module:attribute'",
            details={"identity_resolver_path": path},
        )
    try:
        candidate = getattr(importlib.import_module(module_name), attribute_name)
        if inspect.isclass(candidate) or not hasattr(candidate, "resolve"):
            candidate = candidate()
    except Exception as exc:
        raise ConfigurationError(
            f"failed to load identity resolver {path!r}: {exc}",
            details={"identity_resolver_path": path},
        ) from exc
    if not isinstance(candidate, IdentityResolver):
        raise ConfigurationError(
            f"identity resolver {path!r} does not implement async resolve()",
            details={"identity_resolver_path": path},
        )
    return candidate

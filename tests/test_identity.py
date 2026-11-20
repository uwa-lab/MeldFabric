from __future__ import annotations

from types import SimpleNamespace

import jwt
import pytest

from meldfabric.errors import IdentityResolutionError
from meldfabric.identity import JWTIdentityResolver

JWT_SECRET = "test-jwt-secret-with-at-least-32-bytes"


@pytest.mark.asyncio
async def test_jwt_identity_resolver_verifies_subject_and_claims() -> None:
    token = jwt.encode(
        {"sub": "user-jwt", "tenant": "tenant-a", "iss": "issuer-a", "aud": "agent-api"},
        JWT_SECRET,
        algorithm="HS256",
    )
    resolver = JWTIdentityResolver(
        key=JWT_SECRET,
        algorithms=("HS256",),
        issuer="issuer-a",
        audience="agent-api",
    )
    request = SimpleNamespace(headers={"X-Identity-Token": f"Bearer {token}"})

    identity = await resolver.resolve(request, claimed_user_id="user-jwt")

    assert identity.user_id == "user-jwt"
    assert identity.attributes["tenant"] == "tenant-a"


@pytest.mark.asyncio
async def test_jwt_identity_resolver_rejects_claimed_identity_mismatch() -> None:
    token = jwt.encode({"sub": "verified-user"}, JWT_SECRET, algorithm="HS256")
    resolver = JWTIdentityResolver(
        key=JWT_SECRET,
        algorithms=("HS256",),
    )
    request = SimpleNamespace(headers={"X-Identity-Token": f"Bearer {token}"})

    with pytest.raises(IdentityResolutionError, match="does not match"):
        await resolver.resolve(request, claimed_user_id="forged-user")

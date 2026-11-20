"""User identity resolver extension point."""

from meldfabric.identity.resolver import (
    ClaimedIdentityResolver,
    IdentityResolver,
    JWTIdentityResolver,
    UserIdentity,
    load_identity_resolver,
)

__all__ = [
    "ClaimedIdentityResolver",
    "IdentityResolver",
    "JWTIdentityResolver",
    "UserIdentity",
    "load_identity_resolver",
]

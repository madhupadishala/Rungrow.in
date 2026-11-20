from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash

from .config import settings
from .policy import Principal

bearer = HTTPBearer(auto_error=False)
password_hash = PasswordHash.recommended()
PRODUCTION_ALGORITHMS = ["RS256", "ES256"]


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)


def issue_access_token(
    user_id: str, tenant_id: str, role: str, permissions: list[str],
    product_ids: list[str] | None = None,
) -> str:
    if settings.app_env == "production":
        raise RuntimeError("Local HS256 token issuance is disabled in production; use the configured OIDC issuer")
    now = datetime.now(timezone.utc)
    claims = {
        "sub": user_id, "tid": tenant_id, "role": role, "perms": permissions,
        "products": product_ids or [], "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now, "exp": now + timedelta(minutes=settings.access_token_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


def _decode_production_token(token: str) -> dict:
    header = jwt.get_unverified_header(token)
    algorithm = header.get("alg")
    if algorithm not in PRODUCTION_ALGORITHMS:
        raise HTTPException(401, "Production access tokens must use RS256 or ES256")
    if settings.jwt_jwks_url:
        client = jwt.PyJWKClient(settings.jwt_jwks_url, cache_keys=True)
        signing_key = client.get_signing_key_from_jwt(token).key
        key = signing_key
    elif settings.jwt_public_key:
        key = settings.jwt_public_key
    else:
        raise HTTPException(503, "OIDC JWKS URL or asymmetric public key is not configured")
    return jwt.decode(
        token, key, algorithms=PRODUCTION_ALGORITHMS,
        issuer=settings.jwt_issuer, audience=settings.jwt_audience,
        options={"require": ["sub", "tid", "role", "exp", "iss", "aud"]},
    )


async def current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    if credentials is None:
        raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    try:
        if settings.app_env == "production":
            data = _decode_production_token(credentials.credentials)
        else:
            data = jwt.decode(
                credentials.credentials, settings.jwt_secret, algorithms=["HS256"],
                issuer=settings.jwt_issuer, audience=settings.jwt_audience,
                options={"require": ["sub", "tid", "role", "exp", "iss"]},
            )
        permissions = data.get("perms", [])
        product_ids = data.get("products", [])
        if not isinstance(permissions, list) or not all(isinstance(x, str) for x in permissions):
            raise HTTPException(401, "Invalid permission claims")
        if not isinstance(product_ids, list) or not all(isinstance(x, str) for x in product_ids):
            raise HTTPException(401, "Invalid product scope claims")
        return Principal(
            data["sub"], data["tid"], data["role"], frozenset(permissions),
            product_ids=frozenset(product_ids),
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(401, "Invalid or expired access token") from exc


def require_permission(permission: str):
    async def guard(principal: Principal = Depends(current_principal)):
        if permission not in principal.permissions and "*" not in principal.permissions:
            raise HTTPException(403, "Permission denied")
        return principal
    return guard

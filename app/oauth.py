from __future__ import annotations

import asyncio
import time
from typing import Any

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError

from mcp.server.auth.provider import AccessToken, TokenVerifier


class JWKSJWTTokenVerifier(TokenVerifier):
    """Validate JWT OAuth access tokens from an external authorization server."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        required_scopes: list[str],
        algorithms: list[str],
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.required_scopes = set(required_scopes)
        self.algorithms = algorithms
        self.jwks = PyJWKClient(jwks_url, cache_keys=True)

    @staticmethod
    def _scopes(claims: dict[str, Any]) -> list[str]:
        raw = claims.get("scope", claims.get("scp", []))
        if isinstance(raw, str):
            return [item for item in raw.split() if item]
        if isinstance(raw, list):
            return [str(item) for item in raw]
        return []

    def _verify_sync(self, token: str) -> AccessToken | None:
        try:
            signing_key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=self.algorithms,
                issuer=self.issuer,
                audience=self.audience,
                options={
                    "require": ["exp", "iss"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
        except (PyJWTError, Exception):
            return None

        scopes = self._scopes(claims)
        if self.required_scopes and not self.required_scopes.issubset(scopes):
            return None

        exp = claims.get("exp")
        if exp is not None and int(exp) <= int(time.time()):
            return None

        client_id = (
            claims.get("client_id")
            or claims.get("azp")
            or claims.get("appid")
            or claims.get("sub")
        )
        if not client_id:
            return None

        subject = claims.get("sub")
        return AccessToken(
            token=token,
            client_id=str(client_id),
            scopes=scopes,
            expires_at=int(exp) if exp is not None else None,
            resource=self.audience,
            subject=str(subject) if subject is not None else None,
            claims={
                "iss": claims.get("iss"),
                "sub": claims.get("sub"),
                "aud": claims.get("aud"),
            },
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        return await asyncio.to_thread(self._verify_sync, token)

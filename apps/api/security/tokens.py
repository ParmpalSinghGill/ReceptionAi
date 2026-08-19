"""Access and refresh tokens — 05 §A1.1, §A1.2, §A1.3.

Two token types with deliberately different designs:

* **Access token** — a JWT, short TTL, unrevocable by design. Checking a
  deny-list on every request reintroduces the database round-trip a JWT exists
  to avoid; a short TTL bounds the blast radius instead.
* **Refresh token** — opaque 256-bit random, stored hashed. It is long-lived,
  so it must be revocable, and revocability requires a lookup anyway. Given we
  are paying for the lookup, a JWT buys nothing and costs the risk of a
  long-lived self-contained credential. Hashed at rest means a database dump
  yields no usable refresh tokens.

**The ``tid`` singleton rule (05 §A1.2).** A user belonging to three tenants
holds one access token at a time, carrying one ``tid``. The alternative — one
token listing all tenants plus an ``X-Tenant-Id`` header — makes the tenant a
*request header*, which contract §10 forbids: any handler that forgets to
validate the header against the token's list is a cross-tenant breach. A
signed, singular ``tid`` removes the choice from the request entirely.

**Algorithm.** HS256 locally, EdDSA in production; the settings model refuses
to start a deployed pod on a symmetric key (05 §A1.1: every service that can
verify would also be able to mint, and our Temporal workers verify).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from packages.core.errors import AuthenticationError
from packages.core.ids import uuid7

__all__ = [
    "AccessClaims",
    "RefreshToken",
    "TokenService",
    "hash_refresh_token",
]


@dataclass(frozen=True, slots=True)
class AccessClaims:
    """The verified contents of an access token — 05 §A1.2."""

    sub: UUID  # user id
    tid: UUID  # ACTIVE tenant id — the value that becomes app.tenant_id
    tsl: str  # tenant plan slug
    jti: UUID
    sid: UUID | None  # session id, ties the access token to a refresh family
    rol: tuple[str, ...]
    prm: frozenset[str]
    aal: int
    iat: datetime
    exp: datetime
    act: UUID | None = None  # RFC 8693 actor claim, for impersonation

    @property
    def is_impersonated(self) -> bool:
        return self.act is not None


@dataclass(frozen=True, slots=True)
class RefreshToken:
    """A freshly minted refresh token: the secret, and what to store."""

    #: Given to the client, once. Never stored.
    secret: str
    #: What goes in ``sessions.refresh_token_hash``. Exactly 32 bytes, which
    #: the ``ck_sessions__hash_len`` CHECK enforces.
    digest: bytes
    family_id: UUID
    expires_at: datetime


def hash_refresh_token(secret: str) -> bytes:
    """SHA-256 of the token secret.

    A plain hash rather than a password KDF, and that is correct here: the
    input is 256 bits of ``secrets.token_urlsafe`` entropy, not a
    human-chosen password. There is no dictionary to attack, so the slow
    hashing that protects passwords buys nothing and would cost a KDF's worth
    of CPU on every refresh.
    """
    return hashlib.sha256(secret.encode()).digest()


class TokenService:
    """Mints and verifies tokens."""

    def __init__(
        self,
        *,
        secret_key: str,
        algorithm: str = "HS256",
        issuer: str,
        audience: str,
        access_ttl_seconds: int = 900,
        refresh_ttl_seconds: int = 2_592_000,
    ) -> None:
        self._secret = secret_key
        self._algorithm = algorithm
        self._issuer = issuer
        self._audience = audience
        self._access_ttl = timedelta(seconds=access_ttl_seconds)
        self._refresh_ttl = timedelta(seconds=refresh_ttl_seconds)

    # ── Access tokens ─────────────────────────────────────────────────────

    def mint_access(
        self,
        *,
        user_id: UUID,
        tenant_id: UUID,
        tenant_plan: str,
        roles: tuple[str, ...],
        permissions: frozenset[str],
        session_id: UUID | None = None,
        aal: int = 1,
        acting_as: UUID | None = None,
    ) -> tuple[str, AccessClaims]:
        """Mint an access token and return it with its decoded claims.

        Permissions ride *in* the token rather than being resolved per request.
        05 §A2.5 caches the expansion in Redis and gates it on ``prm_v``; Phase
        1 has no ``permissions_version`` column, so embedding them is the
        honest simplification — it has the same revocation semantics as the
        cached path (bounded by the 15-minute access TTL) with one fewer moving
        part. When ``prm_v`` lands, this becomes a role list plus a resolver
        and the claim set is unchanged.
        """
        now = datetime.now(UTC)
        expires = now + self._access_ttl
        jti = uuid7()

        payload: dict[str, Any] = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": str(user_id),
            "jti": str(jti),
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int(expires.timestamp()),
            "tid": str(tenant_id),
            "tsl": tenant_plan,
            "rol": list(roles),
            "prm": sorted(permissions),
            "aal": aal,
            "sid": str(session_id) if session_id else None,
            "act": str(acting_as) if acting_as else None,
        }
        token = jwt.encode(payload, self._secret, algorithm=self._algorithm)
        return token, self._to_claims(payload)

    def verify_access(self, token: str) -> AccessClaims:
        """Verify signature, expiry, issuer and audience.

        Raises:
            AuthenticationError: with the frozen code the failure table
                (05 §A3.4) prescribes — ``token_expired`` for a routine expiry
                (metric only) versus ``invalid_token`` for a bad signature
                (which throttles the source IP). A client cannot tell them
                apart from a single 401, and it must: one means "refresh", the
                other means "stop".
        """
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["exp", "iat", "sub", "jti"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError(
                "Access token has expired", code="token_expired"
            ) from exc
        except jwt.PyJWTError as exc:
            # The library message is not forwarded: it can distinguish "bad
            # signature" from "wrong audience", which tells a prober which
            # part of a forged token to fix.
            raise AuthenticationError(
                "Credentials are not valid", code="invalid_token"
            ) from exc

        try:
            return self._to_claims(payload)
        except (KeyError, ValueError, TypeError) as exc:
            raise AuthenticationError(
                "Token claims are malformed", code="invalid_token"
            ) from exc

    @staticmethod
    def _to_claims(payload: dict[str, Any]) -> AccessClaims:
        return AccessClaims(
            sub=UUID(payload["sub"]),
            tid=UUID(payload["tid"]),
            tsl=str(payload.get("tsl", "free")),
            jti=UUID(payload["jti"]),
            sid=UUID(payload["sid"]) if payload.get("sid") else None,
            rol=tuple(payload.get("rol") or ()),
            prm=frozenset(payload.get("prm") or ()),
            aal=int(payload.get("aal", 1)),
            iat=datetime.fromtimestamp(payload["iat"], UTC),
            exp=datetime.fromtimestamp(payload["exp"], UTC),
            act=UUID(payload["act"]) if payload.get("act") else None,
        )

    # ── Refresh tokens ────────────────────────────────────────────────────

    def mint_refresh(self, *, family_id: UUID | None = None) -> RefreshToken:
        """Mint an opaque refresh token.

        ``family_id`` chains a rotation: each refresh mints a new row pointing
        at its parent, and reuse of a rotated token revokes the whole family
        (05 §A1.3). A first login starts a new family.
        """
        secret = secrets.token_urlsafe(32)  # 256 bits
        return RefreshToken(
            secret=secret,
            digest=hash_refresh_token(secret),
            family_id=family_id or uuid7(),
            expires_at=datetime.now(UTC) + self._refresh_ttl,
        )

    @staticmethod
    def refresh_matches(secret: str, stored_digest: bytes) -> bool:
        """Compare in constant time.

        ``hmac.compare_digest`` rather than ``==``: a byte-by-byte comparison
        leaks the length of the matching prefix through timing, and a refresh
        token is a bearer credential for thirty days.
        """
        return hmac.compare_digest(hash_refresh_token(secret), stored_digest)

"""``SecretsPort`` — contract §5 and §8, from 05 §B5.4.

Note the asymmetry the port encodes: callers write plaintext *in*, but the only
way plaintext comes *out* is :meth:`SecretsPort.resolve`, which is called by
adapters inside a request, never by an HTTP handler. The admin UI calls
:meth:`SecretsPort.describe`, which returns metadata and cannot return a value
even by accident, because :class:`SecretMeta` has no field for one.

Contract §8: secrets are envelope-encrypted (KMS-wrapped DEK per tenant) and
plaintext is never persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

__all__ = ["SecretMeta", "SecretRef", "SecretValue", "SecretsPort"]


class SecretValue(str):
    """A ``str`` subclass that refuses to be logged.

    Why a subclass and not a wrapper: adapters pass secrets to vendor SDKs that
    expect ``str``. Wrapping forces ``.get()`` calls everywhere, and the one
    place someone forgets is the one that logs it. Overriding
    ``__repr__``/``__str__``/``__format__`` means f-strings, logging, and reprs
    all show the mask; only an explicit :meth:`reveal` yields the value.

    It lives in ``core`` rather than beside its adapter because the *type* is
    part of the port contract — a port returning a bare ``str`` would make
    "never log secrets" a review convention instead of a property of the value.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "SecretValue(***)"

    def __str__(self) -> str:
        return "***"

    def __format__(self, format_spec: str) -> str:
        return "***"

    def reveal(self) -> str:
        """The plaintext. Every call site is a deliberate, reviewable decision."""
        return super().__str__()


@dataclass(frozen=True, slots=True)
class SecretRef:
    """A handle to a stored secret. Carries no plaintext, by construction."""

    secret_id: UUID
    tenant_id: UUID
    name: str
    key_version: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SecretMeta:
    """Everything the admin UI may see about a secret.

    There is deliberately no ciphertext, no wrapped DEK, and no value field: a
    metadata type that *could* carry a secret eventually will.
    """

    secret_id: UUID
    tenant_id: UUID
    scope: str
    name: str
    #: Non-secret fingerprint for display ("sk-…4f2a").
    last_four: str | None
    key_version: int
    expires_at: datetime | None
    rotated_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime


@runtime_checkable
class SecretsPort(Protocol):
    """Tenant secret read/write, envelope-encrypted.

    Every method is tenant-scoped explicitly rather than relying on ambient
    context: a secrets adapter may run inside an ops job or a Temporal activity
    where there is no bound request, and an implicit tenant there would be a
    cross-tenant leak waiting to happen.
    """

    async def put(
        self,
        *,
        tenant_id: UUID,
        name: str,
        value: str,
        metadata: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
        actor: str,
    ) -> SecretRef: ...

    async def resolve(self, *, tenant_id: UUID, name: str) -> SecretValue: ...

    async def describe(self, *, tenant_id: UUID, name: str) -> SecretMeta:
        """Metadata only — never the value. This is what the admin UI calls."""
        ...

    async def delete(self, *, tenant_id: UUID, name: str, actor: str) -> None: ...

    async def rotate_dek(self, *, tenant_id: UUID) -> None: ...

    async def shred(self, *, tenant_id: UUID) -> None:
        """Crypto-shred: revoke all DEKs. Irreversible."""
        ...

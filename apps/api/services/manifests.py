"""Where a tenant's installed manifest lives — and why it is a setting.

Contract §4 makes a vertical a manifest; 01 §5 gives it a
``vertical_installs`` table with a JSONB document and a version. **Phase 1 does
not have that table** — the walking-skeleton migration creates tenancy,
identity and conversations only.

So the install is stored in ``tenant_settings``, which exists, is tenant-owned,
is RLS-protected exactly as ``vertical_installs`` will be, and whose ``value``
column is already JSONB. The namespace and key below are the whole convention:

    namespace = "vertical", key = "install"  →  {"key", "version", "manifest"}

**Why this is a considered choice and not a shortcut.** The alternative is
adding a migration for a table whose real shape 01 §5 specifies with columns
Phase 1 has no use for (install status, publish workflow, per-location
overrides). Guessing at half of that now means either a second migration to
correct it or living with a table shaped by what a demo needed. A setting row
carries the same JSONB with the same isolation, and the move to the real table
is one migration that reads these rows — no API change, because every caller
goes through :class:`ManifestInstallService`.

The one thing this deliberately does *not* do is fall back to a file on disk
when a tenant has no install. A tenant with no manifest configured must fail
loudly; silently serving the first-party medical manifest to a restaurant would
be a cross-vertical version of the cross-tenant bug this codebase is built to
prevent.
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

import structlog
from packages.core.errors import ConfigurationError
from packages.persistence.models.tenancy import TenantSetting
from packages.persistence.repositories.tenants import TenantSettingRepository
from packages.persistence.session import TenantSession
from packages.verticals.manifest import VerticalManifest, load_manifest

__all__ = ["INSTALL_KEY", "INSTALL_NAMESPACE", "ManifestInstallService"]

log = structlog.get_logger(__name__)

INSTALL_NAMESPACE: Final = "vertical"
INSTALL_KEY: Final = "install"

#: Free-form tenant text folded into prompt layer 4 (03 §7.1). Separate from
#: the install so editing the business description does not rewrite the
#: manifest document and bump its version.
CUSTOMIZATION_NAMESPACE: Final = "agent"
CUSTOMIZATION_KEY: Final = "customization"


class ManifestInstallService:
    """Reads (and writes) the manifest installed for a tenant."""

    def __init__(self, session: TenantSession) -> None:
        self._session = session
        self._settings = TenantSettingRepository(session)

    async def load(self, *, version: str | None = None) -> VerticalManifest:
        """The tenant's installed manifest.

        Args:
            version: When given, assert the install matches. A resume passes
                the version the conversation was started under (03 §4.4), and a
                mismatch means the manifest was republished mid-conversation —
                which must not silently continue on the new graph, because the
                agent set may have changed.

        Raises:
            ConfigurationError: when no manifest is installed, or when a
                pinned version no longer matches. Both are 500-class: the
                caller did nothing wrong, and the tenant's configuration is
                incomplete.
        """
        raw = await self._settings.get_value(INSTALL_NAMESPACE, INSTALL_KEY)
        if not raw:
            raise ConfigurationError(
                "No vertical manifest is installed for this tenant. Run "
                "`tools/seed_demo.py`, or install one through the admin API. "
                "Deliberately not falling back to a first-party manifest — "
                "serving a dental agent to a restaurant is worse than an error."
            )

        document = raw.get("manifest") if isinstance(raw, dict) else None
        if not isinstance(document, dict):
            raise ConfigurationError(
                "The installed vertical setting is malformed: expected a "
                "'manifest' object."
            )

        manifest = load_manifest(document)

        if version is not None and manifest.cache_version != version:
            raise ConfigurationError(
                f"This conversation was started on manifest {version!r}, but "
                f"{manifest.cache_version!r} is now installed. Refusing to "
                f"resume on a different graph — the agent set may have changed "
                f"(03 §4.4)."
            )
        return manifest

    async def customization(self) -> str:
        """Prompt layer 4: the tenant's free-form description of itself."""
        value = await self._settings.get_value(
            CUSTOMIZATION_NAMESPACE, CUSTOMIZATION_KEY
        )
        if isinstance(value, dict):
            return str(value.get("text", ""))
        return str(value or "")

    async def install(
        self, *, manifest: VerticalManifest, tenant_id: UUID, actor: UUID | None = None
    ) -> None:
        """Install or replace the tenant's manifest. Idempotent.

        Upsert by hand rather than via ``ON CONFLICT``: the repository layer
        has no upsert, the row count here is one, and a read-then-write inside
        the enclosing transaction is correct under the partial unique index
        (``uq_tenant_settings__tenant_ns_key_global``) — a concurrent installer
        collides on it rather than silently interleaving.
        """
        payload: dict[str, Any] = {
            "key": manifest.key,
            "version": manifest.version,
            "cache_version": manifest.cache_version,
            "manifest": manifest.model_dump(mode="json"),
        }

        existing = await self._settings.list(
            TenantSetting.namespace == INSTALL_NAMESPACE,
            TenantSetting.key == INSTALL_KEY,
            TenantSetting.location_id.is_(None),
            limit=1,
        )
        if existing:
            row = existing[0]
            row.value = payload
            row.updated_by_user_id = actor
            await self._session.flush()
            log.info(
                "vertical.reinstalled",
                tenant_id=str(tenant_id),
                manifest=manifest.cache_version,
            )
            return

        await self._settings.add(
            TenantSetting(
                tenant_id=tenant_id,
                location_id=None,
                namespace=INSTALL_NAMESPACE,
                key=INSTALL_KEY,
                value=payload,
                schema_version=manifest.schema_version,
                description=f"Installed vertical: {manifest.display_name}",
                updated_by_user_id=actor,
            )
        )
        log.info(
            "vertical.installed",
            tenant_id=str(tenant_id),
            manifest=manifest.cache_version,
        )

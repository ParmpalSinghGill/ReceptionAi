#!/usr/bin/env python3
"""Seed the demo tenant, the medical manifest install, and a demo user.

    python3 tools/seed_demo.py

**Idempotent.** Every step is "create if absent, update if present", so running
it twice is safe and running it after a schema change refreshes what it owns.
That matters more than it sounds: a seed script that must only be run once is a
seed script someone eventually runs twice, on a database they cared about.

**It connects as the migrator role, not the app role.** Creating a tenant is
something a tenant cannot do — ``tenants`` RLS is ``id = app_current_tenant()``,
so the app role cannot insert the first row by construction (that is the point
of the policy). In production this is a ``SECURITY DEFINER`` provisioning
function granted to a signup role; locally, the migrator credential is the
honest equivalent, and using it here rather than weakening a policy keeps the
policy meaning what it says.

What it creates:

* the permission catalogue and the five platform template roles (05 §A2.3)
* tenant ``sunrise-dental`` with one location
* the medical manifest installed into ``tenant_settings``
* an owner user, ``demo@sunrisedental.in`` / ``demo1234``
* a second tenant with its own conversation, which exists **only** so
  ``tests/api/test_tenant_isolation.py`` can prove the demo endpoint cannot
  reach it. A negative test needs something real to fail to reach.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.api.security.passwords import hash_password  # noqa: E402
from apps.api.security.permissions import (  # noqa: E402
    ROLE_GRANTS,
    ROLE_RANKS,
    permission_catalogue,
)
from packages.core.ids import uuid7  # noqa: E402
from packages.verticals import load_manifest_file  # noqa: E402
from packages.verticals.manifests import manifest_path  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine  # noqa: E402

DEMO_TENANT_SLUG = "sunrise-dental"
DEMO_TENANT_NAME = "Sunrise Dental"
DEMO_EMAIL = "demo@sunrisedental.in"
DEMO_PASSWORD = "demo1234"  # noqa: S105 - a local development fixture

#: A second tenant, existing only to be unreachable. See the module docstring.
OTHER_TENANT_SLUG = "riverside-dental"
OTHER_TENANT_NAME = "Riverside Dental"


def migration_dsn() -> str:
    """The migrator credential, from the same env the Makefile uses."""
    explicit = os.environ.get("MIGRATION_DATABASE_URL")
    if explicit:
        return explicit
    password = os.environ.get("POSTGRES_PASSWORD", "localpass")
    port = os.environ.get("POSTGRES_PORT", "55432")
    database = os.environ.get("POSTGRES_DB", "receptionai")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    return (
        f"postgresql+asyncpg://receptionai_migrator:{password}"
        f"@{host}:{port}/{database}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Steps
# ─────────────────────────────────────────────────────────────────────────────


async def seed_permissions(conn: AsyncConnection) -> dict[str, UUID]:
    """Insert the permission catalogue. Returns ``code -> id``.

    Derived from ``apps/api/security/permissions.py`` rather than written out
    again, so a permission added to the enum cannot be missing from the
    database — which would make every role that grants it silently narrower
    than the grid says.
    """
    ids: dict[str, UUID] = {}
    for code, resource, action, description, is_sensitive in permission_catalogue():
        row = (
            await conn.execute(
                text(
                    "INSERT INTO permissions "
                    "  (id, code, resource, action, description, is_sensitive) "
                    "VALUES (:id, :code, :resource, :action, :description, :sensitive) "
                    "ON CONFLICT (code) DO UPDATE "
                    "   SET description = EXCLUDED.description, "
                    "       is_sensitive = EXCLUDED.is_sensitive "
                    "RETURNING id"
                ),
                {
                    "id": uuid7(),
                    "code": code,
                    "resource": resource,
                    "action": action,
                    "description": description,
                    "sensitive": is_sensitive,
                },
            )
        ).scalar_one()
        ids[code] = row
    return ids


async def seed_platform_roles(
    conn: AsyncConnection, permission_ids: dict[str, UUID]
) -> dict[str, UUID]:
    """The five template roles (``tenant_id IS NULL``) and their grants."""
    role_ids: dict[str, UUID] = {}

    for code, grants in ROLE_GRANTS.items():
        existing = (
            await conn.execute(
                text(
                    "SELECT id FROM roles WHERE code = :code AND tenant_id IS NULL"
                ),
                {"code": code},
            )
        ).scalar_one_or_none()

        if existing is None:
            role_id = uuid7()
            await conn.execute(
                text(
                    "INSERT INTO roles (id, tenant_id, code, name, description, "
                    "                   is_system, rank) "
                    "VALUES (:id, NULL, :code, :name, :description, TRUE, :rank)"
                ),
                {
                    "id": role_id,
                    "code": code,
                    "name": code.title(),
                    "description": f"Platform template role: {code}",
                    "rank": ROLE_RANKS[code],
                },
            )
        else:
            role_id = existing

        role_ids[code] = role_id

        # Replace the grants wholesale rather than diffing: the grid in
        # `permissions.py` is the source of truth, and a partial update would
        # leave a permission granted after it was removed from the grid.
        await conn.execute(
            text("DELETE FROM role_permissions WHERE role_id = :role_id"),
            {"role_id": role_id},
        )
        for perm in sorted(grants):
            await conn.execute(
                text(
                    "INSERT INTO role_permissions (role_id, permission_id, tenant_id) "
                    "VALUES (:role_id, :permission_id, NULL) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"role_id": role_id, "permission_id": permission_ids[perm.value]},
            )

    return role_ids


async def seed_tenant(
    conn: AsyncConnection, *, slug: str, display_name: str, legal_name: str
) -> UUID:
    """Create or refresh a tenant. Returns its id."""
    existing = (
        await conn.execute(
            text("SELECT id FROM tenants WHERE slug = :slug AND deleted_at IS NULL"),
            {"slug": slug},
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    tenant_id = uuid7()
    await conn.execute(
        text(
            "INSERT INTO tenants (id, slug, legal_name, display_name, status, "
            "                     default_timezone, default_locale, "
            "                     default_currency, default_text_search_config, "
            "                     compliance_profile) "
            "VALUES (:id, :slug, :legal_name, :display_name, 'active', "
            "        'Asia/Kolkata', 'en-IN', 'INR', 'english', 'standard')"
        ),
        {
            "id": tenant_id,
            "slug": slug,
            "legal_name": legal_name,
            "display_name": display_name,
        },
    )
    return tenant_id


async def seed_location(conn: AsyncConnection, tenant_id: UUID, name: str) -> UUID:
    existing = (
        await conn.execute(
            text(
                "SELECT id FROM locations "
                " WHERE tenant_id = :tenant_id AND code = 'main' "
                "   AND deleted_at IS NULL"
            ),
            {"tenant_id": tenant_id},
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    location_id = uuid7()
    await conn.execute(
        text(
            "INSERT INTO locations (id, tenant_id, code, name, timezone, currency, "
            "                       locale, city, country_code, is_active) "
            "VALUES (:id, :tenant_id, 'main', :name, 'Asia/Kolkata', 'INR', "
            "        'en-IN', 'Pune', 'IN', TRUE)"
        ),
        {"id": location_id, "tenant_id": tenant_id, "name": name},
    )
    return location_id


async def install_manifest(conn: AsyncConnection, tenant_id: UUID, key: str) -> str:
    """Install a first-party manifest into ``tenant_settings``.

    Validated before it is written. Storing an invalid manifest would move the
    failure from this script — where someone is watching — to the first
    conversation, where nobody is.

    The namespace/key convention is owned by
    ``apps/api/services/manifests.py``; see that module for why Phase 1 uses a
    setting rather than 01 §5's ``vertical_installs`` table.
    """
    from apps.api.services.manifests import INSTALL_KEY, INSTALL_NAMESPACE

    manifest = load_manifest_file(manifest_path(key))
    payload: dict[str, Any] = {
        "key": manifest.key,
        "version": manifest.version,
        "cache_version": manifest.cache_version,
        "manifest": manifest.model_dump(mode="json"),
    }

    await conn.execute(
        text(
            "INSERT INTO tenant_settings "
            "  (id, tenant_id, location_id, namespace, key, value, "
            "   schema_version, description) "
            "VALUES (:id, :tenant_id, NULL, :namespace, :key, "
            "        CAST(:value AS jsonb), :schema_version, :description) "
            "ON CONFLICT (tenant_id, namespace, key) "
            "  WHERE location_id IS NULL "
            "  DO UPDATE SET value = EXCLUDED.value, "
            "                schema_version = EXCLUDED.schema_version, "
            "                description = EXCLUDED.description"
        ),
        {
            "id": uuid7(),
            "tenant_id": tenant_id,
            "namespace": INSTALL_NAMESPACE,
            "key": INSTALL_KEY,
            "value": json.dumps(payload),
            "schema_version": manifest.schema_version,
            "description": f"Installed vertical: {manifest.display_name}",
        },
    )
    return manifest.cache_version


async def seed_customization(conn: AsyncConnection, tenant_id: UUID, text_value: str) -> None:
    """Prompt layer 4 — the tenant's own description of itself (03 §7.1)."""
    from apps.api.services.manifests import CUSTOMIZATION_KEY, CUSTOMIZATION_NAMESPACE

    await conn.execute(
        text(
            "INSERT INTO tenant_settings "
            "  (id, tenant_id, location_id, namespace, key, value, description) "
            "VALUES (:id, :tenant_id, NULL, :namespace, :key, "
            "        CAST(:value AS jsonb), 'Tenant prompt customization') "
            "ON CONFLICT (tenant_id, namespace, key) "
            "  WHERE location_id IS NULL "
            "  DO UPDATE SET value = EXCLUDED.value"
        ),
        {
            "id": uuid7(),
            "tenant_id": tenant_id,
            "namespace": CUSTOMIZATION_NAMESPACE,
            "key": CUSTOMIZATION_KEY,
            "value": json.dumps({"text": text_value}),
        },
    )


async def seed_user(
    conn: AsyncConnection,
    *,
    tenant_id: UUID,
    role_id: UUID,
    email: str,
    password: str,
    full_name: str,
) -> UUID:
    """Create or refresh a user and their membership."""
    existing = (
        await conn.execute(
            text(
                "SELECT id FROM users "
                " WHERE email_normalized = lower(btrim(:email)) "
                "   AND deleted_at IS NULL"
            ),
            {"email": email},
        )
    ).scalar_one_or_none()

    if existing is None:
        user_id = uuid7()
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, password_algo, "
                "                   full_name, display_name, status, locale, "
                "                   timezone, email_verified_at) "
                "VALUES (:id, :email, :hash, 'argon2id', :full_name, "
                "        :display_name, 'active', 'en-IN', 'Asia/Kolkata', now())"
            ),
            {
                "id": user_id,
                "email": email,
                "hash": hash_password(password),
                "full_name": full_name,
                "display_name": full_name.split()[0],
            },
        )
    else:
        user_id = existing
        # Reset the password on re-seed: the whole point of the demo user is
        # that the documented credentials work.
        await conn.execute(
            text(
                "UPDATE users SET password_hash = :hash, password_algo = 'argon2id', "
                "                 status = 'active', locked_until = NULL, "
                "                 failed_login_count = 0 "
                " WHERE id = :id"
            ),
            {"id": user_id, "hash": hash_password(password)},
        )

    membership = (
        await conn.execute(
            text(
                "SELECT id FROM user_tenant_roles "
                " WHERE tenant_id = :tenant_id AND user_id = :user_id "
                "   AND role_id = :role_id AND revoked_at IS NULL"
            ),
            {"tenant_id": tenant_id, "user_id": user_id, "role_id": role_id},
        )
    ).scalar_one_or_none()

    if membership is None:
        await conn.execute(
            text(
                "INSERT INTO user_tenant_roles "
                "  (id, tenant_id, user_id, role_id, is_owner, accepted_at) "
                "VALUES (:id, :tenant_id, :user_id, :role_id, TRUE, now())"
            ),
            {
                "id": uuid7(),
                "tenant_id": tenant_id,
                "user_id": user_id,
                "role_id": role_id,
            },
        )

    return user_id


# ─────────────────────────────────────────────────────────────────────────────
# The booking engine (01 §5) — the diary the agent actually books against
# ─────────────────────────────────────────────────────────────────────────────
#
# Everything below is expressed in the GENERIC vocabulary of 01 §5, because that
# is the point of the model: a dentist is a `resource` whose `resource_type` is
# `human`, a surgery is a `resource` whose type is `space`, and "cardiology" or
# "hygiene" is a value in `resources.attributes`. Nothing here is a column a
# dental practice needed and a restaurant would not.
#
# The four dials of 01 §14.0, set for a dental practice:
#   time_unit                = 'minute'   (a restaurant is the same; a hotel is 'night')
#   slot_granularity_minutes = 30         (a restaurant floats: NULL)
#   is_capacity_shared       = FALSE      (a chair serves one patient at a time)
#   attribute_predicate      = {"match": {"skills": [...]}}

#: Resource types. Two, because an appointment consumes a *person* and a *room*
#: and they are freed at different moments — the clinician the instant the
#: appointment ends, the room only after it has been cleaned. That difference is
#: `respects_buffers` on the requirement, and it is why a practice with four
#: dentists and four surgeries can run a 15-minute turnaround without either
#: resource blocking the other unnecessarily.
_RESOURCE_TYPES: tuple[dict[str, Any], ...] = (
    {
        "code": "clinician",
        "name": "Clinician",
        "kind": "human",
        "slot_granularity_minutes": 30,
        "attribute_schema": {
            "type": "object",
            "properties": {
                "skills": {"type": "array", "items": {"type": "string"}},
                "languages": {"type": "array", "items": {"type": "string"}},
                "gdc_number": {"type": "string"},
                "accepts_new_patients": {"type": "boolean"},
            },
        },
        "filterable_attributes": ["skills", "languages", "accepts_new_patients"],
    },
    {
        "code": "treatment_room",
        "name": "Surgery",
        "kind": "space",
        "slot_granularity_minutes": 30,
        "attribute_schema": {
            "type": "object",
            "properties": {
                "equipment": {"type": "array", "items": {"type": "string"}},
                "floor": {"type": "integer"},
                "step_free": {"type": "boolean"},
            },
        },
        "filterable_attributes": ["equipment", "step_free"],
    },
)

#: Three dentists and a hygienist, deliberately NOT interchangeable.
#:
#: `skills` is what a service's requirement matches against — a hygiene visit
#: needs someone with `hygiene`, and only Marta has it, so the engine allocates
#: correctly without a line of code that knows what a hygienist is.
#:
#: The rest of the attributes exist so the agent can *recommend* rather than
#: enumerate: "I can get you in with Dr Okafor at 3:40 — he covers our same-day
#: emergency slots" is a better answer than three bare times. They are read from
#: the tool's output at conversation time and never hardcoded anywhere in
#: `packages/ai` or `packages/booking` (invariant I4), which is what keeps the
#: same mechanism working for a restaurant's tables or a salon's stylists.
#:
#: Four clinicians who all do the same thing would teach the model nothing, so
#: the scopes genuinely differ and each has one distinguishing note.
_RESOURCES: tuple[dict[str, Any], ...] = (
    {
        "type": "clinician",
        "code": "dr-rahman",
        "name": "Dr Ananya Deshpande",
        "sort_order": 1,
        "attributes": {
            "skills": ["exam", "checkup", "restorative", "emergency"],
            "scope": "General dentistry and same-day emergencies",
            "languages": ["English", "Urdu", "Punjabi"],
            "sees_children": True,
            "note": "Very calm with nervous patients and happy to talk "
                    "through what she is doing as she goes.",
            "gdc_number": "248811",
            "accepts_new_patients": True,
        },
    },
    {
        "type": "clinician",
        "code": "dr-okafor",
        "name": "Dr Rohan Kulkarni",
        "sort_order": 2,
        "attributes": {
            "skills": ["exam", "checkup", "restorative", "emergency", "endodontics"],
            "scope": "Emergency and same-day care, root canal treatment",
            "languages": ["English"],
            "sees_children": False,
            "note": "Takes most of our acute pain and trauma appointments; "
                    "quick and thorough when something has broken.",
            "gdc_number": "271004",
            "accepts_new_patients": True,
        },
    },
    {
        "type": "clinician",
        "code": "dr-whitfield",
        "name": "Dr Meera Iyer",
        "sort_order": 3,
        "attributes": {
            "skills": ["exam", "checkup", "restorative", "cosmetic"],
            "scope": "Cosmetic dentistry — whitening, veneers, aligners",
            "languages": ["English"],
            "sees_children": False,
            "note": "Runs our cosmetic consultations; her list is usually "
                    "booked a week or two ahead.",
            "gdc_number": "199522",
            # Deliberately False: it gives the `accepts_new_patients` predicate
            # on `new-patient-exam` something real to exclude, so the allocation
            # filter is exercised by the seed rather than being decorative.
            "accepts_new_patients": False,
        },
    },
    {
        "type": "clinician",
        "code": "hyg-kowalczyk",
        "name": "Sunita Rane",
        "sort_order": 4,
        "attributes": {
            "skills": ["hygiene"],
            "scope": "Dental hygienist — scale and polish, gum health",
            "languages": ["English", "Polish"],
            "sees_children": True,
            "note": "Our hygienist. Gentle with sensitive gums and good with "
                    "children's first cleans.",
            "gdc_number": "310447",
            "accepts_new_patients": True,
        },
    },
    {
        "type": "treatment_room",
        "code": "surgery-1",
        "name": "Surgery 1",
        "sort_order": 1,
        "attributes": {
            "equipment": ["xray", "intraoral_camera"],
            "floor": 0,
            "step_free": True,
        },
    },
    {
        "type": "treatment_room",
        "code": "surgery-2",
        "name": "Surgery 2",
        "sort_order": 2,
        "attributes": {"equipment": ["xray"], "floor": 0, "step_free": True},
    },
    {
        "type": "treatment_room",
        "code": "surgery-3",
        "name": "Surgery 3",
        "sort_order": 3,
        "attributes": {
            "equipment": ["xray", "intraoral_camera"],
            "floor": 1,
            "step_free": False,
        },
    },
)

#: **Why every service is 25 + 5 = 30 minutes.**
#:
#: This is a DEMO-LEGIBILITY choice, not a claim that dental practices work this
#: way. Real durations are ragged — a new-patient exam genuinely takes 45
#: minutes and a check-up 20 — and the booking engine supports that perfectly
#: well; nothing in `packages/booking` knows or cares about half hours.
#:
#: But the demo is about to be shown in a calendar view with one row per half
#: hour, and mixed 20/40/45-minute appointments make that unreadable. A uniform
#: 30-minute footprint on a 30-minute grid gives exactly 18 slots between 09:00
#: and 18:00, every one starting on the hour or the half hour.
#:
#: The split matters: 25 minutes of appointment plus a 5-minute turnaround
#: buffer, so the FOOTPRINT is 30 and consecutive bookings abut exactly. A flat
#: 30 + a buffer would make it 35 and every slot would drift.
#:
#: Restore real durations by editing this table; re-seeding applies whatever is
#: here.
#:
#: Services, with real durations, buffers, prices and policies. The buffers are
#: the turnaround time between patients: 10 minutes for a check-up, 15 for a
#: longer appointment. They widen `blocking_period` (a GENERATED column), which
#: is what the exclusion constraint indexes — so a 20-minute check-up genuinely
#: occupies 30 minutes of a surgery's timeline and 20 of the dentist's.
_SERVICES: tuple[dict[str, Any], ...] = (
    {
        "code": "new-patient-exam",
        "name": "New Patient Examination",
        "category": "diagnostic",
        "duration_minutes": 25,
        "buffer_after_minutes": 5,
        "price": "45.0000",
        "lead_time_minutes": 120,
        "cancellation_window_minutes": 1440,
        "cancellation_fee": "25.0000",
        "sort_order": 1,
        "intake_schema": [
            {
                "key": "reason_for_visit",
                "type": "string",
                "required": True,
                "pii_class": "phi",
                "prompt": "What brings you in today?",
            }
        ],
        "requirements": (
            {
                "type": "clinician",
                "role": "primary",
                "predicate": {
                    "match": {"skills": ["exam"], "accepts_new_patients": True}
                },
                "strategy": "least_utilised",
                # The clinician is free the moment the appointment ends;
                # cleaning the room is the room's problem, not theirs.
                "respects_buffers": False,
            },
            {
                "type": "treatment_room",
                "role": "space",
                "predicate": {"match": {"equipment": ["xray"]}},
                "strategy": "first_available",
                "respects_buffers": True,
            },
        ),
    },
    {
        "code": "checkup",
        "name": "Routine Check-up",
        "category": "diagnostic",
        "duration_minutes": 25,
        "buffer_after_minutes": 5,
        "price": "35.0000",
        "lead_time_minutes": 60,
        "cancellation_window_minutes": 1440,
        "cancellation_fee": "25.0000",
        "sort_order": 2,
        "intake_schema": [],
        "requirements": (
            {
                "type": "clinician",
                "role": "primary",
                "predicate": {"match": {"skills": ["checkup"]}},
                "strategy": "least_utilised",
                "respects_buffers": False,
            },
            {
                "type": "treatment_room",
                "role": "space",
                "predicate": {},
                "strategy": "first_available",
                "respects_buffers": True,
            },
        ),
    },
    {
        "code": "hygienist",
        "name": "Hygiene Visit",
        "category": "preventive",
        "duration_minutes": 25,
        "buffer_after_minutes": 5,
        "price": "68.0000",
        "lead_time_minutes": 60,
        "cancellation_window_minutes": 1440,
        "cancellation_fee": "25.0000",
        "sort_order": 3,
        "intake_schema": [],
        "requirements": (
            {
                "type": "clinician",
                "role": "primary",
                # Only Marta has `hygiene`. One data value, no code.
                "predicate": {"match": {"skills": ["hygiene"]}},
                "strategy": "first_available",
                "respects_buffers": False,
            },
            {
                "type": "treatment_room",
                "role": "space",
                "predicate": {},
                "strategy": "first_available",
                "respects_buffers": True,
            },
        ),
    },
    {
        "code": "emergency",
        "name": "Emergency Appointment",
        "category": "urgent",
        "duration_minutes": 25,
        "buffer_after_minutes": 5,
        "price": "75.0000",
        # Bookable right now: someone in acute pain must not be told the
        # earliest slot is in two hours because of a lead-time rule written for
        # routine care.
        "lead_time_minutes": 0,
        # Same-day only, so an emergency slot cannot be quietly consumed by a
        # booking three weeks out.
        "booking_horizon_days": 1,
        # No fee: charging someone for cancelling an emergency they no longer
        # need is not a policy any practice would defend.
        "cancellation_window_minutes": 0,
        "cancellation_fee": None,
        "sort_order": 4,
        "intake_schema": [
            {
                "key": "reason_for_visit",
                "type": "string",
                "required": True,
                "pii_class": "phi",
                "prompt": "Can you tell me what is happening, in your own words?",
            }
        ],
        "requirements": (
            {
                "type": "clinician",
                "role": "primary",
                "predicate": {"match": {"skills": ["emergency"]}},
                "strategy": "first_available",
                "respects_buffers": False,
            },
            {
                "type": "treatment_room",
                "role": "space",
                "predicate": {},
                "strategy": "first_available",
                "respects_buffers": True,
            },
        ),
    },
)

#: Opening hours, in LOCAL wall-clock time plus a weekday mask (01 §5.6). Stored
#: this way, not as UTC, so that "we open at eight" is still eight o'clock after
#: the clocks change — a stored UTC offset would be an hour wrong for half the
#: year, which is the single most common booking bug there is.
#: How many appointments the practice runs at once, site-wide.
#:
#: One, for the demo: the user's calendar shows a row per half hour, and two
#: appointments in one row reads as a bug. It is also how a genuine
#: single-surgery practice operates.
#:
#: This is `availability_rules.capacity_override` — 01 §5.6's "a restaurant that
#: seats 40, or 25 on the patio in winter". It is CONFIGURATION: the engine
#: enforces whatever number is here, `None` means no site-wide limit (the
#: default every other tenant gets), and a four-surgery practice sets 4. No
#: single-surgery assumption reaches `packages/booking`.
#:
#: The cost is real and worth stating: with this at 1, three of the four
#: clinicians are idle at any moment and today's emergency slots run out four
#: times faster. Raise it to 4 to see the practice run at full capacity.
_CONCURRENT_APPOINTMENTS: int | None = 1

#: 09:00-18:00, Monday to Saturday. Nine hours on a 30-minute grid is exactly
#: eighteen slots a day, which is what makes a calendar view readable — one row
#: per half hour, no ragged 9:20 or 9:45 boundaries.
_OPENING_HOURS: tuple[tuple[list[int], time, time], ...] = (
    ([1, 2, 3, 4, 5, 6], time(9, 0), time(18, 0)),
)

#: Two holidays, one of each kind. The August bank holiday is a concrete date;
#: Christmas Day is `annual`, so it recurs every year from one row rather than
#: needing a new row each December (01 §5.8).
_HOLIDAYS: tuple[dict[str, Any], ...] = (
    {
        "name": "Summer bank holiday",
        "date": "2026-08-31",
        "recurrence": "none",
        "calendar_code": "GB-ENG",
    },
    {
        "name": "Christmas Day",
        "date": "2026-12-25",
        "recurrence": "annual",
        "calendar_code": "GB-ENG",
    },
)


async def seed_booking_engine(
    conn: AsyncConnection, tenant_id: UUID, location_id: UUID
) -> dict[str, Any]:
    """Resource types, resources, services, requirements, hours and holidays.

    Idempotent throughout, like the rest of this script: every insert is an
    ``ON CONFLICT ... DO UPDATE`` on the natural key, so re-running refreshes the
    catalogue rather than duplicating it or failing.
    """
    type_ids: dict[str, UUID] = {}
    for spec in _RESOURCE_TYPES:
        type_ids[spec["code"]] = (
            await conn.execute(
                text(
                    "INSERT INTO resource_types "
                    "  (id, tenant_id, code, name, kind, is_capacity_shared, "
                    "   time_unit, slot_granularity_minutes, attribute_schema, "
                    "   filterable_attributes) "
                    "VALUES (:id, :tenant_id, :code, :name, "
                    "        CAST(:kind AS resource_kind), FALSE, 'minute', "
                    "        :granularity, CAST(:schema AS jsonb), :filterable) "
                    "ON CONFLICT (tenant_id, code) DO UPDATE "
                    "   SET name = EXCLUDED.name, "
                    "       attribute_schema = EXCLUDED.attribute_schema, "
                    "       filterable_attributes = EXCLUDED.filterable_attributes, "
                    "       slot_granularity_minutes = EXCLUDED.slot_granularity_minutes "
                    "RETURNING id"
                ),
                {
                    "id": uuid7(),
                    "tenant_id": tenant_id,
                    "code": spec["code"],
                    "name": spec["name"],
                    "kind": spec["kind"],
                    "granularity": spec["slot_granularity_minutes"],
                    "schema": json.dumps(spec["attribute_schema"]),
                    "filterable": spec["filterable_attributes"],
                },
            )
        ).scalar_one()

    resource_ids: dict[str, UUID] = {}
    for spec in _RESOURCES:
        resource_ids[spec["code"]] = (
            await conn.execute(
                text(
                    "INSERT INTO resources "
                    "  (id, tenant_id, location_id, resource_type_id, code, name, "
                    "   capacity, attributes, sort_order) "
                    "VALUES (:id, :tenant_id, :location_id, :type_id, :code, :name, "
                    "        1, CAST(:attributes AS jsonb), :sort_order) "
                    "ON CONFLICT (tenant_id, location_id, code) "
                    "  WHERE deleted_at IS NULL "
                    "  DO UPDATE SET name = EXCLUDED.name, "
                    "                attributes = EXCLUDED.attributes, "
                    "                sort_order = EXCLUDED.sort_order "
                    "RETURNING id"
                ),
                {
                    "id": uuid7(),
                    "tenant_id": tenant_id,
                    "location_id": location_id,
                    "type_id": type_ids[spec["type"]],
                    "code": spec["code"],
                    "name": spec["name"],
                    "attributes": json.dumps(spec["attributes"]),
                    "sort_order": spec["sort_order"],
                },
            )
        ).scalar_one()

    service_ids: dict[str, UUID] = {}
    for spec in _SERVICES:
        service_id = (
            await conn.execute(
                text(
                    "INSERT INTO services "
                    "  (id, tenant_id, location_id, code, name, category, "
                    "   duration_minutes, buffer_after_minutes, price, currency, "
                    "   lead_time_minutes, booking_horizon_days, "
                    "   cancellation_window_minutes, cancellation_fee, "
                    "   intake_schema, sort_order) "
                    "VALUES (:id, :tenant_id, :location_id, :code, :name, :category, "
                    "        :duration, :buffer_after, :price, 'INR', "
                    "        :lead_time, :horizon, :cancel_window, :cancel_fee, "
                    "        CAST(:intake AS jsonb), :sort_order) "
                    "ON CONFLICT (tenant_id, location_id, code) "
                    "  WHERE location_id IS NOT NULL AND deleted_at IS NULL "
                    "  DO UPDATE SET name = EXCLUDED.name, "
                    "                duration_minutes = EXCLUDED.duration_minutes, "
                    "                buffer_after_minutes = EXCLUDED.buffer_after_minutes, "
                    "                price = EXCLUDED.price, "
                    "                lead_time_minutes = EXCLUDED.lead_time_minutes, "
                    "                booking_horizon_days = EXCLUDED.booking_horizon_days, "
                    "                cancellation_window_minutes = "
                    "                    EXCLUDED.cancellation_window_minutes, "
                    "                cancellation_fee = EXCLUDED.cancellation_fee, "
                    "                intake_schema = EXCLUDED.intake_schema "
                    "RETURNING id"
                ),
                {
                    "id": uuid7(),
                    "tenant_id": tenant_id,
                    "location_id": location_id,
                    "code": spec["code"],
                    "name": spec["name"],
                    "category": spec["category"],
                    "duration": spec["duration_minutes"],
                    "buffer_after": spec["buffer_after_minutes"],
                    "price": spec["price"],
                    "lead_time": spec["lead_time_minutes"],
                    "horizon": spec.get("booking_horizon_days", 90),
                    "cancel_window": spec["cancellation_window_minutes"],
                    "cancel_fee": spec["cancellation_fee"],
                    "intake": json.dumps(spec["intake_schema"]),
                    "sort_order": spec["sort_order"],
                },
            )
        ).scalar_one()
        service_ids[spec["code"]] = service_id

        for requirement in spec["requirements"]:
            await conn.execute(
                text(
                    "INSERT INTO service_resource_requirements "
                    "  (id, tenant_id, service_id, resource_type_id, role, quantity, "
                    "   units_per_resource, attribute_predicate, allocation_strategy, "
                    "   respects_buffers) "
                    "VALUES (:id, :tenant_id, :service_id, :type_id, :role, 1, 1, "
                    "        CAST(:predicate AS jsonb), :strategy, :respects) "
                    "ON CONFLICT (tenant_id, service_id, resource_type_id, role) "
                    "  DO UPDATE SET attribute_predicate = EXCLUDED.attribute_predicate, "
                    "                allocation_strategy = EXCLUDED.allocation_strategy, "
                    "                respects_buffers = EXCLUDED.respects_buffers"
                ),
                {
                    "id": uuid7(),
                    "tenant_id": tenant_id,
                    "service_id": service_id,
                    "type_id": type_ids[requirement["type"]],
                    "role": requirement["role"],
                    "predicate": json.dumps(requirement["predicate"]),
                    "strategy": requirement["strategy"],
                    "respects": requirement["respects_buffers"],
                },
            )

    # Opening hours at the LOCATION level. A rule with no resource_id and no
    # resource_type_id is "the practice is open"; every resource inherits it, so
    # adding a fifth dentist needs no new rule.
    #
    # No natural unique key on availability_rules, so idempotence is
    # delete-then-insert scoped to the location-level rows this script owns.
    await conn.execute(
        text(
            "DELETE FROM availability_rules "
            " WHERE tenant_id = :tenant_id AND location_id = :location_id "
            "   AND resource_id IS NULL AND resource_type_id IS NULL "
            "   AND service_id IS NULL AND label = 'opening_hours'"
        ),
        {"tenant_id": tenant_id, "location_id": location_id},
    )
    for weekdays, opens, closes in _OPENING_HOURS:
        await conn.execute(
            text(
                "INSERT INTO availability_rules "
                "  (id, tenant_id, location_id, effect, weekdays, start_time, "
                "   end_time, effective_period, capacity_override, label) "
                "VALUES (:id, :tenant_id, :location_id, 'open', :weekdays, "
                "        :opens, :closes, "
                "        daterange(DATE '2026-01-01', NULL, '[)'), "
                "        :concurrency, 'opening_hours')"
            ),
            {
                "id": uuid7(),
                "tenant_id": tenant_id,
                "location_id": location_id,
                "weekdays": weekdays,
                "opens": opens,
                "closes": closes,
                "concurrency": _CONCURRENT_APPOINTMENTS,
            },
        )

    for holiday in _HOLIDAYS:
        await conn.execute(
            text(
                "INSERT INTO holidays "
                "  (id, tenant_id, location_id, name, holiday_date, effect, "
                "   recurrence, calendar_code, source) "
                "VALUES (:id, :tenant_id, NULL, :name, :date, 'closed', "
                "        :recurrence, :calendar_code, 'imported') "
                "ON CONFLICT (tenant_id, holiday_date, name) "
                "  WHERE location_id IS NULL "
                "  DO UPDATE SET recurrence = EXCLUDED.recurrence, "
                "                calendar_code = EXCLUDED.calendar_code"
            ),
            {
                "id": uuid7(),
                "tenant_id": tenant_id,
                "name": holiday["name"],
                "date": date.fromisoformat(holiday["date"]),
                "recurrence": holiday["recurrence"],
                "calendar_code": holiday["calendar_code"],
            },
        )

    return {"types": type_ids, "resources": resource_ids, "services": service_ids}


async def reset_demo_bookings(conn: AsyncConnection, tenant_id: UUID) -> int:
    """Delete every booking the demo tenant holds except the seeded fixtures.

    **Repeated verification poisons its own fixture.** Each harness run leaves a
    confirmed booking behind, so after a few dozen runs today's emergency slots
    are full, the agent correctly reports "that slot was just taken", and the
    next person to run a harness reads correct behaviour as a regression. It has
    already cost one round of misdiagnosis.

    Seeded rows (``external_system = 'seed'``) survive: they are what makes the
    diary non-trivial, and a demo against a completely empty calendar proves
    less than one with something already in it.

    Items go first — ``booking_items`` has ``ON DELETE CASCADE`` from
    ``bookings``, so this is belt and braces rather than necessity, but it makes
    the intent legible and keeps the function correct if that FK is ever
    loosened.
    """
    await conn.execute(
        text(
            "DELETE FROM booking_items WHERE tenant_id = :tenant_id "
            "  AND booking_id IN (SELECT id FROM bookings "
            "   WHERE tenant_id = :tenant_id "
            "     AND external_system IS DISTINCT FROM 'seed')"
        ),
        {"tenant_id": tenant_id},
    )
    result = await conn.execute(
        text(
            "DELETE FROM bookings WHERE tenant_id = :tenant_id "
            "  AND external_system IS DISTINCT FROM 'seed'"
        ),
        {"tenant_id": tenant_id},
    )
    # Holds too: an abandoned hold from a crashed run keeps blocking its slot
    # until its TTL passes, which is correct behaviour and confusing in a demo.
    await conn.execute(
        text(
            "DELETE FROM booking_holds WHERE tenant_id = :tenant_id "
            "  AND status = 'active'"
        ),
        {"tenant_id": tenant_id},
    )
    return int(result.rowcount or 0)


async def seed_existing_bookings(
    conn: AsyncConnection,
    tenant_id: UUID,
    location_id: UUID,
    catalogue: dict[str, Any],
) -> list[str]:
    """A rolling few working days of appointments, so the diary looks alive.

    Without these, every slot is free and the solver is never asked the question
    it exists to answer. With them the next working week has real holes in it,
    and the buffered surgery occupancy means the gaps are not where a naive
    reader would guess.

    **Everything is relative to today**, computed at seed time. Fixed dates were
    the previous design and they rot: a demo recorded against "11 August" reads
    as stale within a week and as absurd within a year, and the portal's list
    view — which hides what has already happened — eventually shows an empty
    page on a freshly seeded database.

    **Idempotent by `external_ref`.** Each generated appointment's ref encodes
    its date and slot (`seed-day-2026-08-13-0900`), so a second `make dev` finds
    the row and skips it rather than inserting a twin. The date is *in* the key
    deliberately: tomorrow's run generates different keys for a different set of
    days, and yesterday's rows are removed by `prune_past_bookings` rather than
    being silently reused for a different appointment.

    **One appointment at a time**, because the demo tenant sets
    `_CONCURRENT_APPOINTMENTS = 1`: the practice takes one booking per slot
    site-wide however many clinicians are on the rota. Generating two at once
    would produce a diary the booking engine itself would refuse to create, and
    the calendar would show a state the product cannot reach. The generator
    therefore walks distinct half-hour slots and never repeats one, while
    rotating the clinician so a calendar shows the practice's range rather than
    one person's day.
    """
    from datetime import UTC
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Asia/Kolkata")
    now_local = datetime.now(tz=zone)
    today = now_local.date()

    fixtures = _rolling_booking_plan(today=today, now_local=now_local)

    # Clear yesterday's window before rebuilding today's. `external_ref` is the
    # idempotence key WITHIN one plan, but the plan itself moves: a run on
    # Tuesday generates different refs and dates from a run on Wednesday, so the
    # ref lookup below never matches Tuesday's rows and they sit in the diary
    # blocking the slots Wednesday's plan wants. The result was a hard failure
    # on `ex_booking_items__no_overlap` — the exclusion constraint catching a
    # bug in the seed, which is precisely what it is for.
    await prune_past_bookings(conn, tenant_id)
    await clear_generated_future(conn, tenant_id)

    references: list[str] = []
    for fixture in fixtures:
        # `external_ref` is the idempotence key: re-seeding finds the existing
        # row rather than inserting a second appointment into the same slot,
        # which the exclusion constraint would (correctly) refuse anyway.
        existing = (
            await conn.execute(
                text(
                    "SELECT reference FROM bookings "
                    " WHERE tenant_id = :tenant_id AND external_system = 'seed' "
                    "   AND external_ref = :ref"
                ),
                {"tenant_id": tenant_id, "ref": fixture["external"]},
            )
        ).scalar_one_or_none()
        if existing is not None:
            references.append(existing)
            continue

        service_row = (
            await conn.execute(
                text(
                    "SELECT id, duration_minutes, buffer_after_minutes, price "
                    "  FROM services WHERE tenant_id = :tenant_id AND code = :code"
                ),
                {"tenant_id": tenant_id, "code": fixture["service"]},
            )
        ).mappings().one()

        target = fixture["local_date"]
        local_start = datetime.combine(target, fixture["local_time"], tzinfo=zone)
        start = local_start.astimezone(UTC)
        end = start + timedelta(minutes=service_row["duration_minutes"])
        reference = _seed_reference(fixture["external"])

        booking_id = (
            await conn.execute(
                text(
                    "INSERT INTO bookings "
                    "  (id, tenant_id, location_id, service_id, reference, status, "
                    "   service_period, buffer_after_minutes, local_date, "
                    "   customer_name, customer_phone_e164, channel, booked_by, "
                    "   price_total, currency, cancellation_window_minutes, "
                    "   cancellation_fee, confirmed_at, external_system, external_ref) "
                    "VALUES (:id, :tenant_id, :location_id, :service_id, :reference, "
                    "        'confirmed', tstzrange(:start, :end, '[)'), :buffer_after, "
                    "        :local_date, :name, :phone, 'voice_inbound', 'staff', "
                    "        :price, 'INR', 1440, 25.0000, now(), 'seed', :external)"
                    "RETURNING id"
                ),
                {
                    "id": uuid7(),
                    "tenant_id": tenant_id,
                    "location_id": location_id,
                    "service_id": service_row["id"],
                    "reference": reference,
                    "start": start,
                    "end": end,
                    "buffer_after": service_row["buffer_after_minutes"],
                    "local_date": target,
                    "name": fixture["name"],
                    "phone": fixture["phone"],
                    "price": service_row["price"],
                    "external": fixture["external"],
                },
            )
        ).scalar_one()

        # One item per allocated resource. `occupies` is passed as the booking's
        # own service_period and immediately overwritten by
        # trg_booking_items_sync_occupancy, which derives the real range from the
        # requirement's respects_buffers — so the clinician gets the service
        # range and the surgery gets the buffered one, from one insert.
        for role, resource_code in (
            ("primary", fixture["clinician"]),
            ("space", fixture["room"]),
        ):
            requirement_id = (
                await conn.execute(
                    text(
                        "SELECT id FROM service_resource_requirements "
                        " WHERE tenant_id = :tenant_id AND service_id = :service_id "
                        "   AND role = :role"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "service_id": service_row["id"],
                        "role": role,
                    },
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO booking_items "
                    "  (id, tenant_id, booking_id, resource_id, role, "
                    "   requirement_id, quantity, occupies) "
                    "VALUES (:id, :tenant_id, :booking_id, :resource_id, :role, "
                    "        :requirement_id, 1, tstzrange(:start, :end, '[)'))"
                ),
                {
                    "id": uuid7(),
                    "tenant_id": tenant_id,
                    "booking_id": booking_id,
                    "resource_id": catalogue["resources"][resource_code],
                    "role": role,
                    "requirement_id": requirement_id,
                    "start": start,
                    "end": end,
                },
            )

        references.append(reference)

    return references


def _seed_reference(external_ref: str) -> str:
    """A stable, human-quotable reference for a seeded booking.

    Derived from the external ref rather than random, so a re-seed against a
    wiped database produces the same reference and any documentation quoting it
    stays true. Real bookings use ``new_reference()``, which is random for the
    enumeration reason documented there.

    Hashed rather than sliced. The refs now encode a date and a slot
    (``seed-day-2026-08-13-0900``), and taking the last segment would give every
    9am appointment in the window the same ``BK-SEED0900`` — which
    ``uq_bookings__tenant_reference`` would reject on the second one, failing the
    seed for a reason that has nothing to do with the data.
    """
    import hashlib

    digest = hashlib.sha256(external_ref.encode()).hexdigest().upper()
    # Same alphabet discipline as `new_reference`: no I/O/0/1 to confuse anyone
    # reading a reference back over the phone.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    body = "".join(
        alphabet[int(digest[i : i + 2], 16) % len(alphabet)] for i in range(0, 8, 2)
    )
    return f"BK-SD{body}"


#: The rolling window the demo diary fills: today plus this many days, walked
#: for working days only. Five to seven working days is what the brief asks for
#: and is also what a receptionist can plausibly see ahead.
_ROLLING_WORKING_DAYS = 6

#: Half-hour starts a generated appointment may take, inside 09:00-18:00. Left
#: deliberately sparse rather than filling all eighteen: a diary with every slot
#: gone shows the agent no availability to offer, and a demo where the assistant
#: can never book anything is worse than one with a few gaps.
_SEED_SLOT_TIMES: tuple[time, ...] = (
    time(9, 0),
    time(10, 0),
    time(11, 30),
    time(13, 0),
    time(14, 30),
    time(16, 0),
    time(17, 0),
)

#: Rotated through so a week shows the practice's range rather than one
#: person's list. Each is paired with a service that clinician's `skills`
#: actually cover — Marta is the only hygienist, so hygiene visits are hers.
_SEED_ROTA: tuple[tuple[str, str, str], ...] = (
    ("dr-rahman", "checkup", "surgery-1"),
    ("hyg-kowalczyk", "hygienist", "surgery-2"),
    ("dr-okafor", "new-patient-exam", "surgery-3"),
    ("dr-rahman", "emergency", "surgery-1"),
    ("dr-whitfield", "checkup", "surgery-2"),
    ("hyg-kowalczyk", "hygienist", "surgery-1"),
    ("dr-okafor", "checkup", "surgery-3"),
)

#: Patients for the generated diary. Drawn from the same realistic pool the
#: rest of the seed uses, for the reason stated at the top of this file: names
#: like "Test Patient" hide the layout bugs a demo is supposed to surface.
_SEED_PATIENTS: tuple[tuple[str, str], ...] = (
    # Numbers use the +91 9xxxx 5xxxx block: 9 is a real Indian mobile prefix,
    # so these exercise the same validation path a caller's number takes, while
    # the 5-series subscriber range is not allocated to a live operator. A seed
    # that dials a real person is a seed nobody can safely demo.
    ("Priya Shah", "+919812350112"),
    ("Rohit Bhatia", "+919812350233"),
    ("Ananya Menon", "+919812350344"),
    ("Karan Malhotra", "+919812350027"),
    ("Fatima Sheikh", "+919812350771"),
    ("Yusuf Ansari", "+919812350486"),
    ("Divya Krishnan", "+919812350740"),
    ("Arjun Nair", "+919812350158"),
    ("Sneha Kulkarni", "+919812350817"),
    ("Vikram Reddy", "+919812350629"),
    ("Meghna Joshi", "+919812350394"),
    ("Aditya Rao", "+919812350205"),
)


def _rolling_booking_plan(
    *, today: date, now_local: datetime
) -> tuple[dict[str, Any], ...]:
    """The appointments to plant, relative to today.

    Deterministic: same day in, same plan out, so re-running the seed produces
    the identical set and the `external_ref` check finds every row. No RNG —
    a random plan would make "did re-seeding change the diary?" unanswerable.

    Two rules the booking engine would otherwise enforce for us, applied here so
    the seed never writes a diary the product could not have produced:

    1. **One appointment per slot, site-wide** (`_CONCURRENT_APPOINTMENTS`).
       Each (day, time) is used at most once.
    2. **Nothing in the past.** Today's already-finished slots are skipped, so a
       seed run at 4pm does not plant a 9am appointment that the list view would
       immediately hide and the agent could never have taken.
    """
    plan: list[dict[str, Any]] = []
    slot_index = 0
    day = today
    working_days = 0

    while working_days < _ROLLING_WORKING_DAYS:
        # Mon-Sat, matching `_OPENING_HOURS`. Sunday is closed, and planting an
        # appointment on a closed day would contradict the availability rules.
        if day.isoweekday() <= 6:
            working_days += 1
            for slot_time in _SEED_SLOT_TIMES:
                # Skip anything already past — including on today, where a slot
                # earlier than the current hour is history the moment it lands.
                if day == today and slot_time <= now_local.time():
                    continue

                clinician, service, room = _SEED_ROTA[slot_index % len(_SEED_ROTA)]
                name, phone = _SEED_PATIENTS[slot_index % len(_SEED_PATIENTS)]
                slot_index += 1

                plan.append(
                    {
                        "service": service,
                        "clinician": clinician,
                        "room": room,
                        "local_date": day,
                        "local_time": slot_time,
                        "name": name,
                        "phone": phone,
                        # The date and slot ARE the key: tomorrow's run keys a
                        # different set of days, and the row for a day that has
                        # passed is pruned rather than reused.
                        "external": f"seed-day-{day.isoformat()}-{slot_time:%H%M}",
                    }
                )
        day += timedelta(days=1)

    return tuple(plan)


async def prune_past_bookings(conn: AsyncConnection, tenant_id: UUID) -> int:
    """Delete seeded appointments whose day has passed.

    The other half of "the demo keeps looking alive": generating a rolling
    window forward is pointless if last month's rows accumulate behind it. A
    demo database that has been seeded weekly for a year would otherwise hold
    hundreds of stale appointments, and the list view's "include past" toggle
    would open onto a wall of them.

    **Only `external_system = 'seed'` rows.** A booking a human made through the
    demo, or one the agent took during a conversation, is evidence of something
    that happened and is not the seed's to delete —
    :func:`reset_demo_bookings` already owns that decision and applies the
    opposite rule.

    Compares on ``local_date`` against the location's own today, not on
    ``now()``: "has this day passed" is a question about the business's
    calendar, and a UTC comparison would delete this evening's appointments for
    any practice west of Greenwich.
    """
    result = await conn.execute(
        text(
            "DELETE FROM bookings b "
            " USING locations l "
            " WHERE b.tenant_id = :tenant_id "
            "   AND l.id = b.location_id AND l.tenant_id = b.tenant_id "
            "   AND b.external_system = 'seed' "
            "   AND b.local_date < (now() AT TIME ZONE l.timezone)::date"
        ),
        {"tenant_id": tenant_id},
    )
    return int(result.rowcount or 0)


async def clear_generated_future(conn: AsyncConnection, tenant_id: UUID) -> int:
    """Delete FUTURE seeded appointments, so the rolling window can be rebuilt.

    Without this the seed is not idempotent — it is a duplicate generator. The
    second run recomputes the same rolling window, tries to insert a booking
    into a slot its own previous run already filled, and dies on
    ``ex_booking_items__no_overlap``:

        conflicting key value violates exclusion constraint
        Key (tenant_id, resource_id, occupies)=(…, ["2026-08-13 08:00:00+00",
        "2026-08-13 08:30:00+00")) conflicts with existing key (…, same range)

    The exclusion constraint was right and the seed was wrong: it deleted past
    seeded rows and unmarked rows, but never the future seeded rows it was
    about to recreate.

    Deliberately narrower than :func:`reset_demo_bookings`. Only
    ``external_system = 'seed'`` rows go, so an appointment a human booked
    through the demo — or one the agent took during a conversation — survives
    a re-seed. Those are evidence of something that happened.
    """
    result = await conn.execute(
        text(
            "DELETE FROM bookings b "
            " USING locations l "
            " WHERE b.tenant_id = :tenant_id "
            "   AND l.id = b.location_id AND l.tenant_id = b.tenant_id "
            "   AND b.external_system = 'seed' "
            "   AND b.local_date >= (now() AT TIME ZONE l.timezone)::date"
        ),
        {"tenant_id": tenant_id},
    )
    return int(result.rowcount or 0)


async def seed_other_tenant_conversation(
    conn: AsyncConnection, tenant_id: UUID
) -> UUID:
    """A conversation in the *other* tenant, with a recognizable secret in it.

    Exists purely so the isolation test has something real to fail to reach. A
    test that asserts "the demo endpoint returned 404 for a random UUID" proves
    nothing — a random UUID is 404 because it does not exist. This row does
    exist, in another tenant, and the demo session must still get a 404.
    """
    existing = (
        await conn.execute(
            text(
                "SELECT id FROM conversations "
                " WHERE tenant_id = :tenant_id AND external_ref = 'isolation-probe' "
                " ORDER BY created_at DESC LIMIT 1"
            ),
            {"tenant_id": tenant_id},
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    conversation_id = uuid7()
    await conn.execute(
        text(
            "INSERT INTO conversations (id, tenant_id, channel, status, "
            "                           external_ref, agent_code, agent_version) "
            "VALUES (:id, :tenant_id, 'web_chat', 'active', 'isolation-probe', "
            "        'medical', '1.0.0')"
        ),
        {"id": conversation_id, "tenant_id": tenant_id},
    )
    await conn.execute(
        text(
            "INSERT INTO turns (id, tenant_id, conversation_id, sequence, role, "
            "                   content) "
            "VALUES (:id, :tenant_id, :conversation_id, 0, 'user', "
            "        'RIVERSIDE-SECRET-must-never-appear-in-a-demo-response')"
        ),
        {
            "id": uuid7(),
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
        },
    )
    return conversation_id


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


#: Tables this script writes that carry `FORCE ROW LEVEL SECURITY`.
#:
#: FORCE means the policies apply *even to the table owner* (contract §2.2), and
#: the policies name only `receptionai_app`, `receptionai_reporting` and
#: `receptionai_ops` — so the migrator, which owns the tables, matches no policy
#: and can write nothing. That is correct: provisioning a tenant is deliberately
#: not something any runtime role can do, because `tenants`' policy is
#: `id = app_current_tenant()` and there is no tenant yet.
#:
#: So the seed lifts FORCE, writes, and restores it, as the owner, inside one
#: transaction. Three things make that safe rather than a hole:
#:
#:   * `ALTER TABLE ... NO FORCE` is transactional in PostgreSQL, so a crash
#:     rolls it back — the window cannot be left open by a failure.
#:   * It requires table ownership, which only the migrator credential has. The
#:     application role cannot do this, and that is the property that matters.
#:   * FORCE is restored in a `finally`, and `verify_force_restored` asserts it
#:     afterwards on a fresh connection, so "the seed silently left RLS off" is
#:     a condition the script itself refuses to exit with.
#:
#: In production this whole dance is one `SECURITY DEFINER` provisioning
#: function granted to a signup role (01 §11.4). This is the local equivalent,
#: and doing it here rather than weakening a policy keeps every policy meaning
#: exactly what it says.
_FORCED_TABLES: tuple[str, ...] = (
    "tenants",
    "locations",
    "tenant_settings",
    "users",
    "roles",
    "role_permissions",
    "user_tenant_roles",
    "conversations",
    "turns",
    # The booking engine (migration 0003). Same reasoning as above: these are
    # FORCE'd, the migrator owns them and matches no policy, so seeding the
    # catalogue needs the lift-and-restore.
    "resource_types",
    "resources",
    "services",
    "service_resource_requirements",
    "availability_rules",
    "holidays",
    "bookings",
    "booking_items",
)


async def _set_force_rls(conn: AsyncConnection, *, enabled: bool) -> None:
    keyword = "FORCE" if enabled else "NO FORCE"
    for table in _FORCED_TABLES:
        # Identifiers cannot be bound as parameters. These names are
        # module-level constants, never request data.
        await conn.execute(text(f"ALTER TABLE {table} {keyword} ROW LEVEL SECURITY"))


async def verify_force_restored(dsn: str) -> None:
    """Assert every table is FORCE'd again, on a connection of its own.

    A separate connection on purpose: checking inside the seed transaction
    would read that transaction's own uncommitted state and pass regardless.
    This is the check that makes the lift-and-restore above trustworthy rather
    than merely intended.
    """
    engine = create_async_engine(dsn, echo=False)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT relname FROM pg_class c "
                        "  JOIN pg_namespace n ON n.oid = c.relnamespace "
                        " WHERE n.nspname = 'public' "
                        "   AND c.relname = ANY(:tables) "
                        "   AND NOT c.relforcerowsecurity"
                    ),
                    {"tables": list(_FORCED_TABLES)},
                )
            ).scalars().all()
        if rows:
            raise RuntimeError(
                f"FORCE ROW LEVEL SECURITY was NOT restored on: {sorted(rows)}. "
                f"The database is in an unsafe state — run "
                f"`ALTER TABLE <t> FORCE ROW LEVEL SECURITY` on each before "
                f"serving traffic."
            )
    finally:
        await engine.dispose()


async def seed(dsn: str, *, reset: bool = False) -> dict[str, Any]:
    engine = create_async_engine(dsn, echo=False)
    try:
        async with engine.begin() as conn:
            await _set_force_rls(conn, enabled=False)
            permission_ids = await seed_permissions(conn)
            role_ids = await seed_platform_roles(conn, permission_ids)

            tenant_id = await seed_tenant(
                conn,
                slug=DEMO_TENANT_SLUG,
                display_name=DEMO_TENANT_NAME,
                legal_name="Sunrise Dental Care Pvt Ltd",
            )
            location_id = await seed_location(
                conn, tenant_id, "Sunrise Dental — Pune"
            )
            manifest_version = await install_manifest(conn, tenant_id, "medical")
            await seed_customization(
                conn,
                tenant_id,
                "Sunrise Dental is a family practice in Pune with three "
                "dentists and one hygienist. We have been on Baner Road "
                "since 2009. Parking is free in the basement.",
            )
            user_id = await seed_user(
                conn,
                tenant_id=tenant_id,
                role_id=role_ids["owner"],
                email=DEMO_EMAIL,
                password=DEMO_PASSWORD,
                full_name="Demo Owner",
            )

            removed = 0
            if reset:
                removed = await reset_demo_bookings(conn, tenant_id)
            catalogue = await seed_booking_engine(conn, tenant_id, location_id)
            # Prune BEFORE generating. Yesterday's seeded rows are gone by the
            # time today's window is planted, so the diary rolls forward rather
            # than accumulating — and the two never contend for a slot, since
            # the pruned days are strictly before the generated ones.
            pruned = await prune_past_bookings(conn, tenant_id)
            booking_references = await seed_existing_bookings(
                conn, tenant_id, location_id, catalogue
            )

            other_tenant_id = await seed_tenant(
                conn,
                slug=OTHER_TENANT_SLUG,
                display_name=OTHER_TENANT_NAME,
                legal_name="Riverside Dental Care Ltd",
            )
            await install_manifest(conn, other_tenant_id, "medical")
            other_conversation_id = await seed_other_tenant_conversation(
                conn, other_tenant_id
            )

            # Restored inside the same transaction, so a failure anywhere above
            # rolls the lift back rather than leaving it applied.
            await _set_force_rls(conn, enabled=True)

        # And verified on a fresh connection, outside the transaction — see
        # `verify_force_restored` for why that separation is the point.
        await verify_force_restored(dsn)

        return {
            "tenant_id": str(tenant_id),
            "tenant_slug": DEMO_TENANT_SLUG,
            "location_id": str(location_id),
            "services": sorted(catalogue["services"]),
            "resources": sorted(catalogue["resources"]),
            "seeded_bookings": booking_references,
            "removed_bookings": removed,
            "pruned_past_bookings": pruned,
            "user_id": str(user_id),
            "email": DEMO_EMAIL,
            "password": DEMO_PASSWORD,
            "manifest": manifest_version,
            "other_tenant_id": str(other_tenant_id),
            "other_conversation_id": str(other_conversation_id),
        }
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=None,
        help="SQLAlchemy async DSN. Defaults to the migrator role on localhost.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the result as JSON only."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Delete every booking except the seeded fixtures first. Use before "
            "a verification run: harness bookings accumulate, fill the diary, "
            "and make correct 'that slot was taken' behaviour look like a bug."
        ),
    )
    args = parser.parse_args()

    result = asyncio.run(seed(args.dsn or migration_dsn(), reset=args.reset))

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print("Seeded the demo tenant.\n")
    print(f"  tenant       {result['tenant_slug']}  ({result['tenant_id']})")
    print(f"  manifest     {result['manifest']}")
    print(f"  login        {result['email']} / {result['password']}")
    print()
    if result.get("removed_bookings"):
        print(f"  reset        removed {result['removed_bookings']} test booking(s)")
    print("  booking engine:")
    print(f"    services     {', '.join(result['services'])}")
    print(f"    resources    {', '.join(result['resources'])}")
    print(f"    in the diary {', '.join(result['seeded_bookings'])}")
    print()
    print("  isolation fixture (must be unreachable from the demo):")
    print(f"    tenant       {OTHER_TENANT_SLUG}  ({result['other_tenant_id']})")
    print(f"    conversation {result['other_conversation_id']}")
    print()
    print("Try it:")
    print("  curl -s -XPOST localhost:8000/api/v1/public/demo/sessions | jq")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

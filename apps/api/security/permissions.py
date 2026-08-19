"""The permission vocabulary and role grid — 05 §A2.2, §A2.3.

The Phase 1 slice. 05 §A2.2 lists the full vocabulary across billing,
knowledge, connectors and channels; the codes below are the ones Phase 1
routes actually check plus the ones the seeded role grid needs to be coherent.
Codes not yet enforced anywhere are deliberately absent — a permission with no
route behind it is a promise the platform has not made, and seeding it into a
role suggests a capability that does not exist.

**Why this is not in ``packages/core``.** 05 §A2.2 puts it at
``packages/core/authz/permissions.py``, and that is right for the *long* run,
when the worker and the connector SDK also check permissions. Phase 1 has one
consumer — the route dependency in this package — and a shared module with one
importer is speculation. The strings themselves are the compatibility surface
(they are stored in ``permissions.code`` rows), and those are unchanged, so
moving this file later is a rename with no data migration.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = ["ROLE_GRANTS", "STEP_UP_REQUIRED", "Perm", "permission_catalogue"]


class Perm(StrEnum):
    """Permission codes. ``<resource>:<action>``, matching the DB CHECK."""

    # ── Tenant & org ──────────────────────────────────────────────────────
    TENANT_READ = "tenant:read"
    TENANT_UPDATE = "tenant:update"
    TENANT_DELETE = "tenant:delete"
    MEMBERS_READ = "members:read"
    MEMBERS_INVITE = "members:invite"
    MEMBERS_UPDATE_ROLE = "members:update_role"
    MEMBERS_REMOVE = "members:remove"
    ROLES_MANAGE = "roles:manage"

    # ── Usage ─────────────────────────────────────────────────────────────
    USAGE_READ = "usage:read"

    # ── Conversations ─────────────────────────────────────────────────────
    CONVERSATIONS_READ = "conversations:read"
    CONVERSATIONS_READ_PII = "conversations:read_pii"
    CONVERSATIONS_WRITE = "conversations:write"
    CONVERSATIONS_EXPORT = "conversations:export"
    CONVERSATIONS_DELETE = "conversations:delete"

    # ── Bookings ──────────────────────────────────────────────────────────
    BOOKINGS_READ = "bookings:read"

    # ── Agents / verticals ────────────────────────────────────────────────
    AGENTS_READ = "agents:read"
    AGENTS_UPDATE = "agents:update"
    AGENTS_PUBLISH = "agents:publish"


#: Permissions whose use requires a fresh MFA assertion — 05 §A2.2, §A1.6.
#: The route dependency enforces ``aal >= 2`` and an auth age under 15 minutes
#: for any route requiring one of these.
STEP_UP_REQUIRED: Final[frozenset[Perm]] = frozenset(
    {
        Perm.TENANT_DELETE,
        Perm.ROLES_MANAGE,
        Perm.MEMBERS_UPDATE_ROLE,
        Perm.CONVERSATIONS_EXPORT,
    }
)


#: The standard role grid — 05 §A2.3. Read by ``tools/seed_demo.py`` to
#: populate ``roles`` and ``role_permissions``, so the table and this table
#: cannot disagree: there is one definition and the database is derived from it.
ROLE_GRANTS: Final[dict[str, frozenset[Perm]]] = {
    "owner": frozenset(Perm),
    "admin": frozenset(
        {
            Perm.TENANT_READ,
            Perm.TENANT_UPDATE,
            Perm.MEMBERS_READ,
            Perm.MEMBERS_INVITE,
            Perm.MEMBERS_UPDATE_ROLE,
            Perm.MEMBERS_REMOVE,
            Perm.USAGE_READ,
            Perm.CONVERSATIONS_READ,
            Perm.CONVERSATIONS_READ_PII,
            Perm.CONVERSATIONS_WRITE,
            Perm.CONVERSATIONS_EXPORT,
            Perm.CONVERSATIONS_DELETE,
            Perm.BOOKINGS_READ,
            Perm.AGENTS_READ,
            Perm.AGENTS_UPDATE,
            Perm.AGENTS_PUBLISH,
        }
    ),
    "manager": frozenset(
        {
            Perm.TENANT_READ,
            Perm.MEMBERS_READ,
            Perm.MEMBERS_INVITE,
            Perm.USAGE_READ,
            Perm.CONVERSATIONS_READ,
            Perm.CONVERSATIONS_READ_PII,
            Perm.CONVERSATIONS_WRITE,
            Perm.BOOKINGS_READ,
            Perm.AGENTS_READ,
        }
    ),
    "agent": frozenset(
        {
            Perm.TENANT_READ,
            Perm.CONVERSATIONS_READ,
            Perm.CONVERSATIONS_READ_PII,
            Perm.CONVERSATIONS_WRITE,
            # The diary is the front desk's core screen: an agent who can take
            # a call about an appointment must be able to see it.
            Perm.BOOKINGS_READ,
            Perm.AGENTS_READ,
        }
    ),
    "viewer": frozenset(
        {
            Perm.TENANT_READ,
            Perm.USAGE_READ,
            # Note: no CONVERSATIONS_READ_PII. A viewer sees that a
            # conversation happened and how it went, with the transcript
            # masked (05 §A2.3). That asymmetry is the entire reason the two
            # permissions are separate rather than one.
            Perm.CONVERSATIONS_READ,
            # Same asymmetry applied to the diary: a viewer sees that the day
            # is busy and which resource is booked, but the customer's name and
            # number are masked in the response unless they also hold
            # `conversations:read_pii`. The list route enforces that; the grant
            # here only says the route is reachable.
            Perm.BOOKINGS_READ,
            Perm.AGENTS_READ,
        }
    ),
}

#: Rank drives "can this role manage that role" — a user may only assign roles
#: at or below their own. Matches ``roles.rank``.
ROLE_RANKS: Final[dict[str, int]] = {
    "owner": 100,
    "admin": 80,
    "manager": 60,
    "agent": 40,
    "viewer": 20,
}


def permission_catalogue() -> list[tuple[str, str, str, str, bool]]:
    """Every permission as ``(code, resource, action, description, sensitive)``.

    The shape ``tools/seed_demo.py`` inserts into ``permissions``. Derived from
    the enum rather than written out again, so a code added above cannot be
    missing from the catalogue.
    """
    descriptions = {
        Perm.TENANT_READ: "View tenant settings and profile",
        Perm.TENANT_UPDATE: "Change tenant settings",
        Perm.TENANT_DELETE: "Delete the tenant and all of its data",
        Perm.MEMBERS_READ: "List members of the tenant",
        Perm.MEMBERS_INVITE: "Invite a new member",
        Perm.MEMBERS_UPDATE_ROLE: "Change a member's role",
        Perm.MEMBERS_REMOVE: "Remove a member",
        Perm.ROLES_MANAGE: "Create and edit custom roles",
        Perm.USAGE_READ: "View usage and cost reporting",
        Perm.CONVERSATIONS_READ: "View conversations with transcripts masked",
        Perm.CONVERSATIONS_READ_PII: "View unmasked conversation transcripts",
        Perm.CONVERSATIONS_WRITE: "Send messages into a conversation",
        Perm.CONVERSATIONS_EXPORT: "Bulk-export conversations",
        Perm.CONVERSATIONS_DELETE: "Delete conversations",
        Perm.BOOKINGS_READ: "View the booking diary",
        Perm.AGENTS_READ: "View the agent configuration",
        Perm.AGENTS_UPDATE: "Edit the agent configuration",
        Perm.AGENTS_PUBLISH: "Publish a new agent version",
    }
    catalogue = []
    for perm in Perm:
        resource, _, action = perm.value.partition(":")
        catalogue.append(
            (
                perm.value,
                resource,
                action,
                descriptions[perm],
                perm in STEP_UP_REQUIRED,
            )
        )
    return catalogue

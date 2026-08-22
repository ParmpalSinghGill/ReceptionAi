"""Fixtures for the API suite.

**Nothing here touches the network.** Every test runs against
:class:`~packages.providers.llm.stub_adapter.StubLLMAdapter`, which implements
the same ``LLMPort`` the real adapters do — so the assertions are about our
framing, our persistence and our authorization, not about a provider's mood.

The database and Redis are real, because the properties under test are
database properties. An RLS isolation proof against a mocked repository proves
that the mock returns what it was told to. These tests are marked
``integration`` and skip cleanly when the stack is not up, so ``make test-unit``
stays runnable offline.
"""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration


def _reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


POSTGRES_PORT = int(_env("POSTGRES_PORT", "55432"))
REDIS_PORT = int(_env("REDIS_PORT", "56379"))

_STACK_UP = _reachable("localhost", POSTGRES_PORT) and _reachable(
    "localhost", REDIS_PORT
)

requires_stack = pytest.mark.skipif(
    not _STACK_UP,
    reason=(
        f"needs the local stack: Postgres on {POSTGRES_PORT} and Redis on "
        f"{REDIS_PORT}. Run `make up && make migrate && "
        f"python3 tools/seed_demo.py`."
    ),
)


@pytest.fixture(scope="session")
def settings():
    """Test settings: the real stack, the stub LLM, no token delay.

    ``stub_token_delay_ms=0`` matters. The app default is non-zero so the local
    UI exercises incremental rendering, but a delay per token would add
    seconds to the suite for no assertion.
    """
    from apps.api.settings import Settings

    return Settings(
        env="test",
        postgres_port=POSTGRES_PORT,
        redis_port=REDIS_PORT,
        llm_provider="stub",
        stub_token_delay_ms=0,
        # A short heartbeat so a test can observe one without waiting 15s.
        sse_heartbeat_seconds=0.2,
        sse_idle_timeout_seconds=3.0,
        # Effectively off: these tests hammer the same endpoints from one IP,
        # and the limiter has its own test that sets a real rule.
        demo_rate_burst=10_000,
        demo_rate_per_second=10_000.0,
        ai_rate_burst=10_000,
        ai_rate_per_second=10_000.0,
    )


@pytest_asyncio.fixture(scope="session")
async def app(settings):
    """The real application, lifespan included.

    Session-scoped: the lifespan builds a connection pool and compiles graphs,
    and paying that per test would dominate the run.
    """
    from apps.api.main import create_app

    application = create_app(settings)
    async with _Lifespan(application):
        yield application


class _Lifespan:
    """Run an app's lifespan around a block.

    Hand-rolled rather than pulling in ``asgi-lifespan``: it is fifteen lines,
    and a test-only dependency that exists to call two methods is a dependency
    someone has to keep current.
    """

    def __init__(self, app) -> None:
        self._app = app
        self._ctx = None

    async def __aenter__(self):
        self._ctx = self._app.router.lifespan_context(self._app)
        await self._ctx.__aenter__()
        return self._app

    async def __aexit__(self, *exc) -> None:
        assert self._ctx is not None
        await self._ctx.__aexit__(*exc)


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    """An HTTP client speaking directly to the ASGI app.

    No socket, no port, no server process — so tests are fast and cannot
    collide on a port with a developer's running instance.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as http:
        yield http


@pytest.fixture(scope="session")
def demo_tenant_id(app) -> UUID:
    """The seeded demo tenant, as the app resolved it.

    Skips rather than fails when the seed has not run: a missing fixture is a
    setup problem, and reporting it as a test failure sends people looking in
    the wrong place.
    """
    tenant_id = app.state.app_state.demo_tenant_id
    if tenant_id is None:
        pytest.skip("demo tenant is not seeded; run `python3 tools/seed_demo.py`")
    return tenant_id


#: Credentials the seed has used for the demo owner, newest first.
#:
#: Resolved by trying each rather than hardcoding one, because the demo tenant
#: has been renamed once already (Northgate Dental → Sunrise Dental) and the
#: hardcoded pair silently turned every test that depends on this fixture into
#: a SKIP. A suite that skips is worse than one that fails: it reports green
#: while asserting nothing, and five of the six booking tests sat in that state
#: undetected. Listing the alternatives keeps the fixture working across a
#: rename instead of rotting on the next one.
_DEMO_LOGINS: tuple[tuple[str, str], ...] = (
    ("demo@sunrisedental.in", "sunrise-dental"),
    ("demo@northgate.dental", "northgate-dental"),
)


@pytest_asyncio.fixture
async def access_token(client) -> str:
    """A bearer token for the seeded demo owner."""
    attempts = []
    for email, tenant_slug in _DEMO_LOGINS:
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": email,
                "password": "demo1234",
                "tenant_slug": tenant_slug,
            },
        )
        if response.status_code == 200:
            return str(response.json()["access_token"])
        attempts.append(f"{email}@{tenant_slug} -> {response.status_code}")

    pytest.skip(
        "cannot log in as the seeded demo user ("
        + "; ".join(attempts)
        + "); run `python3 tools/seed_demo.py`"
    )


@pytest.fixture
def auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


@pytest.fixture
def viewer_headers(app, access_token: str) -> dict[str, str]:
    """A bearer token for the same tenant and user, carrying the VIEWER role.

    Minted directly from the app's own :class:`TokenService` rather than by
    logging in as a second seeded user, for two reasons. The seed does not
    create one, so a login-based fixture would skip everywhere and the masking
    tests would quietly never run. And the property under test is a property of
    the *permission set*, not of a particular row in ``users`` — deriving the
    token from ``ROLE_GRANTS["viewer"]`` means it stays correct if the
    role's grants are edited, which is exactly when a masking regression would
    otherwise slip through.

    The claims are otherwise identical to the owner's, so a test comparing the
    two responses is isolating one variable: ``conversations:read_pii``.
    """
    from apps.api.security.permissions import ROLE_GRANTS

    tokens = app.state.app_state.tokens
    claims = tokens.verify_access(access_token)

    viewer_token, _ = tokens.mint_access(
        user_id=claims.sub,
        tenant_id=claims.tid,
        tenant_plan=claims.tsl,
        roles=("viewer",),
        permissions=frozenset(perm.value for perm in ROLE_GRANTS["viewer"]),
        aal=claims.aal,
    )
    return {"Authorization": f"Bearer {viewer_token}"}


@pytest_asyncio.fixture
async def demo_session(client) -> dict:
    """A started demo conversation: ``conversation_id``, ``ticket``, ``greeting``."""
    response = await client.post("/api/v1/public/demo/sessions")
    assert response.status_code == 201, response.text
    return dict(response.json())


@pytest.fixture(scope="session")
def other_tenant_conversation_id() -> UUID:
    """A conversation belonging to a DIFFERENT tenant.

    Read straight from the database as the superuser, deliberately bypassing
    the application entirely — the point of the isolation test is that this id
    is real and unreachable, and asking the app for it would be circular.
    """
    import psycopg2

    dsn = (
        f"host=localhost port={POSTGRES_PORT} dbname="
        f"{_env('POSTGRES_DB', 'receptionai')} user="
        f"{_env('POSTGRES_SUPERUSER', 'postgres')} password="
        f"{_env('POSTGRES_PASSWORD', 'localpass')}"
    )
    try:
        with psycopg2.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM conversations WHERE external_ref = 'isolation-probe' "
                "ORDER BY created_at DESC LIMIT 1"
            )
            row = cursor.fetchone()
    except Exception as exc:
        pytest.skip(f"cannot read the isolation fixture: {exc}")

    if row is None:
        pytest.skip(
            "the isolation fixture is missing; run `python3 tools/seed_demo.py`"
        )
    return UUID(str(row[0]))

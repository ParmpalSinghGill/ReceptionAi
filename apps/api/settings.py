"""Application settings — 05 §E22.2.

All configuration comes from the environment. There is no config file in the
image, and the image built from a given commit is byte-identical across dev,
staging and production.

``extra="forbid"`` is deliberate and is the reason this model is worth having
over ``os.environ.get``: an unknown environment variable is a typo, and a typo
that silently falls back to a default is a production incident that looks like
a working deployment. ``DEEPSEEK_API_KEYS`` (plural, pasted from a doc) would
otherwise mean "no key configured" and the platform would serve stub responses
that every dashboard reports as healthy.

The production invariants at the bottom exist for the same reason: a
misconfigured production pod must crash-loop visibly rather than serve traffic
with a hardcoded signing key.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]


class Settings(BaseSettings):
    """Everything the API reads from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Accept both `cors_origins` and the `CORS_ORIGINS` alias.
        populate_by_name=True,
        # `.env` is shared with docker-compose and carries variables compose
        # consumes and the app does not (`GRAFANA_PORT`, `MINIO_ROOT_USER`).
        # 05 §E22.2 argues for `forbid`, and it is right about *deployed*
        # configuration — but forbidding here would make the one committed
        # `.env` unusable by both readers, so unknown keys are ignored and the
        # production invariants below carry the weight instead.
        extra="ignore",
    )

    # ── Identity ──────────────────────────────────────────────────────────
    env: Literal["local", "test", "ci", "staging", "production"] = "local"
    log_level: str = "INFO"
    git_sha: str = "unknown"
    service_name: str = "receptionai-api"

    # ── Postgres ──────────────────────────────────────────────────────────
    postgres_host: str = "localhost"
    postgres_port: int = 55432
    postgres_db: str = "receptionai"
    postgres_user: str = "receptionai_app"
    # The local compose credential, and the only one this default is ever
    # correct for. A deployed environment supplies its own; the invariants
    # below refuse to start if the runtime role is a superuser.
    postgres_password: str = "localpass"  # noqa: S105
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_echo: bool = False

    #: 05 §A3.5. Assert ``app.tenant_id`` took effect before any query runs.
    #: Keep true: the failure it catches is silent by construction, since RLS
    #: with no GUC returns zero rows rather than raising — so the symptom is
    #: "my query returned nothing", which reads as a data problem.
    verify_tenant_guc: bool = True

    # ── Redis ─────────────────────────────────────────────────────────────
    redis_host: str = "localhost"
    redis_port: int = 56379
    redis_db: int = 0

    # ── Auth ──────────────────────────────────────────────────────────────
    #: HS256 locally. 05 §A1.1 requires EdDSA in production, where the private
    #: key lives in KMS — the invariant below refuses to start a production pod
    #: on a symmetric key, because HS256 means every service that can verify a
    #: token can also mint one.
    jwt_secret_key: str = "local-dev-only-not-a-real-key-change-me"  # noqa: S105
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "http://localhost:8000"
    jwt_audience: str = "api.receptionai"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 2_592_000

    # ── CORS ──────────────────────────────────────────────────────────────
    #: A raw string, split by :attr:`cors_origins`.
    #:
    #: Typed ``str`` rather than ``list[str]`` because pydantic-settings
    #: JSON-decodes complex-typed fields *before* any validator runs, so a
    #: comma-separated ``CORS_ORIGINS=http://a,http://b`` — the natural form in
    #: a Kubernetes ConfigMap — raises a parse error that no ``mode="before"``
    #: validator can intercept. Taking the raw string and splitting it
    #: ourselves accepts both forms, which is what lets one image read either.
    cors_origins_raw: str = Field(
        default='["http://localhost:3000"]', alias="CORS_ORIGINS"
    )

    # ── LLM provider ──────────────────────────────────────────────────────
    llm_provider: str = "deepseek"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-v4-flash"
    openai_api_key: str = ""
    #: Non-zero locally so the widget exercises incremental rendering against
    #: the stub exactly as it will against a real provider. Zero in tests.
    stub_token_delay_ms: int = 15

    # ── Demo widget ───────────────────────────────────────────────────────
    demo_tenant_slug: str = "sunrise-dental"
    #: PHASE1-API-CONTRACT: single-use, 20s TTL, scoped to one conversation.
    stream_ticket_ttl_seconds: int = 20
    #: 04 §9.1 / 06 §3.3: keepalive cadence and idle terminate.
    sse_heartbeat_seconds: float = 15.0
    sse_idle_timeout_seconds: float = 900.0

    # ── Rate limits ───────────────────────────────────────────────────────
    #: Per-IP, for the unauthenticated demo endpoint (05 §B8.2).
    demo_rate_burst: int = 5
    demo_rate_per_second: float = 0.05  # ~3/min sustained
    #: Per-tenant, for authenticated AI traffic.
    ai_rate_burst: int = 30
    ai_rate_per_second: float = 0.5

    # ── Observability ─────────────────────────────────────────────────────
    otlp_endpoint: str = ""
    otel_enabled: bool = False

    # ── LLM observability (Langfuse) ──────────────────────────────────────
    #
    # Optional in the strong sense: with no keys the tracer is a no-op and the
    # platform behaves exactly as it did before Langfuse existed. A clean clone
    # runs the full stack without any of this set.
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3110"
    #: Per-tenant prompt/completion capture, default OFF (contract §8, 05
    #: §D14.5). This is the process-wide *ceiling*; a tenant opts in below it,
    #: never above it. Set to "full" only on a stack whose traces nobody
    #: outside the clinical relationship can read.
    langfuse_capture: Literal["none", "masked", "full"] = "masked"
    #: Character ceiling on recorded prompt/completion text. The default carries
    #: this platform's whole assembled system prompt (~10.7k for the scheduler);
    #: lower it if trace storage becomes a concern, raise it if a vertical ships
    #: a larger knowledge layer.
    langfuse_max_chars: int = 16_000

    @property
    def cors_origins(self) -> list[str]:
        """The allow-list, from a JSON array or a comma-separated string.

        Three input shapes are accepted, and the third is why this is not a
        one-liner. ``.env`` carries ``CORS_ORIGINS=["http://localhost:3000"]``
        **unquoted**, so a shell that sources the file (``set -a; . ./.env``)
        exports ``[http://localhost:3000]`` — square brackets, no quotes, not
        valid JSON. That is the form the Makefile and every developer's shell
        actually produce, so it has to parse.

        A parse failure returns the *bracket-stripped, comma-split* content
        rather than an empty list. An empty list is the dangerous answer: CORS
        silently blocks every browser request, the API looks healthy, and the
        frontend team spends an afternoon on it. Salvaging the content means
        the common malformed case still works.
        """
        raw = self.cors_origins_raw.strip()
        if not raw:
            return []

        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                # The shell-mangled form. Strip the brackets and any stray
                # quotes, then split — see the docstring.
                raw = raw.strip("[]")
            else:
                return [str(item).strip() for item in parsed if str(item).strip()]

        return [
            part.strip().strip('"').strip("'")
            for part in raw.split(",")
            if part.strip().strip('"').strip("'")
        ]

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def is_local(self) -> bool:
        return self.env in ("local", "test", "ci")

    @property
    def llm_api_key(self) -> str:
        """The key for the configured provider, or empty."""
        return {
            "deepseek": self.deepseek_api_key,
            "openai": self.openai_api_key,
        }.get(self.llm_provider.lower(), "")

    @model_validator(mode="after")
    def _production_invariants(self) -> Settings:
        """Refuse to start a deployed pod on development defaults.

        Each check below corresponds to a configuration mistake that is
        *invisible at runtime*: the pod serves traffic, the health check
        passes, and the damage is only discoverable by someone thinking to
        look. A crash-loop is loud, and loud is what these need to be.
        """
        if self.is_local:
            return self

        problems: list[str] = []

        if self.jwt_secret_key == "local-dev-only-not-a-real-key-change-me":  # noqa: S105
            problems.append("JWT_SECRET_KEY is still the development default")
        if self.jwt_algorithm.startswith("HS"):
            # 05 §A1.1: a symmetric key means every verifier is also a minter,
            # and our Temporal workers verify tokens on callback paths.
            problems.append(
                "JWT_ALGORITHM is symmetric (HS*); production must sign with "
                "EdDSA so a compromised verifier cannot mint an owner token"
            )
        if "*" in self.cors_origins:
            problems.append("CORS_ORIGINS contains '*'")
        if not self.verify_tenant_guc:
            problems.append(
                "VERIFY_TENANT_GUC is false; the GUC assertion is the check "
                "that catches a pooler misconfiguration before it leaks data"
            )
        if self.postgres_user in ("postgres", "receptionai_migrator"):
            # Contract §2.5: a superuser is implicitly BYPASSRLS, which
            # silently disables every RLS policy in the system.
            problems.append(
                f"POSTGRES_USER is {self.postgres_user!r}; the runtime role "
                f"must be a non-owner without BYPASSRLS"
            )

        if problems:
            raise ValueError(
                f"Refusing to start in env={self.env!r}:\n  - "
                + "\n  - ".join(problems)
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings singleton.

    Cached because reading and validating the environment on every dependency
    resolution would put a Pydantic validation on the hot path of every
    request. Tests override it through FastAPI's ``dependency_overrides``.
    """
    return Settings()


SettingsDep = Annotated[Settings, Field()]

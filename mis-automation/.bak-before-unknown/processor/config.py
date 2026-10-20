"""Runtime configuration, read from environment variables (see config/.env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql://mis:mis@localhost:5432/mis"
    api_key: str = ""
    timezone: str = "Asia/Kolkata"            # business time zone for report dates and scheduling
    lease_minutes: int = 30                    # how long a claimed file stays locked
    max_attempts: int = 3                      # then the event becomes 'dead' and needs a human
    auto_register_partners: bool = False       # False: unknown codes stay 'unregistered_code' until a person registers the partner
    range_end_future_policy: str = "review"    # 'review' | 'clamp' for range filenames ending after upload day
    max_upload_mb: int = 100
    manifest_path: str = "config/expected_reports.yaml"
    seed_dir: str = "config/profiles"
    alias_dictionary_path: str = "config/column_aliases.yaml"
    discovery_lookback_days: int = 3
    discovery_overlap_hours: int = 6
    llm_provider: str = "none"                 # 'anthropic' | 'none'
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-5-5"
    llm_timeout_seconds: int = 60
    approved_migrations: tuple = ()           # additive migrations a human has approved, e.g. ("002",)
    review_secret: str = ""                   # HMAC key for emailed review links; blank = links disabled
    review_ttl_hours: int = 48
    public_base_url: str = ""                 # where a person's browser reaches the processor, e.g. https://mis.example.com
    pool_min_size: int = 1
    pool_max_size: int = 5
    extra: dict = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        e = os.environ if env is None else env
        return cls(
            database_url=e.get("DATABASE_URL", cls.database_url),
            api_key=e.get("PROCESSOR_API_KEY", ""),
            timezone=e.get("MIS_TIMEZONE", cls.timezone),
            lease_minutes=int(e.get("MIS_LEASE_MINUTES", cls.lease_minutes)),
            max_attempts=int(e.get("MIS_MAX_ATTEMPTS", cls.max_attempts)),
            auto_register_partners=_bool(e.get("MIS_AUTO_REGISTER_PARTNERS"), False),
            range_end_future_policy=e.get("MIS_RANGE_END_FUTURE_POLICY", "review"),
            max_upload_mb=int(e.get("MIS_MAX_UPLOAD_MB", cls.max_upload_mb)),
            manifest_path=e.get("MIS_MANIFEST_PATH", cls.manifest_path),
            seed_dir=e.get("MIS_SEED_DIR", cls.seed_dir),
            alias_dictionary_path=e.get("MIS_ALIAS_DICTIONARY", cls.alias_dictionary_path),
            discovery_lookback_days=int(e.get("MIS_LOOKBACK_DAYS", cls.discovery_lookback_days)),
            discovery_overlap_hours=int(e.get("MIS_OVERLAP_HOURS", cls.discovery_overlap_hours)),
            llm_provider=e.get("MIS_LLM_PROVIDER", "none"),
            anthropic_api_key=e.get("ANTHROPIC_API_KEY", ""),
            llm_model=e.get("MIS_LLM_MODEL", cls.llm_model),
            llm_timeout_seconds=int(e.get("MIS_LLM_TIMEOUT_SECONDS", cls.llm_timeout_seconds)),
            approved_migrations=tuple(x.strip() for x in e.get("MIS_APPROVED_MIGRATIONS", "").split(",") if x.strip()),
            review_secret=e.get("MIS_REVIEW_SECRET", ""),
            review_ttl_hours=int(e.get("MIS_REVIEW_TTL_HOURS", cls.review_ttl_hours)),
            public_base_url=e.get("MIS_PUBLIC_BASE_URL", "").rstrip("/"),
            pool_min_size=int(e.get("MIS_POOL_MIN", cls.pool_min_size)),
            pool_max_size=int(e.get("MIS_POOL_MAX", cls.pool_max_size)),
        )

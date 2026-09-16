"""Runtime configuration loaded from environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All tunables live here so the rest of the app sees typed values."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    admin_user: str = Field(default="admin", alias="ADMIN_USER")
    admin_password: str = Field(alias="ADMIN_PASSWORD")

    backup_path: Path = Field(default=Path("/backup"), alias="BACKUP_PATH")
    default_quota_gb: int = Field(default=500, alias="DEFAULT_QUOTA_GB")
    # Upper bound enforced server-side; the form's `max` attribute is only
    # a hint and a hand-rolled POST can ignore it.
    max_quota_gb: int = Field(default=100_000, alias="MAX_QUOTA_GB")

    samba_container: str = Field(default="timenest-samba", alias="SAMBA_CONTAINER")
    # Where the samba container's share fragments show up in this container.
    # compose mounts ./data/config as /config:ro.
    shares_path: Path = Field(default=Path("/config/shares.d"), alias="SHARES_PATH")

    enable_metrics: bool = Field(default=True, alias="ENABLE_METRICS")
    # /metrics is not public. A logged-in session always works (so the
    # endpoint is clickable from a browser); Prometheus authenticates with
    # `Authorization: Bearer <METRICS_TOKEN>` when a token is configured.
    metrics_token: str | None = Field(default=None, alias="METRICS_TOKEN")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    timezone: str = Field(default="UTC", alias="TZ")

    data_dir: Path = Field(default=Path("/data"), alias="DATA_DIR")
    session_secret: str | None = Field(default=None, alias="SESSION_SECRET")
    # Set to true when the UI is only ever reached over https so the
    # session cookie stops being sent over plaintext.
    session_https_only: bool = Field(default=False, alias="SESSION_HTTPS_ONLY")

    # Walking a Time Machine sparsebundle means stat()ing tens of thousands
    # of band files, which on a spun-down USB drive takes seconds. Results
    # are cached this long; backups move slowly enough that a few minutes
    # of staleness is invisible.
    usage_cache_ttl: int = Field(default=300, alias="USAGE_CACHE_TTL")
    smart_cache_ttl: int = Field(default=900, alias="SMART_CACHE_TTL")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

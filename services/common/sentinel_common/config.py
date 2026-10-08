from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """12-factor configuration. Every field maps to an UPPER_CASE env var."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    service_name: str = "sentinel"
    log_level: str = "INFO"
    port: int = 8000

    # backing services
    postgres_dsn: str = "postgresql://sentinel:sentinel@localhost:5432/sentinel"
    redis_url: str = "redis://localhost:6379/0"
    amqp_url: str = "amqp://sentinel:sentinel@localhost:5672/"

    # ingest-api
    api_keys: str = "dev-key"  # comma separated
    rate_limit_per_minute: int = 1200
    max_batch_size: int = 500

    # processor / detector
    prefetch: int = 50
    bucket_sec: int = 10
    window_buckets: int = 6
    detector_interval_sec: int = 10
    min_events: int = 20
    error_rate_floor: float = 0.2
    latency_floor_ms: float = 500.0
    z_threshold: float = 3.0
    ewma_alpha: float = 0.1
    warmup_samples: int = 5
    heartbeat_timeout_sec: int = 90
    retention_hours: int = 48

    # alert-service
    webhook_url: str = ""
    webhook_kind: str = "generic"  # generic | teams | slack
    alert_cooldown_sec: int = 300
    auto_resolve_sec: int = 600
    dashboard_url: str = ""

    # loadgen
    ingest_url: str = "http://localhost:8000"
    ingest_api_key: str = "dev-key"
    events_per_second: int = 20
    fault_every_sec: int = 300
    fault_duration_sec: int = 90

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()

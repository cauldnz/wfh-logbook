"""Configuration loaded from environment / .env.

The sessionisation parameters here are SEED values only. On first run they
populate the ``config`` table; on subsequent runs the database is the source
of truth (per ARCHITECTURE §7.5). A warning is logged if `.env` values differ
from DB values, but the DB is never overwritten from `.env`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

UniFiFlavour = Literal["auto", "udm", "classic"]
TelegramMode = Literal["webhook", "polling"]


class Settings(BaseSettings):
    """All runtime configuration. Values are read from process env or `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ UniFi
    unifi_host: str = ""
    unifi_site: str = "default"
    unifi_username: str = ""
    unifi_password: str = ""
    unifi_verify_tls: bool = False
    unifi_api_flavour: UniFiFlavour = "auto"

    # ----------------------------------------------------------- Work network
    work_ssid: str = ""
    work_device_macs: str = ""

    # ---------------------------------------------------- Sessionisation seed
    gap_bridge_minutes: int = 10
    min_session_minutes: int = 2
    daily_cap_hours: int = 12
    local_timezone: str = "Australia/Sydney"
    rule_version: str = "2026.1"

    # ---------------------------------------------------------------- Service
    # LAN service binds 0.0.0.0 by design (single-host Docker container).
    http_host: str = "0.0.0.0"
    http_port: int = 8088
    poll_interval_seconds: int = 60
    data_dir: Path = Field(default=Path("/data"))
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    # --------------------------------------------------------------- Telegram
    telegram_bot_token: str = ""
    telegram_mode: TelegramMode = "webhook"
    telegram_allowed_user_ids: str = ""
    telegram_webhook_secret: str = ""
    public_base_url: str = ""

    # ----------------------------------------------------- Cloudflare Tunnel
    cloudflare_tunnel_token: str = ""

    # ---------------------------------------------------------------- Helpers
    @field_validator("data_dir", mode="before")
    @classmethod
    def _coerce_path(cls, v: str | Path) -> Path:
        return Path(v) if isinstance(v, str) else v

    def parsed_device_macs(self) -> list[tuple[str, str]]:
        """Parse ``WORK_DEVICE_MACS`` of form ``aa:bb=Label,cc:dd=Other``.

        Returns ``[(mac_lower, label), ...]``. Whitespace tolerated.
        """
        if not self.work_device_macs.strip():
            return []
        out: list[tuple[str, str]] = []
        for raw in self.work_device_macs.split(","):
            chunk = raw.strip()
            if not chunk:
                continue
            if "=" not in chunk:
                raise ValueError(
                    f"WORK_DEVICE_MACS entry {chunk!r} must be of the form 'MAC=Label'"
                )
            mac, label = chunk.split("=", 1)
            out.append((mac.strip().lower(), label.strip()))
        return out

    def parsed_allowed_user_ids(self) -> list[int]:
        """Parse comma-separated Telegram user IDs into a list of ints."""
        if not self.telegram_allowed_user_ids.strip():
            return []
        out: list[int] = []
        for raw in self.telegram_allowed_user_ids.split(","):
            chunk = raw.strip()
            if not chunk:
                continue
            out.append(int(chunk))
        return out

    def db_path(self) -> Path:
        """The on-disk SQLite path."""
        return self.data_dir / "wfh-logbook.sqlite"

    def db_url(self) -> str:
        """SQLAlchemy URL for the SQLite DB. Caller ensures data_dir exists."""
        return f"sqlite:///{self.db_path()}"


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide Settings singleton.

    Tests override via FastAPI dependency overrides; production code reads
    this once at startup.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_cache() -> None:
    """Test helper: clear the singleton so the next get_settings() rereads env."""
    global _settings
    _settings = None

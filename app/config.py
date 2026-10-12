import os
import re
from dataclasses import dataclass
from pathlib import Path

VERSION = os.environ.get("APP_VERSION", "dev")

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str) -> int:
    """'90', '30s', '5m', '6h', '7d' -> secondes."""
    match = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", value)
    if not match:
        raise ValueError(f"Durée invalide : {value!r}")
    return int(match.group(1)) * _UNITS[match.group(2) or "s"]


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Variable d'environnement manquante : {name}")
    return value


def _duration(name: str, default: str) -> int:
    return parse_duration(_env(name, default))


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    app_url: str
    username: str
    password_hash: str
    secret_key: str
    session_max_age: int
    cookie_secure: bool
    login_max_failures: int
    login_lock_seconds: int
    intervals: dict[str, int]
    expiring_days: int
    rdap_min_delay: float
    rdap_timeout: float
    rdap_retries: int
    rdap_backoff: float
    rdap_max_retry_after: int
    telegram_token: str
    telegram_chat_id: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "domain-watch.db"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def setup_mode(self) -> bool:
        return not self.username or not self.password_hash


def _clean_secret(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].strip()
    # Un hash bcrypt ne contient jamais « $$ » : s'il en contient, Compose ne les a pas
    # dédoublés (selon l'outil qui lance le projet). On le fait ici.
    return value.replace("$$", "$")


def load_settings() -> Settings:
    return Settings(
        data_dir=Path(_env("DATA_DIR", "/data")),
        app_url=_env("APP_URL", "").rstrip("/"),
        username=_env("AUTH_USERNAME", "").strip().strip("'\"").strip(),
        password_hash=_clean_secret(_env("AUTH_PASSWORD_HASH", "")),
        secret_key=_env("SECRET_KEY", ""),
        session_max_age=_duration("SESSION_MAX_AGE", "7d"),
        cookie_secure=_env("COOKIE_SECURE", "false").lower() in ("1", "true", "yes"),
        login_max_failures=int(_env("LOGIN_MAX_FAILURES", "5")),
        login_lock_seconds=_duration("LOGIN_LOCK_DURATION", "15m"),
        intervals={
            "active": _duration("INTERVAL_ACTIVE", "24h"),
            "expiring": _duration("INTERVAL_EXPIRING", "6h"),
            "expired": _duration("INTERVAL_EXPIRED", "6h"),
            "redemption": _duration("INTERVAL_REDEMPTION", "1h"),
            "pending_delete": _duration("INTERVAL_PENDING_DELETE", "5m"),
            "available": _duration("INTERVAL_AVAILABLE", "24h"),
            "unknown": _duration("INTERVAL_UNKNOWN", "1h"),
        },
        expiring_days=int(_env("EXPIRING_DAYS", "30")),
        rdap_min_delay=float(_env("RDAP_MIN_DELAY", "2")),
        rdap_timeout=float(_env("RDAP_TIMEOUT", "20")),
        rdap_retries=int(_env("RDAP_RETRIES", "3")),
        rdap_backoff=float(_env("RDAP_BACKOFF", "5")),
        rdap_max_retry_after=_duration("RDAP_MAX_RETRY_AFTER", "2m"),
        telegram_token=_env("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID", ""),
    )

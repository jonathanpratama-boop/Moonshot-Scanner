"""Environment-driven settings. No setting here enables a paid call by itself."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: str | Path = ".env") -> None:
    """Minimal KEY=VALUE loader. Never overrides variables already set in the environment."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: os.environ.get("ASTRA_DB", str(PROJECT_ROOT / "var" / "astra.db")))
    outbox_dir: str = field(default_factory=lambda: os.environ.get("ASTRA_OUTBOX_DIR", str(PROJECT_ROOT / "var" / "outbox")))
    worker_id: str = field(default_factory=lambda: os.environ.get("ASTRA_WORKER_ID", f"local-{os.getpid()}"))
    display_timezones: list[str] = field(
        default_factory=lambda: [
            z.strip() for z in os.environ.get("ASTRA_DISPLAY_TIMEZONES", "America/New_York,Asia/Jakarta").split(",") if z.strip()
        ]
    )
    # SEC EDGAR (free, no key). Identification is required by SEC fair-access policy.
    sec_user_agent: str = field(default_factory=lambda: os.environ.get("ASTRA_SEC_USER_AGENT", "").strip())
    sec_max_issuers: int = field(default_factory=lambda: _int("ASTRA_SEC_MAX_ISSUERS", 25))
    sec_min_interval_seconds: float = field(default_factory=lambda: _float("ASTRA_SEC_MIN_INTERVAL_SECONDS", 0.25))
    sec_timeout_seconds: float = field(default_factory=lambda: _float("ASTRA_SEC_TIMEOUT_SECONDS", 20.0))
    sec_max_retries: int = field(default_factory=lambda: _int("ASTRA_SEC_MAX_RETRIES", 2))
    # Work queue
    lease_seconds: int = field(default_factory=lambda: _int("ASTRA_LEASE_SECONDS", 300))
    # Default expiry for a DETECTED candidate that receives no research (US sessions after detection).
    default_expiry_sessions: int = field(default_factory=lambda: _int("ASTRA_DEFAULT_EXPIRY_SESSIONS", 5))
    # Sampled non-signals retained per scan run for outcome baselines.
    non_signal_samples_per_run: int = field(default_factory=lambda: _int("ASTRA_NON_SIGNAL_SAMPLES", 3))


def settings() -> Settings:
    return Settings()

"""Configuration for the overlay service, read from the environment."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


class Settings:
    def __init__(self) -> None:
        self.influx_host = os.getenv("INFLUXDB_HOST", "influxdb")
        self.influx_port = int(os.getenv("INFLUXDB_PORT", "8086"))
        self.influx_user = os.getenv("INFLUXDB_USER", "influxdb_user")
        self.influx_password = os.getenv("INFLUXDB_PASSWORD", "")
        self.influx_db = os.getenv("INFLUXDB_DB", "GarminStats")
        use_https = os.getenv("INFLUXDB_HTTPS", "").lower() == "true"
        self.influx_scheme = "https" if use_https else "http"

        self.db_path = Path(os.getenv("OVERLAY_DB", "/data/overlay.db"))
        self.training_log_dir = Path(os.getenv("TRAINING_LOG_DIR", "/training_log"))

        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def influx_url(self) -> str:
        return f"{self.influx_scheme}://{self.influx_host}:{self.influx_port}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

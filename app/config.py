from __future__ import annotations

from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    HOST: str = "0.0.0.0"
    PORT: int = 8191
    LOG_LEVEL: str = "info"
    MAX_TIMEOUT: int = 60000
    MAX_BROWSERS: int = 10
    HEADLESS: bool = False
    BROWSER_EXECUTABLE_PATH: Optional[str] = None

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
VERSION = "0.1.0"

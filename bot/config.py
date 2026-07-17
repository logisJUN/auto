"""Loads secrets from .env and strategy parameters from config.yaml into one object."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Secrets:
    bybit_api_key: str
    bybit_api_secret: str
    bybit_testnet: bool
    newsapi_key: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    dashboard_token: str


@dataclass
class Config:
    secrets: Secrets
    raw: dict = field(repr=False, default_factory=dict)

    def __getitem__(self, key):
        return self.raw[key]

    def get(self, *path, default=None):
        node = self.raw
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


def load_config(env_path: str | Path | None = None, yaml_path: str | Path | None = None) -> Config:
    load_dotenv(env_path or ROOT_DIR / ".env")

    api_key = os.getenv("BYBIT_API_KEY", "").strip()
    api_secret = os.getenv("BYBIT_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise RuntimeError(
            "BYBIT_API_KEY / BYBIT_API_SECRET가 설정되지 않았습니다. "
            ".env 파일을 만들고 (.env.example 참고) API 키를 채워주세요."
        )

    secrets = Secrets(
        bybit_api_key=api_key,
        bybit_api_secret=api_secret,
        bybit_testnet=_env_bool("BYBIT_TESTNET", True),
        newsapi_key=os.getenv("NEWSAPI_KEY", "").strip() or None,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip() or None,
        dashboard_token=os.getenv("DASHBOARD_TOKEN", "changeme").strip(),
    )

    yaml_file = Path(yaml_path or ROOT_DIR / "config.yaml")
    with open(yaml_file, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    return Config(secrets=secrets, raw=raw)

"""读取环境变量与路径常量。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

load_dotenv(BASE_DIR / ".env")


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


HOST = os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = _int("PORT", 8000)
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "").strip()
CACHE_TTL_HOURS = _float("CACHE_TTL_HOURS", 168)
REQUEST_TIMEOUT_SECONDS = _int("REQUEST_TIMEOUT_SECONDS", 30)

PROVIDERS_PATH = str(DATA_DIR / "providers.json")
CACHE_PATH = str(DATA_DIR / "answers.db")
ADMIN_HTML = BASE_DIR / "app" / "static" / "admin.html"

MAX_TITLE_LENGTH = 2000
MAX_OPTIONS_COUNT = 26

# 首次运行时用于自动创建第一个 AI 条目
SEED = {
    "name": "默认 AI",
    "base_url": os.getenv("OPEN_AI_BASE_URL", "").strip(),
    "api_key": os.getenv("OPEN_AI_API_KEY", "").strip(),
    "model": os.getenv("OPEN_AI_MODEL", "").strip(),
}
if not SEED["api_key"] or SEED["api_key"] == "your-api-key":
    SEED["api_key"] = ""

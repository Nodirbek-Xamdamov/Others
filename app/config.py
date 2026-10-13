from __future__ import annotations

import os
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _ids(s: str) -> set[int]:
    return {int(x) for x in re.findall(r"-?\d+", s or "")}


@dataclass(frozen=True)
class Settings:
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    llm_model: str = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
    # vergul bilan bir nechta model: asosiy model limitga tushsa (429) kutmasdan shularga o'tiladi
    llm_fallback_models: tuple = tuple(m.strip() for m in os.getenv(
        "LLM_FALLBACK_MODEL", "openai/gpt-oss-20b,qwen/qwen3.8-27b").split(",") if m.strip())
    # arzon model: oddiy savollar shunda boshlanadi, murakkablashsa (2+ tool-raund yoki xato) llm_model'ga o'tadi
    llm_simple_model: str = os.getenv("LLM_SIMPLE_MODEL", "").strip()
    company_name: str = os.getenv("COMPANY_NAME", "Fleet")
    # fleetio = savollarga Fleetio saytidan (API, faqat o'qish) javob; sheet = xlsx jadvaldan (eski rejim)
    data_source: str = (os.getenv("DATA_SOURCE", "sheet").strip().lower() or "sheet")
    spreadsheet_path: str = os.getenv("SPREADSHEET_PATH", "data/Fleet_Truck_Info_Board.xlsx")
    sheet_url: str = os.getenv("SHEET_URL", "")
    refresh_minutes: int = int(os.getenv("REFRESH_MINUTES", "10") or 0)
    # vaqtinchalik login/parol (/start dan keyin so'raladi); ikkalasi bo'sh = auth o'chiq
    bot_login: str = os.getenv("BOT_LOGIN", "")
    bot_password: str = os.getenv("BOT_PASSWORD", "")
    allowed_user_ids: frozenset = frozenset(_ids(os.getenv("ALLOWED_USER_IDS", "")))
    admin_user_ids: frozenset = frozenset(_ids(os.getenv("ADMIN_USER_IDS", "")))


settings = Settings()


def spreadsheet_file() -> Path:
    """Jadval faylini qaytaradi. SHEET_URL berilgan bo'lsa, Google Sheets'dan yangisini yuklab oladi."""
    local = Path(settings.spreadsheet_path)
    if not local.is_absolute():
        local = ROOT / local
    if not settings.sheet_url:
        return local
    url = settings.sheet_url
    m = re.search(r"docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if m and "export" not in url:
        url = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx"
    target = ROOT / "data" / "live_sheet.xlsx"
    target.parent.mkdir(exist_ok=True)
    tmp = target.with_suffix(".tmp")
    with urllib.request.urlopen(url, timeout=30) as r, open(tmp, "wb") as f:
        f.write(r.read())
    tmp.replace(target)
    return target

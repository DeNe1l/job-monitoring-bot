"""Загрузка config.yaml и переменных окружения (.env)."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent

DEFAULTS = {
    "excel_path": "1_Поиск_работы_30_дней_IT.xlsx",
    "excel_sheet": "IT-каналы",
    "priorities": ["A", "B", "C"],
    "extra_telegram_channels": [],
    "check_interval_minutes": 30,
    "request_delay_sec": 2,
    "max_age_days": 14,
    "recheck_hours": 12,
    "recheck_limit": 20,
    "notify_closed": True,
    "alert_after_failures": 3,
    "digest_threshold": 5,
    "telegram": {"pages": 1, "pages_first_run": 3, "head_chars": 200},
    "hh": {"period_days": 7, "pages": 2, "profiles": [{"name": "Москва", "params": {"area": 1}}]},
    "habr": {"pages": 1, "locations": ["москва"], "allow_remote": True},
    "search_queries": {"hh": ["python"], "habr": ["python"]},
    "exclude_always": [],
    "exclude_seniority": [],
    "level_override": [],
    "tracks": {},
    "closed_markers_telegram": [],
    "closed_markers_web": ["вакансия в архиве"],
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_env_file(path: Path) -> None:
    """Мини-загрузчик .env: строки вида KEY=VALUE (уже заданные переменные не трогаем)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def load_config(path: str | None = None) -> dict:
    _load_env_file(BASE_DIR / ".env")
    cfg_path = Path(path) if path else BASE_DIR / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, user_cfg)

    excel = Path(cfg["excel_path"])
    cfg["excel_path"] = excel if excel.is_absolute() else BASE_DIR / excel
    cfg["db_path"] = BASE_DIR / "vacancies.db"
    cfg["log_path"] = BASE_DIR / "jobbot.log"
    cfg["env"] = {
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        "TELEGRAM_PROXY": os.environ.get("TELEGRAM_PROXY", "").strip(),
        "HH_ACCESS_TOKEN": os.environ.get("HH_ACCESS_TOKEN", "").strip(),
        "HH_USER_AGENT": os.environ.get("HH_USER_AGENT", "JobMonitor/1.0").strip(),
    }
    return cfg

"""Общие структуры данных и вспомогательные функции."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Vacancy:
    key: str                         # уникальный ключ: "hh:123", "habr:456", "tg:канал:789"
    kind: str                        # hh | habr | telegram
    source: str                      # человекочитаемое название источника
    title: str
    url: str
    company: str = ""
    location: str = ""
    published: Optional[datetime] = None
    text: str = ""                   # полный текст поста (для Telegram)
    match_text: str = ""             # что проверяет фильтр: название или «шапка» поста
    closed: bool = False             # в тексте есть пометка «вакансия закрыта»
    track: str = ""                  # заполняется фильтром
    dupe_hash: str = ""              # «отпечаток» для поиска дублей между источниками


def norm(s: str) -> str:
    """Нижний регистр, «ё» -> «е», только буквы и цифры через пробел."""
    return re.sub(r"[\W_]+", " ", (s or "").lower().replace("ё", "е")).strip()


def compute_dupe_hash(v: Vacancy) -> str:
    if v.kind == "telegram":
        base = norm(v.text or v.title)[:300]          # репосты одного текста в разных каналах
    else:
        base = norm(v.title) + "|" + norm(v.company)   # одна вакансия на hh и на Habr
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def parse_dt(value) -> Optional[datetime]:
    """Разбирает даты вида 2026-10-23T10:00:00+0300 / ...Z / ...+03:00. Возвращает UTC."""
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

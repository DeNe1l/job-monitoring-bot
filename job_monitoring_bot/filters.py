"""Фильтр вакансий по ключевым словам в названии (два трека: Python/backend и системная/IT-аналитика)."""
from __future__ import annotations

import re
from typing import List, Optional


def _e(s: str) -> str:
    return s.replace("ё", "е").replace("Ё", "Е")


def _compile(patterns: List[str]) -> List[re.Pattern]:
    # Регистр не важен; «ё» в шаблонах заменяем на «е» (в тексте делаем то же самое)
    return [re.compile(_e(p), re.IGNORECASE) for p in (patterns or [])]


def _any(patterns: List[re.Pattern], text: str) -> bool:
    return any(p.search(text) for p in patterns)


class Matcher:
    def __init__(self, cfg: dict):
        self.always = _compile(cfg["exclude_always"])
        self.senior = _compile(cfg["exclude_seniority"])
        self.junior = _compile(cfg["level_override"])
        self.tracks = []
        self.titles = {}
        for key, t in cfg["tracks"].items():
            self.titles[key] = t.get("title", key)
            self.tracks.append({
                "key": key,
                "include": _compile(t.get("include")),
                "exclude": _compile(t.get("exclude")),
                "soft": _compile(t.get("soft_exclude")),
                "soft_unless": _compile(t.get("soft_exclude_unless")),
            })

    def classify(self, text: str) -> Optional[str]:
        """Возвращает ключ трека ('dev' / 'analyst') или None, если вакансия не подходит."""
        text = _e(text or "")
        if _any(self.always, text):
            return None
        if _any(self.senior, text) and not _any(self.junior, text):
            return None
        for t in self.tracks:
            if not _any(t["include"], text):
                continue
            if _any(t["exclude"], text):
                continue
            if t["soft"] and _any(t["soft"], text) and not _any(t["soft_unless"], text):
                continue
            return t["key"]
        return None

"""Уведомления: Telegram-бот (если настроен) или вывод в консоль."""
from __future__ import annotations

import html
import logging
import re
import time
from typing import Dict, Iterable, List

import requests

log = logging.getLogger("jobbot.notify")

esc = html.escape


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


class Notifier:
    def __init__(self, token: str = "", chat_id: str = "", proxy: str = "",
                 track_titles: Dict[str, str] = None, digest_threshold: int = 5, dry_run: bool = False):
        self.token = token
        self.chat_id = chat_id
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.track_titles = track_titles or {}
        self.digest_threshold = digest_threshold
        self.dry_run = dry_run

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id) and not self.dry_run

    # ---------- низкий уровень ----------
    def _safe(self, msg: str) -> str:
        return msg.replace(self.token, "***") if self.token else msg

    def send_text(self, text: str, preview: bool = False) -> bool:
        """Отправляет HTML-текст. Без настроенного Telegram печатает в консоль."""
        if not self.enabled:
            print("\n" + _plain(text))
            return True
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": not preview}
        for attempt in range(1, 4):
            try:
                r = requests.post(url, json=payload, timeout=20, proxies=self.proxies)
            except requests.RequestException as exc:
                log.warning("Telegram недоступен: %s", self._safe(str(exc)))
                time.sleep(3 * attempt)
                continue
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                try:
                    wait = int(r.json().get("parameters", {}).get("retry_after", 5))
                except (ValueError, AttributeError):
                    wait = 5
                time.sleep(min(wait, 60))
                continue
            log.error("Telegram API ответил %s: %s", r.status_code, self._safe(r.text[:200]))
            return False
        return False

    # ---------- форматирование ----------
    def _track(self, row) -> str:
        return self.track_titles.get(row["track"], row["track"])

    def format_vacancy(self, row) -> str:
        link = f'<a href="{esc(row["url"], quote=True)}">{esc(row["title"])}</a>'
        lines = [f"🆕 <b>{esc(self._track(row))}</b> · {esc(row['source'])}", f"<b>{link}</b>"]
        meta = " · ".join(x for x in (row["company"], row["location"]) if x)
        if meta:
            lines.append(esc(meta))
        if row["snippet"]:
            lines.append(esc(row["snippet"][:300]))
        return "\n".join(lines)

    def format_digest(self, rows) -> List[str]:
        """Список одним (или несколькими) сообщениями, сгруппированный по трекам."""
        messages, current = [], f"🆕 <b>Новых вакансий: {len(rows)}</b>\n"
        by_track: Dict[str, list] = {}
        for r in rows:
            by_track.setdefault(self._track(r), []).append(r)
        for track, items in by_track.items():
            block = [f"\n<b>{esc(track)}</b>"]
            for r in items:
                tail = " · ".join(x for x in (r["company"], r["source"]) if x)
                block.append(f'• <a href="{esc(r["url"], quote=True)}">{esc(r["title"])}</a> — {esc(tail)}')
            for line in block:
                if len(current) + len(line) + 1 > 3500:
                    messages.append(current)
                    current = ""
                current += line + "\n"
        messages.append(current)
        return messages

    # ---------- высокий уровень ----------
    def send_vacancies(self, rows) -> List[str]:
        """Отправляет новые вакансии. Возвращает ключи тех, что доставлены."""
        rows = [dict(r) for r in rows]
        if not rows:
            return []
        if len(rows) > self.digest_threshold:
            ok = all(self.send_text(m) for m in self.format_digest(rows))
            return [r["key"] for r in rows] if ok else []
        sent = []
        for r in rows:
            if self.send_text(self.format_vacancy(r)):
                sent.append(r["key"])
            time.sleep(0.5 if self.enabled else 0)
        return sent

    def send_closed(self, rows: Iterable) -> None:
        for r in rows:
            link = f'<a href="{esc(r["url"], quote=True)}">{esc(r["title"])}</a>'
            self.send_text(f"❌ Вакансия закрыта: {link}")

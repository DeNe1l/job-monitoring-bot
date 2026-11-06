#!/usr/bin/env python3
"""Бот мониторинга IT-вакансий (Python/backend и системная/IT-аналитика).

Примеры:
    python main.py --list-sources     # какие источники возьмёт бот из Excel
    python main.py --dry-run          # пробный прогон: всё печатается в консоль, ничего не сохраняется
    python main.py --test-notify      # проверить, что сообщения приходят в Telegram
    python main.py --get-chat-id      # узнать TELEGRAM_CHAT_ID после /start боту
    python main.py --once             # одна проверка (удобно для cron / планировщика)
    python main.py                    # работать постоянно, проверяя каждые N минут
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

import requests

from config import load_config
from filters import Matcher
from models import compute_dupe_hash
from notifier import Notifier, esc
from sources import SourceError, check_alive, load_sources, make_session
from storage import Storage

log = logging.getLogger("jobbot")


def setup_logging(cfg: dict, verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    fh = RotatingFileHandler(cfg["log_path"], maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def build_notifier(cfg: dict, matcher: Matcher, dry_run: bool) -> Notifier:
    env = cfg["env"]
    return Notifier(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"], env["TELEGRAM_PROXY"],
                    matcher.titles, cfg["digest_threshold"], dry_run)


def run_once(cfg: dict, db: Storage, notifier: Notifier, matcher: Matcher, session, sources=None) -> dict:
    now = datetime.now(timezone.utc)
    first_run = db.is_empty()
    if sources is None:
        sources, skipped = load_sources(cfg, session)
        for name, reason in skipped:
            log.info("Пропущен источник «%s»: %s", name, reason)
    log.info("Проверяю источников: %d%s", len(sources), " (первый запуск)" if first_run else "")

    # 1. Собираем вакансии из всех источников. Сбой одного источника не мешает остальным.
    fetched = []
    for src in sources:
        try:
            items = src.fetch(first_run)
        except Exception as exc:
            fails = db.health_fail(src.name, str(exc))
            log.warning("Источник «%s» недоступен (%d подряд): %s", src.name, fails, exc)
            if fails == cfg["alert_after_failures"]:
                notifier.send_text(f"⚠️ Источник «{esc(src.name)}» не отвечает уже {fails} проверок подряд:\n"
                                   f"{esc(str(exc))[:300]}")
            continue
        if db.health_ok(src.name, cfg["alert_after_failures"]):
            notifier.send_text(f"✅ Источник «{esc(src.name)}» снова работает")
        log.info("«%s»: получено %d", src.name, len(items))
        fetched.extend(items)
        time.sleep(cfg["request_delay_sec"])

    # 2. Фильтр: актуальность, ключевые слова, дубли
    max_age = timedelta(days=cfg["max_age_days"])
    closed_rows, new_count = [], 0
    seen_keys, seen_hashes = set(), set()
    for v in fetched:
        v.dupe_hash = compute_dupe_hash(v)
        if v.closed:                                   # автор поста пометил вакансию закрытой
            row = db.mark_closed(v.key)
            if row and row["notified"]:
                closed_rows.append(row)
            continue
        if v.published and now - v.published > max_age:
            continue
        track = matcher.classify(v.match_text or v.title)
        if not track:
            continue
        if v.key in seen_keys or v.dupe_hash in seen_hashes:
            continue
        seen_keys.add(v.key)
        seen_hashes.add(v.dupe_hash)
        if db.find(v.key, v.dupe_hash):
            continue
        v.track = track
        db.add(v, now)
        new_count += 1
    db.expire_old(now - max_age)

    # 3. Перепроверяем, что ранее найденные вакансии hh/Habr ещё открыты
    for row in db.to_recheck(("hh", "habr"), cfg["recheck_hours"], cfg["recheck_limit"], now):
        alive = check_alive(session, row["url"], cfg["closed_markers_web"])
        db.set_checked(row["key"], now)
        if alive is None:
            continue
        if alive:
            db.reset_closed_hits(row["key"])
        elif db.add_closed_hit(row["key"]) >= 2:       # закрыта по двум проверкам подряд — не ложная тревога
            closed = db.mark_closed(row["key"])
            if closed and closed["notified"]:
                closed_rows.append(closed)
        time.sleep(cfg["request_delay_sec"])

    # 4. Уведомления (неотправленные с прошлых раз тоже уйдут)
    pending = db.pending()
    sent = notifier.send_vacancies(pending)
    db.mark_notified(sent)
    if cfg["notify_closed"] and closed_rows:
        notifier.send_closed(closed_rows)

    log.info("Готово: новых %d, отправлено %d, закрытых %d", new_count, len(sent), len(closed_rows))
    return {"new": new_count, "sent": len(sent), "closed": len(closed_rows)}


def cmd_get_chat_id(cfg: dict) -> None:
    token = cfg["env"]["TELEGRAM_BOT_TOKEN"]
    if not token:
        sys.exit("Сначала впишите TELEGRAM_BOT_TOKEN в файл .env")
    proxy = cfg["env"]["TELEGRAM_PROXY"]
    proxies = {"http": proxy, "https": proxy} if proxy else None
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20, proxies=proxies)
    chats = {}
    for upd in r.json().get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("first_name") or chat.get("title")
    if not chats:
        sys.exit("Сообщений нет. Откройте своего бота в Telegram, нажмите Start и запустите команду снова.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({name})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Бот мониторинга IT-вакансий")
    ap.add_argument("--once", action="store_true", help="одна проверка и выход")
    ap.add_argument("--dry-run", action="store_true", help="пробный прогон: в консоль, без сохранения и Telegram")
    ap.add_argument("--list-sources", action="store_true", help="показать источники из Excel и выйти")
    ap.add_argument("--test-notify", action="store_true", help="отправить тестовое сообщение в Telegram")
    ap.add_argument("--get-chat-id", action="store_true", help="показать chat id после /start боту")
    ap.add_argument("--config", help="путь к другому config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg, args.verbose)

    if args.get_chat_id:
        return cmd_get_chat_id(cfg)

    matcher = Matcher(cfg)
    session = make_session()

    if args.list_sources:
        sources, skipped = load_sources(cfg, session)
        print(f"Будут проверяться ({len(sources)}):")
        for s in sources:
            print(f"  [{s.priority or '-'}] {s.kind:9} {s.name}  {s.url}")
        print(f"\nПропущены ({len(skipped)}):")
        for name, reason in skipped:
            print(f"  {name}: {reason}")
        return

    notifier = build_notifier(cfg, matcher, args.dry_run)
    if args.test_notify:
        ok = notifier.send_text("✅ Бот мониторинга вакансий подключён и может отправлять уведомления.")
        print("Отправлено" if ok and notifier.enabled else "Telegram не настроен или недоступен — смотрите jobbot.log")
        return
    if not notifier.enabled and not args.dry_run:
        log.warning("Telegram не настроен (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID в .env): "
                    "уведомления будут только в консоли.")

    db = Storage(":memory:" if args.dry_run else cfg["db_path"])
    if args.once or args.dry_run:
        run_once(cfg, db, notifier, matcher, session)
        return

    log.info("Бот запущен. Проверка каждые %s мин. Остановить: Ctrl+C", cfg["check_interval_minutes"])
    try:
        while True:
            try:
                run_once(cfg, db, notifier, matcher, session)
            except SourceError as exc:
                log.error("%s", exc)                       # например, нет Excel-файла
            except Exception:
                log.exception("Непредвиденная ошибка в цикле проверки")
            pause = cfg["check_interval_minutes"] * 60 * random.uniform(0.9, 1.1)
            time.sleep(pause)
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")


if __name__ == "__main__":
    main()

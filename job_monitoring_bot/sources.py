"""Источники вакансий: hh.ru, Habr Career, публичные Telegram-каналы. Список берётся из Excel."""
from __future__ import annotations

import logging
import re
import time
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import openpyxl
import requests
from bs4 import BeautifulSoup

from models import Vacancy, parse_dt

log = logging.getLogger("jobbot.sources")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class SourceError(Exception):
    """Источник недоступен или ответил не так, как ожидалось."""


# ----------------------------------------------------------------------
#  HTTP
# ----------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"})
    return s


def _retry_after(resp, default: int) -> int:
    try:
        return int(resp.headers.get("Retry-After", default))
    except (TypeError, ValueError):
        return default


def http_get(session, url, params=None, headers=None, retries: int = 3, timeout: int = 25):
    """GET с повторами при сетевых ошибках, 429 и 5xx. Остальные статусы возвращает как есть."""
    last_error = "неизвестная ошибка"
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_error = str(exc)
            time.sleep(2 * attempt)
            continue
        if r.status_code == 429:
            last_error = "HTTP 429 (слишком частые запросы)"
            time.sleep(min(_retry_after(r, 5 * attempt), 60))
            continue
        if r.status_code >= 500:
            last_error = f"HTTP {r.status_code}"
            time.sleep(2 * attempt)
            continue
        return r
    raise SourceError(f"{url}: {last_error}")


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


# ----------------------------------------------------------------------
#  Базовый класс
# ----------------------------------------------------------------------
class BaseSource:
    kind = ""

    def __init__(self, name: str, url: str, priority: str, cfg: dict, session):
        self.name = name
        self.url = url
        self.priority = priority
        self.cfg = cfg
        self.session = session

    def fetch(self, first_run: bool = False) -> List[Vacancy]:
        raise NotImplementedError

    def _pause(self) -> None:
        time.sleep(self.cfg.get("request_delay_sec", 2))


# ----------------------------------------------------------------------
#  hh.ru
# ----------------------------------------------------------------------
def parse_hh_html(html, source_name: str) -> List[Vacancy]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[Vacancy] = []
    for a in soup.select('a[href*="/vacancy/"][data-qa^="serp-item__title"]'):
        m = re.search(r"/vacancy/(\d+)", a.get("href", ""))
        if not m:
            continue
        # карточка = самый большой родитель, внутри которого только эта одна вакансия
        card, node = a, a.parent
        while node is not None and len(node.select('a[href*="/vacancy/"][data-qa^="serp-item__title"]')) <= 1:
            card, node = node, node.parent
        vid = m.group(1)
        out.append(Vacancy(
            key=f"hh:{vid}", kind="hh", source=source_name,
            title=_text(a), url=f"https://hh.ru/vacancy/{vid}",
            company=_text(card.select_one('[data-qa^="vacancy-serp__vacancy-employer"]')),
            location=_text(card.select_one('[data-qa^="vacancy-serp__vacancy-address"]')),
        ))
    return out


def parse_hh_api(data: dict, source_name: str) -> List[Vacancy]:
    out: List[Vacancy] = []
    for it in data.get("items", []):
        vid = str(it.get("id", ""))
        if not vid:
            continue
        out.append(Vacancy(
            key=f"hh:{vid}", kind="hh", source=source_name,
            title=it.get("name", ""), url=it.get("alternate_url") or f"https://hh.ru/vacancy/{vid}",
            company=(it.get("employer") or {}).get("name", ""),
            location=(it.get("area") or {}).get("name", ""),
            published=parse_dt(it.get("published_at")),
        ))
    return out


class HHSource(BaseSource):
    kind = "hh"

    def fetch(self, first_run: bool = False) -> List[Vacancy]:
        hh = self.cfg["hh"]
        env = self.cfg["env"]
        token = env.get("HH_ACCESS_TOKEN")
        found: dict = {}
        requests_made = 0
        for profile in hh["profiles"]:
            for query in self.cfg["search_queries"]["hh"]:
                for page in range(hh["pages"]):
                    items = self._api(query, profile, page, token) if token else self._html(query, profile, page)
                    requests_made += 1
                    for v in items:
                        found.setdefault(v.key, v)
                    self._pause()
                    if len(items) < 20:      # страница неполная — дальше смотреть нет смысла
                        break
        if not found and requests_made >= 3:
            raise SourceError(
                "hh.ru: не найдено ни одной вакансии. Вероятно, включилась защита от ботов, "
                "изменилась вёрстка или нужен токен приложения (HH_ACCESS_TOKEN)."
            )
        return list(found.values())

    def _html(self, query, profile, page) -> List[Vacancy]:
        params = {
            "text": query, "search_field": "name", "order_by": "publication_time",
            "search_period": self.cfg["hh"]["period_days"], "items_on_page": 50,
            "page": page, "L_save_area": "true", **profile.get("params", {}),
        }
        r = http_get(self.session, "https://hh.ru/search/vacancy", params=params)
        if r.status_code in (403, 429) or "captcha" in r.url.lower():
            raise SourceError(f"hh.ru отклонил запрос (HTTP {r.status_code}): защита от ботов. Нужен токен или другой способ.")
        if r.status_code != 200:
            raise SourceError(f"hh.ru: HTTP {r.status_code}")
        return parse_hh_html(r.content, self.name)

    def _api(self, query, profile, page, token) -> List[Vacancy]:
        ua = self.cfg["env"]["HH_USER_AGENT"]
        headers = {"Authorization": f"Bearer {token}", "HH-User-Agent": ua, "User-Agent": ua}
        params = {
            "text": query, "search_field": "name", "order_by": "publication_time",
            "period": self.cfg["hh"]["period_days"], "per_page": 50, "page": page,
            **profile.get("params", {}),
        }
        r = http_get(self.session, "https://api.hh.ru/vacancies", params=params, headers=headers)
        if r.status_code in (401, 403):
            raise SourceError(f"hh API отклонил токен (HTTP {r.status_code}). Проверьте HH_ACCESS_TOKEN.")
        if r.status_code != 200:
            raise SourceError(f"hh API: HTTP {r.status_code}")
        return parse_hh_api(r.json(), self.name)


# ----------------------------------------------------------------------
#  Habr Career
# ----------------------------------------------------------------------
def _habr_location_ok(location: str, remote: bool, habr_cfg: dict) -> bool:
    if remote and habr_cfg.get("allow_remote", True):
        return True
    if not location:
        return True                      # локация не указана — не теряем вакансию
    low = location.lower().replace("ё", "е")
    return any(w.lower().replace("ё", "е") in low for w in habr_cfg.get("locations", []))


def parse_habr_json(data: dict, source_name: str, habr_cfg: dict) -> List[Vacancy]:
    out: List[Vacancy] = []
    for it in data.get("list", []):
        href = it.get("href") or ""
        m = re.search(r"/vacancies/(\d+)", href) or re.search(r"(\d+)", str(it.get("id", "")))
        if not m:
            continue
        vid = m.group(1)
        remote = bool(it.get("remoteWork"))
        locs = [l.get("title", "") for l in (it.get("locations") or []) if isinstance(l, dict)]
        location = ", ".join(x for x in locs if x)
        if not _habr_location_ok(location, remote, habr_cfg):
            continue
        if remote:
            location = (location + " (удалённо)").strip()
        out.append(Vacancy(
            key=f"habr:{vid}", kind="habr", source=source_name,
            title=it.get("title", ""), url=f"https://career.habr.com/vacancies/{vid}",
            company=(it.get("company") or {}).get("title", ""), location=location,
            published=parse_dt((it.get("publishedDate") or {}).get("date")),
        ))
    return out


def parse_habr_html(html, source_name: str, habr_cfg: dict) -> List[Vacancy]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[Vacancy] = []
    for card in soup.select(".vacancy-card"):
        a = card.select_one('a[href*="/vacancies/"]')
        m = re.search(r"/vacancies/(\d+)", a.get("href", "")) if a else None
        if not m:
            continue
        meta = _text(card.select_one(".vacancy-card__meta"))
        remote = "удален" in meta.lower().replace("ё", "е")
        if not _habr_location_ok(meta, remote, habr_cfg):
            continue
        t = card.select_one("time")
        out.append(Vacancy(
            key=f"habr:{m.group(1)}", kind="habr", source=source_name,
            title=_text(a), url=f"https://career.habr.com/vacancies/{m.group(1)}",
            company=_text(card.select_one(".vacancy-card__company-title")), location=meta,
            published=parse_dt(t.get("datetime")) if t else None,
        ))
    return out


class HabrSource(BaseSource):
    kind = "habr"

    def fetch(self, first_run: bool = False) -> List[Vacancy]:
        habr = self.cfg["habr"]
        found: dict = {}
        for query in self.cfg["search_queries"]["habr"]:
            for page in range(1, habr["pages"] + 1):
                for v in self._query(query, page, habr):
                    found.setdefault(v.key, v)
                self._pause()
        return list(found.values())

    def _query(self, query, page, habr) -> List[Vacancy]:
        params = {"q": query, "type": "all", "sort": "date", "page": page}
        r = http_get(self.session, "https://career.habr.com/api/frontend/vacancies",
                     params=params, headers={"Accept": "application/json"})
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                data = None
            if isinstance(data, dict) and "list" in data:
                return parse_habr_json(data, self.name, habr)
        # запасной вариант: обычная HTML-страница поиска
        r = http_get(self.session, "https://career.habr.com/vacancies", params=params)
        if r.status_code != 200:
            raise SourceError(f"Habr Career: HTTP {r.status_code}")
        return parse_habr_html(r.content, self.name, habr)


# ----------------------------------------------------------------------
#  Telegram (публичная веб-версия канала t.me/s/<канал>)
# ----------------------------------------------------------------------
_GENERIC_TITLE = re.compile(
    r"^(?:новая\s+|открыта\s+)?(?:вакансия|vacancy|hiring|job|work|ищем|в\s+поиске|remote|удаленка|удаленно)[\s:!.\-]*$",
    re.IGNORECASE,
)


def make_tg_title(text: str) -> str:
    """Название вакансии для поста: первая «содержательная» строка без хэштегов, ссылок и эмодзи."""
    for line in (text or "").splitlines():
        s = re.sub(r"#\w+", " ", line)
        s = re.sub(r"https?://\S+", " ", s)
        s = re.sub(r"^[\W_]+", "", s).strip()          # убираем эмодзи и значки в начале
        s = re.sub(r"\s+", " ", s)
        if len(re.findall(r"[^\W\d_]", s)) < 4:
            continue
        if _GENERIC_TITLE.match(s.replace("ё", "е")):
            continue
        return s[:200]
    return (text or "").strip()[:120]


def parse_telegram_html(html, handle: str, source_name: str, cfg: dict) -> List[Vacancy]:
    soup = BeautifulSoup(html, "html.parser")
    head_chars = cfg["telegram"]["head_chars"]
    closed_re = [re.compile(p.replace("ё", "е"), re.IGNORECASE) for p in cfg.get("closed_markers_telegram", [])]
    out: List[Vacancy] = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        body = msg.select_one(".tgme_widget_message_text")
        if not body:
            continue
        data_post = msg.get("data-post", "")                      # "канал/12345"
        channel, _, msg_id = data_post.partition("/")
        if not msg_id.isdigit():
            continue
        links = [a.get("href", "") for a in body.select("a[href]")]
        for br in body.find_all("br"):
            br.replace_with("\n")
        text = body.get_text().strip()
        if not text:
            continue
        haystack = text + "\n" + "\n".join(links)
        m_hh = re.search(r"hh\.ru/vacancy/(\d+)", haystack)
        m_habr = re.search(r"career\.habr\.com/vacancies/(\d+)", haystack)
        key = f"hh:{m_hh.group(1)}" if m_hh else f"habr:{m_habr.group(1)}" if m_habr else f"tg:{channel.lower()}:{msg_id}"
        t = msg.select_one("a.tgme_widget_message_date time")
        out.append(Vacancy(
            key=key, kind="telegram", source=f"Telegram: {source_name}",
            title=make_tg_title(text), url=f"https://t.me/{channel}/{msg_id}",
            published=parse_dt(t.get("datetime")) if t else None,
            text=text, match_text=text[:head_chars],
            closed=any(p.search(text.replace("ё", "е")) for p in closed_re),
        ))
    return out


class TelegramSource(BaseSource):
    kind = "telegram"

    def __init__(self, name, url, priority, cfg, session, handle: str):
        super().__init__(name, url, priority, cfg, session)
        self.handle = handle

    def fetch(self, first_run: bool = False) -> List[Vacancy]:
        tg = self.cfg["telegram"]
        pages = tg["pages_first_run"] if first_run else tg["pages"]
        out: List[Vacancy] = []
        before = None
        for page in range(pages):
            params = {"before": before} if before else None
            r = http_get(self.session, f"https://t.me/s/{self.handle}", params=params)
            if r.status_code != 200:
                raise SourceError(f"t.me/s/{self.handle}: HTTP {r.status_code}")
            posts = parse_telegram_html(r.content, self.handle, self.name, self.cfg)
            if not posts:
                if page == 0:
                    raise SourceError("в веб-версии канала нет постов (канал закрытый или веб-просмотр отключён)")
                break
            out.extend(posts)
            ids = [int(p.url.rsplit("/", 1)[1]) for p in posts]
            before = min(ids)
            if page + 1 < pages:
                self._pause()
        return out


# ----------------------------------------------------------------------
#  Список источников из Excel
# ----------------------------------------------------------------------
def classify_link(url: str) -> Tuple[str, Optional[str], str]:
    """Возвращает (тип, имя_канала, причина_пропуска). Тип: hh | habr | telegram | skip."""
    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    if host.endswith("hh.ru"):
        return "hh", None, ""
    if host == "career.habr.com":
        return "habr", None, ""
    if host == "freelance.habr.com":
        return "skip", None, "фриланс-заказы, а не вакансии"
    if host in ("t.me", "telegram.me"):
        if not parts:
            return "skip", None, "нет имени канала"
        if parts[0] == "s" and len(parts) > 1:
            parts = parts[1:]
        handle = parts[0]
        if handle.startswith("+") or handle.lower() == "joinchat":
            return "skip", None, "закрытый канал по приглашению"
        if handle.lower().endswith("bot"):
            return "skip", None, "это бот: его нельзя читать как канал (бота можно опрашивать только вручную)"
        return "telegram", handle, ""
    return "skip", None, "неизвестный тип источника"


def load_sources(cfg: dict, session) -> Tuple[List[BaseSource], List[Tuple[str, str]]]:
    """Читает лист Excel и создаёт источники. Возвращает (источники, [(название, причина пропуска)])."""
    path = cfg["excel_path"]
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except FileNotFoundError:
        raise SourceError(f"Не найден Excel-файл: {path}")
    if cfg["excel_sheet"] not in wb.sheetnames:
        raise SourceError(f"В файле нет листа «{cfg['excel_sheet']}». Есть: {', '.join(wb.sheetnames)}")
    rows = wb[cfg["excel_sheet"]].iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(rows, [])]
    col = {name: i for i, name in enumerate(header)}
    if "Ссылка" not in col or "Источник" not in col:
        raise SourceError("На листе должны быть столбцы «Источник» и «Ссылка»")

    wanted = {p.strip().upper() for p in cfg["priorities"]}
    sources: List[BaseSource] = []
    skipped: List[Tuple[str, str]] = []
    used_hh = used_habr = False
    seen_handles = set()

    def cell(row, name):
        i = col.get(name)
        return str(row[i]).strip() if i is not None and i < len(row) and row[i] is not None else ""

    for row in rows:
        name, link, prio = cell(row, "Источник"), cell(row, "Ссылка"), cell(row, "Приоритет").upper()
        if not link:
            continue
        if prio and wanted and prio not in wanted:
            skipped.append((name, f"приоритет {prio} не выбран в config.yaml"))
            continue
        kind, handle, reason = classify_link(link)
        if kind == "skip":
            skipped.append((name, reason))
        elif kind == "hh" and not used_hh:
            sources.append(HHSource(name, link, prio, cfg, session)); used_hh = True
        elif kind == "habr" and not used_habr:
            sources.append(HabrSource(name, link, prio, cfg, session)); used_habr = True
        elif kind == "telegram" and handle.lower() not in seen_handles:
            seen_handles.add(handle.lower())
            sources.append(TelegramSource(name, link, prio, cfg, session, handle))
    for handle in cfg.get("extra_telegram_channels", []):
        if handle.lower() not in seen_handles:
            seen_handles.add(handle.lower())
            sources.append(TelegramSource(handle, f"https://t.me/{handle}", "", cfg, session, handle))
    wb.close()
    return sources, skipped


# ----------------------------------------------------------------------
#  Проверка, что вакансия ещё открыта
# ----------------------------------------------------------------------
def check_alive(session, url: str, markers: List[str]) -> Optional[bool]:
    """True — открыта, False — закрыта/в архиве, None — не удалось определить."""
    try:
        r = http_get(session, url, retries=1)
    except SourceError:
        return None
    if r.status_code in (404, 410):
        return False
    if r.status_code != 200:
        return None
    page = r.text.lower().replace("ё", "е")
    if any(m.lower().replace("ё", "е") in page for m in markers):
        return False
    return True

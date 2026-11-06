"""Офлайн-тесты (без интернета): python -m unittest discover -s tests -v"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as bot
from config import load_config
from filters import Matcher
from models import Vacancy
from notifier import Notifier
from sources import (classify_link, load_sources, make_session, make_tg_title,
                     parse_habr_json, parse_hh_api, parse_hh_html, parse_telegram_html)
from storage import Storage

CFG = load_config()
CFG["request_delay_sec"] = 0


class TestMatcher(unittest.TestCase):
    def setUp(self):
        self.m = Matcher(CFG)

    def check(self, title, expected):
        self.assertEqual(self.m.classify(title), expected, title)

    def test_dev(self):
        for t in ["Junior Python Developer", "Junior/Middle Python разработчик", "Стажёр-разработчик Python",
                  "Стажер-разработчик", "Backend developer", "Бэкенд-разработчик (Python/Go)",
                  "Python-разработчик", "Trainee Backend"]:
            self.check(t, "dev")

    def test_dev_rejected(self):
        for t in ["Senior Python Developer", "Python-разработчик (Middle)", "Стажер-разработчик Java",
                  "Backend-разработчик (Java)", "Преподаватель Python", "Тимлид Python", "Team Lead Backend",
                  "Ведущий разработчик Python", "Frontend разработчик", "Бухгалтер"]:
            self.check(t, None)

    def test_analyst(self):
        for t in ["Системный аналитик", "Junior системный аналитик", "IT-аналитик", "ИТ аналитик",
                  "Technical Analyst", "Аналитик интеграций", "System Analyst (Junior)",
                  "Стажёр-аналитик (IT)", "Системный аналитик / бизнес-аналитик"]:
            self.check(t, "analyst")

    def test_analyst_rejected(self):
        for t in ["Ведущий системный аналитик", "Финансовый аналитик", "Data Analyst", "Продуктовый аналитик",
                  "Маркетинговый аналитик", "Senior System Analyst"]:
            self.check(t, None)

    def test_ascii_hh_company_in_head_not_senior(self):
        # «Ведущая IT-компания ищет junior» — не должно считаться «ведущим» разработчиком
        self.check("Ведущая IT-компания ищет Junior Python разработчика", "dev")


TG_HTML = """
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="PyJobs/101">
  <div class="tgme_widget_message_text">🔥 #вакансия <b>Junior Python Developer</b><br/>Компания «Рога»<br/>
  <a href="https://hh.ru/vacancy/998877?from=tg">Откликнуться</a></div>
  <a class="tgme_widget_message_date" href="https://t.me/PyJobs/101"><time datetime="2026-10-23T09:00:00+00:00">09:00</time></a>
</div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="PyJobs/102">
  <div class="tgme_widget_message_text">Вакансия закрыта: Системный аналитик</div>
  <a class="tgme_widget_message_date" href="https://t.me/PyJobs/102"><time datetime="2026-10-23T10:00:00+00:00">10:00</time></a>
</div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="PyJobs/103">
  <div class="tgme_widget_message_text">Вакансия<br/>Системный аналитик (Junior), Москва, гибрид</div>
  <a class="tgme_widget_message_date" href="https://t.me/PyJobs/103"><time datetime="2026-10-23T11:00:00+00:00">11:00</time></a>
</div></div>
"""

HH_HTML = """
<div data-qa="vacancy-serp__results">
 <div data-qa="vacancy-serp__vacancy"><a data-qa="serp-item__title" href="https://hh.ru/vacancy/111?q=1"><span>Junior Python разработчик</span></a>
   <a data-qa="vacancy-serp__vacancy-employer">ООО Тест</a><div data-qa="vacancy-serp__vacancy-address">Москва</div></div>
 <div data-qa="vacancy-serp__vacancy"><a data-qa="serp-item__title" href="https://hh.ru/vacancy/222"><span>Системный аналитик</span></a>
   <a data-qa="vacancy-serp__vacancy-employer">АО Банк</a></div>
</div>"""

HABR_JSON = {"list": [
    {"id": 1000, "title": "Python-разработчик (junior)", "href": "/vacancies/1000", "company": {"title": "Яндекс"},
     "locations": [{"title": "Москва"}], "remoteWork": False, "publishedDate": {"date": "2026-10-23T10:12:25+03:00"}},
    {"id": 1001, "title": "Системный аналитик", "href": "/vacancies/1001", "company": {"title": "Сбер"},
     "locations": [{"title": "Казань"}], "remoteWork": False},
    {"id": 1002, "title": "Backend developer", "href": "/vacancies/1002", "company": {"title": "Remote Co"},
     "locations": [{"title": "Казань"}], "remoteWork": True},
]}


class TestParsers(unittest.TestCase):
    def test_telegram(self):
        posts = parse_telegram_html(TG_HTML, "PyJobs", "Job for Python", CFG)
        self.assertEqual(len(posts), 3)
        self.assertEqual(posts[0].key, "hh:998877")             # ссылка на hh -> общий ключ с hh
        self.assertEqual(posts[0].title, "Junior Python Developer")
        self.assertEqual(posts[0].url, "https://t.me/PyJobs/101")
        self.assertTrue(posts[1].closed)
        self.assertFalse(posts[2].closed)
        self.assertEqual(posts[2].title, "Системный аналитик (Junior), Москва, гибрид")
        self.assertEqual(posts[2].key, "tg:pyjobs:103")

    def test_tg_title(self):
        self.assertEqual(make_tg_title("🔥 #python #remote\n\nТребуется Python разработчик"), "Требуется Python разработчик")

    def test_hh_html(self):
        v = parse_hh_html(HH_HTML, "hh")
        self.assertEqual([x.key for x in v], ["hh:111", "hh:222"])
        self.assertEqual(v[0].company, "ООО Тест")
        self.assertEqual(v[0].location, "Москва")
        self.assertEqual(v[1].company, "АО Банк")

    def test_hh_api(self):
        v = parse_hh_api({"items": [{"id": "5", "name": "X", "alternate_url": "https://hh.ru/vacancy/5",
                                     "employer": {"name": "E"}, "area": {"name": "Москва"},
                                     "published_at": "2026-10-23T10:00:00+0300"}]}, "hh")
        self.assertEqual(v[0].key, "hh:5")
        self.assertEqual(v[0].published.hour, 7)                # +03:00 -> UTC

    def test_habr_json_location_filter(self):
        v = parse_habr_json(HABR_JSON, "Habr", CFG["habr"])
        self.assertEqual([x.key for x in v], ["habr:1000", "habr:1002"])   # Казань без удалёнки отброшена
        self.assertIn("удалённо", v[1].location)

    def test_classify_link(self):
        self.assertEqual(classify_link("https://spb.hh.ru/")[0], "hh")
        self.assertEqual(classify_link("https://career.habr.com/?ysclid=1")[0], "habr")
        self.assertEqual(classify_link("https://t.me/forpython"), ("telegram", "forpython", ""))
        self.assertEqual(classify_link("https://t.me/g_jobbot")[0], "skip")
        self.assertEqual(classify_link("https://freelance.habr.com/")[0], "skip")


class TestExcel(unittest.TestCase):
    def test_real_excel(self):
        sources, skipped = load_sources(CFG, make_session())
        kinds = [s.kind for s in sources]
        self.assertEqual(kinds.count("hh"), 1)
        self.assertEqual(kinds.count("habr"), 1)
        self.assertGreaterEqual(kinds.count("telegram"), 20)
        reasons = " ".join(r for _, r in skipped)
        self.assertIn("бот", reasons)
        self.assertIn("фриланс", reasons)

    def test_priority_filter(self):
        cfg = dict(CFG, priorities=["A"])
        sources, _ = load_sources(cfg, make_session())
        self.assertTrue(all(s.priority == "A" for s in sources))


class FakeSource:
    kind = "fake"

    def __init__(self, name, items, fail=False):
        self.name, self.items, self.fail = name, items, fail

    def fetch(self, first_run=False):
        if self.fail:
            raise RuntimeError("boom")
        return list(self.items)


class FakeNotifier(Notifier):
    def __init__(self, titles):
        super().__init__(track_titles=titles, digest_threshold=3)
        self.texts, self.vac_batches = [], []

    def send_text(self, text, preview=False):
        self.texts.append(text)
        return True

    def send_vacancies(self, rows):
        self.vac_batches.append([r["title"] for r in rows])
        return [r["key"] for r in rows]

    def send_closed(self, rows):
        self.texts.append("closed:" + ",".join(r["title"] for r in rows))


def vac(key, title, company="C", kind="hh", published=None, closed=False, text=""):
    return Vacancy(key=key, kind=kind, source="src", title=title, url=f"https://x/{key}", company=company,
                   published=published, match_text=text or title, text=text, closed=closed)


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.db = Storage(":memory:")
        self.m = Matcher(CFG)
        self.n = FakeNotifier(self.m.titles)
        self.session = make_session()

    def run_cycle(self, sources):
        return bot.run_once(CFG, self.db, self.n, self.m, self.session, sources=sources)

    def test_new_filtered_deduped_and_not_repeated(self):
        old = datetime.now(timezone.utc) - timedelta(days=30)
        items = [vac("hh:1", "Junior Python Developer", "A"),
                 vac("habr:1", "Junior Python Developer", "A", kind="habr"),     # дубль с hh (то же название + компания)
                 vac("hh:2", "Senior Python Developer"),                          # не подходит
                 vac("hh:3", "Системный аналитик", "B"),
                 vac("hh:4", "Python разработчик", "Z", published=old)]           # протухла
        r = self.run_cycle([FakeSource("s", items)])
        self.assertEqual(r["new"], 2)
        self.assertEqual(sorted(self.n.vac_batches[0]), ["Junior Python Developer", "Системный аналитик"])
        r2 = self.run_cycle([FakeSource("s", items)])
        self.assertEqual(r2["new"], 0)
        self.assertEqual(len(self.n.vac_batches), 2)
        self.assertEqual(self.n.vac_batches[1], [])

    def test_telegram_closed_post_marks_closed_and_notifies(self):
        self.run_cycle([FakeSource("s", [vac("hh:9", "Junior Python Developer", kind="telegram", text="Junior Python")])])
        self.assertEqual(self.db.get("hh:9")["notified"], 1)
        self.run_cycle([FakeSource("s", [vac("hh:9", "x", kind="telegram", closed=True)])])
        self.assertEqual(self.db.get("hh:9")["status"], "closed")
        self.assertTrue(any(t.startswith("closed:") for t in self.n.texts))

    def test_source_failure_alert_once_and_recovery(self):
        bad = FakeSource("Сломанный", [], fail=True)
        for _ in range(CFG["alert_after_failures"] + 2):
            self.run_cycle([bad])
        self.assertEqual(sum("не отвечает" in t for t in self.n.texts), 1)
        self.run_cycle([FakeSource("Сломанный", [])])
        self.assertTrue(any("снова работает" in t for t in self.n.texts))

    def test_failed_delivery_is_retried(self):
        class Flaky(FakeNotifier):
            ok = False
            def send_vacancies(self, rows):
                return super().send_vacancies(rows) if self.ok else []
        self.n = Flaky(self.m.titles)
        items = [vac("hh:1", "Junior Python Developer")]
        self.run_cycle([FakeSource("s", items)])
        self.assertEqual(self.db.get("hh:1")["notified"], 0)
        self.n.ok = True
        self.run_cycle([FakeSource("s", items)])
        self.assertEqual(self.db.get("hh:1")["notified"], 1)

    def test_recheck_needs_two_closed_confirmations(self):
        self.run_cycle([FakeSource("s", [vac("hh:5", "Junior Python Developer")])])
        later = datetime.now(timezone.utc) + timedelta(days=1)
        rows = self.db.to_recheck(("hh",), CFG["recheck_hours"], 10, later)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.db.add_closed_hit("hh:5"), 1)
        self.assertEqual(self.db.add_closed_hit("hh:5"), 2)
        self.assertIsNotNone(self.db.mark_closed("hh:5"))
        self.assertIsNone(self.db.mark_closed("hh:5"))


class TestFormatting(unittest.TestCase):
    def test_digest_and_escape(self):
        n = Notifier(track_titles={"dev": "Python/backend"}, digest_threshold=2)
        rows = [{"key": f"k{i}", "title": f"Dev <{i}> & Co", "url": "https://x/?a=1&b=2", "company": "C",
                 "location": "", "source": "hh", "snippet": "", "track": "dev"} for i in range(3)]
        msgs = n.format_digest(rows)
        self.assertIn("Новых вакансий: 3", msgs[0])
        self.assertIn("&lt;0&gt; &amp; Co", msgs[0])
        self.assertIn("&amp;b=2", msgs[0])


if __name__ == "__main__":
    unittest.main()

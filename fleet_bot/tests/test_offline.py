"""Internetsiz testlar: jadval yuklash, SQL xavfsizligi, tezkor buyruqlar, router,
agent tool-calling sikli (soxta LLM bilan) va Telegram handlerlari (soxta Telegram bilan).

    python -m tests.test_offline
"""
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import fmt, quick  # noqa: E402
from app.agent import FleetAgent, SqlRunner  # noqa: E402
from app.loader import load  # noqa: E402
from app.router import UNIT_ONLY, Router  # noqa: E402

XLSX = Path(__file__).resolve().parent.parent / "data" / "Fleet_Truck_Info_Board.xlsx"
data = load(XLSX)
sql = SqlRunner(data)
ok = 0


def check(cond, name):
    global ok
    assert cond, name
    ok += 1
    print("  ✓", name)


def count(q):
    return data.conn.execute(q).fetchone()[0]


print("1) Yuklash")
n = dict(data.conn.execute("SELECT sheet, COUNT(*) FROM all_trucks GROUP BY 1").fetchall())
check(n["TRUCK INFO"] == 157, "TRUCK INFO = 157 qator")
check(set(n) == {"TRUCK INFO", "CAC Trucks", "DEMIR Trucks", "New trucks", "Rental trucks", "Inactive Trucks"}, "6 ta varaq all_trucks'da")
check(count("SELECT COUNT(*) FROM trucks") == 157, "trucks view = faqat aktiv park (157)")
check(count("SELECT COUNT(DISTINCT unit_number) FROM trucks") == 157, "naiv COUNT(DISTINCT unit_number) ham 157 (oldin 593 edi)")
check(count("SELECT COUNT(*) FROM rental_trucks") == n["Rental trucks"], "rental_trucks view")
check(count("SELECT COUNT(*) FROM inactive_trucks") == n["Inactive Trucks"], "inactive_trucks view")
r = data.conn.execute("SELECT driver, vin, current_mileage, miles_until_pm FROM trucks WHERE unit_number='417'").fetchone()
check(r == ("Quentin Brownlee", "3AKJHHDR2NSMZ0397", 764483, -2866), "unit 417 qiymatlari to'g'ri")
check(count("SELECT COUNT(*) FROM trucks WHERE driver IS NULL AND location_note LIKE '%YARD%'") > 0,
      "'NEW YARD' haydovchi emas, location_note'ga tushgan")
tables = {t.name for t in data.tables}
check({"recent_issues", "gps_tracking", "pti_checks", "contacts"} <= tables, "qo'shimcha jadvallar bor")

print("2) SQL xavfsizligi")
for bad in ["DELETE FROM all_trucks", "DROP TABLE all_trucks", "SELECT 1; DROP TABLE all_trucks", "PRAGMA table_info(trucks)",
            "UPDATE all_trucks SET driver='x'", "ATTACH DATABASE 'x.db' AS x"]:
    check("error" in sql.run(bad), f"bloklandi: {bad}")
check(sql.run("WITH x AS (SELECT 1 a) SELECT a FROM x")["rows"] == [(1,)], "WITH ... SELECT ishlaydi")
check("error" in sql.run("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT COUNT(*) FROM c"), "cheksiz so'rov timeout bilan to'xtaydi")
check(count("SELECT COUNT(*) FROM all_trucks") == sum(n.values()), "baza o'zgarmagan")

print("3) Formatlash")
check(fmt.num(764483) == "764,483" and fmt.cell("year", 2022) == "2022", "raqamlar: 764,483, yil ajratilmaydi")
check(fmt.date("2027-04-01") == "Apr 1, 2027" and fmt.date("2020-01-01").endswith("❗️"), "sana: Apr 1, 2027 / o'tgan ❗️")
check(fmt.pm(-2866) == "PM overdue by 2,866 mi ❗️", "PM o'tgan")
h = fmt.md_to_html("**Total**: <5> `VIN`\n- a\n| unit | driver |\n|---|---|\n| 417 | A |\n| 418 | B |")
check(h == "<b>Total</b>: &lt;5&gt; <code>VIN</code>\n• a\n• 417 · A\n• 418 · B", "markdown -> xavfsiz Telegram HTML")

print("4) Tezkor buyruqlar")
u = quick.unit_lookup(sql, "417").text
check("Quentin Brownlee" in u and "overdue" in u, "/unit 417")
check("No truck found" in quick.unit_lookup(sql, "zzzz999").text, "/unit topilmasa")
u = quick.unit_lookup(sql, "3871").text
check(u.startswith("🚛 <b>Unit 3871</b>") and "Active fleet" in u and "Also in CAC list" in u and u.count("🚛") == 1,
      "3871: bitta karta + boshqa varaq farqi")
check("Inactive" in quick.unit_lookup(sql, "001 ZZ").text, "aktiv bo'lmagan mashina ham topiladi")
many = quick.unit_lookup(sql, "001")
check("trucks match" in many.text and len(many.units) > 3, "ko'p moslik -> ro'yxat + tugmalar")
quick.unit_lookup(sql, "'; DROP TABLE all_trucks; --")
check(count("SELECT COUNT(*) FROM all_trucks") == sum(n.values()), "/unit injection xavfsiz")
p = quick.pm_report(sql)
check("Overdue: 8" in p.text and "417" in p.units, "/pm")
check("Cab card" in quick.expiring(sql, 60).text, "/expiring")
check("Cab card" not in quick.expiring(sql, 30, ("insp",)).text, "/expiring faqat inspeksiya")
check("Trucks without a driver: 43" in quick.no_driver(sql).text, "/nodriver")
check("<b>157 trucks</b>" in quick.stats(sql).text, "/stats = 157")
check("Taj Green" in quick.driver_lookup(sql, "Taj Green").text, "haydovchi bo'yicha karta")

print("5) Router (LLM'siz)")
router = Router(sql)
check(all(UNIT_ONLY.match(t) for t in ("417", "0832 ED", "unit 3871", "#417", "3871?"))
      and not any(UNIT_ONLY.match(t) for t in ("417 unit kimda?", "PM muddati o'tganlar")), "faqat raqam -> tezkor qidiruv")
cases = {
    "417 unit kimda?": "Unit 417", "Quentin Brownlee qaysi mashinada?": "Unit 417", "9693 JCE": "Unit 9693 JCE",
    "Nechta mashina bor?": "Fleet overview", "Сколько машин?": "Fleet overview", "How many trucks do we have?": "Fleet overview",
    "PM muddati o‘tgan mashinalar": "PM status", "PM overdue trucks": "PM status",
    "Keyingi 60 kunda inspeksiyasi tugaydiganlar": "within 60 days", "Cab card muddati o'tganlar": "Cab card",
    "Haydovchisi yo'q mashinalar qayerda turibdi?": "without a driver", "Trucks without drivers": "without a driver",
}
for q, want in cases.items():
    rep = router.route(q)
    check(rep is not None and want in rep.text, f"router: {q!r}")
for q in ("Riverview guruhida nechta mashina bor?", "2026 yilgi Volvo'lar", "PM gacha 2000 mildan kam qolganlar",
          "Eng ko'p probegli 5 ta mashina", "salom"):
    check(router.route(q) is None, f"LLM'ga o'tadi: {q!r}")


print("6) Agent sikli (soxta LLM)")


class FakeCompletions:
    """user xabariga -> run_sql tool call. Natijani LLM qayta yozmaydi — Python formatlaydi."""

    def __init__(self, query):
        self.calls, self.query = [], query

    async def create(self, **kw):
        self.calls.append(kw)
        tc = SimpleNamespace(id="call_1", function=SimpleNamespace(
            name="run_sql", arguments=json.dumps({"query": self.query, "title": "Test title"})))
        msg = SimpleNamespace(content=None, tool_calls=[tc])
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)


def fake_agent(query):
    a = FleetAgent(data, api_key="x", base_url="http://localhost", model="m")
    a.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(query)))
    return a


agent = fake_agent("SELECT COUNT(*) AS trucks FROM trucks WHERE miles_until_pm < 0")
ans = asyncio.run(agent.answer("PM muddati o'tgan mashinalar nechta?"))
check(ans.text == "📊 Test title: <b>8</b>" and ans.via == "sql", "bitta son -> LLM'siz, aniq (8)")
fake = agent.client.chat.completions
check(len(fake.calls) == 1 and fake.calls[0]["tools"][0]["function"]["name"] == "run_sql", "bitta LLM chaqiruv")
sp = fake.calls[0]["messages"][0]["content"]
check(dt.date.today().isoformat() in sp and "trucks — ACTIVE FLEET, 157" in sp and "English" in sp,
      "system prompt: sana, aktiv park, ingliz tili")
print(f"     system prompt uzunligi: {len(sp)} belgi (~{len(sp) // 3} token)")
ans = asyncio.run(fake_agent("SELECT vehicle_group, COUNT(*) AS trucks FROM trucks WHERE driver IS NULL "
                             "GROUP BY 1 ORDER BY 2 DESC").answer("x"))
check("<b>Total: 43</b>" in ans.text, "guruhlab sanash -> jami qatori to'g'ri (43)")
ans = asyncio.run(fake_agent("SELECT vehicle_group, COUNT(*) AS trucks_without_driver FROM trucks WHERE driver IS NULL "
                             "GROUP BY 1").answer("x"))
check("<b>Total: 43</b>" in ans.text, "jami qatori: trucks_without_driver alias bilan ham")
ans = asyncio.run(fake_agent("SELECT unit_number, driver, location_note, current_mileage FROM trucks "
                             "ORDER BY current_mileage DESC LIMIT 3").answer("x"))
check("— 3 trucks" in ans.text and len(ans.units) == 3 and "1,251,218" in ans.text, "ro'yxat + tugmalar + 1,251,218 format")
check("<b>2541 MAS</b> · 🅿️ NEW YARD" in ans.text, "haydovchi yo'q -> '—' o'rniga joyi")
ans = asyncio.run(fake_agent("SELECT unit_number, driver, NULL AS owner FROM trucks WHERE unit_number IN ('417', '2804')").answer("x"))
check("Owner" not in ans.text, "hammasi bo'sh ustun yashirildi")


print("7) Telegram handlerlari (soxta Telegram)")
from aiogram import Bot  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.methods import EditMessageText, SendMessage  # noqa: E402
from aiogram.types import Chat, Message, MessageEntity, Update, User  # noqa: E402

from app.bot import BTN_STATS, State, build_dispatcher  # noqa: E402


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent, self.all = [], []

    async def make_request(self, bot, method, timeout=None):
        self.all.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            self.sent.append(method)
            return Message(message_id=len(self.sent), date=dt.datetime.now(), chat=Chat(id=1, type="private"),
                           text=method.text).as_(bot)
        return True

    async def stream_content(self, *a, **k):
        yield b""

    async def close(self):
        pass


async def tg_test():
    session = FakeSession()
    bot = Bot("123456:TEST", session=session)
    state = State(fake_agent("SELECT unit_number, driver FROM trucks WHERE miles_until_pm < 0 ORDER BY miles_until_pm"))
    dp = build_dispatcher(state)
    user = User(id=42, is_bot=False, first_name="Test")
    chat = Chat(id=1, type="private")

    async def send(text, uid=1):
        entities = None
        if text.startswith("/"):
            entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
        m = Message(message_id=uid, date=dt.datetime.now(), chat=chat, from_user=user, text=text, entities=entities)
        await dp.feed_update(bot, Update(update_id=uid, message=m))
        return session.sent[-1]

    m = await send("/start")
    check("Hello" in m.text and "157 trucks" in m.text and m.reply_markup is not None, "/start + menyu tugmalari")
    check("Quentin" in (await send("/unit 417", 2)).text, "/unit orqali")
    check("Fleet overview" in (await send(BTN_STATS, 3)).text, "menyu tugmasi -> statistika")
    before = len(session.sent)
    m = await send("Which trucks are overdue on PM and who drives them?", 4)
    check(len(session.sent) == before + 2 and isinstance(m, EditMessageText) and "Test title" in m.text,
          "LLM savoli: 'Looking it up…' -> javobga tahrirlanadi")
    check(m.reply_markup is not None and m.reply_markup.inline_keyboard[0][0].callback_data.startswith("u:"),
          "javob tagida unit tugmalari")
    check("SELECT" in (await send("/sql", 5)).text, "/sql oxirgi so'rovni ko'rsatadi")
    check(len(state.history[1]) == 4, "suhbat tarixi saqlandi")
    await send("/reset", 6)
    check(len(state.history[1]) == 0, "/reset tarixni tozaladi")


asyncio.run(tg_test())


print("8) Fleetio (soxta HTTP, to'qima ma'lumot)")
import io  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
import urllib.error  # noqa: E402
import urllib.parse  # noqa: E402

from app import fleetio  # noqa: E402
from app.fleetio import Catalog, FleetioClient, FleetioError  # noqa: E402
from app.fleetio_agent import FleetioAgent  # noqa: E402

for k in list(os.environ):  # haqiqiy .env dan kelgan kalitlar testga aralashmasin
    if k.startswith("SECUREFLEET_"):
        del os.environ[k]
tmp_env = Path(tempfile.mkdtemp()) / ".env"
tmp_env.write_text("SECUREFLEET_KEY=\nSECUREFLEET_ACCOUNT_TOKEN=acct-test\n", encoding="utf-8")
fleetio.ENV_FILE = tmp_env

VEHICLES = [  # to'qima yozuvlar
    {"id": 1, "name": "T-101", "vehicle_status_name": "Active", "vin": None, "default_image_url_small": "http://x/1.png",
     "comments_count": 0, "issues_count": 2, "driver": {"id": 9, "name": "Test Driver", "email": None}},
    {"id": 2, "name": "T-102", "vehicle_status_name": "Active", "issues_count": 0},
    {"id": 3, "name": "T-103", "vehicle_status_name": "In Shop"},
    {"id": 4, "name": "T-104", "vehicle_status_name": "Active"},
    {"id": 5, "name": "T-105", "vehicle_status_name": None},
]


class FakeHTTP:
    def __init__(self, status=200):
        self.reqs, self.status = [], status

    def __call__(self, req, timeout=None):
        self.reqs.append(req)
        if self.status != 200:
            raise urllib.error.HTTPError(req.full_url, self.status, "err", {}, io.BytesIO(b'{"error":"x"}'))
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))
        if "/vehicles/" in req.full_url:
            body = VEHICLES[0]
        elif q.get("start_cursor") == "c2":
            body = {"next_cursor": None, "records": VEHICLES[3:]}
        else:
            body = {"next_cursor": "c2", "estimated_remaining_count": 2, "records": VEHICLES[:3]}
        return io.BytesIO(json.dumps(body).encode())


cat = Catalog()
check(len(cat.routes) == 133 and all(r["method"] == "GET" for r in cat.routes.values()), "katalog: 133 ta, hammasi GET")
check(cat.help("list_vehicles")["filter"]["name"] == ["eq", "like"], "fleetio_help: filtr maydonlari va operatorlari")
fc = FleetioClient(cat, opener=FakeHTTP())
url, dropped = fc.build_url("list_vehicles", {"filter": {"name": {"like": "417"}}, "sort": {"name": "asc"},
                                              "per_page": 500, "hack": "1"}, "https://h/api")
qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
check(qs["filter[name][like]"] == ["417"] and qs["sort[name]"] == ["asc"] and qs["per_page"] == ["100"] and dropped == ["hack"],
      "URL: deepObject filtr/sort, per_page<=100, noma'lum parametr tashlandi")
check("per_page=20" in fc.build_url("list_vehicles", {}, "https://h/api")[0], "per_page default 20")
check(fc.build_url("list_vehicles", {"filter": '{"vin": {"like": "0397"}}'}, "h")[0].count("filter%5Bvin%5D") == 1,
      "filtr JSON matn bo'lib kelsa ham ishlaydi")
check(fc.build_url("get_vehicle", {"id": "41 7/x"}, "https://h/api")[0] == "https://h/api/vehicles/41%207%2Fx",
      "path parametri escape qilinadi")
for bad, want in [(("delete_vehicle", {}), "Unknown tool"), (("get_vehicle", {}), "needs path parameter")]:
    try:
        fc.build_url(*bad, "h")
        check(False, want)
    except FleetioError as e:
        check(want in str(e), f"xato: {want}")
try:
    fc.call("list_vehicles")
    check(False, "key yo'q")
except FleetioError as e:
    check("not configured" in str(e) and not fc.configured(), "key yo'q -> aniq xabar, so'rov yuborilmaydi")
check(not fc._open.reqs, "key yo'qligida HTTP so'rov yo'q")

tmp_env.write_text("SECUREFLEET_KEY=test-key\nSECUREFLEET_ACCOUNT_TOKEN=acct-test\n", encoding="utf-8")
check(fc.configured(), "key .env ga qo'shilsa restartsiz ko'rinadi")
res = fc.call("list_vehicles", count_by="vehicle_status_name")
check(res["counts"] == {"Active": 3, "In Shop": 1, "(none)": 1} and res["total"] == 5, "count_by: 2 sahifa, aniq son (5)")
reqs = fc._open.reqs
check(len(reqs) == 2 and all(r.get_method() == "GET" for r in reqs), "faqat GET, 2 sahifa")
VEHICLES[0]["vehicle_status_id"] = 501
res = fc.call("list_vehicles", count_by="vehicle_status_id")
check(res["count_by"] == "vehicle_status_name" and res["counts"]["Active"] == 3, "count_by *_id -> *_name (ID emas, nom)")
del VEHICLES[0]["vehicle_status_id"]
h = {k.lower(): v for k, v in reqs[0].header_items()}
check(h["authorization"] == "Token test-key" and h["account-token"] == "acct-test" and h["x-api-version"] == "2025-05-05",
      "headerlar: Token, Account-Token, X-Api-Version")
check(all("test-key" not in u for u in res["_urls"]), "URL'larda kalit yo'q")
res = fc.call("list_vehicles")
r0 = res["records"][0]
check(res["more_available"] and "vin" not in r0 and "default_image_url_small" not in r0 and "comments_count" not in r0
      and r0["issues_count"] == 2 and r0["driver"] == {"id": 9, "name": "Test Driver"}, "ixchamlash: null, *_url, 0 tashlandi")
res = fc.call("list_vehicles", fields=["name", "driver.name"])
check(res["records"][0] == {"id": 1, "name": "T-101", "driver.name": "Test Driver"}, "fields: kerakli maydonlar")
check(fc.call("get_vehicle", {"id": 1})["record"]["name"] == "T-101", "bitta yozuv (get_vehicle)")
try:
    FleetioClient(cat, opener=FakeHTTP(401)).call("list_vehicles")
    check(False, "401")
except FleetioError as e:
    check("401" in str(e), "401 -> tushunarli xato")


class ScriptedLLM:
    """Birinchi chaqiruvda tool call, keyingisida yakuniy matn."""

    def __init__(self, tool_args):
        self.calls, self.tool_args = [], tool_args

    async def create(self, **kw):
        self.calls.append(kw)
        if len(self.calls) == 1:
            tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="fleetio_get", arguments=json.dumps(self.tool_args)))
            msg = SimpleNamespace(content=None, tool_calls=[tc])
        else:
            msg = SimpleNamespace(content="**Vehicles by status**\n- Active: 3\n- In Shop: 1", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)


def fleetio_agent(tool_args=None, opener=None):
    a = FleetioAgent(FleetioClient(cat, opener=opener or FakeHTTP()), api_key="x", base_url="http://localhost", model="m")
    a.client = SimpleNamespace(chat=SimpleNamespace(completions=ScriptedLLM(
        tool_args or {"tool": "list_vehicles", "count_by": "vehicle_status_name", "all_pages": True})))
    return a


fa = fleetio_agent()
ans = asyncio.run(fa.answer("Mashinalar statuslar bo'yicha nechta?"))
llm = fa.client.chat.completions
check(ans.text == "<b>Vehicles by status</b>\n• Active: 3\n• In Shop: 1" and ans.via == "fleetio", "agent: tool -> javob HTML")
check(len(llm.calls) == 2 and '"Active": 3' in llm.calls[1]["messages"][-1]["content"], "tool natijasi LLM'ga qaytdi")
check(len(ans.queries) == 2 and all(q.startswith("/vehicles?") for q in ans.queries), "Answer.queries: bajarilgan GET'lar")
sp = llm.calls[0]["messages"][0]["content"]
check([t["function"]["name"] for t in llm.calls[0]["tools"]] == ["fleetio_get", "fleetio_help"], "LLM'ga 2 ta meta-tool")
check("get_vehicle_current_assignment(id)" in sp and "list_vehicles: name, vin" in sp and dt.date.today().isoformat() in sp
      and "English" in sp, "system prompt: katalog, filtrlar, sana, ingliz tili")
print(f"     system prompt uzunligi: {len(sp)} belgi (~{len(sp) // 4} token)")
out = json.loads(fa.run_tool("fleetio_get", {"tool": "create_vehicle"}, []))
check("Unknown tool" in out["error"], "katalogda yo'q (yozuvchi) tool bajarilmaydi")
out = json.loads(fa.run_tool("fleetio_help", {"tools": ["list_issues", "zzz"]}, []))
check(out[0]["tool"] == "list_issues" and "error" in out[1], "fleetio_help ishlaydi")
out = json.loads(fa.run_tool("fleetio_get", {"tool": "get_vehicle", "id": 1, "params": {"fields": ["name"]}}, []))
check(out.get("record") == {"id": 1, "name": "T-101"}, "id tashqarida, fields params ichida kelsa ham to'g'rilanadi")
fa = fleetio_agent()
fa.client.chat.completions.create = (lambda **kw: asyncio.sleep(0, SimpleNamespace(usage=None, choices=[SimpleNamespace(
    message=SimpleNamespace(content='<tool_call>\n<function=fleetio_help>', tool_calls=None))])))
check(asyncio.run(fa.answer("x")).text.startswith("Sorry"), "model tool'ni matn qilib yozsa foydalanuvchiga chiqmaydi")

from app.bot import BTN_F_SERVICE, F_CANNED, FleetioState, build_fleetio_dispatcher  # noqa: E402


async def tg_fleetio_test():
    session = FakeSession()
    bot = Bot("123456:TEST", session=session)
    state = FleetioState(fleetio_agent())
    dp = build_fleetio_dispatcher(state)
    user, chat = User(id=42, is_bot=False, first_name="Test"), Chat(id=1, type="private")

    async def send(text, uid=1):
        entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))] if text.startswith("/") else None
        m = Message(message_id=uid, date=dt.datetime.now(), chat=chat, from_user=user, text=text, entities=entities)
        await dp.feed_update(bot, Update(update_id=uid, message=m))
        return session.sent[-1]

    m = await send("/start")
    check("live data from Fleetio" in m.text and m.reply_markup is not None and "not connected" not in m.text,
          "fleetio /start + tugmalar")
    before = len(session.sent)
    m = await send(BTN_F_SERVICE, 2)
    check(len(session.sent) == before + 2 and isinstance(m, EditMessageText) and "Vehicles by status" in m.text,
          "tugma: 'Checking Fleetio…' -> javobga tahrirlanadi")
    user_msgs = [x for x in state.agent.client.chat.completions.calls[0]["messages"] if x["role"] == "user"]
    check(user_msgs[-1]["content"] == F_CANNED[BTN_F_SERVICE], "tugma tayyor savolga aylandi")
    check("GET /vehicles?" in (await send("/api", 3)).text, "/api oxirgi Fleetio so'rovlarini ko'rsatadi")
    tmp_env.write_text("SECUREFLEET_KEY=\nSECUREFLEET_ACCOUNT_TOKEN=acct-test\n", encoding="utf-8")
    check("not connected" in (await send("Who drives T-101?", 4)).text, "key yo'q -> LLM chaqirilmaydi, ogohlantirish")


asyncio.run(tg_fleetio_test())


print("9) Login/parol")
from aiogram.methods import AnswerCallbackQuery, DeleteMessage  # noqa: E402
from aiogram.types import CallbackQuery  # noqa: E402

from app import auth as auth_mod  # noqa: E402
from app.auth import Auth  # noqa: E402

store = Path(tempfile.mkdtemp()) / "authorized_users.json"
tmp_env.write_text("SECUREFLEET_KEY=test-key\nSECUREFLEET_ACCOUNT_TOKEN=acct-test\n", encoding="utf-8")


async def tg_auth_test():
    session = FakeSession()
    bot = Bot("123456:TEST", session=session)
    state = FleetioState(fleetio_agent())
    au = Auth("demo@", "p#ss 1", store)
    dp = build_fleetio_dispatcher(state, au)
    user = User(id=77, is_bot=False, first_name="Ali")
    n = [0]

    async def send(text, chat_type="private"):
        n[0] += 1
        entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))] if text.startswith("/") else None
        m = Message(message_id=n[0], date=dt.datetime.now(), chat=Chat(id=77, type=chat_type), from_user=user,
                    text=text, entities=entities)
        await dp.feed_update(bot, Update(update_id=n[0], message=m))
        return session.sent[-1]

    llm_calls = state.agent.client.chat.completions.calls
    check("enter your <b>login</b>" in (await send("How many vehicles?")).text and not llm_calls,
          "login bo'lmagan savol LLM'ga bormaydi, login so'raladi")
    check("enter your <b>login</b>" in (await send("/start")).text, "/start -> login so'raladi")
    check("password" in (await send("demo@")).text, "login -> parol so'raladi")
    m = await send("wrong")
    check("Wrong login or password" in m.text and not au.is_authorized(77), "xato parol -> rad")
    check(any(isinstance(x, DeleteMessage) for x in session.all), "parol xabari chatdan o'chirildi")
    await send("demo@")
    before = len(session.sent)
    m = await send("p#ss 1")
    check(au.is_authorized(77) and "Logged in" in session.sent[before].text and "live data from Fleetio" in m.text,
          "to'g'ri login/parol -> kirdi + yordam matni")
    check(Auth("demo@", "p#ss 1", store).is_authorized(77), "sessiya faylda saqlandi (restartdan keyin ham)")
    check(not Auth("demo@", "new-pass", store).is_authorized(77), "parol o'zgarsa eski sessiya bekor")
    m = await send("How many vehicles?")
    check("Vehicles by status" in m.text and llm_calls, "kirgandan keyin savol ishlaydi")
    check("Logged out" in (await send("/logout")).text and not au.is_authorized(77), "/logout")
    check("login" in (await send("/help")).text, "chiqqandan keyin yana login so'raladi")
    check("private chat" in (await send("/start", "group")).text, "guruhda login qabul qilinmaydi")
    cb = CallbackQuery(id="1", from_user=user, chat_instance="x", data="u:1")
    await dp.feed_update(bot, Update(update_id=999, callback_query=cb))
    check(isinstance(session.all[-1], AnswerCallbackQuery) and session.all[-1].show_alert, "tugma bosilsa -> 'log in first'")
    for _ in range(auth_mod.MAX_ATTEMPTS):
        await send("demo@")
        m = await send("bad")
    check("Too many" in m.text and "Too many" in (await send("demo@")).text, "5 xato -> blok")


asyncio.run(tg_auth_test())
print(f"\nHammasi o'tdi: {ok} ta tekshiruv ✅")

"""Invoys xabari -> Fleetio work order (Service > Work Orders > Add Work Order).

Saytda qo'lda bajariladigan tartibning o'zi, API orqali:
  Vehicle           — Unit raqami (Trailer qatori bo'lib, ish faqat treylerga tegishli bo'lsa — treyler)
  Status            — doim Completed
  Repair Priority   — matnda Roadside / Road side / RS / RC bo'lsa Emergency, aks holda Scheduled
  Issued by         — API key egasi (GET /users/me)
  Issue / Start / Expected Completion / Actual Completion Date = xabardagi Date
  Assigned To       — Driver (kontaktlardan)
  Vendor            — ustaxona nomi+manzili bo'yicha qidiriladi, topilmasa yangi yaratiladi
  Invoice Number va PO Number = Ref
  Issue (yangi)     — Summary = For, No Priority, Reported = Date, Label = Responsible, Reference Number = Ref,
                      Telegram'dagi fayl(lar) Documents'ga biriktiriladi
  Line item         — "For" bo'yicha service task, Subtotal = Amount

Majburiy faqat Unit va Date (Fleetio'da ham Vehicle va Issue Date majburiy). Qolganlari bo'lmasa bo'sh qoladi:
ustaxona yo'q — vendor'siz, Amount yo'q — line item narxsiz, For yo'q — issue va line item'siz (fayl work order'ga).

Yozish ketma-ketligi: vendor -> label -> fayl -> issue -> work order. Har qadam natijasi jurnalga
(data/work_orders_log.json) yoziladi: o'rtada uzilsa, qayta urinish yaratilgan narsani ikkinchi marta yaratmaydi.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import fmt
from . import wo_match as M
from .config import ROOT
from .fleetio import FleetioClient, FleetioError, credentials, env, web_link  # noqa: F401 — web_link: wo_bot uchun
from .fleetio_write import FleetioUncertain, FleetioWriter
from .wo_parser import Invoice

log = logging.getLogger("workorders")

DATA = ROOT / "data"
TASKS_FILE = DATA / "fleetio_service_tasks.json"          # hamma service tasklar (Fleetio'dan)
EXAMPLES_FILE = DATA / "service_task_examples.json"       # eski yozuvlar: "For" matni -> jamoa tanlagan task
ALIASES_FILE = DATA / "service_task_aliases.json"         # tugma orqali o'rgatilgan mosliklar
VENDORS_FILE = DATA / "cache" / "fleetio_vendors.json"
LEDGER_FILE = DATA / "work_orders_log.json"

RPC_SCHEDULED, RPC_EMERGENCY = (1, "Scheduled"), (3, "Emergency")   # Fleetio VMRS repair priority class id'lari
DEFAULT_TZ = "America/New_York"
_RAILS_TZ = {"Eastern Time (US & Canada)": "America/New_York", "Central Time (US & Canada)": "America/Chicago",
             "Mountain Time (US & Canada)": "America/Denver", "Pacific Time (US & Canada)": "America/Los_Angeles",
             "Arizona": "America/Phoenix", "Indiana (East)": "America/Indiana/Indianapolis", "Alaska": "America/Anchorage",
             "Hawaii": "Pacific/Honolulu", "UTC": "UTC"}
TASKS_MAX_AGE_S = 7 * 86400
VENDORS_FULL_SYNC_S = 86400
REFRESH_EVERY_S = 300        # fon yangilash: mashinalar va vendorlar (o'zgarganlari) har 5 daqiqada
SLOW_REFRESH_EVERY = 6       # kontaktlar va labellar har 6-yangilashda (30 daqiqa)
CONTACTS_FRESH_MIN_AGE_S = 600  # haydovchi topilmasa kontaktlar qayta yuklanadi, lekin ko'pi bilan 10 daqiqada bir
OLD_DATE_WARN_DAYS = 60

TASK_PROMPT = """You choose the Fleetio service task for a repair or expense invoice of a US trucking company.
Pick exactly ONE task id for the description given in FOR.
- TEAM = tasks this company's staff actually use (with the number of recent uses). Prefer this list and follow the staff's own conventions shown in EXAMPLES (earlier descriptions and the task they chose).
- CATALOG = other tasks that exist in Fleetio. Use one only when nothing in TEAM fits.
- If several jobs are listed, choose the main one (usually the first or the most expensive job).
- Purchases and fees (parking, scale, hotel, tools, tolls ...) go to the matching "... Expense (s)" task.
Answer with JSON only, no other text:
{"task_id": <id>, "confidence": "high" | "medium" | "low", "alternatives": [<id>, <id>, <id>]}
confidence: high = clearly this task, medium = a reasonable choice, low = a guess."""


class StepFailed(Exception):
    """Yozish qadami bajarilmadi. uncertain=True: javob kelmadi, yozuv yaratilgan bo'lishi ham mumkin."""

    def __init__(self, step: str, message: str, uncertain: bool = False):
        super().__init__(message)
        self.step, self.message, self.uncertain = step, message, uncertain


@dataclass
class FileBlob:
    name: str
    mime: str
    data: bytes


@dataclass
class Plan:
    inv: Invoice
    vehicle: dict | None = None
    target_kind: str = "unit"            # "unit" | "trailer"
    when: str = ""                       # ISO: xabardagi sana, akkaunt vaqt zonasida 00:00
    status: dict | None = None           # Completed
    rpc: tuple = RPC_SCHEDULED
    me: dict | None = None               # {"contact_id", "name"}
    driver: dict | None = None
    vendor: dict | None = None           # mavjud vendor
    vendor_new: dict | None = None       # yaratiladigan vendor (POST tanasi)
    labels: list[dict] = field(default_factory=list)
    labels_new: list[str] = field(default_factory=list)
    priority_id: int | None = None
    task: dict | None = None
    task_how: str = ""                   # learned | history | ai | ai? | chosen
    task_options: list[dict] = field(default_factory=list)
    task_skipped: bool = False           # service task yo'q: line item'siz yaratiladi
    duplicate: dict | None = None        # shu mashina + shu invoice raqamli mavjud work order
    meter_void: bool = False
    problems: list[str] = field(default_factory=list)   # bo'lsa hech narsa yaratilmaydi
    warnings: list[str] = field(default_factory=list)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data, indent: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
    tmp.replace(path)


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class _Cached:
    """Kichik ro'yxatlar uchun xotiradagi kesh: ttl o'tsa yoki fresh so'ralsa qayta yuklanadi."""

    def __init__(self, loader, ttl: float):
        self.loader, self.ttl = loader, ttl
        self.value, self.at = None, 0.0
        self.lock = threading.Lock()

    def get(self, fresh: bool = False, min_age: float = 60.0):
        with self.lock:
            age = time.monotonic() - self.at
            if self.value is None or age > self.ttl or (fresh and age > min_age):
                self.value, self.at = self.loader(), time.monotonic()
            return self.value

    def reload(self):
        with self.lock:
            self.value, self.at = self.loader(), time.monotonic()
            return self.value


class Ledger:
    """Xabar -> natija jurnali (chat:message_id kaliti bilan). Restartdan keyin ham takror yaratishdan saqlaydi."""

    MAX = 3000

    def __init__(self, path: Path = LEDGER_FILE):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, dict] = _read_json(path, {})

    def get(self, key: str) -> dict | None:
        return self.data.get(key)

    def put(self, key: str, job: dict) -> None:
        with self.lock:
            job["updated_at"] = _now_iso()
            self.data.pop(key, None)
            self.data[key] = job
            while len(self.data) > self.MAX:
                self.data.pop(next(iter(self.data)))
            _write_json(self.path, self.data, indent=1)


class RefData:
    """Fleetio ma'lumotnomalari (faqat o'qish): mashinalar, kontaktlar, labellar, vendorlar, service tasklar."""

    def __init__(self, client: FleetioClient, writer: FleetioWriter):
        self.client, self.writer = client, writer
        self._me = _Cached(self._load_me, 6 * 3600)
        self._account = _Cached(self._load_account, 6 * 3600)
        self._statuses = _Cached(lambda: client.raw_all("list_work_order_statuses"), 6 * 3600)
        self._priorities = _Cached(lambda: client.raw_all("list_issue_priorities"), 6 * 3600)
        self._vehicles = _Cached(lambda: client.raw_all("list_vehicles"), 1800)
        self._contacts = _Cached(self._load_contacts, 3600)
        self._labels = _Cached(lambda: [{"id": x["id"], "name": x.get("name") or ""} for x in client.raw_all("list_labels")], 1800)
        self._vendors: dict[str, dict] = {}
        self._vendors_meta: dict = {}
        self._vendors_lock = threading.Lock()       # faqat xotiradagi lug'at uchun (qisqa)
        self._vendors_sync_lock = threading.Lock()  # bir vaqtda bitta sinxron (tarmoq so'rovi shu qulf ostida)
        self.create_lock = threading.Lock()         # vendor/label yaratish: parallel invoyslar bittasini ikki marta yaratmasin
        self._book: M.TaskBook | None = None
        self._book_lock = threading.Lock()
        self._ticks = 0

    # ---------------------------------------------------------------- kichik ma'lumotnomalar
    def _load_me(self) -> dict:
        u = self.writer.current_user()
        if not u.get("contact_id"):
            raise FleetioError("Fleetio did not return the API user's contact (users/me).")
        return {"contact_id": u["contact_id"], "name": " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x),
                "tz": u.get("time_zone_name") or ""}

    def _load_account(self) -> dict:
        recs = self.client.raw_all("list_accounts")
        token = credentials()["account"]
        for a in recs:
            if not token or token in (a.get("token"), a.get("url_token"), a.get("account_url_token")):
                return a
        return recs[0] if recs else {}

    def _load_contacts(self) -> list[dict]:
        return [{"id": c["id"], "name": c.get("name") or "", "vehicle_operator": bool(c.get("vehicle_operator"))}
                for c in self.client.raw_all("list_contacts")]

    def me(self) -> dict:
        return self._me.get()

    def tzinfo(self) -> dt.tzinfo:
        """Akkaunt vaqt zonasi: FLEETIO_TZ (.env) > Fleetio foydalanuvchisiniki > New York."""
        name = env("FLEETIO_TZ")
        if not name:
            try:
                name = _RAILS_TZ.get(self.me()["tz"], "")
            except FleetioError:
                name = ""
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name or DEFAULT_TZ)
        except Exception:  # noqa: BLE001 — tzdata o'rnatilmagan bo'lsa: AQSh sharqiy qishki vaqti
            return dt.timezone(dt.timedelta(hours=-5))

    def today(self) -> dt.date:
        return dt.datetime.now(self.tzinfo()).date()

    def when(self, day: dt.date) -> str:
        """Sana -> ISO (akkaunt vaqt zonasida kun boshi; sayt ham sanani shunday saqlaydi)."""
        tz = self.tzinfo()
        hour = 0 if getattr(tz, "key", None) else 1   # zaxira (-05:00) zonada yozda ham shu kunda qolishi uchun
        return dt.datetime.combine(day, dt.time(hour, 0), tzinfo=tz).isoformat()

    def completed_status(self) -> dict:
        sts = self._statuses.get()
        done = [s for s in sts if s.get("completed")] or [s for s in sts if (s.get("name") or "").lower() == "completed"]
        if not done:
            raise FleetioError("No 'Completed' work order status in Fleetio.")
        return done[0]

    def no_priority_id(self) -> int | None:
        for p in self._priorities.get():
            if p.get("slug") == "no-priority" or (p.get("name") or "").lower() == "no priority":
                return p["id"]
        return None

    def meter_required(self, vehicle: dict) -> bool:
        """Akkaunt sozlamasi: tugallangan work order'da hisoblagich majburiymi (ayrim mashina turlari mustasno)."""
        try:
            acc = self._account.get()
        except FleetioError:
            return False
        if not acc.get("require_a_meter_entry_on_service_entry_or_completed_work_order"):
            return False
        return vehicle.get("vehicle_type_id") not in (acc.get("excluded_vehicle_type_ids_from_required_meter_readings") or [])

    def vehicles(self, fresh: bool = False) -> list[dict]:
        return self._vehicles.get(fresh)

    def contacts(self, fresh: bool = False) -> list[dict]:
        return self._contacts.get(fresh, min_age=CONTACTS_FRESH_MIN_AGE_S)

    def labels(self, fresh: bool = False) -> list[dict]:
        return self._labels.get(fresh)

    def add_label(self, lab: dict) -> None:
        if self._labels.value is not None:
            self._labels.value.append({"id": lab["id"], "name": lab.get("name") or ""})

    # ---------------------------------------------------------------- vendorlar (3000+: diskda kesh, fonda sinxron)
    @staticmethod
    def _vendor_row(v: dict) -> dict:
        return {k: v.get(k) for k in ("id", "name", "street_address", "city", "region", "postal_code", "phone", "service")}

    def _vendors_from_disk(self) -> None:
        with self._vendors_lock:
            if self._vendors or self._vendors_meta:
                return
            disk = _read_json(VENDORS_FILE, {})
            self._vendors = {str(v["id"]): v for v in disk.get("vendors") or []}
            self._vendors_meta = {k: disk.get(k) for k in ("full_at", "synced_at")}

    def vendors(self, sync: bool = False) -> list[dict]:
        """Vendorlar keshdan (fon har 5 daqiqada yangilaydi). sync=True — avval oxirgi o'zgarishlar olinadi
        (bitta so'rov): yangi vendor yaratishdan oldin, saytda hozirgina qo'shilgani bo'lsa topilsin."""
        self._vendors_from_disk()
        if sync or not self._vendors:
            self.sync_vendors(incremental_only=bool(self._vendors))
        with self._vendors_lock:
            return list(self._vendors.values())

    def cached_vendors(self) -> list[dict]:
        with self._vendors_lock:
            return list(self._vendors.values())

    def sync_vendors(self, incremental_only: bool = False) -> None:
        """Kuniga bir marta to'liq (3000+ vendor, ~35 sahifa), qolgan safar faqat o'zgarganlar.
        Tarmoq so'rovi paytida lug'at qulflanmaydi: sinxron ketayotganda ham invoyslar keshdan ishlayveradi."""
        with self._vendors_sync_lock:
            self._vendors_from_disk()
            now = time.time()
            meta = dict(self._vendors_meta)
            full = not self._vendors or (now - float(meta.get("full_at") or 0) > VENDORS_FULL_SYNC_S and not incremental_only)
            if full:
                rows = self.client.raw_all("list_vendors", max_pages=200)
                fresh = {str(v["id"]): self._vendor_row(v) for v in rows}
                with self._vendors_lock:
                    self._vendors = fresh
                meta["full_at"] = now
                log.info("Vendorlar to'liq yuklandi: %d", len(fresh))
            else:  # oxirgi sinxrondan beri o'zgarganlar (5 daqiqa zaxira bilan)
                since = dt.datetime.fromtimestamp(float(meta.get("synced_at") or now) - 300, dt.timezone.utc)
                rows = self.client.raw_all("list_vendors", {"filter": {"updated_at": {"gte": since.strftime("%Y-%m-%dT%H:%M:%SZ")}}})
                with self._vendors_lock:
                    for v in rows:
                        self._vendors[str(v["id"])] = self._vendor_row(v)
            meta["synced_at"] = now
            with self._vendors_lock:
                self._vendors_meta = meta
                snapshot = list(self._vendors.values())
            if full or rows:
                _write_json(VENDORS_FILE, {**meta, "vendors": snapshot})

    def add_vendor(self, v: dict) -> None:
        with self._vendors_lock:
            self._vendors[str(v["id"])] = self._vendor_row(v)

    # ---------------------------------------------------------------- service tasklar
    def sync_tasks(self) -> int:
        """Fleetio'dagi hamma service tasklarni faylga saqlaydi (arxivlanganlarsiz)."""
        rows = [t for t in self.client.raw_all("list_service_tasks", max_pages=200) if not t.get("archived_at")]
        tasks = sorted(({"id": t["id"], "name": " ".join((t.get("name") or "").split()), "standard": bool(t.get("standard"))}
                        for t in rows), key=lambda t: t["name"].lower())
        _write_json(TASKS_FILE, {"synced_at": _now_iso(), "source": "Fleetio GET /service_tasks (archived tasks excluded)",
                                 "count": len(tasks), "tasks": tasks}, indent=1)
        with self._book_lock:
            self._book = None
        log.info("Service tasklar saqlandi: %d", len(tasks))
        return len(tasks)

    def book(self) -> M.TaskBook:
        with self._book_lock:
            if self._book is None:
                if not TASKS_FILE.exists():
                    raise FleetioError("Service task list is missing (data/fleetio_service_tasks.json).")
                self._book = M.TaskBook(_read_json(TASKS_FILE, {}).get("tasks") or [], _read_json(EXAMPLES_FILE, {}),
                                        _read_json(ALIASES_FILE, {}))
            return self._book

    def learn_task(self, purpose: str, task_id: int) -> None:
        """Foydalanuvchi tugma bilan tanlagan moslikni eslab qoladi — keyingi safar so'ramaydi."""
        key = M.task_key(purpose)
        if not key:
            return
        aliases = _read_json(ALIASES_FILE, {})
        aliases[key] = int(task_id)
        _write_json(ALIASES_FILE, aliases, indent=1)
        self.book().aliases[key] = int(task_id)

    def warm(self) -> None:
        """Bot ishga tushganda fon rejimida: birinchi xabar kutib qolmasin."""
        try:
            if not TASKS_FILE.exists() or time.time() - TASKS_FILE.stat().st_mtime > TASKS_MAX_AGE_S:
                self.sync_tasks()
            self.book()
            for load in (self.me, self.completed_status, self.no_priority_id, self.vehicles, self.contacts, self.labels,
                         self.vendors):
                load()
            log.info("Work order ma'lumotnomalari tayyor: %d mashina, %d kontakt, %d label, %d vendor, %d task",
                     len(self.vehicles()), len(self.contacts()), len(self.labels()), len(self._vendors),
                     len(self.book().by_id))
        except Exception as e:  # noqa: BLE001 — isitish majburiy emas, birinchi xabarda qayta uriniladi
            log.warning("Work order ma'lumotnomalarini oldindan yuklab bo'lmadi: %s", e)

    def refresh(self) -> None:
        """Fon yangilash (REFRESH_EVERY_S da bir): invoys kelganda Fleetio'dan ro'yxat kutilmasin.
        Mashinalar va vendorlar har safar, kontaktlar/labellar har SLOW_REFRESH_EVERY-safar, tasklar haftada."""
        self._ticks += 1
        jobs = [("vehicles", self._vehicles.reload), ("vendors", self.sync_vendors)]
        if self._ticks % SLOW_REFRESH_EVERY == 0:
            jobs += [("contacts", self._contacts.reload), ("labels", self._labels.reload)]
        if not TASKS_FILE.exists() or time.time() - TASKS_FILE.stat().st_mtime > TASKS_MAX_AGE_S:
            jobs.append(("service tasks", self.sync_tasks))
        for name, job in jobs:
            try:
                job()
            except Exception as e:  # noqa: BLE001 — keyingi safar yana uriniladi; so'rov yo'lida eski kesh ishlaydi
                log.warning("Fon yangilash (%s) bo'lmadi: %s", name, e)


class WorkOrders:
    def __init__(self, client: FleetioClient, writer: FleetioWriter | None = None, llm_chat=None,
                 llm_models: list[str] | None = None, ledger: Ledger | None = None):
        self.client = client
        self.writer = writer or FleetioWriter()
        self.ref = RefData(client, self.writer)
        self.llm_chat = llm_chat          # LLMChat._chat (ixtiyoriy): service task tanlash uchun
        self.llm_models = llm_models      # arzon (kichik) model zanjiri; None = LLMChat'ning asosiy modellari
        self.ledger = ledger or Ledger()

    # ---------------------------------------------------------------- sozlamalar (.env, restartsiz)
    @staticmethod
    def allowed_chats() -> set[int]:
        return {int(x) for x in re.findall(r"-?\d+", env("WO_CHAT_IDS"))}

    @staticmethod
    def dry_run() -> bool:
        return env("WO_DRY_RUN").lower() in ("1", "true", "yes", "on")

    # ---------------------------------------------------------------- reja (faqat o'qish)
    async def plan(self, inv: Invoice) -> Plan:
        # service task'ni LLM bilan tanlash Fleetio tekshiruvlari bilan bir vaqtda boshlanadi (2-3 s tejaladi)
        llm = self._start_llm(inv)
        try:
            plan = await asyncio.to_thread(self._plan_sync, inv)
            if not plan.problems and plan.task is None and not plan.task_skipped:
                await self._pick_task(plan, llm)
        finally:
            _drop(llm)
        return plan

    def _start_llm(self, inv: Invoice) -> asyncio.Task | None:
        if self.llm_chat is None or inv.problems or not inv.purpose:
            return None
        try:
            book = self.ref.book()
        except FleetioError:
            return None
        if book.local(inv.purpose)[0] is not None:
            return None  # jamoaning eski tanlovi yoki o'rgatilgan moslik bor — LLM kerak emas
        return asyncio.create_task(self._ask_llm(book, inv.purpose))

    def _plan_sync(self, inv: Invoice) -> Plan:
        p = Plan(inv=inv, problems=list(inv.problems), warnings=list(inv.warnings))
        ref = self.ref
        try:
            p.me = ref.me()
            p.status = ref.completed_status()
            p.priority_id = ref.no_priority_id()
            self._plan_vehicle(p)
            self._plan_driver(p)
            self._plan_labels(p)
            self._plan_vendor(p)
            self._plan_task(p)
            if p.vehicle and inv.ref:
                dups = ref.client.raw("list_work_orders", {"per_page": 5, "filter": {
                    "vehicle_id": {"eq": p.vehicle["id"]}, "invoice_number": {"eq": inv.ref}}})
                recs = dups.get("records") if isinstance(dups, dict) else dups
                if recs:
                    p.duplicate = {"id": recs[0]["id"], "number": recs[0].get("number")}
        except FleetioError as e:
            p.problems.append(f"Fleetio is not available right now: {fmt.esc(str(e))}")
            return p
        p.rpc = RPC_EMERGENCY if inv.emergency else RPC_SCHEDULED
        if inv.date:
            p.when = ref.when(inv.date)
            age = (ref.today() - inv.date).days
            if age > OLD_DATE_WARN_DAYS:
                p.warnings.append(f"The date is {age} days ago — check that it is right.")
        if not inv.ref:
            p.warnings.append("No Ref number in the message — Invoice / PO number left empty.")
        return p

    def _plan_vehicle(self, p: Plan) -> None:
        inv = p.inv
        text, kind = M.pick_target(inv)
        if not text:
            return
        vehicle, why = M.match_vehicle(text, self.ref.vehicles())
        if vehicle is None:
            vehicle, why = M.match_vehicle(text, self.ref.vehicles(fresh=True))
        if vehicle is None and kind == "trailer" and inv.unit:  # treyler Fleetio'da yo'q -> unit'ga yoziladi
            unit, why2 = M.match_vehicle(inv.unit, self.ref.vehicles())
            if unit is not None:
                p.warnings.append(f"Trailer {fmt.esc(inv.trailer)} is not in Fleetio — the work order goes to unit {fmt.esc(str(unit['name']))}.")
                vehicle, kind = unit, "unit"
            else:
                why = why2
        if vehicle is None:
            p.problems.append(why)
            return
        p.vehicle, p.target_kind = vehicle, kind
        p.meter_void = self.ref.meter_required(vehicle)

    def _plan_driver(self, p: Plan) -> None:
        inv = p.inv
        if not inv.drivers:
            p.warnings.append("No driver in the message — Assigned To left empty.")
            return
        prefer = None
        if inv.unit:  # shu unit'ga hozir biriktirilgan haydovchi
            unit = p.vehicle if p.target_kind == "unit" else M.match_vehicle(inv.unit, self.ref.vehicles())[0]
            prefer = ((unit or {}).get("driver") or {}).get("id")
        for attempt in (False, True):
            contacts = self.ref.contacts(fresh=attempt)
            for name in inv.drivers:
                c = M.match_contact(name, contacts, prefer)
                if c:
                    p.driver = c
                    return
        p.warnings.append(f"Driver “{fmt.esc(' & '.join(inv.drivers))}” was not found (or is ambiguous) in Fleetio contacts — Assigned To left empty.")

    def _plan_labels(self, p: Plan) -> None:
        inv = p.inv
        if not inv.responsible:
            p.warnings.append("No Responsible line — the issue gets no label.")
            return
        found, new, skipped = M.match_labels(inv.responsible, self.ref.labels(), inv.drivers)
        if new:  # yangi yaratishdan oldin ro'yxatni yangilab yana tekshiramiz
            found, new, skipped = M.match_labels(inv.responsible, self.ref.labels(fresh=True), inv.drivers)
        p.labels, p.labels_new = found, new
        if skipped:
            p.warnings.append(f"Responsible text “{fmt.esc('; '.join(skipped))}” was not turned into a label.")

    def _plan_vendor(self, p: Plan) -> None:
        v = p.inv.vendor
        if v is None or not v.name:
            p.warnings.append("No shop name in the message — Vendor left empty.")
            return
        p.vendor = M.match_vendor(v, self.ref.vendors())
        if p.vendor is None:  # keshda yo'q: saytda hozirgina qo'shilgan bo'lishi mumkin — o'zgarishlarni olib qayta qidiramiz
            p.vendor = M.match_vendor(v, self.ref.vendors(sync=True))
        if p.vendor is None:
            p.vendor_new = M.vendor_payload(v)
            if not v.has_location:
                p.warnings.append(f"New vendor “{fmt.esc(v.name)}” is created with the name only (no address or phone in the message).")
        elif not p.vendor.get("service"):
            p.warnings.append(f"Vendor “{fmt.esc(str(p.vendor['name']))}” is not marked as a Service vendor in Fleetio.")

    def _plan_task(self, p: Plan) -> None:
        if not p.inv.purpose:
            p.task_skipped = True
            return
        p.task, p.task_how = self.ref.book().local(p.inv.purpose)

    # ---------------------------------------------------------------- service task: LLM
    async def _pick_task(self, p: Plan, llm: asyncio.Task | None = None) -> None:
        book = self.ref.book()
        purpose = p.inv.purpose
        picked: list[int] = []
        confidence = ""
        if llm is None and self.llm_chat is not None:
            llm = asyncio.create_task(self._ask_llm(book, purpose))
        if llm is not None:
            try:
                picked, confidence = await llm
            except Exception as e:  # noqa: BLE001 — LLM bo'lmasa foydalanuvchining o'zi tugmadan tanlaydi
                log.warning("Service task uchun LLM ishlamadi: %s", str(e)[:200])
        if picked and confidence in ("high", "medium"):
            p.task, p.task_how = book.by_id[picked[0]], "ai" if confidence == "high" else "ai?"
        else:
            p.task_options = book.options(purpose, picked)
            if not p.task_options:
                self.skip_task(p, f"No service task found for “{fmt.esc(purpose)}” — the work order has no line item.")

    @staticmethod
    def skip_task(p: Plan, why: str) -> None:
        p.task, p.task_skipped, p.task_options = None, True, []
        p.warnings.append(why)

    async def _ask_llm(self, book: M.TaskBook, purpose: str) -> tuple[list[int], str]:
        examples = "\n".join(f'- "{t}" -> {tid} {book.by_id[tid]["name"]} (x{n})' for t, tid, n in book.similar_examples(purpose))
        team = "\n".join(f"{tid} | {book.by_id[tid]['name']} | {n}" for tid, n in book.team_tasks())
        catalog = "\n".join(f"{tid} | {book.by_id[tid]['name']}" for tid in book.catalog_matches(purpose, 40))
        user = (f"FOR: {purpose}\n\nEXAMPLES:\n{examples or '(none)'}\n\nTEAM (id | name | uses):\n{team}\n\n"
                f"CATALOG (id | name):\n{catalog or '(none)'}")
        resp = await self.llm_chat([{"role": "system", "content": TASK_PROMPT}, {"role": "user", "content": user}],
                                   max_tokens=200, models=self.llm_models)
        raw = resp.choices[0].message.content or ""
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group()) if m else {}
        ids = [data.get("task_id")] + list(data.get("alternatives") or [])
        ids = [t["id"] for t in (book.task(i) for i in ids) if t]
        main = book.task(data.get("task_id"))
        log.info("Service task (LLM): %r -> %s [%s]", purpose, main["name"] if main else None, data.get("confidence"))
        if main is None:
            return ids, "low"
        return ids, str(data.get("confidence") or "low").lower()

    # ---------------------------------------------------------------- bajarish (yozish)
    async def execute(self, plan: Plan, files: list[FileBlob], key: str, uploaded: list[dict] | None = None) -> dict:
        """Rejani Fleetio'da bajaradi. -> job (jurnal yozuvi). Xato bo'lsa job['status']='failed', job['error'].
        uploaded — oldindan (reja paytida) yuklangan fayllar (documents_attributes), files bilan bir tartibda."""
        return await asyncio.to_thread(self._execute_sync, plan, files, key, uploaded or [])

    def _execute_sync(self, plan: Plan, files: list[FileBlob], key: str, uploaded: list[dict] | None = None) -> dict:
        job = self.ledger.get(key) or {}
        job.setdefault("steps", {})
        job.update(status="running", error="", unit=str(plan.vehicle["name"]), ref=plan.inv.ref,
                   amount=str(plan.inv.amount or ""), date=plan.inv.date.isoformat())
        if uploaded and not job["steps"].get("documents"):
            job["steps"]["documents"] = list(uploaded)
        self.ledger.put(key, job)
        try:
            self._run_steps(plan, files, job, key)
            job["status"] = "created"
            job.pop("failed_step", None)
            job.pop("uncertain", None)
        except StepFailed as e:
            job.update(status="failed", error=e.message, failed_step=e.step, uncertain=e.uncertain)
            log.error("Work order yaratilmadi (%s, %s): %s", key, e.step, e.message)
        self.ledger.put(key, job)
        return job

    def _step(self, name: str, fn):
        try:
            return fn()
        except FleetioUncertain as e:
            raise StepFailed(name, str(e), uncertain=True) from None
        except FleetioError as e:
            raise StepFailed(name, str(e)) from None

    def _run_steps(self, plan: Plan, files: list[FileBlob], job: dict, key: str) -> None:
        steps, inv, w = job["steps"], plan.inv, self.writer
        save = lambda: self.ledger.put(key, job)  # noqa: E731

        # 1) vendor (xabarda ustaxona bo'lmasa — bo'sh)
        if plan.vendor:
            steps["vendor"] = {"id": plan.vendor["id"], "name": plan.vendor["name"], "created": False}
        elif plan.vendor_new and not steps.get("vendor"):
            steps["vendor"] = self._step("vendor", lambda: self._create_vendor(plan))
            save()

        # 2) label(lar)
        label_ids = [lab["id"] for lab in plan.labels]
        made = steps.setdefault("labels", {})
        for name in plan.labels_new:
            if name not in made:
                made[name] = self._step("label", lambda n=name: self._create_label(n))
                save()
            label_ids.append(made[name])
        steps["label_ids"] = label_ids

        # 3) fayl(lar)
        docs = steps.setdefault("documents", [])
        for f in files[len(docs):]:
            docs.append(self._step("file upload", lambda f=f: w.upload(f.data, f.name, f.mime)))
            save()

        # 4) issue ("For" bo'lmasa issue yo'q — summary Fleetio'da majburiy; fayl va label work order'ga)
        if inv.purpose and not steps.get("issue"):
            if job.get("failed_step") == "issue" and job.get("uncertain"):
                steps["issue"] = self._step("issue", lambda: self._find_recent_issue(plan))
            if not steps.get("issue"):
                body = {"asset_id": plan.vehicle["id"], "asset_type": "Vehicle", "summary": inv.purpose,
                        "reported_at": plan.when, "reported_by_id": plan.me["contact_id"],
                        "issue_priority_id": plan.priority_id, "label_ids": label_ids,
                        "custom_fields": {"reference_number": inv.ref} if inv.ref else None,
                        "documents_attributes": docs}
                issue = self._step("issue", lambda: w.create("create_issue", _clean(body)))
                steps["issue"] = {"id": issue["id"], "number": issue.get("number")}
            save()

        # 5) work order
        if not steps.get("work_order"):
            if job.get("failed_step") == "work order" and job.get("uncertain") and inv.ref:
                found = self._step("work order", lambda: self.client.raw("list_work_orders", {"per_page": 5, "filter": {
                    "vehicle_id": {"eq": plan.vehicle["id"]}, "invoice_number": {"eq": inv.ref}}}))
                recs = found.get("records") if isinstance(found, dict) else found
                if recs:
                    steps["work_order"] = self._wo_summary(recs[0])
            if not steps.get("work_order"):
                wo = self._step("work order", lambda: w.create("create_work_order", self._wo_body(plan, steps)))
                steps["work_order"] = self._wo_summary(wo)
            save()

        # 6) tekshiruv: issue work order bilan yopildimi (xato bo'lsa natijaga ta'sir qilmaydi)
        if not steps.get("issue"):
            return
        try:
            issue = self.client.raw("get_issue", {"id": steps["issue"]["id"]})
            steps["issue"].update(number=issue.get("number"), state=issue.get("state"),
                                  documents=issue.get("documents_count"))
        except FleetioError as e:
            log.warning("Issue holatini o'qib bo'lmadi: %s", e)

    def _create_vendor(self, plan: Plan) -> dict:
        with self.ref.create_lock:  # parallel invoyslar: birinchisi yaratadi, keyingisi o'shani topadi
            same = M.match_vendor(plan.inv.vendor, self.ref.cached_vendors())
            if same is not None:
                return {"id": same["id"], "name": same["name"], "created": False}
            try:
                v = self.writer.create("create_vendor", plan.vendor_new)
            except FleetioUncertain:
                raise
            except FleetioError as e:  # masalan "name has already been taken": shu nomli mavjud vendorga tushamiz
                same = M.same_name_vendor(plan.inv.vendor, self.ref.vendors(sync=True))
                if same is None:
                    raise
                log.warning("Vendor yaratilmadi (%s) — mavjud '%s' ishlatiladi", e, same["name"])
                return {"id": same["id"], "name": same["name"], "created": False}
            self.ref.add_vendor(v)
            return {"id": v["id"], "name": v.get("name") or plan.vendor_new["name"], "created": True}

    def _create_label(self, name: str) -> int:
        with self.ref.create_lock:
            for x in self.ref.labels():  # parallel invoys hozirgina yaratgan bo'lishi mumkin
                if x["name"].strip().lower() == name.lower():
                    return x["id"]
            try:
                lab = self.writer.create("create_label", {"name": name})
            except FleetioUncertain:
                raise
            except FleetioError:  # allaqachon bor bo'lishi mumkin
                for x in self.ref._labels.reload():
                    if x["name"].strip().lower() == name.lower():
                        return x["id"]
                raise
            self.ref.add_label(lab)
            return lab["id"]

    def _find_recent_issue(self, plan: Plan) -> dict | None:
        """Oldingi urinishda javob kelmagan bo'lsa: issue aslida yaratilganmi (shu mashina, shu summary, oxirgi 1 soat)."""
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = self.client.raw("list_issues", {"per_page": 20, "filter": {"vehicle_id": {"eq": plan.vehicle["id"]},
                                                                         "created_at": {"gte": since}}})
        for x in (res.get("records") if isinstance(res, dict) else res) or []:
            if (x.get("summary") or x.get("name") or "").strip().lower() == plan.inv.purpose.lower():
                return {"id": x["id"], "number": x.get("number")}
        return None

    @staticmethod
    def _wo_summary(wo: dict) -> dict:
        rpc = wo.get("vmrs_repair_priority_class") or {}
        return {"id": wo["id"], "number": wo.get("number"), "status": wo.get("work_order_status_name"),
                "priority": rpc.get("name"), "total": wo.get("total_amount")}

    def _wo_body(self, plan: Plan, steps: dict) -> dict:
        inv = plan.inv
        body = {
            "vehicle_id": plan.vehicle["id"],
            "work_order_status_id": plan.status["id"],
            "vmrs_repair_priority_class_id": plan.rpc[0],
            "issued_by_id": plan.me["contact_id"],
            "issued_at": plan.when,
            "started_at": plan.when,
            "expected_completed_at": plan.when,
            "completed_at": plan.when,
            "contact_id": plan.driver["id"] if plan.driver else None,
            "vendor_id": (steps.get("vendor") or {}).get("id"),
            "invoice_number": inv.ref,
            "purchase_order_number": inv.ref,
            "description": inv.description,
        }
        if steps.get("issue"):
            body["issue_ids"] = [steps["issue"]["id"]]
        else:  # issue yo'q: fayl va label work order'ning o'ziga
            body["documents_attributes"] = steps.get("documents")
            body["label_ids"] = steps.get("label_ids")
        if plan.task:
            item = {"type": "WorkOrderServiceTaskLineItem", "item_type": "ServiceTask", "item_id": plan.task["id"]}
            if inv.amount is not None:
                item["subtotal"] = float(inv.amount)
            body["work_order_line_items_attributes"] = [item]
        if plan.meter_void:  # jamoa service entry'larda ham shunday qiladi: qiymat 0, "void" belgilangan
            body["starting_meter_entry_attributes"] = {"value": 0, "void": True}
            body["ending_meter_same_as_start"] = True
        return _clean(body)


def _clean(body: dict) -> dict:
    return {k: v for k, v in body.items() if v not in (None, "", [], {})}


def _drop(task: asyncio.Task | None) -> None:
    """Kerak bo'lmay qolgan fon vazifasi: tugamagan bo'lsa bekor, tugagan bo'lsa xatosi "o'qiladi" (log shovqini yo'q)."""
    if task is None:
        return
    if task.done():
        if not task.cancelled():
            task.exception()
    else:
        task.cancel()


# ====================================================================== matnlar (Telegram HTML, inglizcha)

def _money(x) -> str:
    return f"${x:,.2f}"


def _task_note(how: str) -> str:
    return {"ai?": " <i>(chosen by AI — please check)</i>", "ai": " <i>(chosen by AI)</i>"}.get(how, "")


def _plan_lines(p: Plan) -> list[str]:
    """Reja/natijadagi umumiy qatorlar."""
    inv, e = p.inv, fmt.esc
    lines = [f"📅 {inv.date:%m/%d/%Y} · {e(str(p.status['name']))} · {p.rpc[1]}"]
    if p.task:
        cost = f"<b>{_money(inv.amount)}</b>" if inv.amount is not None else "no amount"
        lines.append(f"🛠 {e(p.task['name'])} — {cost}{_task_note(p.task_how)}")
    if p.vendor or p.vendor_new:
        vendor = p.vendor["name"] if p.vendor else p.vendor_new["name"]
        lines.append(f"🏪 {e(str(vendor))}" + ("" if p.vendor else " <i>(new vendor)</i>"))
    who = f"Assigned to {e(p.driver['name'])} · " if p.driver else ""
    lines.append(f"👤 {who}Issued by {e(p.me['name'])}")
    if inv.ref:
        lines.append(f"🧾 Invoice / PO # {e(inv.ref)}")
    return lines


def format_problems(p: Plan) -> str:
    out = ["⚠️ <b>Work order not created</b>"] + [f"• {x}" for x in p.problems]
    out.append("\nFix the message (edit it) or send it again.")
    return "\n".join(out)


def format_preview(p: Plan, n_files: int) -> str:
    """WO_DRY_RUN=1: Fleetio'ga hech narsa yozilmaydi, nima yaratilishi ko'rsatiladi."""
    e = fmt.esc
    labels = [x["name"] for x in p.labels] + [f"{x} (new)" for x in p.labels_new]
    out = [f"🧪 <b>Test mode — nothing was saved.</b> Would create a work order for unit <b>{e(str(p.vehicle['name']))}</b>:"]
    out += _plan_lines(p)
    issue = f"Issue “{e(p.inv.purpose)}”" if p.inv.purpose else "No issue"
    out.append(f"📌 {issue} · label {e(', '.join(labels) or '—')} · {n_files} file(s)")
    out += [f"ℹ️ {w}" for w in p.warnings]
    return "\n".join(out)


def format_result(p: Plan, job: dict) -> str:
    e, steps = fmt.esc, job.get("steps") or {}
    wo, issue = steps.get("work_order") or {}, steps.get("issue") or {}
    if job.get("status") == "created":
        out = [f"✅ <b>Work order {e(str(wo.get('number') or ''))}</b> created · unit <b>{e(str(p.vehicle['name']))}</b>"]
        out += _plan_lines(p)
        labels = [x["name"] for x in p.labels] + p.labels_new
        bits = [f"📌 Issue {e(str(issue.get('number') or ''))} “{e(p.inv.purpose)}”" if issue else "📌 No issue"]
        if labels:
            bits.append("label " + e(", ".join(labels)))
        bits.append(f"{len(steps.get('documents') or [])} file(s)")
        if issue.get("state"):
            bits.append(e(str(issue["state"])))
        out.append(" · ".join(bits))
        out.append(f'🔗 <a href="{web_link("work_orders", wo["id"])}">Open in Fleetio</a>')
        notes = list(p.warnings)
        if wo.get("status") and str(wo["status"]).lower() != str(p.status["name"]).lower():
            notes.append(f"Fleetio saved the status as {e(str(wo['status']))} (expected {e(str(p.status['name']))}).")
        if wo.get("priority") and wo["priority"] != p.rpc[1]:
            notes.append(f"Fleetio saved the repair priority as {e(str(wo['priority']))} (expected {p.rpc[1]}).")
        if issue.get("state") and str(issue["state"]).lower() == "open":
            notes.append("The issue is still Open in Fleetio (it was not resolved by the work order).")
        return "\n".join(out + [f"ℹ️ {n}" for n in notes])
    out = [f"⚠️ <b>Work order not finished</b> — the “{e(str(job.get('failed_step')))}” step failed:",
           e(str(job.get("error") or ""))[:900]]
    saved = []
    if (steps.get("vendor") or {}).get("created"):
        saved.append(f"vendor “{e(str(steps['vendor']['name']))}”")
    saved += [f"label “{e(n)}”" for n in (steps.get("labels") or {})]
    if issue:
        saved.append(f"issue {e(str(issue.get('number') or issue.get('id')))}")
    if saved:
        out.append("Already saved in Fleetio: " + ", ".join(saved) + ". Retry continues from here without creating them again.")
    if job.get("uncertain"):
        out.append("Fleetio did not answer, so this step may have been saved anyway — Retry checks first.")
    return "\n".join(out)


# ====================================================================== qo'lda ishga tushirish

def _cli(argv: list[str]) -> None:
    """python -m app.workorders tasks            — service tasklar ro'yxatini Fleetio'dan yangilash
       python -m app.workorders examples [N]     — oxirgi N (600) service entry'dan "For" -> task misollarini qayta qurish
       python -m app.workorders check FILE       — fayldagi xabar matni uchun rejani ko'rsatish (hech narsa yozilmaydi)"""
    from .wo_parser import parse_invoice

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    svc = WorkOrders(FleetioClient())
    cmd = argv[0] if argv else ""
    if cmd == "tasks":
        print(f"{svc.ref.sync_tasks()} service tasks saved to {TASKS_FILE}")
    elif cmd == "examples":
        n = int(argv[1]) if len(argv) > 1 else 600
        entries, cursor = [], None
        while len(entries) < n:
            params = {"per_page": 100, "sort": {"created_at": "desc"}}
            if cursor:
                params["start_cursor"] = cursor
            page = svc.client.raw("list_service_entries", params)
            entries += page.get("records") or []
            cursor = page.get("next_cursor")
            if not cursor:
                break
        rows = []
        for i, entry in enumerate(entries[:n], 1):
            full = svc.client.raw("get_service_entry", {"id": entry["id"]})
            rows.append({"for": parse_invoice(entry.get("general_notes") or "").purpose,
                         "task_ids": [(li.get("service_task") or {}).get("id") for li in full.get("service_entry_line_items") or []]})
            if i % 50 == 0:
                print(f"  {i}/{min(n, len(entries))}")
            time.sleep(0.15)
        data = {"generated_at": _now_iso(),
                "source": f"{len(rows)} newest Fleetio service entries: the 'For:' text in the notes -> service task the staff chose",
                **M.build_examples(rows)}
        _write_json(EXAMPLES_FILE, data, indent=1)
        print(f"{len(data['examples'])} examples saved to {EXAMPLES_FILE}")
    elif cmd == "check" and len(argv) > 1:
        text = Path(argv[1]).read_text(encoding="utf-8")
        inv = parse_invoice(text, today=svc.ref.today())
        plan = asyncio.run(svc.plan(inv))
        if plan.problems:
            print(fmt.strip_html(format_problems(plan)))
        else:
            if plan.task is None and plan.task_options:
                print("Service task not decided; options:", [t["name"] for t in plan.task_options])
                plan.task, plan.task_how = (plan.task_options or [{"id": 0, "name": "?"}])[0], "chosen"
            print(fmt.strip_html(format_preview(plan, 1)))
            print("\nIssue label ids:", [x["id"] for x in plan.labels], "| new labels:", plan.labels_new)
            steps = {}
            if plan.vendor or plan.vendor_new:
                steps["vendor"] = {"id": (plan.vendor or {}).get("id", "<new>")}
            if inv.purpose:
                steps["issue"] = {"id": "<new>"}
            print("Work order body:", json.dumps(svc._wo_body(plan, steps), ensure_ascii=False, indent=1))
            if plan.duplicate:
                print("DUPLICATE: work order", plan.duplicate["number"], "already has this invoice number for this vehicle")
    else:
        print(_cli.__doc__)


if __name__ == "__main__":
    import sys

    _cli(sys.argv[1:])

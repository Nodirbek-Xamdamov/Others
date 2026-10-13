"""Work order oqimi — internetsiz testlar (soxta Fleetio, soxta Telegram, soxta LLM yo'q).

    python -m tests.test_workorders
"""
import asyncio
import datetime as dt
import io
import json
import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.methods import EditMessageText, GetFile, SendMessage  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Document, File, Message, Update, User  # noqa: E402

from app import fleetio, workorders  # noqa: E402
from app import wo_match as M  # noqa: E402
from app.bot import FleetioState, build_fleetio_dispatcher  # noqa: E402
from app.fleetio import FleetioError  # noqa: E402
from app.fleetio_write import FleetioWriter  # noqa: E402
from app.wo_bot import WOBot  # noqa: E402
from app.wo_parser import looks_like_invoice, parse_amount, parse_date, parse_invoice  # noqa: E402
from app.workorders import Ledger, WorkOrders  # noqa: E402

ok = 0
tmp = Path(tempfile.mkdtemp())
env_file = tmp / ".env"
env_file.write_text("SECUREFLEET_KEY=k\nSECUREFLEET_ACCOUNT_TOKEN=acct\nWO_CHAT_IDS=1\n", encoding="utf-8")
fleetio.ENV_FILE = env_file
workorders.VENDORS_FILE = tmp / "vendors.json"
workorders.ALIASES_FILE = tmp / "aliases.json"

DAY = dt.date.today() - dt.timedelta(days=2)
SAMPLE = f"""Unit# 3968
Driver: Reynaldo Suarez
Date: {DAY:%m/%d/%Y}
Amount: $ 70
For: Truck tire repair
Note: Paid with EFS
Money Transfer code 3885738103
Ref# 216117
Responsible: Owner of the truck

Tread Source Commercial Tires & Service
1305 S Shiloh Rd, Garland, TX 75042, United States
+14692687464"""


def check(cond, name):
    global ok
    assert cond, name
    ok += 1
    print("  ✓", name)


print("1) Xabarni ajratish")
inv = parse_invoice(SAMPLE)
check(inv.ok and inv.unit == "3968" and inv.drivers == ["Reynaldo Suarez"] and inv.date == DAY, "unit / driver / date")
check(str(inv.amount) == "70.00" and inv.ref == "216117" and inv.purpose == "Truck tire repair", "amount / ref / for")
check(inv.note == "Paid with EFS\nMoney Transfer code 3885738103" and inv.responsible == "Owner of the truck", "note davomi, responsible")
v = inv.vendor
check(v.name == "Tread Source Commercial Tires & Service" and v.street == "1305 S Shiloh Rd" and v.city == "Garland"
      and v.region == "Texas" and v.postal_code == "75042" and v.country == "US" and v.phone_pretty == "(469) 268-7464",
      "ustaxona bloki: nom, manzil, telefon")
check("Tread Source" not in inv.description and inv.description.startswith("Unit# 3968"), "description — ustaxona blokisiz matn")
check(not inv.emergency and parse_invoice(SAMPLE.replace("Truck tire repair", "RS for tire repair")).emergency
      and parse_invoice(SAMPLE.replace("Paid with EFS", "Road side call")).emergency, "RS / Road side -> Emergency")
check([str(parse_amount(x)) for x in ("$ 1,234.56", "$40,00", "714.14 $", "$\xa0249.08", "525")] ==
      ["1234.56", "40.00", "714.14", "249.08", "525.00"] and parse_amount("$7 38.38") is None, "summa yozilishlari")
check(parse_date("10/5/2026") == dt.date(2026, 10, 5) and parse_date("2026-10-05") == dt.date(2026, 10, 5)
      and parse_date("10/11/202") is None, "sana yozilishlari")
old = parse_invoice("Truck #: 8658\nTrailer#: 44319\nDrivers: A B & C D\nDate: 10/11/202\nAmount: $58.00\nFor: Parking\n"
                    "Report Reference # 4011078343,\nResponsible: Company\nCAC")
check(old.unit == "8658" and old.trailer == "44319" and old.drivers == ["A B", "C D"] and old.ref == "4011078343"
      and old.vendor is None and old.responsible == "Company CAC" and any("not a valid date" in p for p in old.problems),
      "eski formatlar: Truck #, Trailer#, Report Reference, 'CAC' ustaxona emas, xato sana")
check(looks_like_invoice(SAMPLE) and not looks_like_invoice("Unit 3968 photo") and not looks_like_invoice(None), "invoys formatini tanish")

print("2) Moslash")
VEH = [{"id": 1, "name": "3968", "vehicle_type_id": 10, "driver": {"id": 21}}, {"id": 2, "name": "9693 JCE"},
       {"id": 3, "name": "9693 RVW"}, {"id": 4, "name": "560542", "vehicle_type_id": 11}, {"id": 5, "name": "0839 CSA"},
       {"id": 6, "name": "4201 TEL"}]
check(M.match_vehicle("3968", VEH)[0]["id"] == 1 and M.match_vehicle("4201", VEH)[0]["id"] == 6
      and M.match_vehicle("839", VEH)[0]["id"] == 5 and M.match_vehicle("9693 jce", VEH)[0]["id"] == 2, "unit -> vehicle")
none, why = M.match_vehicle("9693", VEH)
check(none is None and "several" in why and M.match_vehicle("7777", VEH)[0] is None, "noaniq / topilmagan unit — taxminsiz")
CON = [{"id": 21, "name": "Reynaldo Lazaro Suarez", "vehicle_operator": True}, {"id": 22, "name": "Reynaldo Suarez Lazaro"},
       {"id": 23, "name": "Jose Manuel Garcia"}, {"id": 24, "name": "Jose Luis Garcia"}, {"id": 25, "name": "Darell Kenard Smith"}]
check(M.match_contact("Reynaldo Suarez", CON)["id"] == 21 and M.match_contact("Reinaldo Suares", CON)["id"] == 21, "haydovchi (o'rta ism, imlo)")
check(M.match_contact("Jose Garcia", CON) is None and M.match_contact("Jose Garcia", CON, 24)["id"] == 24
      and M.match_contact("LaDarrell Smith", CON) is None and M.match_contact("Jose", CON) is None, "noaniq ism — bo'sh qoladi")
LAB = [{"id": 31, "name": "Owner of the truck"}, {"id": 32, "name": "Company"}, {"id": 33, "name": "Driver"}]
f, n, s = M.match_labels("Company($38.60), Owner of th truck($42.50)", LAB)
check([x["id"] for x in f] == [32, 31] and not n and M.match_labels("Reynaldo Suarez", LAB, ["Reynaldo Suarez"])[0][0]["id"] == 33
      and M.match_labels("Geovanys Pena", LAB)[1] == ["Geovanys Pena"], "Responsible -> label(lar)")
VEN = [{"id": 41, "name": "Tread Source Commercial Tires & Service", "postal_code": "75042", "phone": "(469) 268-7464", "service": True},
       {"id": 42, "name": "TA Truck Service", "postal_code": "79556", "service": True},
       {"id": 43, "name": "DB Truck & Trailer Repair", "service": True}]
vv = parse_invoice(SAMPLE.replace("Tires & Service", "Tires and Service LLC")).vendor
check(M.match_vendor(v, VEN)["id"] == 41 and M.match_vendor(vv, VEN)["id"] == 41, "vendor: '&' / 'and' / LLC farqi")
other = parse_invoice(SAMPLE.replace("Tread Source Commercial Tires & Service", "TA Truck Service").replace("75042", "10001")
                      .replace("+14692687464", "")).vendor
abc = parse_invoice(SAMPLE.replace("Tread Source Commercial Tires & Service", "ABC Truck & Trailer Repair")).vendor
check(M.match_vendor(other, VEN) is None and M.match_vendor(abc, VEN) is None, "boshqa filial / o'xshash umumiy nom — yangi vendor")
book = M.TaskBook([{"id": 1, "name": "Tire Repair (s)"}, {"id": 2, "name": "Tire Replacement"}],
                  M.build_examples([{"for": "Trailer tire repair", "task_ids": [1]}, {"for": "tire repair.", "task_ids": [1]},
                                    {"for": "Trailer tires replacement", "task_ids": [2]}]))
check(book.local("Truck tire repair")[0]["id"] == 1 and book.local("Truck tires replacement")[0] is None
      and book.options("Truck tires replacement")[0]["id"] == 2, "service task: eski tanlovlar bo'yicha, aks holda nomzodlar")

print("3) Yozish klienti")


class FakeHTTP:
    def __init__(self, fail=None):
        self.reqs, self.fail = [], fail

    def __call__(self, req, timeout=None):
        self.reqs.append(req)
        if self.fail and self.fail in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 422, "x", {}, io.BytesIO(b'{"errors":{"completed_at":["is invalid"]}}'))
        if "uploads/policies" in req.full_url:
            body = {"policy": "P", "signature": "S", "path": "/api/A/"}
        elif "amazonaws" in req.full_url:
            body = {"url": "https://cdn.example/F1", "type": "application/pdf"}
        else:
            body = {"id": 500, "number": "#16"}
        return io.BytesIO(json.dumps(body).encode())


http = FakeHTTP()
w = FleetioWriter(opener=http)
check(w.create("create_label", {"name": "x"})["id"] == 500 and http.reqs[-1].get_method() == "POST"
      and http.reqs[-1].full_url.endswith("/labels") and json.loads(http.reqs[-1].data) == {"name": "x"}, "POST /labels (JSON)")
for bad in ("delete_work_order", "update_vehicle", "create_vehicle"):
    try:
        w.create(bad, {})
        check(False, bad)
    except FleetioError:
        check(len(http.reqs) == 1, f"ruxsat etilmagan amal yuborilmaydi: {bad}")
doc = w.upload(b"%PDF", "inv.pdf", "application/pdf")
check(doc["file_url"] == "https://cdn.example/F1" and doc["file_size"] == 4 and "policy=P" in http.reqs[-1].full_url
      and http.reqs[-1].data == b"%PDF" and "filename=inv.pdf" in http.reqs[-1].full_url, "fayl: policy -> yuklash -> file_url")
try:
    FleetioWriter(opener=FakeHTTP(fail="/work_orders")).create("create_work_order", {})
    check(False, "422")
except FleetioError as e:
    check("completed_at: is invalid" in str(e), "422 xatosi maydon nomi bilan ko'rsatiladi")

print("4) Telegram -> Fleetio (soxta)")


class FakeClient:
    """FleetioClient o'rniga: raw / raw_all."""

    def __init__(self):
        self.dups = []

    def raw_all(self, name, params=None, max_pages=60):
        return {"list_work_order_statuses": [{"id": 8, "name": "Open"}, {"id": 9, "name": "Completed", "completed": True}],
                "list_issue_priorities": [{"id": 5, "slug": "no-priority", "name": "No Priority"}],
                "list_vehicles": VEH, "list_contacts": CON, "list_labels": [dict(x) for x in LAB], "list_vendors": VEN,
                "list_accounts": [{"require_a_meter_entry_on_service_entry_or_completed_work_order": True,
                                   "excluded_vehicle_type_ids_from_required_meter_readings": [10]}]}[name]

    def raw(self, name, params=None):
        if name == "list_work_orders":
            return {"records": self.dups}
        if name == "get_issue":
            return {"number": "#3476", "state": "Resolved", "documents_count": 1}
        return {"records": []}


class FakeWriter:
    def __init__(self):
        self.calls, self.fail = [], None

    def current_user(self):
        return {"contact_id": 77, "first_name": "Charlie", "last_name": "Pearson", "time_zone_name": "Eastern Time (US & Canada)"}

    def create(self, op, body):
        if self.fail == op:
            raise FleetioError("Fleetio rejected the data (422): completed_at: is invalid")
        self.calls.append((op, body))
        return {"id": 100 + len(self.calls), "number": f"#{len(self.calls)}", "name": body.get("name"),
                "work_order_status_name": "Completed", "vmrs_repair_priority_class": {"name": "Scheduled"}}

    def upload(self, data, name, mime):
        self.calls.append(("upload", name))
        return {"name": name, "file_url": "https://cdn.example/F1", "file_name": name, "file_mime_type": mime, "file_size": len(data)}


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u", file_path="docs/f.pdf")
        if isinstance(method, (SendMessage, EditMessageText)):
            self.sent.append(method)
            return Message(message_id=1000 + len(self.sent), date=dt.datetime.now(), chat=Chat(id=1, type="private"),
                           text=method.text).as_(bot)
        return True

    async def stream_content(self, *a, **k):
        yield b"%PDF-fake"

    async def close(self):
        pass


async def flow():
    session = FakeSession()
    bot = Bot("123456:TEST", session=session)
    writer, client = FakeWriter(), FakeClient()
    svc = WorkOrders(client, writer, ledger=Ledger(tmp / "log.json"))
    wo = WOBot(svc)
    dp = build_fleetio_dispatcher(FleetioState(None), None, wo)
    user = User(id=42, is_bot=False, first_name="Test")
    n = [10]

    async def send(caption, chat_id=1, file=True, text=None):
        n[0] += 1
        d = Document(file_id="F1", file_unique_id="U1", file_name="invoice.pdf", mime_type="application/pdf", file_size=9) if file else None
        m = Message(message_id=n[0], date=dt.datetime.now(), chat=Chat(id=chat_id, type="private"), from_user=user,
                    document=d, caption=caption if file else None, text=text)
        await dp.feed_update(bot, Update(update_id=n[0], message=m))
        return session.sent[-1]

    async def press(data):
        n[0] += 1
        msg = Message(message_id=1, date=dt.datetime.now(), chat=Chat(id=1, type="private"), text="x")
        await dp.feed_update(bot, Update(update_id=n[0], callback_query=CallbackQuery(
            id="1", from_user=user, chat_instance="x", data=data, message=msg)))
        return session.sent[-1]

    m = await send(SAMPLE, chat_id=999)
    check("not enabled" in m.text and "999" in m.text and not writer.calls, "ruxsat yo'q chat: yozilmaydi, chat ID ko'rsatiladi")

    m = await send(SAMPLE)
    ops = [c[0] for c in writer.calls]
    check(ops == ["upload", "create_issue", "create_work_order"], "mavjud vendor/label: fayl -> issue -> work order")
    issue, body = writer.calls[1][1], writer.calls[2][1]
    when = body["issued_at"]
    check(issue == {"asset_id": 1, "asset_type": "Vehicle", "summary": "Truck tire repair", "reported_at": when,
                    "reported_by_id": 77, "issue_priority_id": 5, "label_ids": [31],
                    "custom_fields": {"reference_number": "216117"}, "documents_attributes": issue["documents_attributes"]}
          and issue["documents_attributes"][0]["file_url"] == "https://cdn.example/F1", "issue maydonlari + fayl")
    check(when.startswith(f"{DAY.isoformat()}T00:00:00-0") and body["started_at"] == body["expected_completed_at"] == body["completed_at"] == when,
          "hamma sanalar = xabardagi Date")
    check(body["vehicle_id"] == 1 and body["work_order_status_id"] == 9 and body["vmrs_repair_priority_class_id"] == 1
          and body["issued_by_id"] == 77 and body["contact_id"] == 21 and body["vendor_id"] == 41
          and body["invoice_number"] == body["purchase_order_number"] == "216117" and body["issue_ids"] == [102]
          and "starting_meter_entry_attributes" not in body and body["description"].startswith("Unit# 3968"),
          "work order: vehicle, Completed, Scheduled, issued by, assigned to, vendor, invoice/PO, issue")
    li = body["work_order_line_items_attributes"]
    task_id = svc.ref.book().local("Truck tire repair")[0]["id"]
    check(li == [{"type": "WorkOrderServiceTaskLineItem", "item_type": "ServiceTask", "item_id": task_id, "subtotal": 70.0}],
          "line item: service task + subtotal = amount")
    check("Work order #3" in m.text and "Tire Repair" in m.text and "$70.00" in m.text and "/acct/work_orders/103" in m.text
          and "Resolved" in m.text, "javob: raqam, task, summa, havola, issue holati")

    before = len(writer.calls)
    m = await send(SAMPLE.replace("Unit# 3968", "Unit# 9693").replace("$ 70", "seventy"))
    check(len(writer.calls) == before and "several vehicles" in m.text and "Amount" in m.text, "xatolar birga ko'rsatiladi, hech narsa yozilmaydi")
    m = await send(SAMPLE.rsplit("\n\n", 1)[0])
    check(len(writer.calls) == before and "Vendor is missing" in m.text, "ustaxona yo'q -> yaratilmaydi")
    m = await send(None, file=False, text=SAMPLE)
    check(len(writer.calls) == before and "file is missing" in m.text, "faylsiz matn -> fayl so'raladi")

    client.dups = [{"id": 900, "number": "#15"}]
    m = await send(SAMPLE)
    check(len(writer.calls) == before and "already has invoice number" in m.text, "takror invoys raqami -> so'raladi")
    client.dups = []
    writer.fail = "create_work_order"
    token = m.reply_markup.inline_keyboard[0][0].callback_data
    m = await press(token)
    check("not finished" in m.text and "completed_at" in m.text and "issue" in m.text and m.reply_markup is not None,
          "work order rad etilsa: xato matni + Retry, issue saqlangani aytiladi")
    issues = sum(1 for c in writer.calls if c[0] == "create_issue")
    writer.fail = None
    m = await press(m.reply_markup.inline_keyboard[0][0].callback_data)
    check("created" in m.text and sum(1 for c in writer.calls if c[0] == "create_issue") == issues
          and [c[0] for c in writer.calls[-1:]] == ["create_work_order"], "Retry: issue qayta yaratilmaydi")

    before = len(writer.calls)
    new = (SAMPLE.replace("Unit# 3968", "Unit: 3968\nTrailer: 560542").replace("Truck tire repair", "Trailer tire repair")
           .replace("Paid with EFS", "Paid with EFS, RC")
           .replace("Owner of the truck", "Geovanys Pena").replace("Ref# 216117", "Ref: 77")
           .replace("Tread Source Commercial Tires & Service", "Brand New Tire Shop").replace("75042", "75001").replace("+14692687464", ""))
    m = await send(new)
    ops = [c[0] for c in writer.calls[before:]]
    check(ops == ["create_vendor", "create_label", "upload", "create_issue", "create_work_order"], "yangi vendor + yangi label yaratiladi")
    vend, body = writer.calls[before][1], writer.calls[-1][1]
    check(vend == {"name": "Brand New Tire Shop", "service": True, "street_address": "1305 S Shiloh Rd", "city": "Garland",
                   "region": "Texas", "postal_code": "75001", "country": "US"}, "yangi vendor maydonlari")
    check(body["vehicle_id"] == 4 and body["vmrs_repair_priority_class_id"] == 3
          and body["starting_meter_entry_attributes"] == {"value": 0, "void": True}, "treyler ishi -> treylerga, RC -> Emergency, void meter")


asyncio.run(flow())
print(f"\nHammasi o'tdi: {ok} ta tekshiruv ✅")

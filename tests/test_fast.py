"""Tezlik qismlari — internetsiz testlar: keep-alive HTTP, tayyor hisobotlar, nomlarni qo'shish, .env keshi.

    python -m tests.test_fast
"""
import io
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx2 as httpx  # noqa: E402

from app import fleetio, net  # noqa: E402
from app.fleetio import Catalog, FleetioClient, FleetioError  # noqa: E402
from app.fleetio_reports import Reports  # noqa: E402

ok = 0
tmp = Path(tempfile.mkdtemp())
fleetio.ENV_FILE = tmp / ".env"
fleetio.ENV_FILE.write_text("SECUREFLEET_KEY=k1\nSECUREFLEET_ACCOUNT_TOKEN=acct\n", encoding="utf-8")


def check(cond, name):
    global ok
    assert cond, name
    ok += 1
    print("  ✓", name)


print("1) Keep-alive HTTP (lokal server)")
conns = Counter()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive

    def setup(self):
        super().setup()
        conns["n"] += 1  # har yangi TCP ulanish

    def do_GET(self):
        if self.path.startswith("/missing"):
            body, code = b'{"errors":{"x":["not found"]}}', 404
        else:
            body, code = json.dumps({"path": self.path}).encode(), 200
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", "3")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.dumps({"got": json.loads(self.rfile.read(n) or b"{}")}).encode()
        self.send_response(201)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"
for i in range(5):
    with net.urlopen(urllib.request.Request(f"{base}/v/{i}", method="GET"), timeout=5) as r:
        assert json.loads(r.read())["path"] == f"/v/{i}"
check(conns["n"] == 1, "5 ta GET — bitta TCP ulanish orqali (har so'rovga yangi ulanish ochilmaydi)")
with net.urlopen(urllib.request.Request(f"{base}/w", data=b'{"a": 1}', headers={"Content-Type": "application/json"},
                                        method="POST"), timeout=5) as r:
    check(json.loads(r.read()) == {"got": {"a": 1}}, "POST tanasi yuboriladi")
try:
    net.urlopen(urllib.request.Request(f"{base}/missing", method="GET"), timeout=5)
    check(False, "404")
except urllib.error.HTTPError as e:
    check(e.code == 404 and b"not found" in e.read() and e.headers.get("retry-after") == "3",
          "HTTP xato -> urllib HTTPError (kod, tana, header) — eski xato ishlovi o'zgarmaydi")
srv.shutdown()


def with_transport(handler):
    old = net._client
    net._client = httpx.Client(transport=httpx.MockTransport(handler))
    return old


calls = Counter()


def flaky(request):
    calls[request.method] += 1
    if calls[request.method] == 1:
        raise httpx.RemoteProtocolError("server closed the connection", request=request)
    return httpx.Response(200, json={"ok": True})


old = with_transport(flaky)
with net.urlopen(urllib.request.Request("https://x.test/a", method="GET"), timeout=5) as r:
    check(json.loads(r.read()) == {"ok": True} and calls["GET"] == 2, "eskirgan ulanish: GET bir marta qayta yuboriladi")
try:
    net.urlopen(urllib.request.Request("https://x.test/a", data=b"{}", method="POST"), timeout=5)
    check(False, "POST")
except urllib.error.URLError:
    check(calls["POST"] == 1, "POST qayta yuborilmaydi (ikki marta yaratib qo'ymaslik uchun) -> URLError")


def slow(request):
    raise httpx.ReadTimeout("timed out", request=request)


net._client = httpx.Client(transport=httpx.MockTransport(slow))
try:
    net.urlopen(urllib.request.Request("https://x.test/a", method="GET"), timeout=1)
    check(False, "timeout")
except TimeoutError:
    check(True, "timeout -> TimeoutError (Fleetio yozishda 'javob kelmadi' holati)")
net._client = old

print("2) .env keshi, vehicle nomini qo'shish")
check(fleetio.credentials()["key"] == "k1", ".env o'qildi")
time.sleep(0.02)
fleetio.ENV_FILE.write_text("SECUREFLEET_KEY=k2-new\nSECUREFLEET_ACCOUNT_TOKEN=acct\n", encoding="utf-8")
check(fleetio.credentials()["key"] == "k2-new" and fleetio.env("SECUREFLEET_ACCOUNT_TOKEN") == "acct",
      "fayl o'zgarsa yangi qiymat darhol ko'rinadi (restartsiz)")

REMINDERS = [{"id": 1, "vehicle_id": 11, "vehicle": {"id": 11, "name": "3968"}, "next_due_at": "2026-10-01",
              "service_task": {"id": 5, "name": "PM Service"}}]


def opener(req, timeout=None):
    return io.BytesIO(json.dumps({"records": REMINDERS, "next_cursor": None}).encode())


fc = FleetioClient(Catalog(), opener=opener)
rec = fc.call("list_service_reminders", fields=["vehicle_id", "next_due_at"])["records"][0]
check(rec == {"id": 1, "vehicle_id": 11, "next_due_at": "2026-10-01", "vehicle_name": "3968"},
      "fields'da vehicle_id so'ralsa nomi ham qo'shiladi (model get_vehicle'ni 17 marta chaqirmasin)")
rec = fc.call("list_service_reminders", fields=["vehicle_id", "vehicle.name"])["records"][0]
check("vehicle_name" not in rec and rec["vehicle.name"] == "3968", "nomi o'zi so'ralgan bo'lsa takrorlanmaydi")

print("3) Tayyor hisobotlar (LLM'siz)")
VEH = [
    {"id": 11, "name": "3968", "vehicle_status_name": "Active OTR", "vehicle_type_name": "Truck", "year": 2022.0,
     "make": "Freightliner", "model": "Cascadia", "vin": "1FUJHHDR0NLNA0001", "license_plate": "ABC123",
     "registration_state": "TX", "primary_meter_value": "212137.0", "primary_meter_unit": "mi",
     "primary_meter_date": "2026-10-14", "group_name": "Dallas", "driver": {"id": 9, "name": "Test Driver"}},
    {"id": 12, "name": "4201 TEL", "vehicle_status_name": "Available", "vehicle_type_name": "Truck"},
    {"id": 13, "name": "560542", "vehicle_status_name": "Available", "vehicle_type_name": "Trailer"},
    {"id": 14, "name": "9693 JCE", "vehicle_status_name": "Preparing", "vehicle_type_name": "Semi-Trailer Truck"},
    {"id": 15, "name": "9693 RVW", "vehicle_status_name": "Preparing", "vehicle_type_name": "Truck",
     "driver": {"id": 8, "name": "Other Driver"}},
]
DATA = {
    ("list_service_reminders", "overdue"): [
        {"vehicle_id": 11, "vehicle": {"name": "3968"}, "service_task": {"name": "PM Service"},
         "next_due_primary_meter_value": "200000.0", "active": True},
        {"vehicle_id": 11, "vehicle": {"name": "3968"}, "service_task": {"name": "Differential Oil"},
         "next_due_at": "2026-09-01T00:00:00-04:00"},
        {"vehicle_id": 12, "vehicle": {"name": "4201 TEL"}, "service_task": {"name": "PM Service"},
         "next_due_at": "2026-09-20T00:00:00-04:00"},
        {"vehicle_id": 12, "vehicle": {"name": "4201 TEL"}, "service_task": {"name": "PM Service"},
         "active": False},  # o'chirilgan reminder — sanalmaydi
    ],
    ("list_service_reminders", "due_soon"): [
        {"vehicle_id": 14, "vehicle": {"name": "9693 JCE"}, "service_task": {"name": "Cabin Air Filter"},
         "next_due_primary_meter_value": "35000.0"}],
    ("list_vehicle_renewal_reminders", "overdue"): [
        {"vehicle": {"name": "3968"}, "vehicle_renewal_type_id": 1, "next_due_at": "2026-08-01"},
        {"vehicle": {"name": "4201 TEL"}, "vehicle_renewal_type_id": 1, "next_due_at": "2026-08-02"},
        {"vehicle": {"name": "9693 JCE"}, "vehicle_renewal_type_id": 2, "next_due_at": "2026-07-01"}],
    ("list_vehicle_renewal_reminders", "due_soon"): [
        {"vehicle": {"name": "9693 RVW"}, "vehicle_renewal_type_id": 2, "next_due_at": "2026-11-02"}],
    ("list_vehicle_renewal_types", None): [{"id": 1, "name": "Registration"}, {"id": 2, "name": "DOT Inspection"}],
    ("list_issues", None): [],
    ("list_vehicles", None): VEH,
}


class FakeClient:
    def __init__(self):
        self.counts = Counter()
        self.lock = threading.Lock()

    def raw_all(self, name, params=None, max_pages=60):
        f = (params or {}).get("filter") or {}
        status = next((v["eq"] for k, v in f.items() if k.endswith("_status")), None)
        with self.lock:
            self.counts[name, status] += 1
        time.sleep(0.05)
        return [dict(x) for x in DATA[name, status]]


cl = FakeClient()
rp = Reports(cl)
t = rp.get("overview")
check("5 vehicles" in t and "Active OTR: <b>1</b>" in t and "Preparing: <b>2</b>" in t and "Truck 3" in t
      and "Trucks without a driver: <b>2</b>" in t, "overview: status/turi bo'yicha, haydovchisiz trucklar (treylersiz)")
t = rp.get("nodriver")
check("— 2" in t and "4201 TEL" in t and "9693 JCE" in t and "560542" not in t, "no driver: treylerlar sanalmaydi")
t = rp.get("service")
check("Overdue: <b>3</b> on 2 vehicles" in t and "Due soon: <b>1</b>" in t and "<b>3968</b> — 2: PM Service, Differential Oil" in t
      and "at 200,000 mi (now 212,137 mi)" not in t and "Cabin Air Filter · at 35,000 mi" in t and "• PM Service — 2" in t,
      "service: o'chirilgan reminder sanalmaydi, mashina va task bo'yicha, hisoblagich qiymatlari")
before = sum(cl.counts.values())
rp.get("service")
check(sum(cl.counts.values()) == before, "hisobot 3 daqiqa keshdan (qayta bosilsa Fleetio'ga bormaydi)")
t = rp.get("renewals")
check("Overdue: <b>3</b> · ⚠️ Due soon: <b>1</b>" in t and "<b>Registration</b> (2): 3968, 4201 TEL" in t
      and "<b>DOT Inspection</b> (1): 9693 JCE" in t and "<b>9693 RVW</b> — DOT Inspection · Nov 2, 2026" in t,
      "renewals: turi nomi bilan, muddati o'tganlar turi bo'yicha")
check("No open issues" in rp.get("issues"), "ochiq issue yo'q -> qisqa javob")
DATA["list_issues", None] = [{"number": "3473", "summary": "Brake light out", "reported_at": "2026-10-12T09:00:00-04:00",
                              "vehicle": {"name": "3968"}, "issue_priority": {"name": "High"}, "overdue": True}]
rp._cache.clear()
t = rp.get("issues")
check("Open issues</b> — 1" in t and "<b>3968</b> — #3473 Brake light out · Oct 12, 2026 · High · ❗️ overdue" in t, "issues ro'yxati")
t = rp.get("unit", "3968")
check("<b>3968</b> · Active OTR · Truck" in t and "Test Driver" in t and "2022 Freightliner Cascadia" in t and "ABC123 (TX)" in t
      and "212,137 mi · Oct 14, 2026" in t and "/acct/vehicles/11" in t, "unit kartasi")
check("several vehicles" in rp.get("unit", "9693") and "not found" in rp.get("unit", "7777"), "unit: noaniq / topilmadi")

cl2 = FakeClient()
rp2 = Reports(cl2)
threads = [threading.Thread(target=rp2.get, args=("service",)) for _ in range(3)]
for th in threads:
    th.start()
for th in threads:
    th.join()
check(cl2.counts["list_service_reminders", "overdue"] == 1, "3 kishi birdan bossa — Fleetio'ga bir marta boriladi")


class Broken(FakeClient):
    def raw_all(self, name, params=None, max_pages=60):
        raise FleetioError("Fleetio is not reachable: timed out")


try:
    Reports(Broken()).get("service")
    check(False, "xato")
except FleetioError as e:
    check("not reachable" in str(e), "Fleetio xatosi chaqiruvchiga chiqadi (bot tushunarli xabar yozadi)")

print(f"\nHammasi o'tdi: {ok} ta tekshiruv ✅")

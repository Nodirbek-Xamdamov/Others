"""Fleetio'ga YOZISH — faqat work order oqimi uchun, qat'iy ro'yxat bo'yicha.

`fleetio.py` (savol-javob, LLM tool'lari) faqat GET qiladi va shunday qoladi: LLM hech qachon yozuvchi so'rov
yubora olmaydi. Bu modulni faqat `workorders.py` chaqiradi. Ruxsat etilgan amallar: vendor, label, issue,
work order yaratish (POST) va faylni Fleetio saqlash xizmatiga yuklash. PATCH/DELETE yo'q.

Fayl biriktirish (rasmiy qo'llanma: developer.fleetio.com/docs/overview/attaching-documents-and-images):
  1) POST /v1/uploads/policies            -> policy, signature, path (1 soat amal qiladi)
  2) POST <saqlash xizmati>?policy=..&signature=..&path=..&filename=..   (tana = fayl) -> url
  3) yozuv yaratishda documents_attributes: [{name, file_url, ...}]
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from .fleetio import FleetioError, credentials, env

log = logging.getLogger("fleetio_write")

WRITE_ROUTES = {
    "create_vendor": "/vendors",
    "create_label": "/labels",
    "create_issue": "/issues",
    "create_work_order": "/work_orders",
}
UPLOAD_POLICY_PATH = "/v1/uploads/policies"
DEFAULT_UPLOAD_URL = "https://lmuavc3zg4.execute-api.us-east-1.amazonaws.com/prod/uploads"
API_VERSION = "2025-05-05"
HTTP_TIMEOUT_S = 40
UPLOAD_TIMEOUT_S = 120
RATE_LIMIT_WAIT_S = 20


class FleetioUncertain(FleetioError):
    """So'rov yuborildi, lekin javob kelmadi (uzilish/timeout): yozuv yaratilgan bo'lishi ham mumkin."""


def _error_text(e: urllib.error.HTTPError) -> str:
    """Fleetio xato javobi -> o'qiladigan matn (422 da qaysi maydon nima uchun rad etilgani)."""
    try:
        raw = e.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        raw = ""
    detail = raw[:400]
    try:
        body = json.loads(raw)
        errs = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errs, dict):
            detail = "; ".join(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in errs.items())
        elif isinstance(errs, list):
            detail = "; ".join(map(str, errs))
        elif isinstance(body, dict):
            detail = str(body.get("detail") or body.get("error") or body.get("message") or body.get("title") or raw)[:400]
    except (json.JSONDecodeError, AttributeError):
        pass
    head = {401: "Fleetio rejected the API key (401).",
            403: "The API key has no permission for this action in Fleetio (403).",
            404: "Not found in Fleetio (404).",
            422: "Fleetio rejected the data (422):"}.get(e.code, f"Fleetio error {e.code}:")
    return f"{head} {detail}".strip()


class FleetioWriter:
    """opener testlarda almashtiriladi (urllib.request.urlopen imzosi bilan)."""

    def __init__(self, opener=None):
        self._open = opener or urllib.request.urlopen

    @staticmethod
    def _headers(creds: dict, json_body: bool) -> dict:
        if not creds["key"] or not creds["account"]:
            raise FleetioError("Fleetio API key is not configured (SECUREFLEET_KEY / SECUREFLEET_ACCOUNT_TOKEN in .env).")
        h = {"Authorization": f"Token {creds['key']}", "Account-Token": creds["account"], "Accept": "application/json",
             "X-Api-Version": creds["version"] or API_VERSION}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def _send(self, req: urllib.request.Request, timeout: float, what: str) -> dict:
        for attempt in range(2):
            try:
                with self._open(req, timeout=timeout) as resp:
                    body = resp.read()
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 0:  # 429 = bajarilmagan, qayta yuborish xavfsiz
                    try:
                        wait = min(float(e.headers.get("Retry-After") or 5), RATE_LIMIT_WAIT_S)
                    except (TypeError, ValueError):
                        wait = 5.0
                    log.warning("Fleetio 429 (%s), %.0fs kutamiz", what, wait)
                    time.sleep(wait)
                    continue
                raise FleetioError(_error_text(e)) from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                reason = getattr(e, "reason", e)
                if req.get_method() == "GET":
                    raise FleetioError(f"Fleetio is not reachable: {reason}") from None
                raise FleetioUncertain(f"No answer from Fleetio for {what} ({reason}); it may or may not have been saved.") from None
            except json.JSONDecodeError:
                raise FleetioUncertain(f"Unreadable answer from Fleetio for {what}.") from None
        raise FleetioError("Fleetio rate limit, try again in a minute.")

    # ------------------------------------------------------------------ o'qish (katalogda yo'q bitta yo'l)
    def current_user(self) -> dict:
        """API key egasi: contact_id (Issued by / Reported by), time_zone_name."""
        creds = credentials()
        req = urllib.request.Request(creds["base_url"] + "/users/me", headers=self._headers(creds, False), method="GET")
        return self._send(req, HTTP_TIMEOUT_S, "users/me")

    # ------------------------------------------------------------------ yaratish
    def create(self, op: str, body: dict) -> dict:
        if op not in WRITE_ROUTES:
            raise FleetioError(f"Write operation '{op}' is not allowed.")
        creds = credentials()
        req = urllib.request.Request(creds["base_url"] + WRITE_ROUTES[op], data=json.dumps(body).encode("utf-8"),
                                     headers=self._headers(creds, True), method="POST")
        log.info("Fleetio POST %s %s", WRITE_ROUTES[op], json.dumps(body, ensure_ascii=False)[:1500])
        out = self._send(req, HTTP_TIMEOUT_S, op)
        log.info("Fleetio %s -> id=%s number=%s", op, out.get("id"), out.get("number"))
        return out

    # ------------------------------------------------------------------ fayl
    def upload(self, data: bytes, filename: str, mime: str) -> dict:
        """Faylni Fleetio saqlash xizmatiga yuklaydi -> documents_attributes elementi."""
        creds = credentials()
        try:
            req = urllib.request.Request(creds["base_url"] + UPLOAD_POLICY_PATH, data=b"",
                                         headers=self._headers(creds, False), method="POST")
            pol = self._send(req, HTTP_TIMEOUT_S, "upload policy")
            if not all(pol.get(k) for k in ("policy", "signature", "path")):
                raise FleetioError("Fleetio did not return an upload policy.")
            query = urllib.parse.urlencode({"signature": pol["signature"], "policy": pol["policy"], "path": pol["path"],
                                            "filename": filename})
            url = env("SECUREFLEET_UPLOAD_URL", DEFAULT_UPLOAD_URL) + "?" + query
            req = urllib.request.Request(url, data=data, headers={"Content-Type": mime or "application/octet-stream"},
                                         method="POST")
            res = self._send(req, UPLOAD_TIMEOUT_S, "file upload")
        except FleetioUncertain as e:  # fayl hali hech narsaga biriktirilmagan — qayta yuklash xavfsiz
            raise FleetioError(str(e)) from None
        if not res.get("url"):
            raise FleetioError("File storage did not return a file URL.")
        log.info("Fayl yuklandi: %s (%d bayt)", filename, len(data))
        return {"name": filename, "file_url": res["url"], "file_name": filename,
                "file_mime_type": mime or res.get("type") or "application/octet-stream", "file_size": len(data)}

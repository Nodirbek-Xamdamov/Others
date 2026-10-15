"""Fleetio (Secure Fleet) API — faqat o'qish.

Endpointlar katalogi `data/fleetio_tools_readonly.json` (rasmiy OpenAPI hujjatidan yaratilgan, 133 ta GET).
Xavfsizlik: faqat GET yuboriladi va faqat katalogdagi yo'llarga; noma'lum parametrlar tashlab yuboriladi.
Kalitlar `.env` dan o'qiladi (fayl o'zgarsa darhol ko'rinadi) — key keyinroq qo'shilsa, restart shart emas.
So'rovlar bitta keep-alive ulanishlar havzasi orqali ketadi (`net.py`): har so'rovga yangi TLS ulanish ochilmaydi.
LLM'ga ketadigan javob ixchamlanadi (bo'sh maydonlar, rasm havolalari, 0 hisoblagichlar olib tashlanadi),
sanash/guruhlash (count_by) esa Python'da aniq hisoblanadi.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from dotenv import dotenv_values

from . import net
from .config import ROOT

log = logging.getLogger("fleetio")

TOOLS_FILE = ROOT / "data" / "fleetio_tools_readonly.json"
ENV_FILE = ROOT / ".env"
DEFAULT_BASE_URL = "https://secure.fleetio.com/api"
HTTP_TIMEOUT_S = 20
DEFAULT_PER_PAGE = 20      # LLM'ga kam token ketishi uchun
MAX_PAGES = 5              # all_pages: ko'pi bilan 5 x 100 = 500 yozuv (sahifalash ketma-ket, har sahifa = alohida kutish)
MAX_RECORDS_TO_LLM = 30
MAX_CHARS_TO_LLM = 5000
MAX_STR = 300
RATE_LIMIT_WAIT_S = 10     # 429 da Retry-After shundan kam bo'lsa bir marta kutamiz
CALL_CACHE_TTL_S = 30      # bir xil (tool, params) bir necha soniyada qayta so'ralsa — tarmoqqa chiqmasdan shu yerdan

_DROP_KEYS = {"account_id", "is_sample", "ai_enabled", "system_of_measurement", "assetable_type"}


class FleetioError(Exception):
    pass


_env_cache: tuple[tuple, dict[str, str]] | None = None


def _env_file() -> dict[str, str]:
    """.env qiymatlari. Fayl faqat o'zgarganda (vaqti/hajmi) qayta o'qiladi — har xabarda diskdan parse qilinmaydi."""
    global _env_cache
    try:
        st = ENV_FILE.stat()
    except OSError:
        return {}
    stamp = (str(ENV_FILE), st.st_mtime_ns, st.st_size)
    cached = _env_cache
    if cached is None or cached[0] != stamp:
        cached = (stamp, {k: (v or "").strip() for k, v in dotenv_values(ENV_FILE).items()})
        _env_cache = cached
    return cached[1]


def credentials() -> dict[str, str]:
    """Kalitlar: avval .env (o'zgarsa darhol ko'rinadi), bo'lmasa muhit o'zgaruvchilari."""
    file_vals = _env_file()

    def get(name: str, default: str = "") -> str:
        return file_vals.get(name) or os.getenv(name, "").strip() or default

    return {"key": get("SECUREFLEET_KEY"), "account": get("SECUREFLEET_ACCOUNT_TOKEN"),
            "base_url": get("SECUREFLEET_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            "version": get("SECUREFLEET_API_VERSION")}


def env(name: str, default: str = "") -> str:
    """Bitta sozlama: avval .env (o'zgarsa darhol ko'rinadi — restart shart emas), bo'lmasa muhit o'zgaruvchisi."""
    return _env_file().get(name) or os.getenv(name, "").strip() or default


def web_link(kind: str, record_id) -> str:
    """Fleetio saytidagi sahifa: https://secure.fleetio.com/<account>/work_orders/<id>."""
    return f"https://secure.fleetio.com/{credentials()['account']}/{kind}/{record_id}"


class Catalog:
    """Read-only tool katalogi: nomlar, yo'llar, parametrlar."""

    def __init__(self, path: Path = TOOLS_FILE):
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        self.api: dict = d["api"]
        self.routes: dict[str, dict] = d["routes"]
        self.tools: dict[str, dict] = {t["name"]: t for t in d["tools"]}
        bad = [n for n, r in self.routes.items() if r["method"] != "GET"]
        if bad:
            raise ValueError(f"Katalogda GET bo'lmagan yo'llar bor: {bad[:5]}")

    def groups(self) -> dict[str, list[str]]:
        """Resurs bo'yicha guruhlangan nomlar (system prompt uchun)."""
        out: dict[str, list[str]] = {}
        for name, r in self.routes.items():
            out.setdefault(r["path"].strip("/").split("/")[0], []).append(name)
        return out

    def label(self, name: str) -> str:
        """list_vehicles yoki get_vehicle(id) — id kerak bo'lsa qavsda."""
        pp = self.routes[name]["path_params"]
        return f"{name}({','.join(pp)})" if pp else name

    def filter_fields(self, name: str) -> list[str]:
        f = self.tools[name]["input_schema"]["properties"].get("filter", {})
        return list(f.get("properties", {}))

    def sort_fields(self, name: str) -> list[str]:
        s = self.tools[name]["input_schema"]["properties"].get("sort", {})
        return list(s.get("properties", {}))

    def help(self, name: str) -> dict:
        """Bitta tool haqida ixcham ma'lumot (fleetio_help uchun)."""
        t, r = self.tools[name], self.routes[name]
        props = t["input_schema"]["properties"]
        params = {}
        for p, s in props.items():
            if p in ("filter", "sort", "start_cursor"):
                continue
            params[p] = s.get("type", "any") + (" (required)" if p in r["path_params"] else "")
        out = {"tool": name, "description": t["description"], "params": params}
        if "filter" in props:
            out["filter"] = {f: sorted(s.get("properties", {})) for f, s in props["filter"].get("properties", {}).items()}
        if "sort" in props:
            out["sort"] = self.sort_fields(name)
        return out

    def suggest(self, name: str) -> list[str]:
        return difflib.get_close_matches(name or "", list(self.routes), n=5, cutoff=0.4)


def _flatten_query(name: str, value, out: list[tuple[str, str]]) -> None:
    """deepObject: filter={"name": {"like": "x"}} -> filter[name][like]=x; ro'yxat -> takroriy kalit."""
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten_query(f"{name}[{k}]", v, out)
    elif isinstance(value, (list, tuple)):
        key = name if name.endswith("[]") else f"{name}[]"
        for v in value:
            out.append((key, _scalar(v)))
    elif value is not None and value != "":
        out.append((name, _scalar(value)))


def _scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def compact(value, depth: int = 0):
    """LLM uchun ixchamlash: bo'sh/null, *_url, 0 hisoblagichlar olib tashlanadi, uzun matn qisqartiriladi."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in _DROP_KEYS or k.endswith("_url") or k.endswith("_url_small") or k.endswith("_url_medium"):
                continue
            if k.endswith("_count") and not v:
                continue
            if depth >= 2 and isinstance(v, (dict, list)):
                continue  # juda chuqur ichki obyektlar tashlanadi
            v = compact(v, depth + 1)
            if v is None or v == "" or v == [] or v == {}:
                continue
            out[k] = v
        return out
    if isinstance(value, list):
        items = [compact(v, depth + 1) for v in value[:10]]
        items = [v for v in items if v not in (None, "", [], {})]
        if len(value) > 10:
            items.append(f"... {len(value) - 10} more")
        return items
    if isinstance(value, str) and len(value) > MAX_STR:
        return value[:MAX_STR] + "…"
    return value


def _get_path(rec: dict, path: str):
    cur = rec
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _project(rec: dict, fields: list[str]) -> dict:
    out = {}
    for f in fields:
        v = _get_path(rec, f)
        if v is not None and v != "":
            out[f] = v
        # vehicle_id so'ralsa, yozuvda bor nomi (vehicle.name / vehicle_name) ham qo'shiladi: aks holda model
        # har bir id uchun alohida get_vehicle chaqiradi (bitta javobda 17 ta qo'shimcha so'rov ko'rilgan)
        if f.endswith("_id") and "." not in f:
            base = f[:-3]
            name = rec.get(f"{base}_name") or _get_path(rec, f"{base}.name")
            if name and f"{base}_name" not in fields and f"{base}.name" not in fields:
                out[f"{base}_name"] = name
    if "id" in rec and "id" not in out:
        out = {"id": rec["id"], **out}
    return out


class FleetioClient:
    """Faqat GET. opener testlarda almashtiriladi (urllib.request.urlopen imzosi bilan)."""

    def __init__(self, catalog: Catalog | None = None, opener=None):
        self.catalog = catalog or Catalog()
        self._open = opener or net.urlopen
        self._cache: dict[str, tuple[float, dict]] = {}

    def configured(self) -> bool:
        c = credentials()
        return bool(c["key"] and c["account"])

    # ------------------------------------------------------------------ URL
    def build_url(self, name: str, params: dict | None, base_url: str) -> tuple[str, list[str]]:
        """-> (url, tashlab yuborilgan parametrlar). Noma'lum tool / id yo'q bo'lsa FleetioError."""
        if name not in self.catalog.routes:
            hint = ", ".join(self.catalog.suggest(name)) or "see the catalog"
            raise FleetioError(f"Unknown tool '{name}'. Did you mean: {hint}")
        r = self.catalog.routes[name]
        params = dict(params or {})
        path = r["path"]
        for p in r["path_params"]:
            v = params.pop(p, None)
            if v is None or str(v).strip() == "":
                raise FleetioError(f"Tool '{name}' needs path parameter '{p}'.")
            path = path.replace("{" + p + "}", urllib.parse.quote(str(v).strip(), safe=""))
        allowed = set(r["query_params"])
        query: list[tuple[str, str]] = []
        dropped: list[str] = []
        for k, v in params.items():
            base = re.split(r"\[", k, maxsplit=1)[0]
            if k in ("filter", "sort") and isinstance(v, str):  # LLM ba'zan obyektni JSON matn qilib yuboradi
                try:
                    v = json.loads(v)
                except json.JSONDecodeError:
                    dropped.append(k)
                    continue
            if k in allowed or base in allowed or f"{base}[]" in allowed:
                if k == "per_page":
                    try:
                        v = max(2, min(100, int(v)))
                    except (TypeError, ValueError):
                        v = DEFAULT_PER_PAGE
                if f"{base}[]" in allowed and not isinstance(v, (list, tuple)):
                    v = [v]  # expand[] kabi ro'yxat parametrlari
                _flatten_query(k, v, query)
            else:
                dropped.append(k)
        if "per_page" in allowed and not any(k == "per_page" for k, _ in query):
            query.append(("per_page", str(DEFAULT_PER_PAGE)))
        url = base_url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        return url, dropped

    # ------------------------------------------------------------------ HTTP
    def _get(self, url: str, creds: dict) -> dict | list:
        headers = {"Authorization": f"Token {creds['key']}", "Account-Token": creds["account"],
                   "Accept": "application/json",
                   "X-Api-Version": creds["version"] or self.catalog.api.get("api_version", "")}
        req = urllib.request.Request(url, headers={k: v for k, v in headers.items() if v}, method="GET")
        if req.get_method() != "GET":  # hech qachon yozuvchi so'rov yubormaymiz
            raise FleetioError("Only GET is allowed")
        for attempt in range(2):
            try:
                with self._open(req, timeout=HTTP_TIMEOUT_S) as resp:
                    body = resp.read()
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 0:
                    wait = float(e.headers.get("Retry-After") or RATE_LIMIT_WAIT_S + 1)
                    if wait <= RATE_LIMIT_WAIT_S:
                        log.warning("Fleetio 429, %.0fs kutamiz", wait)
                        time.sleep(wait)
                        continue
                    raise FleetioError(f"Fleetio rate limit: try again in {wait:.0f} seconds.") from None
                raise FleetioError(_http_error_text(e)) from None
            except urllib.error.URLError as e:
                raise FleetioError(f"Fleetio is not reachable: {e.reason}") from None
            except (TimeoutError, OSError) as e:  # javobni o'qish paytidagi uzilish URLError'ga o'ralmaydi
                raise FleetioError(f"Fleetio is not reachable: {e}") from None
        raise FleetioError("Fleetio rate limit.")

    # ------------------------------------------------------------------ ichki kod uchun (LLM'siz)
    def raw(self, name: str, params: dict | None = None) -> dict | list:
        """Katalogdagi GET'ning to'liq (ixchamlanmagan) javobi."""
        creds = credentials()
        if not creds["key"] or not creds["account"]:
            raise FleetioError("Fleetio API key is not configured (SECUREFLEET_KEY / SECUREFLEET_ACCOUNT_TOKEN in .env).")
        url, _ = self.build_url(name, params, creds["base_url"])
        return self._get(url, creds)

    def raw_all(self, name: str, params: dict | None = None, max_pages: int = 60) -> list[dict]:
        """Ro'yxatning hamma sahifalari (per_page=100), to'liq yozuvlar."""
        params = dict(params or {}, per_page=100)
        params.pop("start_cursor", None)
        page = self.raw(name, params)
        if isinstance(page, list):
            return page
        records = list(page.get("records") or [])
        pages = 1
        while page.get("next_cursor") and pages < max_pages:
            page = self.raw(name, dict(params, start_cursor=page["next_cursor"]))
            records += page.get("records") or []
            pages += 1
        return records

    # ------------------------------------------------------------------ tool call
    def call(self, name: str, params: dict | None = None, *, fields: list[str] | None = None,
             count_by: str | None = None, all_pages: bool = False) -> dict:
        """call() keshlaydi (bir xil chaqiruv CALL_CACHE_TTL_S ichida tarmoqqa chiqmaydi — model ko'pincha
        bir xil tool'ni qayta chaqiradi, ayniqsa all_pages bilan ko'p sahifali so'rovlarda sezilarli tezlashadi)."""
        key = json.dumps([name, params or {}, fields, count_by, all_pages], sort_keys=True, default=str)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < CALL_CACHE_TTL_S:
            return dict(cached[1])  # nusxa: chaqiruvchi "_urls"ni pop qiladi, keshdagi asl nusxa buzilmasin
        if len(self._cache) > 200:
            now = time.monotonic()
            self._cache = {k: v for k, v in self._cache.items() if now - v[0] < CALL_CACHE_TTL_S}
        out = self._call_uncached(name, params, fields=fields, count_by=count_by, all_pages=all_pages)
        self._cache[key] = (time.monotonic(), out)
        return dict(out)

    def _call_uncached(self, name: str, params: dict | None = None, *, fields: list[str] | None = None,
                       count_by: str | None = None, all_pages: bool = False) -> dict:
        """Bitta read-only tool'ni bajaradi va LLM uchun ixcham natija qaytaradi.

        Natijada `_urls` — bajarilgan so'rovlar (kalitlarsiz, /api buyrug'i uchun)."""
        creds = credentials()
        if not creds["key"] or not creds["account"]:
            raise FleetioError("Fleetio API key is not configured (SECUREFLEET_KEY / SECUREFLEET_ACCOUNT_TOKEN in .env).")
        params = dict(params or {})
        is_list = "per_page" in self.catalog.routes.get(name, {}).get("query_params", [])
        if (all_pages or count_by) and is_list:
            params["per_page"] = 100
            params.pop("start_cursor", None)
        url, dropped = self.build_url(name, params, creds["base_url"])
        urls = [url]
        data = self._get(url, creds)
        if not (isinstance(data, dict) and isinstance(data.get("records"), list)):
            # bitta obyekt yoki massiv
            if isinstance(data, list):
                recs = [compact(x) for x in data]
                if fields:
                    recs = [_project(x, fields) for x in recs]
                out = {"returned": len(recs), "records": recs}
            else:
                out = {"record": _project(data, fields) if fields else compact(data)}
            return self._finish(out, dropped, urls)

        records = list(data["records"])
        page = data
        pages = 1
        while (all_pages or count_by) and page.get("next_cursor") and pages < MAX_PAGES:
            nxt = dict(params, start_cursor=page["next_cursor"])
            url, _ = self.build_url(name, nxt, creds["base_url"])
            urls.append(url)
            page = self._get(url, creds)
            records += page.get("records") or []
            pages += 1
        more = bool(page.get("next_cursor"))
        out: dict = {"returned": len(records), "more_available": more}
        if more:
            out["next_cursor"] = page.get("next_cursor")
            est = page.get("estimated_remaining_count")
            if est is not None:
                out["estimated_remaining_count"] = est
                out["note"] = "estimated_remaining_count is approximate and capped at 500."
        else:
            out["total"] = len(records)  # hamma sahifa olindi -> aniq son
        if count_by and count_by.endswith("_id"):  # vehicle_status_id -> vehicle_status_name (ID emas, nom ko'rinsin)
            alt = count_by[:-3] + "_name"
            if any(isinstance(r, dict) and r.get(alt) for r in records):
                count_by = alt
        if count_by:
            cnt = Counter(str(_get_path(r, count_by)) if _get_path(r, count_by) not in (None, "") else "(none)"
                          for r in records)
            out["count_by"] = count_by
            out["counts"] = dict(cnt.most_common())
            if more:
                out["note"] = f"Counts cover the first {len(records)} records only (more exist)."
            return self._finish(out, dropped, urls)
        recs = [_project(r, fields) for r in records] if fields else [compact(r) for r in records]
        out["records"] = recs[:MAX_RECORDS_TO_LLM]
        if len(recs) > MAX_RECORDS_TO_LLM:
            out["shown"] = MAX_RECORDS_TO_LLM
        return self._finish(out, dropped, urls)

    @staticmethod
    def _finish(out: dict, dropped: list[str], urls: list[str]) -> dict:
        if dropped:
            out["ignored_params"] = dropped
        # LLM'ga ketadigan hajmni cheklaymiz: oxirgi yozuvlarni tashlab boramiz
        while "records" in out and len(json.dumps(out, ensure_ascii=False, default=str)) > MAX_CHARS_TO_LLM \
                and len(out["records"]) > 1:
            out["records"] = out["records"][:max(1, len(out["records"]) * 2 // 3)]
            out["shown"] = len(out["records"])
            out["note"] = "Output truncated; request fewer fields (fields=[...]) or add filters."
        if "record" in out and len(json.dumps(out, ensure_ascii=False, default=str)) > MAX_CHARS_TO_LLM:
            s = json.dumps(out["record"], ensure_ascii=False, default=str)[:MAX_CHARS_TO_LLM]
            out["record"] = s + "…"
            out["note"] = "Record truncated; request specific fields (fields=[...])."
        out["_urls"] = urls
        return out


def _http_error_text(e: urllib.error.HTTPError) -> str:
    try:
        body = e.read().decode("utf-8", "replace")[:300]
    except Exception:  # noqa: BLE001
        body = ""
    msg = {401: "Fleetio rejected the API key (401). Check SECUREFLEET_KEY / SECUREFLEET_ACCOUNT_TOKEN.",
           403: "No permission for this data in Fleetio (403).",
           404: "Not found in Fleetio (404)."}.get(e.code, f"Fleetio error {e.code}")
    return f"{msg} {body}".strip()

"""Savol -> SQL -> javob. Hisob-kitobni SQLite qiladi, shuning uchun raqamlar aniq chiqadi.

Bitta LLM chaqiruv: planner schema + savol -> run_sql(query, title). Natijani LLM qayta yozmaydi —
Python o'zi formatlaydi (sarlavha + ro'yxat). Sabablari:
  * aniqlik: LLM natijani qayta yozganda raqamlarni o'zi qo'shib, xato qilishi mumkin (test: 43 o'rniga 35);
  * tezlik: Groq bepul tarifida har modelga 8K token/daqiqa — bitta chaqiruv 2 baravar kam token.
Rate limit (429) bo'lsa kutib o'tirmaymiz: darhol keyingi modelga o'tamiz (Groq'da limit har model uchun alohida).
OpenAI-mos API: Groq ham, OpenAI ham shu kod bilan ishlaydi, faqat .env o'zgaradi.
Bot javoblari faqat ingliz tilida.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError

from . import fmt
from .loader import LoadedData, schema_text

log = logging.getLogger("agent")

MAX_ROWS_TO_LLM = 25
MAX_ROWS_SHOWN = 40
MAX_PLANNER_ROUNDS = 3
SQL_TIMEOUT_S = 3.0
LLM_TIMEOUT_S = 25
HISTORY_CHARS = 400

SYSTEM_PROMPT = """You are the fleet assistant of "{company}", a US trucking company. You answer questions about the company's trucks by querying a read-only SQLite database with the run_sql tool. The user may write in English, Uzbek or Russian.

DATABASE:
{schema}

RULES
- Any question about trucks, drivers, units, VIN, plates, dates, PM, GPS, issues, PTI -> call run_sql. Never answer data questions from memory.
- "fleet", "our trucks", "how many trucks", "all trucks" -> table `trucks` only (the active fleet). Use rental_trucks, other_truck_lists, inactive_trucks, gps_tracking, recent_issues, pti_checks only when the user asks about them.
- Write ONE query that answers the whole question (JOIN, GROUP BY, CASE, subqueries). Call run_sql several times in parallel only for unrelated parts.
- The query result is shown to the user AS IS (title + one line per row), so select only the columns a manager needs, in a sensible order, and give computed columns clear snake_case English aliases (COUNT(*) AS trucks, SUM(...) AS at_shop). Lists of trucks: unit_number first, then driver (or COALESCE(driver, location_note) AS driver_or_location), then the asked detail. Meaningful ORDER BY. LIMIT 50.
- Text search: LIKE '%...%' (case-insensitive). Names may be misspelled: search by surname or part of it, in driver OR coworker. Unit: (unit_number = 'X' OR unit_number LIKE 'X %'). VIN end: vin LIKE '%1234'.
- Dates are 'YYYY-MM-DD' text; compare with today's date (given below). Overdue/expired = < today. Within N days = BETWEEN today AND date(today, '+N day').
- PM: miles_until_pm < 0 = overdue; 0..3000 = due soon.
- title: short English heading of the result, e.g. "Trucks with overdue PM", "Trucks in Riverview group".
- ALWAYS write in English (titles and any text reply), even if the question is in Uzbek or Russian.
- If the question is not about the data (greeting, thanks, what can you do), reply in 1-3 short English sentences without tools.
- If a query returns 0 rows because of spelling, retry once with a broader LIKE. If there are genuinely none, reply with one short English sentence saying so.

Today: {today}."""

TOOLS = [{
    "type": "function",
    "function": {
        "name": "run_sql",
        "description": "Runs one read-only SQLite SELECT on the fleet database; the rows are shown to the user under the title.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "SQLite SELECT query"},
                "title": {"type": "string", "description": "short English heading of the result"},
            },
            "required": ["query", "title"],
        },
    },
}]

_FORBIDDEN = re.compile(r"(?i)\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|vacuum|reindex)\b")
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


class SqlRunner:
    """Faqat o'qish uchun, timeout va qatorlar limiti bilan SQL bajaruvchi."""

    def __init__(self, data: LoadedData):
        self.data = data
        self._lock = threading.Lock()
        # Ulanish faqat o'qish uchun: authorizer doimiy o'rnatiladi (Python 3.10 da uni None bilan o'chirib bo'lmaydi)
        data.conn.set_authorizer(
            lambda action, *_: sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY)

    def run(self, query: str, params: tuple = ()) -> dict:
        q = query.strip().rstrip(";").strip()
        if not re.match(r"(?is)^(select|with)\b", q) or ";" in q or _FORBIDDEN.search(q):
            return {"error": "Only a single SELECT query is allowed."}
        conn = self.data.conn
        deadline = time.monotonic() + SQL_TIMEOUT_S
        with self._lock:
            conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
            try:
                cur = conn.execute(q, params)
                cols = [d[0] for d in cur.description or []]
                rows = cur.fetchmany(500)
            except sqlite3.Error as e:
                return {"error": f"SQL error: {e}"}
            finally:
                conn.set_progress_handler(None, 0)
        return {"columns": cols, "rows": rows}

    @staticmethod
    def format_for_llm(res: dict, limit: int = MAX_ROWS_TO_LLM) -> str:
        if "error" in res:
            return res["error"]
        rows, cols = res["rows"], res["columns"]
        if not rows:
            return "0 rows (nothing found)."
        lines = [f"{len(rows)}{'+' if len(rows) >= 500 else ''} rows. Columns: {' | '.join(cols)}"]
        for r in rows[:limit]:
            lines.append(" | ".join(fmt.cell(c, v) for c, v in zip(cols, r)))
        if len(rows) > limit:
            lines.append(f"... {len(rows) - limit} more rows not shown.")
        return "\n".join(lines)


@dataclass
class Answer:
    text: str                                        # Telegram HTML
    queries: list[str] = field(default_factory=list)
    units: list[str] = field(default_factory=list)   # inline tugmalar uchun
    via: str = "sql"                                 # 'sql' | 'text'


@dataclass
class _Result:
    title: str
    query: str
    res: dict


class LLMChat:
    """OpenAI-mos chat: 429 bo'lsa kutmasdan keyingi modelga o'tadi (FleetAgent va FleetioAgent uchun umumiy)."""

    def __init__(self, *, api_key: str, base_url: str | None, model: str, fallback_models: list[str] | None = None,
                 simple_model: str | None = None):
        # max_retries=0: SDK 429 da 14-35 s uxlab qolmasin — o'zimiz darhol boshqa modelga o'tamiz
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url or None, timeout=LLM_TIMEOUT_S, max_retries=0)
        self.models = list(dict.fromkeys([model] + [m for m in (fallback_models or []) if m]))
        # arzon model band/xato bersa shu zanjirning o'zi asosiy (aqlli) modelga tushib qoladi — alohida qoida kerak emas
        self.simple_models = list(dict.fromkeys([simple_model] + self.models)) if simple_model else self.models
        self._cooldown: dict[str, float] = {}

    @staticmethod
    def _model_params(model: str) -> dict:
        if "gpt-oss" in model:  # reasoning'ni qisqartiramiz: tezroq va kamroq token
            return {"reasoning_effort": "low", "extra_body": {"include_reasoning": False}}
        return {}

    async def _chat(self, messages: list[dict], *, models: list[str] | None = None, **kw):
        models = models or self.models
        last_err: Exception | None = None
        for attempt in range(2):
            for model in models:
                if self._cooldown.get(model, 0) > time.monotonic():
                    continue
                try:
                    t = time.monotonic()
                    resp = await self.client.chat.completions.create(
                        model=model, messages=messages, temperature=0.1, **self._model_params(model), **kw)
                    u = resp.usage
                    log.info("LLM %s %.2fs tokens=%s/%s", model, time.monotonic() - t,
                             getattr(u, "prompt_tokens", "?"), getattr(u, "completion_tokens", "?"))
                    return resp
                except RateLimitError as e:
                    wait = _retry_after(e)
                    self._cooldown[model] = time.monotonic() + min(wait, 60)
                    log.warning("Rate limit %s (%.0fs) -> keyingi model", model, wait)
                    last_err = e
                except (APIStatusError, APITimeoutError, APIConnectionError) as e:
                    log.warning("LLM xato %s: %s", model, str(e)[:300])
                    last_err = e
            # hamma model limitda: eng yaqin bo'shaydiganini qisqa kutamiz (bir marta)
            soonest = min((self._cooldown.get(m, 0) for m in models), default=0) - time.monotonic()
            if attempt == 0 and 0 < soonest <= 8:
                await asyncio.sleep(soonest)
                continue
            break
        raise last_err or RuntimeError("LLM did not respond")


class FleetAgent(LLMChat):
    def __init__(self, data: LoadedData, *, api_key: str, base_url: str | None, model: str,
                 fallback_models: list[str] | None = None, simple_model: str | None = None, company: str = "Fleet"):
        super().__init__(api_key=api_key, base_url=base_url, model=model, fallback_models=fallback_models,
                         simple_model=simple_model)
        self.company = company
        self.set_data(data)

    def set_data(self, data: LoadedData) -> None:
        self.data = data
        self.sql = SqlRunner(data)
        self._schema = schema_text(data)
        # haydovchisiz mashinalar qayerda turibdi (javobda '—' o'rniga ko'rsatish uchun)
        self.parked: dict[str, str] = {}
        for u, loc in data.conn.execute(
                "SELECT unit_number, location_note FROM all_trucks WHERE sheet IN ('TRUCK INFO', 'Rental trucks') "
                "AND driver IS NULL AND location_note IS NOT NULL ORDER BY sheet = 'TRUCK INFO'"):
            self.parked[u.upper()] = loc  # TRUCK INFO oxirida yoziladi -> ustun turadi

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT.format(company=self.company, schema=self._schema, today=dt.date.today().isoformat())

    # ------------------------------------------------------------------ pipeline

    async def answer(self, question: str, history: list[dict] | None = None) -> Answer:
        messages: list[dict] = [{"role": "system", "content": self.system_prompt()}]
        for h in history or []:
            messages.append({"role": h["role"], "content": h["content"][:HISTORY_CHARS]})
        messages.append({"role": "user", "content": question})
        queries: list[str] = []
        results: list[_Result] = []

        for _ in range(MAX_PLANNER_ROUNDS):
            resp = await self._chat(messages, tools=TOOLS, tool_choice="auto", max_tokens=1200)
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return Answer(fmt.md_to_html(msg.content or "") or _SORRY, queries, via="text")
            messages.append({
                "role": "assistant", "content": msg.content or "",
                "tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                               for tc in msg.tool_calls],
            })
            round_results = []
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                query, title = args.get("query", ""), (args.get("title") or "").strip()
                queries.append(query)
                res = await asyncio.to_thread(self.sql.run, query) if query else {"error": "empty query"}
                log.info("SQL: %s -> %s", query.replace("\n", " ")[:300], res.get("error") or f"{len(res['rows'])} qator")
                round_results.append(_Result(title, query, res))
                content = SqlRunner.format_for_llm(res)
                if "error" not in res and not res["rows"]:
                    content += " If this may be a spelling issue, retry with a broader LIKE; otherwise reply that there are none."
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})
            results = [r for r in round_results if "error" not in r.res]
            if any(r.res["rows"] for r in results):
                break  # natija bor -> LLM'ga qaytarmaymiz, o'zimiz formatlaymiz
            # xato yoki bo'sh -> planner natijani ko'rib qayta urinadi yoki "none" deb javob beradi

        if not results:
            return Answer("🔎 I couldn't find data for that question. Try rephrasing it "
                          "(for example, with a unit number or the driver's last name).", queries, via="text")
        return Answer(render(results, self.parked), queries, _units_from(results), via="sql")


_SORRY = "Sorry, I couldn't prepare an answer. Please try rephrasing the question."

_LABELS = {
    "unit_number": "Unit", "driver": "Driver", "coworker": "Co-driver", "location_note": "Location",
    "driver_or_location": "Driver / location", "vehicle_group": "Group", "owner": "Owner", "make": "Make",
    "model": "Model", "year": "Year", "vin": "VIN", "plate_number": "Plate", "cab_card_expiration": "Cab card exp.",
    "inspection_expiration": "Inspection exp.", "current_mileage": "Mileage (mi)", "miles_until_pm": "Miles to PM",
    "next_pm_due_miles": "Next PM (mi)", "latest_pm_miles": "Last PM (mi)", "color": "Color", "recent_issue": "Issue",
    "location": "Location", "fuel_level_pct": "Fuel", "last_driver": "Last driver", "provider": "Provider",
    "list_name": "List", "engine_status": "Engine", "last_trip": "Last trip", "last_connected": "Last signal",
    "current_driver": "Driver", "mileage": "Mileage (mi)",
}
_COUNT_COL = re.compile(r"(?i)(count|cnt|trucks|total|num|qty|quantity)|^n$")
_DRIVER_COLS = {"driver", "driver_or_location", "current_driver"}


def _label(col: str) -> str:
    return _LABELS.get(col.lower(), col.replace("_", " ").strip().capitalize())


def _is_scalar(res: dict) -> bool:
    return len(res["rows"]) == 1 and len(res["columns"]) == 1


def _fill_driver(cols: list[str], rows: list[tuple], parked: dict[str, str]) -> list[tuple]:
    """Haydovchi bo'sh bo'lsa — mashina qayerda turganini yozamiz ('🅿️ NEW YARD'), '—' o'rniga."""
    low = [c.lower() for c in cols]
    if "unit_number" not in low or not parked:
        return rows
    ui = low.index("unit_number")
    dis = [i for i, c in enumerate(low) if c in _DRIVER_COLS]
    if not dis:
        return rows
    out = []
    for row in rows:
        row = list(row)
        loc = parked.get(str(row[ui]).upper()) if row[ui] is not None else None
        for i in dis:
            if row[i] is None and loc:
                row[i] = f"🅿️ {loc}"
        out.append(tuple(row))
    return out


def render(results: list[_Result], parked: dict[str, str] | None = None) -> str:
    """SQL natijasi -> Telegram HTML: sarlavha + • ro'yxat. Qiymatlar fmt.cell orqali (Apr 1, 2027 ❗️ · 764,483)."""
    blocks = []
    for r in results:
        cols, rows = r.res["columns"], _fill_driver(r.res["columns"], r.res["rows"], parked or {})
        title = fmt.esc(r.title or "Result")
        if not rows:
            blocks.append(f"🔎 <b>{title}</b>: none found.")
        elif _is_scalar(r.res):
            blocks.append(f"📊 {title}: <b>{fmt.esc(fmt.cell(cols[0], rows[0][0]))}</b>")
        elif len(rows) == 1:
            lines, shown = [f"📌 <b>{title}</b>"], set()
            for c, v in zip(cols, rows[0]):
                val = fmt.cell(c, v)
                if v is None or (isinstance(v, str) and val in shown):  # takror qiymatni (driver_or_location) tashlaymiz
                    continue
                shown.add(val)
                lines.append(f"• {fmt.esc(_label(c))}: <b>{fmt.esc(val)}</b>")
            blocks.append("\n".join(lines))
        else:
            keep = [i for i, _ in enumerate(cols) if i == 0 or any(row[i] is not None for row in rows)]  # bo'sh ustunlar yashiriladi
            trucks = "unit_number" in [c.lower() for c in cols]
            head = f"📋 <b>{title}</b>" + (f" — {len(rows)} trucks" if trucks else "")
            lines = [head, f"<i>{fmt.esc(' · '.join(_label(cols[i]) for i in keep))}</i>"]
            for row in rows[:MAX_ROWS_SHOWN]:
                vals = [fmt.esc(fmt.cell(cols[i], row[i])) for i in keep]
                vals[0] = f"<b>{vals[0]}</b>"
                lines.append("• " + " · ".join(vals))
            if len(rows) > MAX_ROWS_SHOWN:
                lines.append(f"… and {len(rows) - MAX_ROWS_SHOWN} more")
            # guruhlab sanash (label | son) -> jami qatori
            if (len(cols) == 2 and _COUNT_COL.search(cols[1])
                    and all(isinstance(row[1], int) for row in rows) and len(rows) <= MAX_ROWS_SHOWN):
                lines.append(f"<b>Total: {fmt.num(sum(row[1] for row in rows))}</b>")
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _units_from(results: list[_Result]) -> list[str]:
    """Natijada unit_number bo'lsa — inline tugmalar uchun (bosilsa to'liq karta)."""
    units: list[str] = []
    for r in results:
        cols = [c.lower() for c in r.res["columns"]]
        if "unit_number" in cols:
            i = cols.index("unit_number")
            units += [str(row[i]) for row in r.res["rows"] if row[i] is not None]
    return list(dict.fromkeys(units))[:20]


def _retry_after(e: RateLimitError) -> float:
    try:
        ra = e.response.headers.get("retry-after")
        if ra:
            return float(ra)
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", str(e))
    if m:
        return int(m.group(1) or 0) * 60 + float(m.group(2))
    return 10.0

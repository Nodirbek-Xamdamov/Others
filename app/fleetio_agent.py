"""Savol -> Fleetio API (faqat o'qish) -> javob.

LLM'ga 133 ta tool'ni alohida bermaymiz (Groq bepul tarifida 8K token/daqiqa — sig'maydi). Buning o'rniga
2 ta meta-tool: fleetio_get (katalogdagi istalgan GET'ni bajaradi) va fleetio_help (parametrlarini ko'rsatadi);
tool nomlari katalogi system prompt'da. Sanash/guruhlash (count_by) Python'da aniq hisoblanadi.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re

from . import fmt
from .agent import HISTORY_CHARS, Answer, LLMChat
from .fleetio import FleetioClient, FleetioError, credentials

log = logging.getLogger("fleetio_agent")

MAX_ROUNDS = 5
ESCALATE_AFTER_ROUNDS = 2  # arzon (simple) model shuncha tool-raunddan keyin ham tugamasa, aqlli modelga o'tadi
MAX_TOKENS = 2500          # 1500 da ko'p parallel tool-chaqiruv yarmida kesilib qolgan (bo'sh tool nomi xatosi)

# system prompt'da filtrlari ko'rsatiladigan asosiy ro'yxatlar (qolganlari uchun fleetio_help)
KEY_LISTS = ["list_vehicles", "list_vehicle_assignments", "list_contacts", "list_issues", "list_faults",
             "list_service_reminders", "list_vehicle_renewal_reminders", "list_work_orders", "list_service_entries",
             "list_fuel_entries", "list_meter_entries", "list_expense_entries", "list_submitted_inspection_forms",
             "list_parts", "list_equipment"]
_GENERIC_FIELDS = {"created_at", "updated_at"}

SYSTEM_PROMPT = """You are the fleet assistant of "{company}". You answer with LIVE data from the company's Fleetio account (the fleet management website) using read-only tools. Users write in English, Uzbek or Russian; ALWAYS reply in English.

CATALOG of read-only Fleetio tools ("(id)" = needs that path parameter in params):
{catalog}

FILTERS for list tools: params.filter = {{"field": {{"op": value}}}}; ops: eq, like (text contains), lt/lte/gt/gte (dates YYYY-MM-DD or numbers), include (labels, id lists). Most list tools also filter and sort by created_at, updated_at. Sort: params.sort = {{"field": "asc" or "desc"}}.
{filters}
For other tools, call fleetio_help first if you are unsure about their parameters.

RULES
- Any question about vehicles, drivers/contacts, assignments, issues, faults, service, reminders, renewals, work orders, inspections, fuel, meters, expenses, parts, vendors or places -> use tools. Never answer data questions from memory and never invent values.
- Find a vehicle by name / unit number with list_vehicles and filter {{"name": {{"like": "417"}}}} (or vin / license_plate). Other tools need the vehicle's numeric id (filter vehicle_id eq <id>). This id-first pattern applies to any entity (contacts, vendors, places, groups...): resolve a name to its numeric id with the matching list tool first, then use that id in the next call — never guess an id.
- Current driver of a vehicle: get_vehicle_current_assignment with the vehicle id.
- "How many" or "by status / type / group" -> set all_pages=true and count_by=<field>; use the *_name field (vehicle_status_name, vehicle_type_name, group_name), never *_id. "total" and "counts" are exact: report them as given, never recount records yourself.
- Plain "list X" / "show X" requests (not a count) -> never set all_pages; call the list tool once with its default per_page. Only a handful of items are shown to the user anyway, so fetching every page just wastes time — if more_available comes back true, just say the list is partial instead of fetching the rest.
- Request only the fields you need (fields=[...]) to keep results small. Don't repeat a tool call with the exact same tool+params you already made this conversation — reuse that result.
- List records already carry the vehicle name (vehicle.name, or vehicle_name next to vehicle_id): never call get_vehicle just to turn ids into names.
- Today is {today}. Overdue = due date before today.
- Answer concisely: a short bold heading, then one line per item ("• name — detail"). No tables, never paste raw JSON — always summarize. If more_available is true, say the list is partial. If there are more than ~15 items, list only the first 15 and add "… and N more" — never write one line per record for long lists.
- Never show API keys, tokens, shared_key or authorization values. Share phones, emails, addresses or license numbers only when the user asks for them.
- If a tool call fails or returns no data, read the error, adjust the filter or parameters and try once more (fleetio_help helps); if it still fails or nothing is found, say so briefly in one sentence — never guess or invent a result.
- Greetings or "what can you do": reply in 1-3 sentences without tools."""

TOOLS = [
    {"type": "function", "function": {
        "name": "fleetio_get",
        "description": "Run one read-only Fleetio tool from the catalog and return its data (JSON).",
        "parameters": {"type": "object", "properties": {
            "tool": {"type": "string", "description": "catalog tool name, e.g. list_vehicles"},
            "params": {"type": "object", "description": "path params (id), filter, sort, per_page (default 20), start_cursor, other query params"},
            "fields": {"type": "array", "items": {"type": "string"},
                       "description": "return only these record fields (dotted for nested, e.g. driver.name)"},
            "count_by": {"type": "string", "description": "count records grouped by this field (fetches all pages; exact)"},
            "all_pages": {"type": "boolean", "description": "fetch all pages (up to 1000 records); gives an exact total"},
        }, "required": ["tool"]}}},
    {"type": "function", "function": {
        "name": "fleetio_help",
        "description": "Show parameters, filter fields/operators and sort fields of up to 5 catalog tools.",
        "parameters": {"type": "object", "properties": {
            "tools": {"type": "array", "items": {"type": "string"}, "description": "catalog tool names"},
        }, "required": ["tools"]}}},
]

_GET_ARGS = {"tool", "params", "fields", "count_by", "all_pages"}


def _label(tc) -> str:
    """Tool-chaqiruv -> foydalanuvchiga ko'rinadigan qisqa nom: list_service_reminders -> 'service reminders'."""
    if tc.function.name != "fleetio_get":
        return ""
    try:
        tool = str(json.loads(tc.function.arguments or "{}").get("tool") or "")
    except (json.JSONDecodeError, AttributeError):
        return ""
    return tool.removeprefix("list_").removeprefix("get_").replace("_", " ")
_RAW_TOOL_TEXT = re.compile(r"<tool_call>|<function=|\"name\"\s*:\s*\"fleetio_")
_FINAL_NUDGE = "Now answer my question using only the data above. Do not call any tools."
_SORRY = "Sorry, I couldn't prepare an answer. Please try rephrasing the question."


class FleetioAgent(LLMChat):
    def __init__(self, fleetio: FleetioClient, *, api_key: str, base_url: str | None, model: str,
                 fallback_models: list[str] | None = None, simple_model: str | None = None, company: str = "Fleet"):
        super().__init__(api_key=api_key, base_url=base_url, model=model, fallback_models=fallback_models,
                         simple_model=simple_model)
        self.fleetio = fleetio
        self.company = company
        cat = fleetio.catalog
        self._catalog = ", ".join(cat.label(n) for n in cat.routes)
        self._filters = "\n".join(
            f"- {n}: " + ", ".join(f for f in cat.filter_fields(n) if f not in _GENERIC_FIELDS)
            for n in KEY_LISTS if n in cat.routes and cat.filter_fields(n))

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT.format(company=self.company, catalog=self._catalog, filters=self._filters,
                                    today=dt.date.today().isoformat())

    # ------------------------------------------------------------------ tools
    def run_tool(self, name: str, args: dict, calls: list[str]) -> str:
        """Tool chaqiruvini bajaradi -> LLM uchun JSON matn. calls ga bajarilgan so'rovlar qo'shiladi."""
        cat = self.fleetio.catalog
        try:
            if name == "fleetio_help":
                names = args.get("tools") or []
                names = [names] if isinstance(names, str) else list(names)[:5]
                out = [cat.help(n) if n in cat.routes else {"tool": n, "error": "unknown tool",
                                                            "did_you_mean": cat.suggest(n)} for n in names]
                return json.dumps(out, ensure_ascii=False)
            if name != "fleetio_get":
                return json.dumps({"error": f"unknown function {name}"})
            tool = str(args.get("tool") or "")
            params = args.get("params") or {}
            if isinstance(params, str):
                params = json.loads(params or "{}")
            params = dict(params)
            # modellar ba'zan id ni params'ga emas, tashqariga, fields/count_by ni esa params ichiga qo'yadi
            for k, v in args.items():
                if k not in _GET_ARGS:
                    params.setdefault(k, v)
            args = dict(args)
            for k in ("fields", "count_by", "all_pages"):
                if k in params and not args.get(k):
                    args[k] = params.pop(k)
            fields = args.get("fields") or None
            if isinstance(fields, str):
                fields = [f.strip() for f in fields.split(",") if f.strip()]
            res = self.fleetio.call(tool, params, fields=fields, count_by=args.get("count_by") or None,
                                    all_pages=bool(args.get("all_pages")))
            base = credentials()["base_url"]
            calls.extend(u.removeprefix(base) for u in res.pop("_urls", []))  # /api buyrug'i uchun (kalitlarsiz)
            log.info("Fleetio %s %s -> %s", tool, json.dumps(params, ensure_ascii=False)[:200],
                     f"{len(res['counts'])} groups" if "counts" in res else f"{res.get('returned', 1)} records")
            return json.dumps(res, ensure_ascii=False, default=str)
        except FleetioError as e:
            log.warning("Fleetio xato: %s", e)
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return json.dumps({"error": f"bad arguments: {e}"})

    # ------------------------------------------------------------------ pipeline
    async def answer(self, question: str, history: list[dict] | None = None, progress=None) -> Answer:
        """progress(label) — ixtiyoriy: har tool-raund oldidan nima tekshirilayotgani ("service reminders")."""
        messages: list[dict] = [{"role": "system", "content": self.system_prompt()}]
        for h in history or []:
            messages.append({"role": h["role"], "content": h["content"][:HISTORY_CHARS]})
        messages.append({"role": "user", "content": question})
        calls: list[str] = []
        escalated = False  # arzon model 2 ta tool-raunddan keyin ham tugamasa yoki xato bersa, aqlli modelga o'tamiz

        for rnd in range(MAX_ROUNDS):
            last = rnd == MAX_ROUNDS - 1  # oxirgi raundda tool'siz javob yozishga majburlaymiz
            if last:
                messages.append({"role": "user", "content": _FINAL_NUDGE})
            models = self.models if escalated else self.simple_models
            resp = await self._chat(messages, tools=TOOLS, tool_choice="none" if last else "auto", max_tokens=MAX_TOKENS,
                                    models=models)
            msg = resp.choices[0].message
            if not msg.tool_calls:
                raw = msg.content or ""
                if _RAW_TOOL_TEXT.search(raw):  # model tool chaqiruvini matn qilib yozib yubordi
                    log.warning("Model tool chaqiruvini matn sifatida qaytardi: %s", raw[:200])
                    return Answer(_SORRY, calls, via="fleetio")
                return Answer(fmt.md_to_html(raw) or _SORRY, calls, via="fleetio" if calls else "text")
            messages.append({
                "role": "assistant", "content": msg.content or "",
                "tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                               for tc in msg.tool_calls],
            })
            async def _run_one(tc):
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                local_calls: list[str] = []  # alohida ro'yxat: parallel thread'lar bitta list'ni birga o'zgartirmasin
                content = await asyncio.to_thread(self.run_tool, tc.function.name, args, local_calls)
                return tc.id, content, local_calls

            if progress is not None:
                labels = list(dict.fromkeys(filter(None, (_label(tc) for tc in msg.tool_calls))))
                if labels:
                    try:
                        await progress(", ".join(labels[:3]) + (" …" if len(labels) > 3 else ""))
                    except Exception as e:  # noqa: BLE001 — holat xabari javobga ta'sir qilmasin
                        log.debug("progress: %s", e)
            # bir raunddagi bir nechta tool-chaqiruv bir vaqtda (ketma-ket emas) — Fleetio kutish vaqti qo'shilmaydi
            for tc_id, content, local_calls in await asyncio.gather(*(_run_one(tc) for tc in msg.tool_calls)):
                messages.append({"role": "tool", "tool_call_id": tc_id, "content": content})
                calls.extend(local_calls)
                if '"error"' in content:
                    escalated = True  # arzon model adashdi -> aqlli model o'zi tuzatsin
            if rnd + 1 >= ESCALATE_AFTER_ROUNDS:
                escalated = True
        return Answer(_SORRY, calls, via="fleetio")

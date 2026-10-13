"""Telegram bot (aiogram 3, long polling)."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict, deque

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           Message, ReplyKeyboardMarkup)

from . import fmt, quick
from .agent import Answer, FleetAgent
from .auth import Auth, AuthMiddleware
from .config import ROOT, settings, spreadsheet_file
from .fleetio import FleetioClient
from .fleetio_agent import FleetioAgent
from .loader import load
from .router import Router
from .wo_bot import WOBot
from .workorders import WorkOrders

log = logging.getLogger("bot")

HISTORY_TURNS = 2          # follow-up savollar uchun oxirgi 2 ta savol-javob eslab qolinadi
CACHE_TTL_S = 600          # bir xil savolga 10 daqiqa ichida qayta LLM chaqirilmaydi
TG_LIMIT = 4000
GREETING = re.compile(r"(?i)^\s*(salom|assalomu?\s*alaykum|assalom|привет|здравствуйте|салом|ассалому\s*алайкум|"
                      r"hello|hi|hey|good\s+(morning|afternoon|evening))[\s!.,)]*$")

BTN_STATS, BTN_PM, BTN_EXP, BTN_NODRV, BTN_HELP = "📊 Fleet overview", "🛠 PM status", "📄 Expirations", "🅿️ No driver", "❓ Help"
KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_STATS), KeyboardButton(text=BTN_PM)],
              [KeyboardButton(text=BTN_EXP), KeyboardButton(text=BTN_NODRV)],
              [KeyboardButton(text=BTN_HELP)]],
    resize_keyboard=True, is_persistent=True, input_field_placeholder="Ask a question, or type a unit # / driver name…",
)

HELP = (
    "👋 <b>Hello!</b> I answer questions about the <b>{company}</b> truck fleet spreadsheet.\n"
    "🚛 Active fleet: <b>{trucks} trucks</b> · data loaded {loaded}\n\n"
    "💬 <b>Ask in plain words</b>, for example:\n"
    "• Who has unit 417?\n"
    "• Which truck does Quentin Brownlee drive?\n"
    "• Which trucks are overdue for PM?\n"
    "• Trucks with inspections expiring next month\n"
    "• How many trucks are in the Riverview group?\n"
    "• Where are the trucks without a driver parked?\n"
    "• List of 2026 Volvos\n\n"
    "⚡️ <b>Quick commands</b> (no AI, instant):\n"
    "/unit 417 — truck card (by unit, VIN or plate)\n"
    "/pm — trucks with PM overdue / due soon\n"
    "/expiring — cab card and inspection expirations (30 days)\n"
    "/nodriver — trucks without a driver\n"
    "/stats — fleet statistics\n"
    "/reset — clear the conversation context\n"
    "/refresh — reload the spreadsheet\n"
    "/sql — the SQL used for the last answer (for checking)"
)


class State:
    def __init__(self, agent: FleetAgent):
        self.agent = agent
        self.router = Router(agent.sql)
        self.history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_TURNS * 2))
        self.last_sql: dict[int, list[str]] = {}
        self.cache: dict[str, tuple[float, Answer]] = {}
        self.file_mtime: float = 0.0
        self.reload_lock = asyncio.Lock()

    async def reload(self) -> str:
        async with self.reload_lock:
            path = await asyncio.to_thread(spreadsheet_file)
            data = await asyncio.to_thread(load, path)
            self.agent.set_data(data)
            self.router.set_sql(self.agent.sql)
            self.cache.clear()
            self.file_mtime = path.stat().st_mtime
            n = data.conn.execute("SELECT COUNT(*) FROM trucks").fetchone()[0]
            log.info("Jadval yuklandi: %s (aktiv park: %d)", path.name, n)
            return f"✅ Data reloaded: <b>{n} trucks</b> in the active fleet."

    def remember(self, chat: int, question: str, answer_html: str) -> None:
        self.history[chat].extend([{"role": "user", "content": question},
                                   {"role": "assistant", "content": fmt.strip_html(answer_html)[:400]}])


def _allowed_id(uid: int | None) -> bool:
    return not settings.allowed_user_ids or (uid is not None and uid in settings.allowed_user_ids)


def allowed(msg: Message) -> bool:
    return _allowed_id(msg.from_user.id if msg.from_user else None)


def chunks(text: str, n: int = TG_LIMIT):
    while text:
        if len(text) <= n:
            yield text
            return
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        yield text[:cut]
        text = text[cut:].lstrip("\n")


def unit_buttons(units: list[str]) -> InlineKeyboardMarkup | None:
    """Javobdagi mashinalar uchun tugmalar: bosilsa — to'liq karta."""
    if not units:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    for i, u in enumerate(units[:20]):
        if i % 4 == 0:
            rows.append([])
        rows[-1].append(InlineKeyboardButton(text=f"🚛 {u}", callback_data=f"u:{u}"[:64]))
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _safe(fn, text: str, **kw):
    """HTML yuboradi; Telegram HTML'ni rad etsa — oddiy matn."""
    try:
        return await fn(text, **kw)
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return None
        log.warning("HTML rad etildi (%s), oddiy matn yuborilmoqda", str(e)[:120])
        return await fn(fmt.strip_html(text), parse_mode=None, **kw)


async def send(msg: Message, text: str, units: list[str] | None = None, edit: Message | None = None) -> None:
    parts = list(chunks(text)) or ["—"]
    for i, part in enumerate(parts):
        markup = unit_buttons(units or []) if i == len(parts) - 1 else None
        if i == 0 and edit is not None:
            await _safe(edit.edit_text, part, reply_markup=markup)
        else:
            await _safe(msg.answer, part, reply_markup=markup)


async def unaddressed_in_group(msg: Message) -> bool:
    """Guruhdagi botga qaratilmagan xabar (buyruq, @mention yoki bot xabariga reply emas) — bot unga aralashmaydi."""
    if msg.chat.type == ChatType.PRIVATE:
        return False
    text = msg.text or msg.caption or ""
    if text.startswith("/"):
        return False
    me = await msg.bot.me()
    replied = msg.reply_to_message.from_user if msg.reply_to_message else None
    if replied is not None and replied.id == me.id:
        return False
    return f"@{me.username}".lower() not in text.lower()


def use_auth(dp: Dispatcher, auth: Auth | None, on_login, bypass=None) -> None:
    """Login/parol: login bo'lmagan foydalanuvchi xabarlari handlerlarga yetmaydi (outer middleware)."""
    if auth is not None and auth.enabled:
        mw = AuthMiddleware(auth, on_login=on_login, allowed=_allowed_id, bypass=bypass)
        dp.message.outer_middleware(mw)
        dp.callback_query.outer_middleware(mw)


def build_dispatcher(state: State, auth: Auth | None = None) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(lambda m: not allowed(m))
    async def denied(msg: Message):
        await msg.answer(f"Sorry, you don't have access to this bot. Your ID: {msg.from_user.id if msg.from_user else '?'}")

    async def help_(msg: Message):
        n = state.agent.data.conn.execute("SELECT COUNT(*) FROM trucks").fetchone()[0]
        await msg.answer(HELP.format(company=fmt.esc(settings.company_name), trucks=n,
                                     loaded=state.agent.data.loaded_at.strftime("%b %d, %H:%M")),
                         reply_markup=KEYBOARD)

    use_auth(dp, auth, help_)
    dp.message.register(help_, CommandStart())
    dp.message.register(help_, Command("help"))
    dp.message.register(help_, F.text == BTN_HELP)

    async def quick_reply(msg: Message, rep: quick.Reply) -> None:
        await send(msg, rep.text, rep.units)
        if msg.text:
            state.remember(msg.chat.id, msg.text, rep.text)

    @dp.message(Command("unit"))
    async def unit(msg: Message, command: CommandObject):
        await quick_reply(msg, quick.unit_lookup(state.agent.sql, command.args or ""))

    @dp.message(Command("pm"))
    @dp.message(F.text == BTN_PM)
    async def pm(msg: Message):
        await quick_reply(msg, quick.pm_report(state.agent.sql))

    @dp.message(Command("expiring"))
    @dp.message(F.text == BTN_EXP)
    async def exp(msg: Message, command: CommandObject | None = None):
        args = command.args if command else None
        days = int(args) if args and args.strip().isdigit() else 30
        await quick_reply(msg, quick.expiring(state.agent.sql, days))

    @dp.message(Command("stats"))
    @dp.message(F.text == BTN_STATS)
    async def st(msg: Message):
        await quick_reply(msg, quick.stats(state.agent.sql))

    @dp.message(Command("nodriver"))
    @dp.message(F.text == BTN_NODRV)
    async def nodrv(msg: Message):
        await quick_reply(msg, quick.no_driver(state.agent.sql))

    @dp.message(Command("reset"))
    async def reset(msg: Message):
        state.history.pop(msg.chat.id, None)
        await msg.answer("🧹 Conversation context cleared.")

    @dp.message(Command("sql"))
    async def sql(msg: Message):
        qs = state.last_sql.get(msg.chat.id)
        text = "\n\n".join(f"<code>{fmt.esc(q)}</code>" for q in qs) if qs else "No SQL has been used yet."
        await send(msg, text)

    @dp.message(Command("refresh"))
    async def refresh(msg: Message):
        if settings.admin_user_ids and msg.from_user.id not in settings.admin_user_ids:
            await msg.answer("This command is for admins only.")
            return
        try:
            await msg.answer(await state.reload())
        except Exception as e:  # noqa: BLE001
            log.exception("reload")
            await msg.answer(f"Could not reload the spreadsheet: {fmt.esc(e)}")

    @dp.callback_query(F.data.startswith("u:"))
    async def unit_cb(cb: CallbackQuery, bot: Bot):
        if not _allowed_id(cb.from_user.id):
            await cb.answer("No access", show_alert=True)
            return
        await cb.answer()
        rep = quick.unit_lookup(state.agent.sql, cb.data[2:])
        if isinstance(cb.message, Message):
            await send(cb.message, rep.text, rep.units)
        else:
            await bot.send_message(cb.from_user.id, rep.text)

    @dp.message(F.text)
    async def question(msg: Message):
        text = msg.text.strip()
        chat = msg.chat.id
        t0 = time.monotonic()
        if GREETING.match(text):
            await help_(msg)
            return
        rep = state.router.route(text)  # tez-tez so'raladiganlar -> LLM'siz, darhol
        if rep is not None:
            await quick_reply(msg, rep)
            log.info("router %.0f ms: %s", (time.monotonic() - t0) * 1000, text[:80])
            return

        hist = list(state.history[chat])
        key = re.sub(r"\W+", " ", text.lower()).strip()
        cached = state.cache.get(key)
        wait = None
        if cached and not hist and time.time() - cached[0] < CACHE_TTL_S:
            ans = cached[1]
        else:
            wait = await msg.answer("🔎 Looking it up…")
            try:
                ans = await state.agent.answer(text, hist)
            except Exception as e:  # noqa: BLE001
                log.exception("LLM xato")
                busy = "429" in str(e) or "rate" in str(e).lower()
                await _safe(wait.edit_text,
                            ("⏳ The AI is busy right now (rate limit). Please resend in 20–30 seconds."
                             if busy else "⚠️ Temporary AI service error. Please try again in a moment.")
                            + "\n\nThe buttons and unit / driver lookup keep working without AI.")
                return
            state.cache[key] = (time.time(), ans)
        state.last_sql[chat] = ans.queries
        state.remember(chat, text, ans.text)
        await send(msg, ans.text, ans.units, edit=wait)
        log.info("javob %.2f s (%s): %s", time.monotonic() - t0, ans.via, text[:80])

    return dp


# --------------------------------------------------------------------------- #
# Fleetio rejimi (DATA_SOURCE=fleetio): savollarga Fleetio saytidan, faqat o'qish
# --------------------------------------------------------------------------- #

BTN_F_OVERVIEW, BTN_F_SERVICE, BTN_F_RENEWALS, BTN_F_ISSUES = (
    "🚚 Fleet overview", "🛠 Service due", "📄 Renewals", "⚠️ Open issues")
F_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_F_OVERVIEW), KeyboardButton(text=BTN_F_SERVICE)],
              [KeyboardButton(text=BTN_F_RENEWALS), KeyboardButton(text=BTN_F_ISSUES)],
              [KeyboardButton(text=BTN_HELP)]],
    resize_keyboard=True, is_persistent=True, input_field_placeholder="Ask about vehicles, drivers, service…",
)
# tugma / buyruq -> tayyor savol (javobni baribir Fleetio'dan agent tayyorlaydi)
F_CANNED = {
    BTN_F_OVERVIEW: "Fleet overview: total number of vehicles and how many are in each status.",
    BTN_F_SERVICE: "Which service reminders are overdue or due soon? Show vehicle, service task and when it is due.",
    BTN_F_RENEWALS: "Which vehicle renewal reminders (registration, inspection, insurance etc.) are overdue or due soon?",
    BTN_F_ISSUES: "List open issues: vehicle, issue summary, priority and reported date.",
}
F_COMMANDS = {"overview": BTN_F_OVERVIEW, "service": BTN_F_SERVICE, "renewals": BTN_F_RENEWALS, "issues": BTN_F_ISSUES}

F_HELP = (
    "👋 <b>Hello!</b> I answer questions about the <b>{company}</b> fleet using <b>live data from Fleetio</b>.\n\n"
    "💬 <b>Ask in plain words</b>, for example:\n"
    "• How many vehicles do we have, by status?\n"
    "• Who is driving unit 417?\n"
    "• Which service reminders are overdue?\n"
    "• Open issues for unit 417\n"
    "• Last fuel entries of unit 417\n"
    "• Which registrations expire this month?\n\n"
    "🧾 <b>Work orders</b>: in an enabled chat, send the invoice file with the caption "
    "(Unit / Driver / Date / Amount / For / Ref / Responsible, then the shop name and address) — "
    "I create the work order in Fleetio.\n\n"
    "⚡️ Buttons below give quick reports.\n"
    "/reset — clear the conversation context\n"
    "/api — Fleetio requests used for the last answer (for checking)"
)
F_NOT_CONFIGURED = ("⚙️ Fleetio is not connected yet: the API key is missing. "
                    "Add <code>SECUREFLEET_KEY</code> to the bot's <code>.env</code> file (no restart needed).")


class FleetioState:
    def __init__(self, agent: FleetioAgent):
        self.agent = agent
        self.history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_TURNS * 2))
        self.last_calls: dict[int, list[str]] = {}
        self.cache: dict[str, tuple[float, Answer]] = {}

    def remember(self, chat: int, question: str, answer_html: str) -> None:
        self.history[chat].extend([{"role": "user", "content": question},
                                   {"role": "assistant", "content": fmt.strip_html(answer_html)[:400]}])


def build_fleetio_dispatcher(state: FleetioState, auth: Auth | None = None, wo: WOBot | None = None) -> Dispatcher:
    dp = Dispatcher()

    # work order oqimi birinchi: ruxsati chat bo'yicha (WO_CHAT_IDS), foydalanuvchi ro'yxati/login unga qo'llanmaydi
    if wo is not None:
        wo.register(dp)

    @dp.message(unaddressed_in_group)
    async def group_chatter(msg: Message):
        return None  # guruhdagi o'zaro yozishmalarga javob bermaymiz (savol uchun botni @mention qilish kerak)

    @dp.message(lambda m: not allowed(m))
    async def denied(msg: Message):
        await msg.answer(f"Sorry, you don't have access to this bot. Your ID: {msg.from_user.id if msg.from_user else '?'}")

    async def help_(msg: Message):
        text = F_HELP.format(company=fmt.esc(settings.company_name))
        if not state.agent.fleetio.configured():
            text += "\n\n" + F_NOT_CONFIGURED
        await msg.answer(text, reply_markup=F_KEYBOARD)

    async def no_login_needed(event) -> bool:
        if wo is not None and wo.is_wo_event(event):
            return True
        return isinstance(event, Message) and await unaddressed_in_group(event)

    use_auth(dp, auth, help_, no_login_needed)
    dp.message.register(help_, CommandStart())
    dp.message.register(help_, Command("help"))
    dp.message.register(help_, F.text == BTN_HELP)

    @dp.message(Command("reset"))
    async def reset(msg: Message):
        state.history.pop(msg.chat.id, None)
        await msg.answer("🧹 Conversation context cleared.")

    @dp.message(Command("api"))
    async def api(msg: Message):
        calls = state.last_calls.get(msg.chat.id)
        text = "\n".join(f"<code>GET {fmt.esc(c)}</code>" for c in calls) if calls else "No Fleetio requests yet."
        await send(msg, text)

    async def ask(msg: Message, question: str) -> None:
        chat = msg.chat.id
        t0 = time.monotonic()
        if not state.agent.fleetio.configured():
            await msg.answer(F_NOT_CONFIGURED)
            return
        hist = list(state.history[chat])
        key = re.sub(r"\W+", " ", question.lower()).strip()
        cached = state.cache.get(key)
        wait = None
        if cached and not hist and time.time() - cached[0] < CACHE_TTL_S:
            ans = cached[1]
        else:
            wait = await msg.answer("🔎 Checking Fleetio…")
            try:
                ans = await state.agent.answer(question, hist)
            except Exception as e:  # noqa: BLE001
                log.exception("LLM xato")
                busy = "429" in str(e) or "rate" in str(e).lower()
                await _safe(wait.edit_text,
                            "⏳ The AI is busy right now (rate limit). Please resend in 20–30 seconds."
                            if busy else "⚠️ Temporary AI service error. Please try again in a moment.")
                return
            if ans.queries:  # Fleetio'ga murojaat bo'lgan javobgina keshlanadi
                state.cache[key] = (time.time(), ans)
        state.last_calls[chat] = ans.queries
        state.remember(chat, question, ans.text)
        await send(msg, ans.text, edit=wait)
        log.info("javob %.2f s (%s, %d so'rov): %s", time.monotonic() - t0, ans.via, len(ans.queries), question[:80])

    for cmd, btn in F_COMMANDS.items():
        async def canned(msg: Message, _q=F_CANNED[btn]):
            await ask(msg, _q)
        dp.message.register(canned, Command(cmd))

    @dp.message(F.text)
    async def question(msg: Message):
        text = msg.text.strip()
        if GREETING.match(text):
            await help_(msg)
            return
        await ask(msg, F_CANNED.get(text, text))

    return dp


async def auto_refresh(state: State) -> None:
    """SHEET_URL bo'lsa har REFRESH_MINUTES da yuklab oladi; lokal fayl bo'lsa, o'zgarganda qayta o'qiydi."""
    if settings.refresh_minutes <= 0:
        return
    while True:
        await asyncio.sleep(settings.refresh_minutes * 60 if settings.sheet_url else 30)
        try:
            if settings.sheet_url:
                await state.reload()
            else:
                path = spreadsheet_file()
                if path.stat().st_mtime != state.file_mtime:
                    await state.reload()
        except Exception:  # noqa: BLE001
            log.exception("auto refresh")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    if not settings.telegram_token or not settings.llm_api_key:
        raise SystemExit(".env faylida TELEGRAM_BOT_TOKEN va LLM_API_KEY bo'lishi kerak.")
    llm = dict(api_key=settings.llm_api_key, base_url=settings.llm_base_url, model=settings.llm_model,
               fallback_models=list(settings.llm_fallback_models), simple_model=settings.llm_simple_model or None,
               company=settings.company_name)
    auth = Auth(settings.bot_login, settings.bot_password, ROOT / "data" / "authorized_users.json")

    wo = None
    if settings.data_source == "fleetio":
        fleetio = FleetioClient()
        agent = FleetioAgent(fleetio, **llm)
        # invoys fayli -> Fleetio work order; service task tanlashda arzon model zanjiri ishlatiladi
        wo = WOBot(WorkOrders(fleetio, llm_chat=agent._chat, llm_models=agent.simple_models))
        dp = build_fleetio_dispatcher(FleetioState(agent), auth, wo)
        commands = [
            BotCommand(command="overview", description="🚚 Fleet overview"),
            BotCommand(command="service", description="🛠 Service reminders due"),
            BotCommand(command="renewals", description="📄 Renewals due"),
            BotCommand(command="issues", description="⚠️ Open issues"),
            BotCommand(command="help", description="❓ What can I ask?"),
            BotCommand(command="reset", description="Clear conversation context"),
            BotCommand(command="api", description="Fleetio requests of the last answer"),
        ]
        source = f"Fleetio ({len(fleetio.catalog.routes)} read-only tools, key {'OK' if fleetio.configured() else 'YOQ'})"
        refresher = None
    else:
        path = spreadsheet_file()
        agent = FleetAgent(load(path), **llm)
        state = State(agent)
        state.file_mtime = path.stat().st_mtime
        dp = build_dispatcher(state, auth)
        commands = [
            BotCommand(command="stats", description="📊 Fleet overview"),
            BotCommand(command="pm", description="🛠 PM overdue / due soon"),
            BotCommand(command="expiring", description="📄 Cab card / inspection expirations"),
            BotCommand(command="nodriver", description="🅿️ Trucks without a driver"),
            BotCommand(command="unit", description="🚛 Truck card: /unit 417"),
            BotCommand(command="help", description="❓ What can I ask?"),
            BotCommand(command="reset", description="Clear conversation context"),
            BotCommand(command="refresh", description="Reload the spreadsheet"),
        ]
        source = f"jadval: {path.name}"
        refresher = auto_refresh(state)

    if auth.enabled:
        commands.append(BotCommand(command="logout", description="Log out"))
    bot = Bot(settings.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                                    link_preview_is_disabled=True))
    await bot.set_my_commands(commands)
    me = await bot.get_me()
    models_desc = (f"{agent.simple_models[0]} -> {', '.join(agent.models)}" if agent.simple_models != agent.models
                  else ", ".join(agent.models))
    log.info("Bot ishga tushdi: @%s | modellar: %s | %s | login: %s", me.username, models_desc, source,
             f"yoqilgan ({len(auth.users)} ta kirgan)" if auth.enabled else "O'CHIQ")

    if refresher is not None:
        asyncio.create_task(refresher)
    if wo is not None:  # ma'lumotnomalar fon rejimida yuklanadi — birinchi invoys kutib qolmasin
        wo.warm_up()
        log.info("Work order: ruxsatli chatlar %s%s", sorted(wo.svc.allowed_chats()) or "YO'Q (WO_CHAT_IDS bo'sh)",
                 " | SINOV rejimi (WO_DRY_RUN)" if wo.svc.dry_run() else "")
    # navbatdagi xabarlar tashlab yuborilmaydi: bot qayta ishga tushayotganda guruhga tashlangan invoys yo'qolmasin
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)

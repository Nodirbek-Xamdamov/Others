"""Telegram'siz terminalda sinash (bot bilan bir xil yo'l).

DATA_SOURCE=sheet: avval router, keyin LLM + SQL. DATA_SOURCE=fleetio: LLM + Fleetio API (faqat o'qish).

    python ask.py "417 unit kimda?"     # bitta savol
    python ask.py                        # interaktiv rejim (chiqish: Ctrl+C)
"""
import asyncio
import logging
import sys
import time

from app.config import settings
from app.fmt import strip_html


def build():
    llm = dict(api_key=settings.llm_api_key, base_url=settings.llm_base_url, model=settings.llm_model,
               fallback_models=list(settings.llm_fallback_models), simple_model=settings.llm_simple_model or None,
               company=settings.company_name)
    if settings.data_source == "fleetio":
        from app.fleetio import FleetioClient
        from app.fleetio_agent import FleetioAgent
        return FleetioAgent(FleetioClient(), **llm), None
    from app.agent import FleetAgent
    from app.config import spreadsheet_file
    from app.loader import load
    from app.router import Router
    agent = FleetAgent(load(spreadsheet_file()), **llm)
    return agent, Router(agent.sql)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    agent, router = build()
    history: list[dict] = []
    questions = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else None
    while True:
        q = questions.pop(0) if questions else (None if questions is not None else input("\nSavol> ").strip())
        if not q:
            break
        t = time.monotonic()
        rep = router.route(q) if router else None
        if rep:
            text, via, calls = rep.text, "router", []
        else:
            a = await agent.answer(q, history[-4:])
            text, via, calls = a.text, a.via, a.queries
        print(f"\n[{via}, {time.monotonic() - t:.2f}s]\n" + strip_html(text))
        if calls and settings.data_source == "fleetio":
            print("\nFleetio so'rovlari:\n" + "\n".join(f"  GET {c}" for c in calls))
        history += [{"role": "user", "content": q}, {"role": "assistant", "content": strip_html(text)[:400]}]
        if questions is not None and not questions:
            break


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, EOFError):
        pass

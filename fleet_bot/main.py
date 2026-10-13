"""Botni ishga tushirish: python main.py"""
import asyncio

from app.bot import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

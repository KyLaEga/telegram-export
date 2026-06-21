#!/usr/bin/env python3.12
# -*- coding: utf-8 -*-
"""
Диагностика авторизации Telegram (изолированно от логики экспорта).
Читает config.json рядом со скриптом и пытается залогиниться.

Код подтверждения Telegram присылает В ПРИЛОЖЕНИЕ Telegram (чат «Telegram»),
а не по СМС. СМС — только запасной канал и может опаздывать.
"""

import asyncio
import json
import os

from pyrogram import Client

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


async def main() -> None:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    print(f"api_id   : {cfg['api_id']!r}")
    print(f"api_hash : {cfg['api_hash'][:4]}…{cfg['api_hash'][-4:]} (len={len(cfg['api_hash'])})")
    print(f"phone    : {cfg['phone']!r}")
    print("→ Сейчас Telegram пришлёт код в ЧАТ «Telegram» в приложении.\n")

    app = Client(
        "tg_export_session",
        api_id=cfg["api_id"],
        api_hash=cfg["api_hash"],
        phone_number=cfg["phone"],
        workdir=SCRIPT_DIR,
    )
    async with app:
        me = await app.get_me()
        print(f"\n✅ Авторизация успешна: {me.first_name} (id={me.id}, @{me.username})")
        print("   Файл tg_export_session.session создан — далее код не понадобится.")


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3.12
# -*- coding: utf-8 -*-
"""
Расширенный логин в Telegram с выбором канала доставки кода.

Зачем: когда код «via Telegram app» не виден (нет активной сессии этого номера)
и СМС у РФ-оператора не доходит — можно ПЕРЕОТПРАВИТЬ код, и Telegram сменит
способ: app → SMS → звонок (робот продиктует код / последние цифры номера).

Создаёт тот же tg_export_session.session, что использует export_media.py,
поэтому после успешного входа основной скрипт запустится без вопросов.
"""

import asyncio
import json
import os

from pyrogram import Client
from pyrogram.errors import (
    SessionPasswordNeeded,
    PhoneCodeInvalid,
    PhoneCodeExpired,
    FloodWait,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


def describe(sent) -> str:
    t = type(sent.type).__name__ if sent.type else "?"
    name = str(sent.type).split(".")[-1] if sent.type else "?"
    nxt = str(sent.next_type).split(".")[-1] if sent.next_type else "нет"
    return f"способ доставки: {name}; следующий доступный: {nxt}"


async def main() -> None:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    phone = cfg["phone"]
    print(f"Номер: {phone}")
    print(f"api_id: {cfg['api_id']}, api_hash длиной {len(cfg['api_hash'])}\n")

    app = Client(
        "tg_export_session",
        api_id=cfg["api_id"],
        api_hash=cfg["api_hash"],
        workdir=SCRIPT_DIR,
    )

    await app.connect()
    try:
        sent = await app.send_code(phone)
        print("📨 Код отправлен. " + describe(sent))

        while True:
            print("\nЧто делаем:")
            print("  [код]  — ввести полученный код")
            print("  r      — переотправить (сменить способ: SMS / звонок)")
            print("  q      — выход")
            choice = input("→ ").strip()

            if choice.lower() == "q":
                print("Отмена.")
                return

            if choice.lower() == "r":
                try:
                    sent = await app.resend_code(phone, sent.phone_code_hash)
                    print("🔁 Переотправлено. " + describe(sent))
                    print("   (если стало CALL — жди ЗВОНКА, робот продиктует код)")
                except FloodWait as e:
                    print(f"⏳ Слишком часто. Подожди {e.value} c и попробуй снова.")
                continue

            code = choice.replace(" ", "")
            if not code.isdigit():
                print("Это не похоже на код. Введи цифры, или r / q.")
                continue

            try:
                await app.sign_in(phone, sent.phone_code_hash, code)
            except PhoneCodeInvalid:
                print("❌ Неверный код, попробуй ещё раз (или r — переотправить).")
                continue
            except PhoneCodeExpired:
                print("❌ Код истёк. Жми r, чтобы получить новый.")
                continue
            except SessionPasswordNeeded:
                pwd = input("🔐 Включена 2FA. Введи облачный пароль: ").strip()
                await app.check_password(pwd)

            break

        me = await app.get_me()
        print(f"\n✅ Вход выполнен: {me.first_name} (id={me.id}, @{me.username})")
        print("   Сессия сохранена. Теперь запускай: python3.12 export_media.py")
    finally:
        await app.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3.12
# -*- coding: utf-8 -*-
"""
QR-вход в Telegram — как «Подключить устройство» в официальном приложении.

Зачем: когда код входа «via Telegram app» не приходит / не виден, а СМС у
РФ-оператора (+7) не доставляется (Telegram для входа через API не предлагает
SMS-фолбэк, next_type=нет) — можно войти ВООБЩЕ без кода. Скрипт показывает QR,
а ты сканируешь его уже залогиненным официальным Telegram:

    Телефон → Настройки → Устройства → «Подключить устройство» → навести камеру.

Использует те же raw-методы, что и официальные клиенты (auth.exportLoginToken /
auth.importLoginToken), и создаёт тот же tg_export_session.session, что и
export_media.py — после входа GUI/CLI запустятся без вопросов.
"""

import asyncio
import base64
import json
import logging
import os
import subprocess
import sys
import time

from pyrogram import Client, raw
from pyrogram.errors import SessionPasswordNeeded
from pyrogram.session import Auth, Session

logging.getLogger("pyrogram").setLevel(logging.CRITICAL)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
QR_PNG = os.path.join(SCRIPT_DIR, "qr_login.png")


def render_qr(url: str, opened: bool) -> bool:
    """Печатает QR в терминал (ASCII) и кладёт чёткий PNG, открывая его один раз."""
    import qrcode

    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)

    qrcode.make(url).save(QR_PNG)  # перезаписываем тот же файл — Preview обновит вид
    if not opened:
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", QR_PNG], check=False)
            elif sys.platform.startswith("linux"):
                subprocess.run(["xdg-open", QR_PNG], check=False)
            elif sys.platform.startswith("win"):
                os.startfile(QR_PNG)  # type: ignore[attr-defined]  # noqa
        except Exception:
            pass
    return True


async def wait_for_scan(app: Client, deadline: int) -> None:
    """Ждёт updateLoginToken (QR отсканирован) ИЛИ истечения QR — затем вернётся."""
    queue = app.dispatcher.updates_queue
    while True:
        timeout = deadline - time.time()
        if timeout <= 0:
            return  # QR истёк — перевыпустим
        try:
            item = await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return
        if item is None:
            continue
        update = item[0] if isinstance(item, tuple) else item
        if isinstance(update, raw.types.UpdateLoginToken):
            return  # отсканировали — перевыпуск вернёт Success/MigrateTo


async def switch_dc(app: Client, dc_id: int) -> None:
    """Переключение на «домашний» DC аккаунта (как делает send_code при миграции)."""
    await app.session.stop()
    await app.storage.dc_id(dc_id)
    await app.storage.auth_key(
        await Auth(app, dc_id, await app.storage.test_mode()).create()
    )
    app.session = Session(
        app, dc_id, await app.storage.auth_key(), await app.storage.test_mode()
    )
    await app.session.start()


async def finalize(app: Client, authorization) -> object:
    user = authorization.user
    await app.storage.user_id(user.id)
    await app.storage.is_bot(False)
    return user


async def handle_2fa(app: Client) -> None:
    print("\n🔐 На аккаунте включён облачный пароль (2FA).")
    pwd = input("Введи пароль: ").strip()
    await app.check_password(pwd)


async def main() -> None:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)

    app = Client(
        "tg_export_session",
        api_id=cfg["api_id"],
        api_hash=cfg["api_hash"],
        workdir=SCRIPT_DIR,
    )
    await app.connect()
    opened = False
    try:
        if await app.storage.user_id():
            try:
                me = await app.get_me()
                print(f"Уже залогинен: {me.first_name} (@{me.username}). QR не нужен.")
                return
            except Exception:
                pass  # сессия мёртвая — идём за QR

        print("Открой Telegram на ТЕЛЕФОНЕ → Настройки → Устройства →")
        print("«Подключить устройство» и наведи камеру на QR.\n")

        while True:
            try:
                r = await app.invoke(
                    raw.functions.auth.ExportLoginToken(
                        api_id=cfg["api_id"], api_hash=cfg["api_hash"], except_ids=[]
                    )
                )
            except SessionPasswordNeeded:
                await handle_2fa(app)
                break

            if isinstance(r, raw.types.auth.LoginToken):
                url = "tg://login?token=" + base64.urlsafe_b64encode(r.token).decode().rstrip("=")
                print("=" * 54)
                opened = render_qr(url, opened)
                print("Жду скан… (QR обновится сам, если истечёт)\n")
                await wait_for_scan(app, r.expires)
                continue

            if isinstance(r, raw.types.auth.LoginTokenMigrateTo):
                await switch_dc(app, r.dc_id)
                try:
                    r = await app.invoke(
                        raw.functions.auth.ImportLoginToken(token=r.token)
                    )
                except SessionPasswordNeeded:
                    await handle_2fa(app)
                    break

            if isinstance(r, raw.types.auth.LoginTokenSuccess):
                user = await finalize(app, r.authorization)
                print(f"\n✅ Вход выполнен: {user.first_name} (id={user.id})")
                break

        me = await app.get_me()
        print(f"Сессия сохранена ({me.first_name}).")
        print("Теперь запускай:  venv/bin/python3.12 run_gui.py")
    finally:
        if os.path.exists(QR_PNG):
            try:
                os.remove(QR_PNG)
            except Exception:
                pass
        try:
            await app.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())

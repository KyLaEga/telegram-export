#!/usr/bin/env python3.12
# -*- coding: utf-8 -*-
"""
Тест многопоточного загрузчика на ОДНОМ файле из канала.
Сравнивает скорость fast (многопоток) vs обычного Pyrogram-стрима и проверяет,
что размер совпадает с эталоном. Запускать ПЕРЕД боевым прогоном.

Запуск:  python3.12 test_fast.py
"""

import asyncio
import filecmp
import json
import os
import sys
import time

from pyrogram import Client

from fast_download import fast_download_file, FastUnavailable, close_all_pools

# число соединений можно задать аргументом: python3.12 test_fast.py 4
CONNECTIONS = int(sys.argv[1]) if len(sys.argv) > 1 else 8

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
SESSION_NAME = "tg_export_session"


def extract(message):
    for kind in ("video", "document", "audio", "animation", "voice", "video_note"):
        m = getattr(message, kind, None)
        if m is not None:
            return m.file_id, getattr(m, "file_size", 0) or 0, kind
    if message.photo is not None:
        return message.photo.file_id, getattr(message.photo, "file_size", 0) or 0, "photo"
    return None


async def main():
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    app = Client(SESSION_NAME, api_id=cfg["api_id"], api_hash=cfg["api_hash"],
                 phone_number=cfg["phone"], workdir=SCRIPT_DIR)

    async with app:
        chat = await app.get_chat(cfg["channel"])
        print(f"Канал: {getattr(chat, 'title', cfg['channel'])}")

        # ищем первый достаточно крупный файл (>20 МБ) для честного теста
        target = None
        async for message in app.get_chat_history(chat.id, limit=200):
            info = extract(message)
            if info and info[1] >= 20 * 1024 * 1024:
                target = (message, *info)
                break
        if target is None:
            print("Не нашёл крупный файл (>20 МБ) среди последних 200 сообщений.")
            return

        message, file_id, size, kind = target
        print(f"Тестовый файл: msg {message.id}, тип {kind}, "
              f"{size/1024/1024:.1f} МБ\n")

        out_fast = os.path.join(SCRIPT_DIR, "_test_fast.bin")
        out_seq = os.path.join(SCRIPT_DIR, "_test_seq.bin")
        for p in (out_fast, out_seq, out_fast + ".dl", out_fast + ".dlmap"):
            if os.path.exists(p):
                os.remove(p)

        # 1) Многопоток
        print("⏩ Многопоточная загрузка…")
        try:
            t0 = time.time()
            last = [0]

            def cb(cur):
                now = time.time()
                if now - last[0] > 0.5:
                    last[0] = now
                    sp = cur / (now - t0) if now > t0 else 0
                    print(f"\r   {cur/1024/1024:6.1f}/{size/1024/1024:.1f} МБ "
                          f"{sp/1024/1024:.2f} МБ/с", end="", flush=True)

            await fast_download_file(app, file_id, out_fast, size, progress=cb,
                                     connections=CONNECTIONS)
            dt_fast = time.time() - t0
            got = os.path.getsize(out_fast)
            print(f"\n   ✓ {got/1024/1024:.1f} МБ за {dt_fast:.1f} c = "
                  f"{got/dt_fast/1024/1024:.2f} МБ/с | размер совпал: {got == size}")
        except FastUnavailable as e:
            print(f"\n   ✗ Многопоток недоступен: {e}")
            dt_fast = None

        # 2) Обычный стрим Pyrogram
        print("\n🐌 Обычная (однопоточная) загрузка…")
        t0 = time.time()
        with open(out_seq, "wb") as fh:
            async for chunk in app.stream_media(file_id):
                fh.write(chunk)
        dt_seq = time.time() - t0
        got2 = os.path.getsize(out_seq)
        print(f"   ✓ {got2/1024/1024:.1f} МБ за {dt_seq:.1f} c = "
              f"{got2/dt_seq/1024/1024:.2f} МБ/с | размер совпал: {got2 == size}")

        # Итог
        print("\n" + "─" * 50)
        if dt_fast:
            print(f"Многопоток:  {size/dt_fast/1024/1024:.2f} МБ/с")
            print(f"Однопоток:   {size/dt_seq/1024/1024:.2f} МБ/с")
            print(f"Ускорение:   ×{dt_seq/dt_fast:.2f}")
            # filecmp.cmp(shallow=False) сравнивает побайтно потоково (блоками) и сам
            # закрывает дескрипторы — не тянем оба файла целиком в RAM, не текут хендлы.
            same = filecmp.cmp(out_fast, out_seq, shallow=False)
            print(f"Содержимое идентично: {same}")
        print("─" * 50)

        for p in (out_fast, out_seq):
            if os.path.exists(p):
                os.remove(p)

        await close_all_pools(app)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⛔ Прервано пользователем.")

#!/usr/bin/env python3.12
# -*- coding: utf-8 -*-
"""
Upload Pipeline: рекурсивная отправка файлов из локальной папки в Telegram-канал/чат.

Симметрично загрузчику (export_media) и переиспользует его инфраструктуру: Reporter
(вывод в CLI/GUI), мягкую остановку (stop_event), сетевые константы, логгер. Гарантии:

  * Идемпотентность (Track Uploads): отдельная БД upload_state.db (synchronous=FULL).
    Уже отправленные файлы (по chat+path+size+mtime) пропускаются — нет дублей в канале
    и двойного трафика при обрыве/рестарте.
  * Нативное медиа (send_video/send_photo/send_audio): сервер генерирует потоковое превью
    (Quick Look / плеер Apple) — иначе видео уйдёт «документом» без предпросмотра.
  * Хронология архива: рекурсивный обход + АЛФАВИТНАЯ сортировка; имя файла кладётся в
    caption → архив индексируется штатным поиском Telegram.
  * Цель: канал по умолчанию из config.json, либо переданный chat (@username / id / из
    списка диалогов).

Запуск из CLI:  python3.12 export_media.py --upload /путь/к/папке [--to @канал]
"""

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass

from pyrogram import Client
from pyrogram.errors import FloodWait

import export_media as em

UPLOAD_DB_NAME = "upload_state.db"

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".3gp"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma"}
ANIM_EXT = {".gif"}

# Служебные файлы, которые НЕ отправляем (наши БД/отчёты/логи/temp загрузчика/сессии).
SKIP_NAMES = {"download_state.db", "index.db", UPLOAD_DB_NAME, "config.json"}
SKIP_SUFFIX = (".dl", ".dlmap", ".part", ".tmp", ".faststart.tmp.mp4",
               "-wal", "-shm", ".log", ".txt", ".session", ".session-journal")


@dataclass
class UploadOptions:
    recursive: bool = True          # обходить вложенные подпапки
    native_media: bool = True       # send_video/photo/audio vs send_document
    skip_uploaded: bool = True      # пропускать уже отправленное (Track Uploads)
    caption_filename: bool = True   # имя файла → caption (индексируемость)
    preview_only: bool = False      # только предпросмотр (для экрана «Сканировать» в GUI)


def classify(path: str) -> str:
    """Тип медиа по расширению — определяет метод отправки."""
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in PHOTO_EXT:
        return "photo"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in ANIM_EXT:
        return "animation"
    return "document"


def _is_skippable(name: str) -> bool:
    low = name.lower()
    if name in SKIP_NAMES or name.startswith("."):
        return True
    return any(low.endswith(s) for s in SKIP_SUFFIX)


def scan_folder(folder: str, recursive: bool) -> list[str]:
    """Список файлов к отправке, отсортированный АЛФАВИТНО по полному пути (хронология архива).

    os.scandir вместо listdir+stat — тип записи берётся из самого каталога, без отдельного
    stat() на файл. В каталоги-симлинки НЕ заходим (follow_symlinks=False), иначе циклический
    симлинк зациклил бы обход. Каждый scandir закрывается до спуска в подкаталог, поэтому
    одновременно открыт ровно один дескриптор каталога.
    """
    files: list[str] = []

    def _scan(dir_path: str) -> None:
        subdirs: list[str] = []
        try:
            with os.scandir(dir_path) as it:
                for entry in it:
                    if _is_skippable(entry.name):
                        continue
                    if entry.is_file():
                        files.append(entry.path)
                    elif recursive and entry.is_dir(follow_symlinks=False):
                        subdirs.append(entry.path)
        except OSError:
            return
        for sub in subdirs:
            _scan(sub)

    _scan(folder)
    files.sort()
    return files


# ── Изолированная immortal-БД отправленного (upload_state.db) ────────────────────
def upload_db_connect() -> sqlite3.Connection:
    path = os.path.join(em.DATA_DIR, UPLOAD_DB_NAME)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")     # каждый отправленный файл — НАМЕРТВО
    conn.execute("PRAGMA mmap_size=268435456")
    conn.execute("PRAGMA cache_size=-10000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS uploads (
            chat_key   TEXT NOT NULL,
            path       TEXT NOT NULL,
            size       INTEGER NOT NULL,
            mtime      INTEGER NOT NULL,
            message_id INTEGER,
            status     TEXT,
            ts         INTEGER,
            PRIMARY KEY (chat_key, path)
        )
    """)
    conn.commit()
    return conn


def is_uploaded(conn, chat_key, path, size, mtime) -> bool:
    """Файл уже отправлен в ЭТОТ чат и не менялся (тот же size+mtime)."""
    row = conn.execute(
        "SELECT size, mtime, status FROM uploads WHERE chat_key=? AND path=?",
        (chat_key, path)).fetchone()
    return bool(row and row["status"] == "DONE"
                and row["size"] == size and row["mtime"] == mtime)


def mark_uploaded(conn, chat_key, path, size, mtime, message_id) -> None:
    conn.execute(
        "INSERT INTO uploads(chat_key, path, size, mtime, message_id, status, ts) "
        "VALUES(?,?,?,?,?, 'DONE', ?) "
        "ON CONFLICT(chat_key, path) DO UPDATE SET "
        "size=excluded.size, mtime=excluded.mtime, message_id=excluded.message_id, "
        "status='DONE', ts=excluded.ts",
        (chat_key, path, size, mtime, message_id, int(time.time())))
    conn.commit()                                   # мгновенная фиксация — без пакетов


async def _resolve_target(app, target, rep):
    """Резолвит цель отправки: числовой id / @username / публичную или ПРИВАТНУЮ
    invite-ссылку (https://t.me/+HASH, t.me/joinchat/HASH). Для приватной ссылки, если
    аккаунт ещё не участник, — вступает по приглашению (join_chat) и затем берёт чат."""
    if not isinstance(target, str):
        return await app.get_chat(target)
    t = target.strip()
    # Числовой id, переданный строкой («-1001953959250»), → int (иначе примут за username).
    if t.lstrip("-").isdigit():
        return await app.get_chat(int(t))
    is_invite = ("t.me/+" in t) or ("t.me/joinchat/" in t) or t.startswith("+")
    try:
        return await app.get_chat(t)
    except Exception as e:                           # noqa: BLE001
        if not is_invite:
            raise em.ExportError(
                f"❌ Не удалось открыть чат «{t}»: {type(e).__name__}: {e}. "
                f"Проверьте @username / id / ссылку и доступ аккаунта.")
        # Приватная ссылка и мы НЕ участники — вступаем по приглашению, затем берём чат.
        rep.status("🔗 Приватная ссылка — вступаю в чат по приглашению…")
        try:
            return await app.join_chat(t)
        except Exception as e2:                      # noqa: BLE001 (напр. уже участник → берём ниже)
            try:
                return await app.get_chat(t)
            except Exception:
                raise em.ExportError(
                    f"❌ Не удалось открыть чат по ссылке: {type(e2).__name__}: {e2}. "
                    f"Убедитесь, что ссылка действующая и аккаунт имеет доступ. Для отправки "
                    f"в КАНАЛ аккаунт должен быть админом с правом публикации.")


async def _send_one(app, chat_id, path, kind, caption, native, progress):
    """Отправляет ОДИН файл подходящим методом; возвращает Message."""
    if native and kind == "video":
        return await app.send_video(chat_id, path, caption=caption, progress=progress)
    if native and kind == "photo":
        return await app.send_photo(chat_id, path, caption=caption, progress=progress)
    if native and kind == "audio":
        return await app.send_audio(chat_id, path, caption=caption, progress=progress)
    if native and kind == "animation":
        return await app.send_animation(chat_id, path, caption=caption, progress=progress)
    return await app.send_document(chat_id, path, caption=caption,
                                   force_document=True, progress=progress)


async def run_upload(cfg, folder, chat=None, options: "UploadOptions | None" = None,
                     reporter=None, stop_event=None, manage_signals: bool = True) -> dict:
    """Отправляет файлы из `folder` в `chat` (или канал из cfg). Возвращает структурный
    результат (предпросмотр/итоги). События — через reporter (CLI/GUI)."""
    options = options or UploadOptions()
    rep = reporter or em.CliReporter()
    em.REPORTER = rep                               # движковые хелперы пишут через REPORTER
    if stop_event is None:
        stop_event = asyncio.Event()
    em.SHUTDOWN = stop_event
    sig_holder: dict = {"feed": None, "tasks": []}
    if manage_signals:
        em.install_signal_handlers(asyncio.get_running_loop(), stop_event, sig_holder)

    folder = os.path.abspath(os.path.expanduser(folder))
    if not os.path.isdir(folder):
        raise em.ExportError(f"❌ Папка не найдена: {folder}")
    em.LOG = em.setup_logger(em.DATA_DIR)           # logs next to upload_state.db (writable dir)
    em.ERR_LOG = em.setup_error_logger(em.DATA_DIR)

    conn = upload_db_connect()
    app = Client(em.SESSION_NAME, api_id=cfg["api_id"], api_hash=cfg["api_hash"],
                 phone_number=cfg["phone"], workdir=em.DATA_DIR, no_updates=True)

    result: dict = {}
    sent = failed = sent_bytes = 0
    try:
      async with app:
        target = chat or cfg["channel"]
        # Резолв цели: id / @username / публичная или приватная invite-ссылка (+ join при нужде).
        ch = await _resolve_target(app, target, rep)
        chat_key = str(ch.id)
        rep.status(f"📡 Цель загрузки: {getattr(ch, 'title', target)} (id={ch.id})")
        rep.status(f"📂 Источник: {folder}\n")

        files = scan_folder(folder, options.recursive)
        todo: list[tuple[str, int, int]] = []
        already = 0
        total_bytes = 0
        for p in files:
            try:
                stt = os.stat(p)
            except OSError:
                continue
            size, mtime = stt.st_size, int(stt.st_mtime)
            if options.skip_uploaded and is_uploaded(conn, chat_key, p, size, mtime):
                already += 1
                continue
            todo.append((p, size, mtime))
            total_bytes += size

        preview = {"total_files": len(files), "already_uploaded": already,
                   "to_upload": len(todo), "to_upload_bytes": total_bytes,
                   "chat_title": getattr(ch, "title", str(target)), "chat_id": ch.id}
        result["preview"] = preview
        rep.upload_preview(preview)
        rep.status("─" * 70)
        rep.status("📋 ПРЕДПРОСМОТР ЗАГРУЗКИ В КАНАЛ")
        rep.status(f"   Файлов в папке     : {len(files)}")
        rep.status(f"   Уже отправлено     : {already}")
        rep.status(f"   📤 К ОТПРАВКЕ       : {len(todo)} файлов, "
                   f"{em.human_gb(total_bytes):.2f} ГБ")
        rep.status("─" * 70 + "\n")

        if options.preview_only:
            return result
        if not todo:
            rep.status("✅ Всё уже загружено — отправлять нечего.")
            return result

        for path, size, mtime in todo:
            if stop_event.is_set():
                break
            name = os.path.basename(path)
            kind = classify(path)
            caption = name if options.caption_filename else None
            t0 = time.time()

            def progress(cur, tot, _name=name, _t0=t0):
                dt = time.time() - _t0
                rep.upload_progress(_name, cur, tot, cur / dt if dt > 0 else 0)

            rep.upload_started(name, size)
            ok = False
            msg = None
            net_tries = 0
            while not ok:
                if stop_event.is_set():
                    break
                try:
                    msg = await _send_one(app, ch.id, path, kind, caption,
                                          options.native_media, progress)
                    ok = True
                except FloodWait as e:
                    w = int(getattr(e, "value", 0) or 0)
                    rep.status(f"\n⏳ FloodWait при отправке {name}: ждём {w} c…")
                    await asyncio.sleep(w + 1)
                except em.NETWORK_ERRORS as e:
                    net_tries += 1
                    if net_tries > em.NET_MAX_RETRIES:
                        em.log_error("-", f"upload {name}: обрыв сети, сдаюсь "
                                          f"({type(e).__name__}: {e})")
                        break
                    rep.status(f"\n🌐 обрыв сети при отправке {name} — повтор "
                               f"{net_tries}/{em.NET_MAX_RETRIES} через {em.NET_RETRY_WAIT} c…")
                    await asyncio.sleep(em.NET_RETRY_WAIT)
                except Exception as e:               # noqa: BLE001 — один битый файл не валит всё
                    em.log_error("-", f"upload {name}: {type(e).__name__}: {e}")
                    rep.upload_failed(name, f"{type(e).__name__}: {e}")
                    break

            if ok and msg is not None:
                mid = getattr(msg, "id", None)
                mark_uploaded(conn, chat_key, path, size, mtime, mid)
                dt = time.time() - t0
                rep.upload_done(name, size, size / dt if dt > 0 else 0, mid)
                em.log(f"UPLOAD ok {name} {size}B → msg {mid}")
                sent += 1
                sent_bytes += size
            elif not stop_event.is_set():
                failed += 1

        summary = (f"отправлено: {sent} ({em.human_gb(sent_bytes):.2f} ГБ), "
                   f"пропущено (уже было): {already}, ошибок: {failed}.")
        rep.status(f"\n✅ Готово. {summary}")
        rep.finished(summary, stop_event.is_set())
        em.log(f"=== UPLOAD DONE {summary}")
        result["stats"] = {"sent": sent, "skipped": already, "failed": failed,
                           "sent_bytes": sent_bytes}
        result["stopped"] = stop_event.is_set()
        if stop_event.is_set():
            rep.status("⚠️  Остановлено досрочно. Перезапуск продолжит с места "
                       "(уже отправленное пропустится).")
    finally:
        conn.commit()
        conn.close()
    return result


async def scan_upload(cfg, folder, chat=None, options: "UploadOptions | None" = None,
                      reporter=None, stop_event=None) -> dict:
    """Предпросмотр отправки (сколько новых/уже отправлено) БЕЗ загрузки — для GUI."""
    from dataclasses import replace as _replace
    opts = _replace(options or UploadOptions(), preview_only=True)
    return await run_upload(cfg, folder, chat=chat, options=opts, reporter=reporter,
                            stop_event=stop_event, manage_signals=False)

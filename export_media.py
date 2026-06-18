#!/usr/bin/env python3.12
# -*- coding: utf-8 -*-
"""
Экспорт медиа из Telegram-канала/чата напрямую на внешний SSD (macOS).

Возможности:
  * Zero-In-Memory: чанки стримятся из сети сразу в файл на внешнем SSD.
  * Идемпотентность + защита TBW: готовые файлы пропускаются по размеру; частичные
    докачиваются; «битые»/недокачанные перекачиваются заново (верификация по размеру).
  * Дедупликация по содержимому (file_unique_id): один файл из разных сообщений
    скачивается один раз.
  * Двухпроходная резолюция качества (Two-Pass Quality Resolution): на pre-flight
    сканировании медиа группируются по «отпечатку» (видео: тип+имя+длительность,
    фото/документы: тип+имя); из каждой группы качается только самая крупная версия
    («мастер»), худшие исключаются ДО обращения к диску (экономия TBW SSD). Судьба
    пропущенных версий пишется в quality_report.txt. Отключается флагом --no-quality.
    Длительности сравниваются нечётко (|Δ| ≤ 1 c) — сервер округляет метаданные при
    пережатии (Fuzzy Duration).
  * Эвристический Guard дубликатов (Frequency Analyzer): на pre-flight любое имя файла,
    встречающееся в канале > 3 раз с РАЗНЫМИ размерами, динамически помечается «generic»
    и роняется на защиту «Имя + Размер» — без ручного ведения словарей шаблонных имён.
  * Apple Quick Look (faststart): свежескачанное видео ремуксится через ffmpeg
    (-c copy -movflags +faststart) — moov-атом переносится в начало, чтобы macOS-предпросмотр
    по пробелу работал. Нет ffmpeg → тихое предупреждение, экспорт продолжается. --no-faststart.
  * Предпросмотр: сколько файлов и какой объём нужно скачать + оценка времени.
  * Живая скорость загрузки справа от прогрессбара.
  * Параллельная загрузка (--workers), автоперехват FloodWait с возобновлением.
  * Хронологический обход (старые→новые) + кэш списка ID на SSD (инкрементальный
    дочитыватель новых сообщений; --rescan для пересбора).
  * Лог-файл export_log.txt, фильтр по типам (--only / --skip), проверка ФС SSD,
    защита от заполнения диска, локальный config.json (--reset).

Механизмы стабильности Production-уровня:
  1. Защита от потери тома (Mount Protection): перед загрузкой и во время неё
     проверяется os.path.ismount целевого тома; если SSD отвалился — немедленный
     останов, чтобы не писать на системный диск macOS.
  2. Санитайзер длины имени (Path Length Truncation): имя файла (с префиксом msg_ID_)
     не превышает 200 символов — середина исходного имени обрезается, расширение
     сохраняется (защита от OSError: File name too long).
  3. Безопасная конкурентность (asyncio.Semaphore): число одновременно качающихся
     файлов ограничено --workers, сохраняя контроль над FloodWait.
  4. Обработка обрывов TCP: TimeoutError/ConnectionError перехватываются, файл
     докачивается с места — пауза 15 c, до 5 попыток возобновления.
  5. Тихий логгер ошибок: критические исключения и пропущенные файлы пишутся в
     error_export.log (msg_id + текст), не засоряя прогресс-бар в консоли.

Зависимости:  pip install pyrogram tgcrypto
Запуск:       python3.12 export_media.py
              python3.12 export_media.py --workers 3 --only video,document
"""

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, replace

from pyrogram import Client
# Импорт ТОЧНО по глубокому пути модуля (та же причина, что и для FileReferenceExpired
# ниже): обобщённый `from pyrogram.errors import FloodWait` в некоторых сборках Pyrogram
# резолвится в иной объект класса, и тогда `except FloodWait` молча пропускает реальный
# флуд от auth.ExportAuthorization, а многопоточный воркер падает наружу каскадом.
from pyrogram.errors.exceptions.flood_420 import FloodWait
# Импорт ТОЧНО по глубокому пути модуля: обобщённый `from pyrogram.errors import
# FileReferenceExpired` в некоторых сборках Pyrogram резолвится в иной объект класса,
# и тогда `except FileReferenceExpired` молча пропускает реальное исключение, а воркер
# падает наружу. Жёсткая привязка к bad_request_400 гарантирует перехват того самого
# типа, который поднимают низкоуровневые методы (get_file/stream_media/download_media).
from pyrogram.errors.exceptions.bad_request_400 import FileReferenceExpired

from fast_download import fast_download_file, FastUnavailable, close_all_pools

# ──────────────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _is_writable_dir(path: str) -> bool:
    """True if files can be created in `path` (False for a read-only app bundle)."""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_probe")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _user_data_dir() -> str:
    """Per-user, always-writable app-data directory, used when SCRIPT_DIR is a
    read-only packaged bundle (.app / .msi / .AppImage)."""
    app = "Telegram Export"
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, app)


# DATA_DIR — where config, the Telegram session and (per-account) logs live. Running
# from source it's the project dir (SCRIPT_DIR), keeping existing files in place. In a
# read-only packaged bundle SCRIPT_DIR isn't writable, so fall back to the per-user
# data dir — otherwise Pyrogram's .session / unknown_errors.txt hit a read-only
# filesystem (errno 30) the moment you request a login code.
DATA_DIR = SCRIPT_DIR if _is_writable_dir(SCRIPT_DIR) else _user_data_dir()
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
SESSION_NAME = "tg_export_session"
CHUNK_SIZE = 1024 * 1024
DB_NAME = "index.db"                          # кэш Telegram: индекс ID/метаданные (SQLite)
STATE_DB_NAME = "download_state.db"           # БЕССМЕРТНАЯ БД скачанного — --rescan её НЕ трогает
LOG_NAME = "export_log.txt"
ERR_LOG_NAME = "error_export.log"            # тихий лог критических ошибок и пропусков
DEFAULT_WORKERS = 3
SAFETY_MARGIN = 2 * 1024 ** 3
MAX_RETRIES = 3
MAX_FILENAME_LEN = 200                        # макс. длина имени файла (с префиксом msg_ID_)
NET_RETRY_WAIT = 15                           # пауза перед возобновлением при обрыве сети, c
NET_MAX_RETRIES = 5                           # попыток возобновления файла при обрыве TCP
FRE_MAX_REFRESH = 2                           # макс. горячих регенераций токена (FRE) на файл
# Сетевые обрывы, после которых имеет смысл подождать и продолжить с места докачки.
# ConnectionError покрывает ConnectionReset/Aborted/BrokenPipe (его подклассы).
NETWORK_ERRORS = (asyncio.TimeoutError, TimeoutError, ConnectionError)
FAST_MIN_SIZE = 10 * 1024 * 1024         # многопоток только для файлов крупнее 10 МБ
DEFAULT_CONNECTIONS = 8                   # соединений на файл в многопотоке
KNOWN_KINDS = ("video", "document", "audio", "voice", "animation", "video_note", "photo")
VIDEO_KINDS = ("video", "animation", "video_note")   # типы, для которых важна длительность/faststart
# Допуск «усадки» после ffmpeg faststart: ремукс (-c copy -movflags +faststart) перепаковывает
# контейнер и может сделать файл на доли процента МЕНЬШЕ серверного эталона (выкинутые free/
# padding-атомы). На реальных данных усадка ≤0.5%, тогда как настоящие обрезки/провалы — ≥8%
# или ~100% (пустышки). Файл в пределах этого допуска от эталона считаем ГОТОВЫМ, не обрезком.
FASTSTART_SHRINK_TOL = 0.02              # 2% — с запасом покрывает faststart, отсекает реальные недокачки
GENERIC_FREQ_THRESHOLD = 3               # имя > N раз с РАЗНЫМИ размерами → динамически generic

# Скорости для оценки времени (из реальных замеров пользователя):
AVG_SPEED_BPS = int(850 * 1024)        # средняя ~850 КБ/с
PEAK_SPEED_BPS = int(1.1 * 1024 * 1024)  # пиковая ~1.1 МБ/с

LOG: logging.Logger | None = None
ERR_LOG: logging.Logger | None = None     # тихий логгер ошибок (error_export.log)
CHAT_ID = None                            # id текущего канала (для обновления file_id)
POOL_SIZE = 0                             # общий размер пула media-сессий (workers*conn)
DL_SEM: "asyncio.Semaphore | None" = None  # семафор безопасной конкурентности загрузок
SHUTDOWN: "asyncio.Event | None" = None    # флаг мягкой остановки (выставляется по SIGINT)
DYNAMIC_GENERIC: set[str] = set()          # имена, динамически признанные generic (Frequency Analyzer)
FFMPEG_STATE = {"missing": False}          # кэш: ffmpeg не найден — больше не пытаемся/не спамим warning
FASTSTART = True                           # постобработка видео под Apple Quick Look (--no-faststart выкл.)
# ──────────────────────────────────────────────────────────────────────────────


def human_mb(n: int) -> float:
    return n / (1024 * 1024)


def human_gb(n: int) -> float:
    return n / (1024 ** 3)


def fmt_speed(bps: float) -> str:
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.2f} МБ/с"
    return f"{bps / 1024:.0f} КБ/с"


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d} дн")
    if h:
        parts.append(f"{h} ч")
    if m or not parts:
        parts.append(f"{m} мин")
    return " ".join(parts)


def log(msg: str) -> None:
    if LOG is not None:
        LOG.info(msg)


def log_error(msg_id, text: str) -> None:
    """Тихий логгер ошибок (механизм 5): пишет в error_export.log, не трогая консоль.

    Критические исключения и пропущенные файлы уходят сюда с указанием msg_id и текста
    ошибки, чтобы прогресс-бар в консоли оставался чистым. Дублируем запись и в основной
    лог (export_log.txt) для единой хронологии.
    """
    line = f"msg {msg_id}: {text}"
    if ERR_LOG is not None:
        ERR_LOG.error(line)
    log(f"ERROR {line}")


# ── Абстракция вывода (Reporter) ────────────────────────────────────────────────
# Движок (run_export и всё, что он вызывает) больше НЕ печатает в stdout напрямую — он
# шлёт события в Reporter. CLI-обёртка ставит CliReporter (воспроизводит прежний вывод
# терминала 1-в-1), а GUI — свой QtReporter (кладёт события в очередь к Qt). Так одна и
# та же проверенная загрузочная логика работает и в терминале, и в окне.
class ExportError(Exception):
    """Фатальная, но НЕ аварийная ошибка движка (нет тома, нет доступа, мало места…).

    Раньше такие случаи звали sys.exit() прямо из движка — это убивало бы GUI-процесс.
    Теперь движок поднимает ExportError, а вызывающий слой (CLI или GUI) решает, как
    показать её пользователю (печать+выход или диалог)."""


class Reporter:
    """Базовый интерфейс событий движка. Методы по умолчанию — no-op."""

    # Текстовые строки (одноразовые статусы пред-флайта, сетевые сообщения и т.п.)
    def status(self, text: str = "") -> None: ...
    def status_inline(self, text: str) -> None: ...   # без перевода строки (\r-апдейты)
    def status_clear(self) -> None: ...               # затереть текущую \r-строку
    # Живой прогресс одного файла. worker ("w1", "w2", …) маршрутизирует событие к
    # конкретной полосе Multi-Worker View в GUI (составной сигнал worker+current+total).
    def file_progress(self, worker, msg_id, current, total, speed) -> None: ...
    def progress_done(self, worker, msg_id) -> None: ...  # файл дозагружен (перевод строки)
    # Жизненный цикл файла / события воркера
    def file_started(self, worker, msg_id, fn, size) -> None: ...
    def file_done(self, worker, msg_id, size, speed) -> None: ...
    def file_skipped(self, msg_id) -> None: ...       # уже на диске (COMPLETE) — пропуск
    def file_dedup(self, msg_id, dup_fn) -> None: ...
    def file_repair_oversize(self, msg_id) -> None: ...
    def file_failed(self, msg_id, retries, fn) -> None: ...
    # Структурные данные для UI (CLI их игнорирует — печать идёт через status())
    def plan_preview(self, summary: dict) -> None: ...
    def stats(self, snapshot: dict) -> None: ...
    def finished(self, summary: str, stopped: bool) -> None: ...
    # Upload Pipeline (отправка папки в канал) — симметрично загрузке
    def upload_started(self, name, size) -> None: ...
    def upload_progress(self, name, current, total, speed) -> None: ...
    def upload_done(self, name, size, speed, message_id) -> None: ...
    def upload_skipped(self, name) -> None: ...
    def upload_failed(self, name, err) -> None: ...
    def upload_preview(self, summary: dict) -> None: ...


class CliReporter(Reporter):
    """Печатает в терминал ровно так же, как делал прежний CLI (до рефакторинга под GUI)."""

    PROGRESS_WIDTH = 24

    def __init__(self) -> None:
        # Активные воркеры: \r-прогресс-бар рендерим только пока качает один поток,
        # иначе несколько баров затирали бы друг друга в одной строке терминала.
        self._active: set = set()

    def status(self, text: str = "") -> None:
        print(text)

    def status_inline(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def status_clear(self) -> None:
        sys.stdout.write("\r" + " " * 40 + "\r")
        sys.stdout.flush()

    def file_progress(self, worker, msg_id, current, total, speed) -> None:
        if len(self._active) > 1:
            return                                # несколько потоков — \r-бар не рисуем
        width = self.PROGRESS_WIDTH
        pct = (current / total) if total else 0
        bar = "█" * int(width * pct) + "─" * (width - int(width * pct))
        sys.stdout.write(
            f"\rmsg {msg_id:>9} │{bar}│ {pct*100:5.1f}% "
            f"{human_mb(current):7.1f}/{human_mb(total):7.1f} МБ  {fmt_speed(speed):>11}"
        )
        sys.stdout.flush()

    def progress_done(self, worker, msg_id) -> None:
        if len(self._active) > 1:
            return
        sys.stdout.write("\n")
        sys.stdout.flush()

    def file_started(self, worker, msg_id, fn, size) -> None:
        self._active.add(worker)
        print(f"▶ [{worker}] msg {msg_id} → {fn} ({human_mb(size):.1f} МБ)")

    def file_done(self, worker, msg_id, size, speed) -> None:
        self._active.discard(worker)
        print(f"✓ [{worker}] msg {msg_id} готово "
              f"({human_mb(size):.1f} МБ, {fmt_speed(speed)})")

    def file_dedup(self, msg_id, dup_fn) -> None:
        print(f"♻️  msg {msg_id:>9}  дубликат ({dup_fn})")

    def file_repair_oversize(self, msg_id) -> None:
        print(f"🔧 msg {msg_id:>9}  файл больше эталона — перекачиваю")

    def file_failed(self, msg_id, retries, fn) -> None:
        print(f"❌ msg {msg_id}: не докачан за {retries} попыток.")

    # ── Upload Pipeline ──
    def upload_progress(self, name, current, total, speed) -> None:
        width = self.PROGRESS_WIDTH
        pct = (current / total) if total else 0
        bar = "█" * int(width * pct) + "─" * (width - int(width * pct))
        disp = (name[:22] + "…") if len(name) > 23 else name
        sys.stdout.write(
            f"\r⬆ {disp:<23} │{bar}│ {pct*100:5.1f}% "
            f"{human_mb(current):7.1f}/{human_mb(total):7.1f} МБ  {fmt_speed(speed):>11}"
        )
        sys.stdout.flush()

    def upload_done(self, name, size, speed, message_id) -> None:
        sys.stdout.write("\n")
        sys.stdout.flush()
        print(f"✓ ⬆ {name} ({human_mb(size):.1f} МБ, {fmt_speed(speed)}) → msg {message_id}")

    def upload_skipped(self, name) -> None:
        print(f"· {name} — уже загружен, пропуск")

    def upload_failed(self, name, err) -> None:
        print(f"❌ ⬆ {name}: {err}")


# Глобальный текущий Reporter (по аналогии с глобалом LOG). По умолчанию — CLI; GUI
# подменяет его на свой в начале run_export. Все движковые функции пишут через REPORTER.
REPORTER: Reporter = CliReporter()


def list_volumes() -> list[str]:
    base = "/Volumes"
    if not os.path.isdir(base):
        return []
    return [os.path.join(base, n) for n in sorted(os.listdir(base))
            if os.path.isdir(os.path.join(base, n))]


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        return input(f"{prompt}{suffix}: ").strip() or default
    except EOFError:
        return default


def detect_fs(path: str) -> str | None:
    if not path.startswith("/Volumes/"):
        return None
    volume = "/" + os.path.join(*path.split("/")[1:3])
    try:
        out = subprocess.run(["diskutil", "info", volume],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if "File System Personality" in line or "Type (Bundle)" in line:
            return line.split(":", 1)[1].strip()
    return None


def validate_dest(path: str) -> str:
    # Движковая проверка: ошибки поднимаем как ExportError (а не sys.exit), чтобы GUI
    # показал диалог, а не убивал процесс. CLI-обёртка ловит ExportError → печать + выход.
    path = os.path.abspath(os.path.expanduser(path))
    if path.startswith("/Volumes/"):
        volume = "/" + os.path.join(*path.split("/")[1:3])
        if not os.path.ismount(volume) and not os.path.isdir(volume):
            vols = list_volumes()
            hint = "\n  ".join(vols) if vols else "(смонтированных томов нет)"
            raise ExportError(f"❌ Том не найден: {volume}\n   Подключён ли SSD?\n  {hint}")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        raise ExportError(f"❌ Не удалось создать папку {path}: {e}")
    if not os.access(path, os.W_OK):
        raise ExportError(f"❌ Папка недоступна для записи: {path}")
    return path


def setup_logger(dest: str) -> logging.Logger:
    logger = logging.getLogger("tg_export")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fh = logging.FileHandler(os.path.join(dest, LOG_NAME), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)
    return logger


def setup_error_logger(dest: str) -> logging.Logger:
    """Отдельный логгер для error_export.log (механизм 5)."""
    logger = logging.getLogger("tg_export_errors")
    logger.setLevel(logging.ERROR)
    logger.handlers.clear()
    fh = logging.FileHandler(os.path.join(dest, ERR_LOG_NAME), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)
    return logger


def volume_root_for(path: str) -> str:
    """Точка монтирования, которой принадлежит path.

    Для внешних дисков macOS это «/Volumes/<Имя>»; для путей на системном диске —
    «/» (всегда примонтирован). Идём вверх по дереву до первого ismount, чтобы корректно
    определять корень тома и для вложенных подпапок на SSD.
    """
    if path.startswith("/Volumes/"):
        return "/" + os.path.join(*path.split("/")[1:3])
    p = os.path.abspath(path)
    while p != "/" and not os.path.ismount(p):
        p = os.path.dirname(p)
    return p


def ensure_volume_mounted(dest: str) -> None:
    """Защита от потери тома (механизм 1): прерываемся, если SSD отвалился.

    Если целевой том больше не примонтирован (os.path.ismount == False), запись пошла бы
    в обычную папку на системном диске macOS — рискуя залить терабайты в корень. В этом
    случае немедленно завершаем процесс (sys.exit), ничего не записав мимо SSD.
    """
    root = volume_root_for(dest)
    if not os.path.ismount(root):
        msg = (f"Том не примонтирован: {root}. Внешний SSD отключён — "
               f"останавливаюсь, чтобы не писать на системный диск macOS.")
        log_error("-", f"MOUNT LOST {root}")
        raise ExportError(f"❌ {msg}")


# ── Конфигурация ──────────────────────────────────────────────────────────────
def interactive_setup() -> dict:
    print("─" * 70)
    print("  Первичная настройка экспорта Telegram → внешний SSD")
    print("─" * 70)
    api_id = ask("API_ID (my.telegram.org)")
    while not api_id.isdigit():
        print("  API_ID должен быть числом.")
        api_id = ask("API_ID (my.telegram.org)")
    api_hash = ask("API_HASH")
    phone = ask("Номер телефона (международный формат, напр. +79991234567)")
    channel = ask("Ссылка/ID целевого канала (@username, t.me/... или id)")

    vols = list_volumes()
    if vols:
        print("\nДоступные внешние тома:")
        for i, v in enumerate(vols, 1):
            print(f"  {i}) {v}   (свободно {human_gb(shutil.disk_usage(v).free):.1f} ГБ)")
        choice = ask("\nВыберите номер тома или введите путь вручную")
        if choice.isdigit() and 1 <= int(choice) <= len(vols):
            sub = ask("Подпапка внутри тома", "telegram_export")
            dest = os.path.join(vols[int(choice) - 1], sub)
        else:
            dest = choice
    else:
        dest = ask("Абсолютный путь к папке назначения на SSD")

    if not (api_id and api_hash and phone and channel and dest):
        sys.exit("❌ Не все поля заполнены.")
    return {"api_id": int(api_id), "api_hash": api_hash,
            "phone": phone, "channel": channel, "dest": dest}


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.chmod(CONFIG_PATH, 0o600)
    print(f"💾 Конфигурация сохранена: {CONFIG_PATH}")


def load_config() -> dict | None:
    if not os.path.exists(CONFIG_PATH):
        return None
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️  Повреждён config.json ({e}). Запустите с --reset.")
        return None
    if not {"api_id", "api_hash", "phone", "channel", "dest"}.issubset(cfg):
        print("⚠️  В config.json не хватает полей. Запустите с --reset.")
        return None
    return cfg


def reset_config() -> None:
    if os.path.exists(CONFIG_PATH):
        os.remove(CONFIG_PATH)
        print(f"🗑  Удалён {CONFIG_PATH}")
    session_file = os.path.join(DATA_DIR, f"{SESSION_NAME}.session")
    if os.path.exists(session_file):
        if ask("Удалить также .session (потребуется новый код)? [y/N]").lower() in ("y", "yes", "д", "да"):
            os.remove(session_file)
            print(f"🗑  Удалён {session_file}")


# ── Двухбазовая архитектура: кэш Telegram + бессмертное состояние скачанного ─────
# ИЗОЛЯЦИЯ. Раньше status/final_size жили в index.db вместе с кэшем Telegram — и откат
# транзакции (сетевой Broken pipe / FileReferenceExpired) «забывал» уже скачанные файлы,
# а --rescan (DELETE FROM messages) сносил весь прогресс. Теперь две физически разные БД:
#
#   index.db  (messages)          — КЭШ Telegram: индекс ID, метаданные медиа, dkey дедупа.
#                                   Расходный: --rescan его чистит, file_id «протухает».
#   download_state.db (local_files) — БЕССМЕРТНАЯ истина о скачанном: (msg_id, final_size,
#                                   status). Её НИКОГДА не трогает --rescan. Каждый готовый
#                                   файл фиксируется в неё мгновенным commit (synchronous=FULL).
#
# Базы подключены через ATTACH (state.*) к одному соединению — это даёт кросс-БД JOIN для
# дедупа без второго подключения, но при этом DELETE при --rescan бьёт ТОЛЬКО по messages.
def db_connect(dest):
    conn = sqlite3.connect(os.path.join(dest, DB_NAME))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")     # переживёт обрыв/жёсткую остановку
    conn.execute("PRAGMA synchronous=NORMAL")
    # Бессмертная БД скачанного — отдельный файл, подключаем как схему `state`.
    conn.execute("ATTACH DATABASE ? AS state", (os.path.join(dest, STATE_DB_NAME),))
    conn.execute("PRAGMA state.journal_mode=WAL")
    conn.execute("PRAGMA state.synchronous=FULL")   # каждый файл фиксируем НАМЕРТВО
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            msg_id     INTEGER PRIMARY KEY,
            file_id    TEXT,
            file_name  TEXT,
            file_size  INTEGER NOT NULL DEFAULT 0,
            uid        TEXT,
            kind       TEXT,
            duration   INTEGER NOT NULL DEFAULT 0,
            dkey       TEXT,
            status     TEXT,
            final_size INTEGER NOT NULL DEFAULT 0
        )
    """)
    # БЕССМЕРТНАЯ таблица скачанного. final_size — РЕАЛЬНЫЙ вес файла на диске после
    # пост-обработки (ffmpeg faststart сдвигает moov-атом). Именно по нему файл опознаётся
    # как COMPLETE при рестартах — независимо от серверного эталона Telegram.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS state.local_files (
            msg_id     INTEGER PRIMARY KEY,
            final_size INTEGER,
            status     TEXT
        )
    """)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    if "final_size" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN final_size INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dkey ON messages(dkey)")
    # Одноразовый бэкфилл из старой однобазовой схемы: переносим уже накопленный прогресс
    # (COMPLETE/final_size, лежавший в messages) в бессмертную БД, чтобы переход на две базы
    # не потерял ни одного скачанного файла. INSERT OR IGNORE — state никогда не перезаписываем.
    try:
        conn.execute(
            "INSERT OR IGNORE INTO state.local_files(msg_id, final_size, status) "
            "SELECT msg_id, final_size, status FROM messages WHERE status IS NOT NULL")
    except sqlite3.OperationalError:
        pass                                    # на новой index.db колонок может не быть
    conn.commit()
    return conn


def db_reset(conn):
    """--rescan: чистим ТОЛЬКО кэш Telegram. state.local_files (скачанное) НЕ трогаем."""
    conn.execute("DELETE FROM messages")
    conn.commit()


def db_load_meta(conn) -> dict:
    """Восстанавливает in-memory структуры сканера: {'analyzed': [...], 'media': {...}}."""
    analyzed, media = [], {}
    for r in conn.execute("SELECT msg_id, file_id, file_name, file_size, uid, "
                          "kind, duration FROM messages"):
        analyzed.append(r["msg_id"])
        if r["kind"]:
            media[str(r["msg_id"])] = {
                "file_id": r["file_id"], "name": r["file_name"], "size": r["file_size"],
                "uid": r["uid"], "kind": r["kind"], "duration": r["duration"]}
    return {"analyzed": analyzed, "media": media}


def db_save_meta(conn, analyzed, media) -> None:
    """Идемпотентно сохраняет индекс ID и метаданные медиа. Поле status не трогаем."""
    conn.executemany("INSERT OR IGNORE INTO messages(msg_id) VALUES(?)",
                     [(int(mid),) for mid in analyzed])
    rows = []
    for mid, rec in media.items():
        dk = dedup_key(rec["name"], rec["size"], rec["kind"], rec.get("duration", 0))
        rows.append((int(mid), rec["file_id"], rec["name"], rec["size"],
                     rec["uid"], rec["kind"], rec.get("duration", 0), dk))
    if rows:
        conn.executemany("""
            INSERT INTO messages(msg_id, file_id, file_name, file_size, uid,
                                 kind, duration, dkey)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(msg_id) DO UPDATE SET
                file_id=excluded.file_id, file_name=excluded.file_name,
                file_size=excluded.file_size, uid=excluded.uid,
                kind=excluded.kind, duration=excluded.duration, dkey=excluded.dkey
        """, rows)
    conn.commit()


def db_find_duplicate(conn, dkey, exclude_msg_id):
    """SQL-проверка дубликата: ЗАВЕРШЁННая (COMPLETE) строка с тем же dkey, но иным msg_id.

    dkey живёт в messages (кэш Telegram), а факт COMPLETE/final_size — в бессмертной
    state.local_files. JOIN по ATTACH сводит их: дубликатом считаем лишь то, что реально
    скачано (есть в state и помечено COMPLETE)."""
    return conn.execute(
        "SELECT m.msg_id AS msg_id, m.file_name AS file_name, "
        "       m.file_size AS file_size, s.final_size AS final_size "
        "FROM messages m JOIN state.local_files s ON m.msg_id=s.msg_id "
        "WHERE m.dkey=? AND s.status='COMPLETE' AND m.msg_id<>? LIMIT 1",
        (dkey, exclude_msg_id)).fetchone()


def master_size_ok(path, rec) -> bool:
    """Мастер-файл дедупликации цел, если его реальный вес совпадает с серверным эталоном
    (file_size) ИЛИ с пост-ремукс размером (final_size). Без учёта final_size ремукс-видео
    в роли «мастера» не прошло бы проверку и дубликат скачался бы заново."""
    if not os.path.exists(path):
        return False
    sz = os.path.getsize(path)
    final = (rec["final_size"] or 0) if "final_size" in rec.keys() else 0
    return sz == rec["file_size"] or (final and sz == final)


def db_set_status(conn, msg_id, status, dkey=None, final_size=None) -> None:
    """МГНОВЕННАЯ бессмертная фиксация состояния скачанного → state.local_files + commit.

    Статус (COMPLETE/PARTIAL) и final_size живут ТОЛЬКО в download_state.db, поэтому даже
    если в следующем цикле вылетит сетевая ошибка и откатится транзакция index.db — уже
    зафиксированный готовый файл не «забудется».

    final_size (если передан) — РЕАЛЬНЫЙ размер файла на диске после пост-обработки
    (ffmpeg faststart). При COMPLETE его пишем; при PARTIAL он не передаётся и НЕ перетирается.
    dkey (если передан) — кладём в messages (кэш Telegram) для дедупа, если он там пуст.
    """
    if final_size is not None:
        conn.execute(
            "INSERT INTO state.local_files(msg_id, final_size, status) VALUES(?,?,?) "
            "ON CONFLICT(msg_id) DO UPDATE SET "
            "final_size=excluded.final_size, status=excluded.status",
            (msg_id, final_size, status))
    else:
        conn.execute(
            "INSERT INTO state.local_files(msg_id, final_size, status) VALUES(?,NULL,?) "
            "ON CONFLICT(msg_id) DO UPDATE SET status=excluded.status",
            (msg_id, status))
    if dkey is not None:
        conn.execute("INSERT OR IGNORE INTO messages(msg_id) VALUES(?)", (msg_id,))
        conn.execute("UPDATE messages SET dkey=COALESCE(dkey, ?) WHERE msg_id=?",
                     (dkey, msg_id))
    conn.commit()                               # мгновенно — никаких пакетных сохранений


def db_update_file_id(conn, msg_id, file_id) -> None:
    """Атомарная перезапись протухшего file_id свежим (Lazy-Refetching Engine).

    file_reference внутри file_id живёт ~часы; после FILE_REFERENCE_EXPIRED свежий
    идентификатор берётся через get_messages и фиксируется здесь немедленным commit —
    иначе следующий рестарт прочитал бы из index.db всё ту же мёртвую ссылку."""
    if conn is None:
        return
    conn.execute("UPDATE messages SET file_id=? WHERE msg_id=?", (file_id, msg_id))
    conn.commit()


def db_complete_info(conn, msg_id):
    """(status, final_size) по msg_id из БЕССМЕРТНОЙ state.local_files; (None, 0) если нет.
    Источник правды о скачанном — только эта БД, не кэш Telegram."""
    if conn is None:
        return None, 0
    row = conn.execute(
        "SELECT status, final_size FROM state.local_files WHERE msg_id=?",
        (msg_id,)).fetchone()
    if not row:
        return None, 0
    return row["status"], (row["final_size"] or 0)


# ── Медиа ─────────────────────────────────────────────────────────────────────
def extract_media(message):
    """(media, original_name, file_size, file_unique_id, kind, duration) или None."""
    for kind in ("video", "document", "audio", "voice", "animation", "video_note"):
        media = getattr(message, kind, None)
        if media is not None:
            name = getattr(media, "file_name", None)
            if not name:
                ext = {"voice": "ogg", "video_note": "mp4", "audio": "mp3",
                       "video": "mp4", "animation": "mp4"}.get(kind, "bin")
                name = f"{kind}.{ext}"
            return (media, name, getattr(media, "file_size", 0) or 0,
                    getattr(media, "file_unique_id", None), kind,
                    int(getattr(media, "duration", 0) or 0))
    if message.photo is not None:
        # У Photo НЕТ file_name. Раньше всем фото присваивалось общее «photo.jpg»,
        # из-за чего два разных снимка с совпавшим размером схлопывались в один
        # dedup-ключ (photo.jpg|size) и второй молча отбраковывался дедупом —
        # «COMPLETE в логе, но файла на диске нет». Привязываем имя к message.id,
        # делая его уникальным и для пути (msg_<id>_photo_<id>.jpg), и для дедупа.
        return (message.photo, f"photo_{message.id}.jpg",
                getattr(message.photo, "file_size", 0) or 0,
                getattr(message.photo, "file_unique_id", None), "photo", 0)
    return None


def dedup_key(name: str, size: int, kind: str, duration: int = 0) -> str:
    """Ключ дедупликации: «имя + размер» (для видео ещё + длительность).

    Намеренно НЕ группируем только по имени: в канале полно файлов с одинаковыми
    именами (3.mp4, video.mp4, IMG_1234.mp4) — это разные ролики. Дубликатом
    считаем лишь полное совпадение имени И размера в байтах (И длительности у видео).

    GENERIC-GUARD (динамический): если имя признано шаблонным (статикой или Frequency
    Analyzer — см. is_generic_name/DYNAMIC_GENERIC), длительность из ключа УБИРАЕТСЯ, и
    дедуп опирается на базовую логику «Имя + Размер». Сервер при пережатии часто округляет
    duration, поэтому для массовых имён (video.mp4, report.pdf…) надёжнее размер.
    """
    base = os.path.basename(name or "").strip().lower()
    if is_generic_name(name):
        return f"{base}|{size}"
    if kind in VIDEO_KINDS and duration:
        return f"{base}|{size}|{duration}"
    return f"{base}|{size}"


def file_state(path: str, total: int, final_size: int = 0, status: str = None) -> str:
    """Состояние файла на диске. total — серверный эталон Telegram.

    Видео после ffmpeg faststart (сдвиг moov-атома) физически тяжелее эталона, поэтому:
      • совпал с total ИЛИ с записанным final_size (реальный пост-ремукс размер) → complete;
      • легаси-файлы (final_size ещё не записан), уже помеченные COMPLETE и лишь «раздутые»
        (sz > total) — доверяем статусу: это ремукс, а не повреждение → complete;
      • усечённые (sz < total) НИКОГДА не доверяем по статусу — это реально недокачанный
        файл → partial (требует докачки).
    """
    if not os.path.exists(path):
        return "missing"
    sz = os.path.getsize(path)
    if not total:
        return "complete" if sz > 0 else "missing"
    if sz == total or (final_size and sz == final_size):
        return "complete"
    if sz < total:
        return "partial"
    return "complete" if status == "COMPLETE" else "oversize"


def truncate_filename(name: str, prefix: str, max_len: int = MAX_FILENAME_LEN) -> str:
    """Санитайзер длины имени (механизм 2): не даём имени превысить max_len символов.

    Итоговое имя на диске = '<prefix><name>' (prefix = 'msg_<id>_'). Если оно длиннее
    max_len, режем СЕРЕДИНУ оригинального имени, сохраняя расширение — иначе ОС бросает
    OSError: File name too long. Результат детерминирован (одно имя при каждом запуске),
    поэтому идемпотентность/докачка/дедуп не ломаются.
    """
    if len(prefix) + len(name) <= max_len:
        return name
    root, ext = os.path.splitext(name)
    budget = max_len - len(prefix) - len(ext)
    if budget < 4:                       # места под имя почти нет — жертвуем расширением
        return (prefix + name)[:max_len][len(prefix):] or name[:1]
    keep = budget - 1                    # один символ под маркер обрезки «…»
    head, tail = (keep + 1) // 2, keep // 2
    middle = f"{root[:head]}…{root[-tail:]}" if tail else f"{root[:head]}…"
    return f"{middle}{ext}"


def dest_path_for(dest, msg_id, name) -> str:
    safe = os.path.basename(name).replace(os.sep, "_")
    prefix = f"msg_{msg_id}_"
    safe = truncate_filename(safe, prefix)
    return os.path.join(dest, f"{prefix}{safe}")


def parse_msg_filename(fn: str):
    """'msg_<id>_<имя>' → (msg_id, original_name) или None (чужой файл)."""
    if not fn.startswith("msg_"):
        return None
    rest = fn[4:]
    sep = rest.find("_")
    if sep <= 0:
        return None
    head = rest[:sep]
    if not head.isdigit():
        return None
    return int(head), rest[sep + 1:]


# Промежуточные/временные суффиксы, которые НИКОГДА нельзя принять за готовый файл
# (Уровень валидации A). .dl/.dlmap — частичные сегменты многопотока; .faststart.tmp —
# незавершённый ремукс ffmpeg; .temp/.tmp/.part — общие маркеры недокачки.
_RECOVERY_TEMP_SUFFIXES = (".dl", ".dlmap", ".temp", ".tmp", ".part")


def _is_recovery_temp(fn: str) -> bool:
    low = fn.lower()
    return ".faststart.tmp" in low or any(low.endswith(s) for s in _RECOVERY_TEMP_SUFFIXES)


def recover_state_from_disk(conn, dest, media) -> int:
    """ФУНКЦИЯ-СПАСАТЕЛЬ (Disk Source of Truth) с трёхуровневой валидацией. Старт.

    Сетевой сбой (Broken pipe) может откатить транзакцию index.db и «забыть» уже скачанные
    файлы, а Pyrogram при коллизии имени дописывает суффикс «-1» (msg_<id>_video-1.mp4) —
    точное совпадение имени его не узнаёт и гонит файл на бесконечную перекачку. Здесь
    файловая система объявляется истиной, а опознание идёт по ПРЕФИКСУ msg_<id>_, а не по
    точному имени (Fuzzy match):

      Уровень A (строгий пропуск): временные/промежуточные файлы (.dl/.dlmap/.temp/.tmp/
                 .part, незавершённый .faststart.tmp) НИКОГДА не считаются готовыми.
      Уровень C (зачистка коллизий): из всех файлов с одним префиксом msg_<id>_ берётся
                 САМЫЙ КРУПНЫЙ (полная версия), остальные дубликаты аппаратно удаляются
                 (os.remove). Победитель канонизируется к имени, которое строит воркер
                 (dest_path_for по серверному имени) — иначе exact-path проверка снова его
                 не найдёт и перекачает.
      Уровень B (санитарный контроль): реальный вес победителя (os.path.getsize) обязан
                 быть НЕ МЕНЬШЕ серверного file_size (с допуском FASTSTART_SHRINK_TOL на
                 ремукс-усадку). Меньше — битый сегмент: не COMPLETE, остаётся как partial
                 под канон-именем, и воркер докачает его с места.

    Готовый победитель заносится в бессмертную state.local_files (COMPLETE + реальный
    размер). Для уже-COMPLETE строк лишь чиним final_size, если он разошёлся с диском.
    """
    try:
        entries = os.listdir(dest)
    except OSError:
        return 0

    # Уровень 1 (Fuzzy match): группируем кандидаты по msg_id-ПРЕФИКСУ, не по точному имени.
    groups: dict[int, list[str]] = {}
    for fn in entries:
        if not fn.startswith("msg_"):
            continue
        if _is_recovery_temp(fn):                      # Уровень A: строгий пропуск временных
            continue
        if not os.path.isfile(os.path.join(dest, fn)):
            continue
        parsed = parse_msg_filename(fn)
        if not parsed:
            continue
        groups.setdefault(parsed[0], []).append(fn)

    added = 0
    for mid, names in groups.items():
        sized: list[tuple[int, str]] = []
        for fn in names:
            try:
                sz = os.path.getsize(os.path.join(dest, fn))
            except OSError:
                continue
            if sz > 0:
                sized.append((sz, fn))
        if not sized:
            continue
        sized.sort(reverse=True)                       # самый крупный — первым
        winner_size, winner_fn = sized[0]
        rec = media.get(str(mid))

        # Уровень C: зачистка коллизий — все дубликаты, кроме победителя, удаляем с диска.
        for _, loser_fn in sized[1:]:
            try:
                os.remove(os.path.join(dest, loser_fn))
                log(f"RECOVER collision rm msg {mid} {loser_fn}")
            except OSError:
                pass

        # Канонизация имени победителя к тому, что строит воркер (dest_path_for по серверному
        # имени) — сдвигаем суффикс «-1»/обрезку к канону, чтобы exact-path проверка нашла файл.
        winner_path = os.path.join(dest, winner_fn)
        name = parse_msg_filename(winner_fn)[1]
        if rec and rec.get("name"):
            canon = dest_path_for(dest, mid, rec["name"])
            # Сравниваем в NFC: macOS HFS+/APFS хранят имена в разложенной форме (NFD), а
            # rec["name"] из Telegram — в NFC, поэтому побайтно строки расходятся даже у
            # физически одного и того же файла. Без нормализации os.replace срабатывал
            # «сам в себя» каждый запуск (холостое переименование + спам в лог).
            if (unicodedata.normalize("NFC", os.path.abspath(canon))
                    != unicodedata.normalize("NFC", os.path.abspath(winner_path))):
                try:
                    os.replace(winner_path, canon)
                    winner_path = canon
                    log(f"RECOVER canon msg {mid} {winner_fn} → {os.path.basename(canon)}")
                except OSError:
                    pass

        st = conn.execute("SELECT status, final_size FROM state.local_files WHERE msg_id=?",
                          (mid,)).fetchone()
        if st and st["status"] == "COMPLETE":
            # Уже COMPLETE в бессмертной БД — доверяем ей. Чиним final_size, если он
            # 0/устарел, иначе faststart-видео (чуть меньше серверного) file_state ложно
            # посчитает PARTIAL и перекачает.
            if (st["final_size"] or 0) != winner_size:
                conn.execute("UPDATE state.local_files SET final_size=? WHERE msg_id=?",
                             (winner_size, mid))
                added += 1
            continue

        # Уровень B (санитарный контроль): победитель меньше эталона БОЛЬШЕ чем на допуск
        # faststart — это битый/недокачанный сегмент. Не COMPLETE: оставляем под канон-именем,
        # воркер докачает по тому же пути (resume по размеру на диске).
        if rec and rec.get("size") and winner_size < rec["size"] * (1 - FASTSTART_SHRINK_TOL):
            continue

        # dkey для дедупа кладём в messages (кэш Telegram). Имя/размер для ключа берём из
        # меты (имя на диске могло быть обрезано truncate_filename); размер — серверный
        # эталон, если известен, чтобы ключ совпал с тем, что строит воркер по item.size.
        duration = rec.get("duration", 0) if rec else 0
        kind = rec.get("kind", "") if rec else ""
        dkey_name = rec["name"] if rec and rec.get("name") else name
        key_size = rec["size"] if rec and rec.get("size") else winner_size
        key = dedup_key(dkey_name, key_size, kind, duration)
        conn.execute("INSERT OR IGNORE INTO messages(msg_id) VALUES(?)", (mid,))
        conn.execute(
            "UPDATE messages SET dkey=COALESCE(dkey, ?), "
            "file_name=COALESCE(file_name, ?), "
            "file_size=CASE WHEN file_size=0 THEN ? ELSE file_size END "
            "WHERE msg_id=?", (key, dkey_name, winner_size, mid))
        # Бессмертная фиксация (Уровень C → COMPLETE): реальный вес → final_size.
        conn.execute(
            "INSERT INTO state.local_files(msg_id, final_size, status) "
            "VALUES(?,?, 'COMPLETE') "
            "ON CONFLICT(msg_id) DO UPDATE SET "
            "final_size=excluded.final_size, status='COMPLETE'",
            (mid, winner_size))
        added += 1
    conn.commit()
    return added


def verify_export(items, dest, conn=None) -> dict:
    """Аудит: сверяет план с реальными файлами на диске.

    Категории по каждому файлу: COMPLETE (размер совпал) / PARTIAL (недокачан) /
    MISSING (нет на диске) / OVERSIZE (локальный больше серверного → повреждён).
    Пишет verify_report.txt в dest; сеть и файлы на диске не трогает. Если передан conn,
    синхронизирует поле status в index.db по результатам сверки (SQL-обновление).
    """
    buckets = {"COMPLETE": [], "PARTIAL": [], "MISSING": [], "OVERSIZE": []}
    state_map = {"complete": "COMPLETE", "partial": "PARTIAL",
                 "missing": "MISSING", "oversize": "OVERSIZE"}
    for it in items:
        path = dest_path_for(dest, it.msg_id, it.name)
        on_disk = os.path.getsize(path) if os.path.exists(path) else 0
        db_status, db_final = db_complete_info(conn, it.msg_id)
        cat = state_map[file_state(path, it.size, db_final, db_status)]
        buckets[cat].append((it, on_disk))
        if conn is not None:
            dkey = dedup_key(it.name, it.size, it.kind, it.duration)
            if cat == "COMPLETE":
                # Записываем реальный вес на диске в final_size — в т.ч. бэкфилл для
                # ремукс-видео, опознанных по статусу (legacy), чтобы впредь матчить точно.
                db_set_status(conn, it.msg_id, "COMPLETE", dkey, final_size=on_disk)
            elif cat == "PARTIAL":
                db_set_status(conn, it.msg_id, "PARTIAL", dkey)
            else:
                # MISSING/OVERSIZE: файла нет или он повреждён — убираем запись из бессмертной
                # БД, чтобы он не числился COMPLETE (иначе дедуп сослался бы на «мастер»,
                # которого нет). Отсутствие записи → файл снова попадёт в план на загрузку.
                conn.execute("DELETE FROM state.local_files WHERE msg_id=?", (it.msg_id,))
                conn.commit()

    report_path = os.path.join(dest, "verify_report.txt")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(f"# Аудит экспорта (--verify) — {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        fh.write(f"# Всего файлов в плане: {len(items)}\n")
        fh.write(f"# COMPLETE={len(buckets['COMPLETE'])} PARTIAL={len(buckets['PARTIAL'])} "
                 f"MISSING={len(buckets['MISSING'])} OVERSIZE={len(buckets['OVERSIZE'])}\n\n")
        for cat in ("MISSING", "PARTIAL", "OVERSIZE", "COMPLETE"):
            rows = buckets[cat]
            fh.write(f"## [{cat}] — {len(rows)}\n")
            for it, on_disk in rows:
                fn = os.path.basename(dest_path_for(dest, it.msg_id, it.name))
                fh.write(f"[{cat}] msg {it.msg_id}  {fn}  "
                         f"disk={human_mb(on_disk):.2f}МБ / ожид={human_mb(it.size):.2f}МБ\n")
            fh.write("\n")

    return {k: len(v) for k, v in buckets.items()} | {"report": report_path}


def progress_bar(worker, msg_id, current, total, speed_bps, width=24):
    # Шим к текущему Reporter: рендеринг живёт в CliReporter.file_progress (CLI) или
    # уходит в очередь к Qt (GUI). worker — ID потока (progress_args): по нему дашборд
    # находит свою полосу прогресса в Multi-Worker View и обновляет её индивидуально.
    REPORTER.file_progress(worker, msg_id, current, total, speed_bps)


@dataclass
class Item:
    msg_id: int
    file_id: str
    name: str
    size: int
    uid: str | None
    kind: str
    duration: int = 0


# ── Двухпроходная резолюция качества (Two-Pass Quality Resolution) ─────────────
# Обезличенные имена: Telegram-дефолты и наши fallback-имена (extract_media), которые
# массово повторяются у РАЗНЫХ роликов («video.mp4» встречается в 87 группах). Плюс
# чисто числовые имена (3.mp4, 6.mp4) и голые родовые слова (photo.jpg, clip.mp4).
_GENERIC_FALLBACK_NAMES = {
    "video.mp4", "photo.jpg", "audio.mp3", "voice.ogg",
    "video_note.mp4", "animation.mp4", "document.bin",
}
_GENERIC_NAME_RE = re.compile(r"^(?:video|photo|image|gif|clip|movie|\d+)\.[a-z0-9]+$")


def build_dynamic_generic_names(media: dict,
                                threshold: int = GENERIC_FREQ_THRESHOLD) -> set[str]:
    """Frequency Analyzer (Динамический Guard): эвристически находит шаблонные имена.

    Вместо ручного словаря/RegEx анализируем РЕАЛЬНУЮ базу текущего канала (media): если
    ЛЮБОЕ имя файла (хоть report.pdf, хоть screencast.mov) встречается > threshold раз и
    при этом под ним лежат РАЗНЫЕ размеры — это явно переиспользуемое («родовое») имя
    разных файлов, а не один и тот же объект. Такое имя помечаем generic для канала.

    Возвращает множество base-имён (lower-case). Дальше is_generic_name/dedup_key/
    media_fingerprint автоматически роняют эти файлы на защиту «Имя + Размер», без правки
    словарей вручную. Имена с одним-единственным размером НЕ трогаем — это настоящие
    побайтные дубликаты, их корректно схлопывает обычный дедуп.
    """
    counts: dict[str, int] = {}
    sizes: dict[str, set[int]] = {}
    for rec in media.values():
        base = os.path.basename(rec.get("name") or "").strip().lower()
        if not base:
            continue
        counts[base] = counts.get(base, 0) + 1
        sizes.setdefault(base, set()).add(int(rec.get("size", 0) or 0))
    return {b for b, n in counts.items() if n > threshold and len(sizes[b]) > 1}


def is_generic_name(name: str, dynamic: "set[str] | None" = None) -> bool:
    """True, если имя не несёт смысла для различения файлов (повторяется у разных роликов).

    Источников два: статический словарь/RegEx (Telegram-дефолты, числовые/родовые имена)
    и динамический набор от Frequency Analyzer. Если dynamic не передан — берём канальный
    DYNAMIC_GENERIC, заполняемый на pre-flight (см. build_dynamic_generic_names).
    """
    base = os.path.basename(name or "").strip().lower()
    if dynamic is None:
        dynamic = DYNAMIC_GENERIC
    if base in dynamic:
        return True
    return base in _GENERIC_FALLBACK_NAMES or bool(_GENERIC_NAME_RE.match(base))


def media_fingerprint(name: str, kind: str, duration: int = 0, size: int = 0) -> str:
    """«Цифровой отпечаток» медиа для резолюции качества.

    В ОТЛИЧИЕ от dedup_key размер обычно НЕ входит в отпечаток: задача — собрать под одним
    ключом разные по качеству (а значит, по байтам) версии одного и того же ролика/файла,
    чтобы потом из группы выбрать самую большую.
      • видео:           (kind, file_name, duration)
      • фото/документы:  (kind, file_name)

    ЗАЩИТА ОТ ЛОЖНОГО СЛИЯНИЯ (generic-name guard): обезличенные имена (video.mp4, photo.jpg,
    3.mp4 …) совпадают у РАЗНЫХ роликов — без защиты резолюция выкинула бы их как «худшие
    версии» (реальная потеря данных: ~1472 файла / 112 ГБ на канале пользователя). Для таких
    имён добавляем РАЗМЕР в отпечаток: тогда в группу попадут лишь побайтно-одинаковые дубли,
    а различные ролики останутся каждый сам по себе и будут скачаны.
    """
    base = os.path.basename(name or "").strip().lower()
    if is_generic_name(name):
        return f"{kind}|{base}|{duration}|{size}"
    if kind in VIDEO_KINDS:
        return f"{kind}|{base}|{duration}"
    return f"{kind}|{base}"


def write_quality_report(dest, losers) -> str:
    """quality_report.txt: связывает каждую пропущенную версию с её мастером.

    losers — список (item_хуже, item_мастер). Файл перезаписывается при каждом запуске,
    чтобы отражать актуальное решение по текущему плану.
    """
    report_path = os.path.join(dest, "quality_report.txt")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(f"# Резолюция качества (Two-Pass) — {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        fh.write(f"# Пропущено худших версий: {len(losers)}\n\n")
        for loser, winner in sorted(losers, key=lambda p: p[0].msg_id):
            fh.write(
                f"Файл msg_{loser.msg_id} пропущен. "
                f"Выбран дубликат лучшего качества: msg_{winner.msg_id} "
                f"(Размер: {human_mb(winner.size):.2f} MB)\n"
            )
    return report_path


def resolve_quality(items, dest, write_report=True, force_include=frozenset()):
    """Двухпроходная резолюция качества.

    Pass 1 (Quality Clash): группируем план по media_fingerprint.
    Pass 2: в каждой группе побеждает файл с МАКСИМАЛЬНЫМ file_size («Мастер-файл»)
    и остаётся в очереди; остальные версии исключаются из плана ДО любого обращения
    к диску — так мы не тратим TBW SSD на скачивание заведомо худших дубликатов.

    Возвращает (masters, losers): masters — отфильтрованный план (по одному лучшему
    на отпечаток), losers — список пар (item_хуже, item_мастер) для отчёта.

    FUZZY DURATION: длительность видео перед группировкой канонизируется — близкие значения
    (|Δ| ≤ 1 c) сводятся к одному представителю. Серверы Telegram при пережатии округляют
    метаданные, и жёсткое сравнение duration упускало бы такие версии (см. canon_duration).
    """
    # Канонизация длительности: внутри одного (kind|имя) близкие duration → общий представитель.
    # Идём в порядке плана (хронологический) — выбор представителей детерминирован.
    canon_reps: dict[str, list[int]] = {}

    def canon_duration(it: Item) -> int:
        if it.kind not in VIDEO_KINDS or not it.duration:
            return it.duration
        base = os.path.basename(it.name or "").strip().lower()
        reps = canon_reps.setdefault(f"{it.kind}|{base}", [])
        for r in reps:
            if abs(it.duration - r) <= 1:
                return r
        reps.append(it.duration)
        return it.duration

    groups: dict[str, list[Item]] = {}
    for it in items:
        fp = media_fingerprint(it.name, it.kind, canon_duration(it), it.size)
        groups.setdefault(fp, []).append(it)

    masters: list[Item] = []
    losers: list[tuple[Item, Item]] = []
    for group in groups.values():
        if len(group) == 1:
            masters.append(group[0])
            continue
        # Победитель — максимум по размеру; при равных размерах берём меньший msg_id,
        # чтобы выбор был детерминированным и не зависел от порядка обхода истории.
        winner = max(group, key=lambda it: (it.size, -it.msg_id))
        masters.append(winner)
        for it in group:
            if it.msg_id == winner.msg_id:
                continue
            # ЗАЩИТА УНИКАЛЬНЫХ (force_include): пользователь на экране ревью вернул этот
            # файл в загрузку — НЕ выкидываем его как «худшую версию», оставляем мастером.
            if it.msg_id in force_include:
                masters.append(it)
            else:
                losers.append((it, winner))

    masters.sort(key=lambda it: it.msg_id)        # хронологический порядок, как в плане
    if write_report and losers:
        write_quality_report(dest, losers)
    return masters, losers


# ── Сбор ID ───────────────────────────────────────────────────────────────────
def _record_media(message, media: dict, analyzed: set) -> None:
    """Извлекает метаданные медиа из объекта Message прямо во время скана истории.

    get_chat_history (messages.GetHistory) уже отдаёт полные сообщения, поэтому
    повторный channels.GetMessages по id не нужен — именно он ловил 30-сек FloodWait.
    """
    mid = message.id
    if mid in analyzed:
        return
    if not getattr(message, "empty", False):
        info = extract_media(message)
        if info is not None:
            m, name, size, uid, kind, duration = info
            media[str(mid)] = {"file_id": m.file_id, "name": name, "size": size,
                               "uid": uid, "kind": kind, "duration": duration}
    analyzed.add(mid)                                 # помечаем все (вкл. без медиа)


async def collect_message_ids(app, chat_id, media: dict, analyzed: set) -> list[int]:
    ids, offset_id = [], 0
    while True:
        if SHUTDOWN is not None and SHUTDOWN.is_set():
            break
        got = 0
        try:
            async for m in app.get_chat_history(chat_id, offset_id=offset_id):
                ids.append(m.id)
                offset_id = m.id
                got += 1
                _record_media(m, media, analyzed)
                if SHUTDOWN is not None and SHUTDOWN.is_set():
                    break
                if got % 500 == 0:
                    REPORTER.status_inline(f"\r   собрано ID: {len(ids)} "
                                           f"(медиа: {len(media)})…")
        except FloodWait as e:
            w = int(getattr(e, "value", 0) or 0)
            REPORTER.status(f"\n⏳ FloodWait при сканировании: ждём {w} c…")
            await asyncio.sleep(w + 1)
            continue
        if got == 0:
            break
    REPORTER.status_clear()
    return sorted(set(ids))


async def collect_new_ids(app, chat_id, since_id: int,
                          media: dict, analyzed: set) -> list[int]:
    new, offset_id, done = [], 0, False
    while not done:
        if SHUTDOWN is not None and SHUTDOWN.is_set():
            break
        got = 0
        try:
            async for m in app.get_chat_history(chat_id, offset_id=offset_id):
                offset_id = m.id
                got += 1
                if m.id <= since_id:
                    done = True
                    break
                new.append(m.id)
                _record_media(m, media, analyzed)
                if SHUTDOWN is not None and SHUTDOWN.is_set():
                    done = True
                    break
        except FloodWait as e:
            w = int(getattr(e, "value", 0) or 0)
            REPORTER.status(f"\n⏳ FloodWait при дочитывании: ждём {w} c…")
            await asyncio.sleep(w + 1)
            continue
        if got == 0:
            break
    return new


async def get_messages_retry(app, chat_id, ids):
    while True:
        try:
            res = await app.get_messages(chat_id, ids)
            return res if isinstance(res, list) else [res]
        except FloodWait as e:
            w = int(getattr(e, "value", 0) or 0)
            REPORTER.status(f"\n⏳ FloodWait при чтении сообщений: ждём {w} c…")
            await asyncio.sleep(w + 1)


async def refresh_file_id(app, chat_id, msg_id, conn=None, worker=None):
    """Lazy-Refetching Engine: горячая регенерация просроченного токена.

    А. get_messages → свежее сообщение; Б. из него — новый валидный file_id (со свежим
    file_reference); В. атомарный UPDATE в index.db (db_update_file_id), чтобы рестарт
    не упёрся в ту же мёртвую ссылку; Г. вызывающий код повторяет загрузку с новым id.
    Поток при этом НЕ падает и НЕ пропускает элемент очереди."""
    try:
        msgs = await get_messages_retry(app, chat_id, [msg_id])
    except Exception:                            # noqa: BLE001
        return None
    m = msgs[0] if msgs else None
    if m is None or getattr(m, "empty", False):
        return None
    info = extract_media(m)
    new_fid = info[0].file_id if info else None
    if new_fid:
        db_update_file_id(conn, msg_id, new_fid)
        tag = f"[{worker}] " if worker else ""
        REPORTER.status(f"{tag}msg {msg_id} Ссылка устарела. Токен безопасности успешно "
                        f"регенерирован на лету, перезапускаю поток загрузки...")
        log(f"REFETCH file_id msg {msg_id} (FILE_REFERENCE_EXPIRED → index.db обновлён)")
    return new_fid


async def build_plan(app, chat_id, msg_ids, allow_kinds, skip_kinds, dest,
                     media: dict, analyzed: set, conn) -> list[Item]:
    """
    Формирует список Item для предпросмотра/загрузки.

    Метаданные медиа извлекаются прямо при сканировании истории (см. _record_media),
    поэтому здесь обычно нечего догружать. channels.GetMessages вызывается лишь как
    запасной путь — для id из старого кэша, у которых метаданных ещё нет. Закэшированный
    file_id может «протухнуть» — при скачивании ссылка обновляется (refresh_file_id).
    """
    to_fetch = [mid for mid in msg_ids if mid not in analyzed]
    if to_fetch:
        # Старый кэш id без метаданных: добираем БЫСТРЫМ проходом по истории
        # (get_chat_history → messages.GetHistory), а НЕ через channels.GetMessages,
        # который ловит 30-секундные FloodWait. Один проход наполняет media/analyzed.
        REPORTER.status(f"   В кэше {len(to_fetch)} сообщений без метаданных — "
                        f"быстро дочитываю историю канала…")
        await collect_message_ids(app, chat_id, media, analyzed)
        db_save_meta(conn, sorted(analyzed), media)
        REPORTER.status(f"   метаданные собраны: медиа {len(media)}.")
    else:
        REPORTER.status("   метаданные готовы (взяты при сканировании истории).")

    items: list[Item] = []
    for mid in msg_ids:
        rec = media.get(str(mid))
        if not rec:
            continue
        kind = rec["kind"]
        if allow_kinds and kind not in allow_kinds:
            continue
        if skip_kinds and kind in skip_kinds:
            continue
        items.append(Item(mid, rec["file_id"], rec["name"], rec["size"],
                          rec["uid"], kind, rec.get("duration", 0)))
    return items


def summarize_plan(items, dest, conn, use_dedup, force_include=frozenset()):
    total_files = len(items)
    total_size = sum(it.size for it in items)
    need_files = need_size = have_files = dup_files = 0
    seen: set[str] = set()
    for it in items:
        path = dest_path_for(dest, it.msg_id, it.name)
        db_status, db_final = db_complete_info(conn, it.msg_id)
        if file_state(path, it.size, db_final, db_status) == "complete":
            have_files += 1
            continue
        # force_include: файл, возвращённый пользователем на экране ревью, дедупом не
        # схлопывается — считаем его к загрузке как уникальный.
        if use_dedup and it.msg_id not in force_include:
            key = dedup_key(it.name, it.size, it.kind, it.duration)
            rec = db_find_duplicate(conn, key, it.msg_id)
            dup = key in seen
            if rec:
                rp = dest_path_for(dest, rec["msg_id"], rec["file_name"])
                if os.path.exists(rp) and master_size_ok(rp, rec):
                    dup = True
            if dup:
                dup_files += 1
                continue
            seen.add(key)
        need_files += 1
        need_size += it.size
    return dict(total_files=total_files, total_size=total_size, need_files=need_files,
                need_size=need_size, have_files=have_files, dup_files=dup_files)


# ── Загрузка ──────────────────────────────────────────────────────────────────
async def download_one(app, file_id, msg_id, dest_path, total_size,
                       use_fast: bool, connections: int,
                       worker=None, conn=None) -> None:
    # worker — ID потока (progress_args): каждое событие прогресса несёт его с собой,
    # чтобы GUI обновлял индивидуальную полосу Multi-Worker View, а не одну общую.
    # conn — index.db: при FILE_REFERENCE_EXPIRED свежий file_id фиксируется атомарным
    # UPDATE (см. refresh_file_id) и загрузка перезапускается, не роняя воркер.
    # ── 1) Многопоточный путь (FastTelethon) для крупных файлов ──
    if use_fast and total_size and total_size >= FAST_MIN_SIZE:
        holder = {"base": None, "t0": time.time()}

        def cb(cur):
            if holder["base"] is None:
                holder["base"], holder["t0"] = cur, time.time()
            dt = time.time() - holder["t0"]
            sp = (cur - holder["base"]) / dt if dt > 0 else 0
            progress_bar(worker, msg_id, cur, total_size, sp)

        fast_try = 0
        while fast_try < 2:
            try:
                await fast_download_file(app, file_id, dest_path, total_size,
                                         progress=cb, connections=connections,
                                         pool_size=POOL_SIZE)
                REPORTER.progress_done(worker, msg_id)
                return
            except FloodWait as e:
                # Главный источник каскада: при многопотоке воркеры параллельно проходят
                # auth.ExportAuthorization на удалённых DC и ловят жёсткий 420-флуд. НЕ
                # роняем поток и НЕ плодим трейсбэки — централизованно ждём в asyncio-цикле
                # (UI-сигналы продолжают доставляться) и повторяем эту же попытку.
                wait_time = int(getattr(e, "value", 0) or 0)
                REPORTER.status(
                    f"\n[⚠️] Превышен лимит запросов Telegram (auth.ExportAuthorization). "
                    f"Запуск принудительной паузы на {wait_time} сек...")
                log(f"FLOODWAIT msg {msg_id} (fast/ExportAuthorization) — пауза {wait_time}c")
                await asyncio.sleep(wait_time + 2)
                continue                             # повтор той же попытки, без расхода fast_try
            except FileReferenceExpired:
                new_fid = await refresh_file_id(app, CHAT_ID, msg_id, conn, worker)
                if new_fid and fast_try == 0:
                    file_id = new_fid                # обновили ссылку — пробуем ещё раз
                    fast_try += 1                    # тратим попытку (FloodWait — нет)
                    continue
                if new_fid:
                    file_id = new_fid                # для последовательного фолбэка
                break
            except FastUnavailable as e:
                REPORTER.progress_done(worker, msg_id)
                REPORTER.status(f"   ↩︎ msg {msg_id}: многопоток недоступен ({e}) — обычный режим")
                log(f"FALLBACK msg {msg_id} {e}")
                break
            except NETWORK_ERRORS as e:
                # Обрыв TCP в многопотоке — уходим в последовательный путь, он умеет
                # ждать и возобновлять докачку с места (механизм 4).
                REPORTER.progress_done(worker, msg_id)
                log_error(msg_id, f"network in fast path ({type(e).__name__}: {e}) "
                                  f"— переключаюсь на последовательный режим")
                break

    # ── 2) Последовательный путь (надёжный фолбэк) ──
    # ref_refreshes — счётчик обновлений протухшей ссылки; net_retries — счётчик
    # возобновлений после обрыва TCP (механизм 4): ждём NET_RETRY_WAIT c и пробуем
    # снова, до NET_MAX_RETRIES раз, докачивая файл с места (resume по размеру на диске).
    ref_refreshes = 0
    net_retries = 0
    while True:
        downloaded = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
        aligned = (downloaded // CHUNK_SIZE) * CHUNK_SIZE
        if aligned != downloaded:
            with open(dest_path, "r+b") as fh:
                fh.truncate(aligned)
            downloaded = aligned
        start_chunk = downloaded // CHUNK_SIZE
        try:
            mode = "r+b" if downloaded else "wb"
            t0, base = time.time(), downloaded
            with open(dest_path, mode) as fh:
                fh.seek(downloaded)
                current = downloaded
                progress_bar(worker, msg_id, current, total_size, 0)
                async for chunk in app.stream_media(file_id, offset=start_chunk):
                    fh.write(chunk)
                    current += len(chunk)
                    dt = time.time() - t0
                    sp = (current - base) / dt if dt > 0 else 0
                    progress_bar(worker, msg_id, current, total_size, sp)
                # Durability: сбрасываем на пластину диска до выхода (паритет с многопотоком,
                # где fsync уже есть). На внешнем USB-SSD это защищает только что дописанный
                # хвост файла, если том выдернут сразу после загрузки.
                fh.flush()
                os.fsync(fh.fileno())
            # ТИХИЙ FILE_REFERENCE_EXPIRED. В Pyrogram 2.0.106 app.stream_media НЕ пробрасывает
            # FileReferenceExpired наружу: get_file ловит её внутри, пишет traceback в лог и
            # генератор просто завершается, отдав 0 байт. Поэтому `except FileReferenceExpired`
            # ниже на последовательном пути МЁРТВ, а кэшированный (протухший) file_id молча даёт
            # нулевую докачку. Признак — поток не отдал НИ ОДНОГО нового байта, хотя файл не
            # дочитан: почти наверняка ссылка устарела. Обновляем её (get_messages → свежий
            # file_id) и повторяем — ровно как делает быстрый путь, где исключение всё же летит.
            if total_size and current < total_size and current == downloaded:
                ref_refreshes += 1
                if ref_refreshes > NET_MAX_RETRIES:
                    REPORTER.status(f"\n⚠️  msg {msg_id}: ссылка на файл не обновляется "
                                    f"(stream отдаёт 0 байт)")
                    log_error(msg_id, "stream_media отдаёт 0 байт — не удалось обновить ссылку")
                    return
                new_fid = await refresh_file_id(app, CHAT_ID, msg_id, conn, worker)
                if new_fid:
                    file_id = new_fid
                    log(f"REFRESH ref msg {msg_id} (тихий FRE на stream_media, "
                        f"попытка {ref_refreshes})")
                else:
                    await asyncio.sleep(2)
                continue
            REPORTER.progress_done(worker, msg_id)
            return
        except FloodWait as e:
            wait_time = int(getattr(e, "value", 0) or 0)
            REPORTER.status(
                f"\n[⚠️] Превышен лимит запросов Telegram (auth.ExportAuthorization). "
                f"Запуск принудительной паузы на {wait_time} сек...")
            log(f"FLOODWAIT msg {msg_id} (sequential) — пауза {wait_time}c")
            await asyncio.sleep(wait_time + 2)
        except FileReferenceExpired:
            ref_refreshes += 1
            if ref_refreshes > 3:
                REPORTER.status(f"\n⚠️  msg {msg_id}: не удалось обновить ссылку на файл")
                return
            new_fid = await refresh_file_id(app, CHAT_ID, msg_id, conn, worker)
            if new_fid:
                file_id = new_fid
            else:
                await asyncio.sleep(2)
        except NETWORK_ERRORS as e:
            # Обрыв TCP-соединения (механизм 4): ждём NET_RETRY_WAIT c и возобновляем
            # докачку с места (цикл заново вычислит downloaded по размеру файла).
            net_retries += 1
            if net_retries > NET_MAX_RETRIES:
                log_error(msg_id, f"обрыв сети — сдаюсь после {NET_MAX_RETRIES} "
                                  f"попыток возобновления ({type(e).__name__}: {e})")
                return
            log_error(msg_id, f"обрыв сети ({type(e).__name__}: {e}) — "
                              f"возобновление {net_retries}/{NET_MAX_RETRIES} "
                              f"через {NET_RETRY_WAIT} c")
            if live:
                REPORTER.status(f"\n🌐 [msg {msg_id}] обрыв сети — повтор "
                                f"{net_retries}/{NET_MAX_RETRIES} через {NET_RETRY_WAIT} c…")
            await asyncio.sleep(NET_RETRY_WAIT)


def cleanup_fast_temp(dest_path: str) -> None:
    """Удаляет временные файлы многопотока (<dest>.dl/.dlmap), если они осели рядом.

    Они остаются «сиротами», когда многопоточный путь успел скачать часть файла, но затем
    упал в последовательный фолбэк (обрыв TCP/CDN): фолбэк пишет уже в финальное имя, а
    .dl/.dlmap так и висят, занимая место. После успешного завершения файла они не нужны.
    """
    for ext in (".dl", ".dlmap"):
        p = dest_path + ext
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


async def download_verified(app, file_id, msg_id, dest_path, total_size,
                            use_fast, connections, worker=None, conn=None) -> bool:
    for attempt in range(1, MAX_RETRIES + 1):
        await download_one(app, file_id, msg_id, dest_path, total_size,
                           use_fast, connections, worker=worker, conn=conn)
        if not total_size:
            cleanup_fast_temp(dest_path)
            return True
        final = os.path.getsize(dest_path)
        if final == total_size:
            cleanup_fast_temp(dest_path)
            return True
        if final > total_size:
            with open(dest_path, "r+b") as fh:
                fh.truncate(total_size)
            if os.path.getsize(dest_path) == total_size:
                cleanup_fast_temp(dest_path)
                return True
        REPORTER.status(f"\n⚠️  msg {msg_id}: {human_mb(final):.2f}/"
                        f"{human_mb(total_size):.2f} МБ — докачка {attempt}/{MAX_RETRIES}")
    return False


async def faststart_postprocess(path: str, msg_id: int) -> None:
    """Apple Quick Look fix: перепаковка видео под предпросмотр macOS (faststart).

    Имя и расширение файла НЕ меняем — сохраняем ровно то, что отдал Telegram (`file_name`).
    Многие видео приходят с moov-атомом в конце контейнера, и AVFoundation (предпросмотр по
    пробелу) такой файл не превьюит. Команда `ffmpeg -i in -c copy -movflags +faststart out`
    БЕЗ перекодирования (только ремукс) переносит moov в начало — Quick Look снова работает;
    результат os.replace'ом кладётся обратно под тем же путём/именем. Запускаем неблокирующим
    процессом через asyncio: остальные воркеры качают параллельно, event loop не стопорится.

    Отказоустойчивость: если ffmpeg не установлен (FileNotFoundError) — единожды пишем
    logging.warning и продолжаем экспорт без падения; повторно не дёргаем (FFMPEG_STATE).
    """
    if not FASTSTART or FFMPEG_STATE["missing"]:
        return
    tmp = f"{path}.faststart.tmp.mp4"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-y", "-i", path,
            "-c", "copy", "-movflags", "+faststart", tmp,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)                 # temp.mp4 заменяет оригинал (атомарно)
            log(f"FASTSTART ok msg {msg_id}")
        else:
            err = (stderr or b"").decode("utf-8", "ignore").strip().splitlines()
            log(f"FASTSTART skip msg {msg_id} rc={proc.returncode} "
                f"{err[-1] if err else ''}")
    except FileNotFoundError:
        FFMPEG_STATE["missing"] = True
        logging.warning("FFmpeg не найден, постобработка пропущена")
    except Exception as e:                        # noqa: BLE001 — постобработка не валит экспорт
        log_error(msg_id, f"faststart postprocess: {type(e).__name__}: {e}")
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ── Состояние ─────────────────────────────────────────────────────────────────
@dataclass
class Stats:
    processed: int = 0
    downloaded: int = 0
    skipped: int = 0
    dedup: int = 0
    repaired: int = 0
    failed: int = 0
    bytes_done: int = 0


async def worker(name, app, dest, queue, conn, mlock, inprog,
                 stats, stop_event, use_dedup, use_fast, connections,
                 force_include=frozenset()):
    while True:
        item: Item | None = await queue.get()
        try:
            if item is None:
                return
            if stop_event.is_set():
                continue
            # Защита от потери тома (механизм 1): если SSD отвалился во время работы,
            # не пишем ни байта мимо него — гасим все воркеры. Жёсткий sys.exit на этот
            # случай делается на пре-флайте (ensure_volume_mounted), здесь — мягкая
            # остановка, чтобы аккуратно сохранить манифест и закрыть пулы.
            if not os.path.ismount(volume_root_for(dest)):
                log_error(item.msg_id, "том размонтирован во время загрузки — остановка")
                REPORTER.status("\n🛑 SSD отключился — останавливаюсь, чтобы не писать на "
                                "системный диск.")
                stop_event.set()
                continue
            stats.processed += 1
            dest_path = dest_path_for(dest, item.msg_id, item.name)
            dkey = dedup_key(item.name, item.size, item.kind, item.duration)

            db_status, db_final = db_complete_info(conn, item.msg_id)
            state = file_state(dest_path, item.size, db_final, db_status)
            if state == "complete":
                stats.skipped += 1
                # Фиксируем реальный вес на диске (бэкфилл final_size для ремукс-видео,
                # опознанных по статусу) — чтобы следующий рестарт матчил точно по размеру.
                on_disk = os.path.getsize(dest_path)
                db_set_status(conn, item.msg_id, "COMPLETE", dkey, final_size=on_disk)
                REPORTER.file_skipped(item.msg_id)
                continue
            if state == "oversize":
                REPORTER.file_repair_oversize(item.msg_id)
                log(f"REPAIR oversize msg {item.msg_id}")
                os.remove(dest_path)
                stats.repaired += 1
            elif state == "partial":
                stats.repaired += 1
                if use_dedup:
                    db_set_status(conn, item.msg_id, "PARTIAL", dkey)

            claimed = False
            # force_include: возвращённый на ревью файл качаем как уникальный — мимо дедупа.
            if use_dedup and item.msg_id not in force_include:
                async with mlock:
                    # SQL-проверка дубликата: уже есть завершённый файл с тем же dkey?
                    rec = db_find_duplicate(conn, dkey, item.msg_id)
                    if rec:
                        rp = dest_path_for(dest, rec["msg_id"], rec["file_name"])
                        if master_size_ok(rp, rec):
                            stats.dedup += 1
                            if os.path.exists(dest_path):
                                os.remove(dest_path)
                            dup_fn = os.path.basename(rp)
                            REPORTER.file_dedup(item.msg_id, dup_fn)
                            log(f"DEDUP msg {item.msg_id} == {dup_fn}")
                            continue
                    if dkey in inprog:
                        stats.dedup += 1
                        continue
                    inprog.add(dkey)
                    claimed = True

            need = (item.size or 0) + SAFETY_MARGIN
            if item.size and shutil.disk_usage(dest).free < need:
                free = shutil.disk_usage(dest).free
                REPORTER.status(f"\n🛑 Мало места: свободно {human_gb(free):.1f} ГБ, "
                                f"нужно ~{human_gb(need):.1f} ГБ. Останавливаюсь.")
                log(f"STOP low-space free={free}")
                stop_event.set()
                if claimed:
                    async with mlock:
                        inprog.discard(dkey)
                continue

            try:
                t0 = time.time()
                fn = os.path.basename(dest_path)
                # Безопасная конкурентность (механизм 3): семафор ограничивает число
                # одновременно качающихся файлов (workers), оставляя контроль над FloodWait.
                async with DL_SEM:
                    REPORTER.file_started(name, item.msg_id, fn, item.size)
                    if item.kind == "photo":
                        REPORTER.status(
                            f"[{name}] msg {item.msg_id}: Скачивание фото -> "
                            f"photo_{item.msg_id}.jpg на SSD")
                    file_id = item.file_id
                    fre_tries = 0
                    # Предохранительный барьер: не более FRE_MAX_REFRESH (=2) горячих
                    # регенераций токена на ОДИН файл. Если Telegram и после двух свежих
                    # file_id снова отдаёт FILE_REFERENCE_EXPIRED — фиксируем FAILED в
                    # download_state.db, помечаем ok=False (ниже сработает file_failed) и
                    # АККУРАТНО переходим к следующему элементу очереди, не роняя воркер.
                    while True:
                        try:
                            ok = await download_verified(
                                app, file_id, item.msg_id, dest_path, item.size,
                                use_fast, connections, worker=name, conn=conn)
                            break
                        except FileReferenceExpired:
                            # Страховка Lazy-Refetching (Hot-Swap & Retry): если протухшая
                            # ссылка всё же пробилась на самый глубокий уровень воркера,
                            # поток НЕ падает и НЕ пропускает элемент молча — регенерируем
                            # токен (get_messages → новый file_id → атомарный UPDATE
                            # index.db) и перезапускаем загрузку с обновлённым file_id.
                            fre_tries += 1
                            new_fid = (await refresh_file_id(app, CHAT_ID, item.msg_id,
                                                             conn, name)
                                       if fre_tries <= FRE_MAX_REFRESH else None)
                            if not new_fid:
                                ok = False
                                # Барьер исчерпан: бессмертная отметка FAILED, чтобы
                                # рестарт не зацикливался на этом же мёртвом объекте.
                                db_set_status(conn, item.msg_id, "FAILED", dkey)
                                log_error(item.msg_id,
                                          "FILE_REFERENCE_EXPIRED: токен не регенерируется "
                                          f"после {FRE_MAX_REFRESH} попыток — файл пропущен")
                                break
                            file_id = new_fid
                    dt = time.time() - t0
                    sp = item.size / dt if dt > 0 else 0
                    REPORTER.file_done(name, item.msg_id, item.size, sp)
                if ok:
                    stats.downloaded += 1
                    stats.bytes_done += item.size
                    log(f"OK msg {item.msg_id} {fn} {item.size}B")
                    # Apple Quick Look: для свежескачанного ВИДЕО переносим moov-атом в начало
                    # (faststart) — иначе предпросмотр по пробелу в macOS не работает. Неблокирующе,
                    # без перекодирования; отсутствие ffmpeg не роняет экспорт.
                    if item.kind in VIDEO_KINDS:
                        await faststart_postprocess(dest_path, item.msg_id)
                    # SQL-обновление статуса: файл на SSD завершён. final_size читаем ПОСЛЕ
                    # faststart — это реальный пост-ремукс вес, по нему рестарт опознаёт файл
                    # как COMPLETE без ложного OVERSIZE (download_verified гарантировал, что
                    # до ремукса размер был ровно серверным, так что ремукс — единственная
                    # причина расхождения). Пишем независимо от дедупа: статус нужен и для
                    # пропуска при рестарте, и для сверки --verify.
                    final_sz = os.path.getsize(dest_path)
                    db_set_status(conn, item.msg_id, "COMPLETE", dkey, final_size=final_sz)
                else:
                    stats.failed += 1
                    REPORTER.file_failed(item.msg_id, MAX_RETRIES, fn)
                    log_error(item.msg_id, f"не докачан за {MAX_RETRIES} попыток ({fn})")
            except OSError as e:
                REPORTER.status(f"\n🛑 Ошибка записи на SSD (msg {item.msg_id}): {e}")
                log_error(item.msg_id, f"ошибка записи на SSD: {e}")
                stop_event.set()
            finally:
                REPORTER.stats(vars(stats))
                if claimed:
                    async with mlock:
                        inprog.discard(dkey)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:                       # noqa: BLE001
            # Тихий логгер (механизм 5): любое необработанное исключение пишем в
            # error_export.log с msg_id и текстом, но воркер не валим — берём следующий
            # файл, чтобы один битый объект не остановил весь экспорт.
            mid = getattr(item, "msg_id", "-") if item is not None else "-"
            stats.failed += 1
            log_error(mid, f"необработанное исключение: {type(e).__name__}: {e}")
        finally:
            queue.task_done()


async def feeder(items, queue, workers, stop_event):
    try:
        for it in items:
            if stop_event.is_set():
                break
            await queue.put(it)
    finally:
        # Сентинелы-стопы воркерам. При нормальном завершении await put() проходит штатно.
        # При принудительной отмене (повторный Ctrl+C → feed.cancel()) сам feeder уже
        # cancelled, и put() бросит CancelledError — гасим её, чтобы не было «грязного»
        # трейсбэка в консоли: воркеры в этот момент и так отменяются обработчиком сигнала.
        for _ in range(workers):
            try:
                await queue.put(None)
            except asyncio.CancelledError:
                break


def install_signal_handlers(loop, stop_event, holder: dict) -> None:
    """Graceful Shutdown: перехват SIGINT/SIGTERM на уровне event loop.

    Через loop.add_signal_handler сигнал больше НЕ поднимает KeyboardInterrupt — вместо
    падения мы запускаем мягкую остановку: выставляем ТОЛЬКО stop_event. По нему feeder
    перестаёт класть новые файлы в очередь (и досыпает None-сентинелы), воркеры — брать
    новые задачи, а ТЕКУЩИЕ I/O-записи на SSD дозавершаются штатно (задачи НЕ отменяем).
    Pyrogram-сессия и пулы закрываются в finally блока run(). Важно: feeder здесь НЕ
    отменяем через cancel() — иначе `await feed` бросил бы CancelledError и привёл к
    жёсткой отмене воркеров. Повторный Ctrl+C — принудительная отмена всех задач.
    """
    def handler():
        if stop_event.is_set():
            print("\n⛔ Повторный сигнал — принудительная остановка.")
            feed = holder.get("feed")
            if feed is not None:
                feed.cancel()
            for t in holder.get("tasks", []):
                t.cancel()
            return
        print("\n🛑 Сигнал остановки получен. Мягко завершаюсь: дозавершаю текущие "
              "загрузки, новых задач не беру… (повторный Ctrl+C — принудительно)")
        log("SIGINT graceful shutdown requested")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handler)
        except (NotImplementedError, RuntimeError):
            # Платформа без add_signal_handler (напр. Windows) — fallback на KeyboardInterrupt.
            pass


def request_stop(loop, stop_event) -> None:
    """Потокобезопасный мягкий стоп из ДРУГОГО потока (GUI). Движок крутится в фоновом
    asyncio-цикле; взводить его Event напрямую из Qt-потока нельзя — делаем это через
    loop.call_soon_threadsafe. Эквивалент первого Ctrl+C: дозагрузить текущее, новых не брать."""
    try:
        loop.call_soon_threadsafe(stop_event.set)
    except RuntimeError:
        stop_event.set()


# Мёртвые JSON-кэши, оставшиеся от версии до перехода на SQLite (index.db). Код их
# больше не читает (см. db_*); чистим один раз, чтобы не путали и не занимали место.
LEGACY_CACHES = (".tg_export_ids.json", ".tg_export_meta.json", ".tg_export_manifest.json")


def cleanup_legacy_caches(dest: str) -> None:
    removed = []
    for fn in LEGACY_CACHES:
        p = os.path.join(dest, fn)
        try:
            if os.path.isfile(p):
                os.remove(p)
                removed.append(fn)
        except OSError:
            pass
    if removed:
        REPORTER.status(f"🧹 Удалены устаревшие кэши (заменены на index.db): {', '.join(removed)}")
        log(f"CLEANUP legacy caches: {', '.join(removed)}")


@dataclass
class Options:
    """Параметры одного прогона экспорта. CLI собирает их из argparse, GUI — из формы."""
    workers: int = 1
    use_dedup: bool = True
    rescan: bool = False
    allow_kinds: "set[str] | None" = None
    skip_kinds: "set[str] | None" = None
    use_fast: bool = True
    connections: int = DEFAULT_CONNECTIONS
    verify: bool = False
    use_quality: bool = True
    faststart: bool = True
    # Защита уникальных: msg_id, которые НЕЛЬЗЯ исключать дедупом/резолюцией качества
    # (пользователь вернул их на экране ревью «Скачать всё»).
    force_include: frozenset = frozenset()
    # Только предпросмотр (скан+план без загрузки) — для экрана «Сканировать» в GUI.
    preview_only: bool = False
    # Качать комиксы/файлы по ССЫЛКАМ из сообщений (web_page + URL в тексте), а не только
    # прикреплённое медиа. link_format — формат сборки комиксов из страниц-галерей.
    download_links: bool = False
    link_format: str = "cbz"            # "cbz" | "pdf"


def _build_preview(items, s, losers, workers, use_dedup) -> dict:
    """Структурные данные предпросмотра + ревью для UI (CLI их не использует)."""
    eta_avg = s["need_size"] / AVG_SPEED_BPS if s["need_size"] else 0
    eta_peak = s["need_size"] / PEAK_SPEED_BPS if s["need_size"] else 0
    eta_par = s["need_size"] / (AVG_SPEED_BPS * max(1, workers)) if s["need_size"] else 0
    review = [{"msg_id": l.msg_id, "name": l.name, "size": l.size,
               "duration": l.duration, "kind": l.kind, "reason": "worse-version",
               "master_msg_id": w.msg_id, "master_size": w.size}
              for (l, w) in losers]
    return dict(s, eta_avg=eta_avg, eta_peak=eta_peak, eta_par=eta_par,
                workers=workers, use_dedup=use_dedup, review=review,
                plan_count=len(items))


async def run_export(cfg, options: "Options", reporter: "Reporter | None" = None,
                     stop_event=None, manage_signals: bool = True) -> dict:
    """Движок экспорта: скан канала → план → (предпросмотр | аудит | загрузка).

    Не печатает в stdout напрямую — все события идут в `reporter` (CLI/GUI). Не зовёт
    sys.exit (поднимает ExportError) и не ставит сигналов сам, если manage_signals=False
    (GUI управляет остановкой через stop_event + request_stop). Возвращает структурный
    результат (предпросмотр/ревью/итоги) для UI; CLI его игнорирует.
    """
    global LOG, ERR_LOG, CHAT_ID, POOL_SIZE, DL_SEM, SHUTDOWN, REPORTER, FASTSTART, DYNAMIC_GENERIC
    if reporter is not None:
        REPORTER = reporter
    FASTSTART = options.faststart
    workers = max(1, options.workers)
    connections = max(1, options.connections)
    use_dedup, use_quality, use_fast = options.use_dedup, options.use_quality, options.use_fast
    rescan, verify = options.rescan, options.verify
    allow_kinds, skip_kinds = options.allow_kinds, options.skip_kinds
    force_include = options.force_include or frozenset()
    POOL_SIZE = workers * connections
    dest = validate_dest(cfg["dest"])
    # Защита от потери тома (механизм 1): до любой записи убеждаемся, что целевой том
    # физически примонтирован; иначе ExportError — чтобы не залить данные на системный диск.
    ensure_volume_mounted(dest)
    LOG = setup_logger(dest)
    ERR_LOG = setup_error_logger(dest)
    cleanup_legacy_caches(dest)            # снести мёртвые JSON-кэши до-SQLite эпохи
    # Семафор безопасной конкурентности (механизм 3): не больше workers файлов разом.
    DL_SEM = asyncio.Semaphore(workers)

    # Graceful Shutdown: единый флаг остановки. В CLI ставим перехват SIGINT/SIGTERM; в GUI
    # (manage_signals=False) stop_event приходит извне и взводится через request_stop().
    if stop_event is None:
        stop_event = asyncio.Event()
    SHUTDOWN = stop_event
    sig_holder: dict = {"feed": None, "tasks": []}
    if manage_signals:
        install_signal_handlers(asyncio.get_running_loop(), stop_event, sig_holder)

    log(f"=== START workers={workers} dedup={use_dedup} fast={use_fast} "
        f"conn={connections} quality={use_quality} only={allow_kinds} skip={skip_kinds} "
        f"force_include={len(force_include)} preview_only={options.preview_only}")

    free = shutil.disk_usage(dest).free
    REPORTER.status(f"💽 Свободно на SSD: {human_gb(free):.1f} ГБ")
    fs = detect_fs(dest)
    if fs:
        REPORTER.status(f"🗂  Файловая система SSD: {fs}")
        if "FAT" in fs.upper() and "EXFAT" not in fs.upper():
            REPORTER.status("⚠️  FAT32 — файлы крупнее 4 ГБ записать НЕ получится! Лучше exFAT/APFS.")

    flt = []
    if allow_kinds:
        flt.append(f"только {','.join(sorted(allow_kinds))}")
    if skip_kinds:
        flt.append(f"кроме {','.join(sorted(skip_kinds))}")
    fast_str = f"вкл ({connections} соед./файл)" if use_fast else "выкл"
    REPORTER.status(f"⚙️  Воркеров: {workers} | многопоток: {fast_str} | "
                    f"дедуп: {'вкл' if use_dedup else 'выкл'} | "
                    f"резолюция качества: {'вкл' if use_quality else 'выкл'}"
                    + (f" | фильтр: {'; '.join(flt)}" if flt else "") + "\n")
    # Аккаунт лимитируется (~5 МБ/с): много одновременных соединений к одному DC
    # провоцируют сброс сокетов сервером («socket.send() raised exception» / Broken pipe),
    # а не ускоряют загрузку. Подсказываем щадящий режим, если суммарная нагрузка велика.
    if use_fast and workers * connections > 6:
        REPORTER.status(f"⚠️  Суммарно {workers}×{connections} = {workers * connections} соединений к "
                        f"DC. На лимитируемом аккаунте это рвёт сокеты (Broken pipe) и не ускоряет "
                        f"скачивание. Рекомендую: --connections 4 --workers 1.\n")

    # Локальная БД индекса/метаданных (SQLite) — заменяет прежние JSON-кэши.
    conn = db_connect(dest)

    # no_updates=True: это качалка, апдейты не нужны — иначе Pyrogram спамит
    # «Peer id invalid …» на чужие каналы в фоне и зря грузит соединение.
    app = Client(SESSION_NAME, api_id=cfg["api_id"], api_hash=cfg["api_hash"],
                 phone_number=cfg["phone"], workdir=DATA_DIR, no_updates=True)

    result: dict = {"dest": dest}    # путь назначения — для экрана отчётов/«открыть папку»
    try:
      async with app:
        chat = await app.get_chat(cfg["channel"])
        CHAT_ID = chat.id
        REPORTER.status(f"📡 Открыт: {getattr(chat, 'title', cfg['channel'])} (id={chat.id})")
        REPORTER.status(f"💾 Назначение: {dest}\n")

        if rescan:
            db_reset(conn)
            REPORTER.status("🧹 --rescan: индекс БД очищен. Скан истории с нуля…\n")

        meta = db_load_meta(conn)
        analyzed: set[int] = set(meta.get("analyzed", []))
        media: dict = meta.get("media", {})          # ключи — str(msg_id)

        cached = sorted(analyzed)                     # индекс ID берём из index.db
        if cached:
            since = max(cached)
            REPORTER.status(f"🗃  Индекс ID найден ({len(cached)} шт). Дочитываю новые (id>{since})…")
            new = await collect_new_ids(app, chat.id, since, media, analyzed)
            msg_ids = sorted(set(cached) | set(new))
            REPORTER.status(f"   Новых сообщений: {len(new)}. Всего: {len(msg_ids)}.\n")
        else:
            REPORTER.status("🔎 Полное сканирование истории канала…")
            msg_ids = await collect_message_ids(app, chat.id, media, analyzed)
            REPORTER.status(f"   Всего сообщений: {len(msg_ids)}.\n")
        db_save_meta(conn, sorted(analyzed), media)

        # ── FREQUENCY ANALYZER (Динамический Guard, pre-flight) ──
        # По собранной базе канала вычисляем шаблонные имена (>N повторов с РАЗНЫМИ размерами)
        # и кладём в DYNAMIC_GENERIC: дальше dedup_key/media_fingerprint автоматически роняют
        # такие файлы на защиту «Имя + Размер» — без ручного ведения словарей.
        DYNAMIC_GENERIC = build_dynamic_generic_names(media)
        if DYNAMIC_GENERIC:
            sample = ", ".join(sorted(DYNAMIC_GENERIC)[:8])
            REPORTER.status(f"🧠 Эвристика дубликатов: динамически помечено generic-имён "
                            f"{len(DYNAMIC_GENERIC)} (защита Имя+Размер): {sample}"
                            + (" …" if len(DYNAMIC_GENERIC) > 8 else ""))
            log(f"DYNAMIC_GENERIC {len(DYNAMIC_GENERIC)}: {sample}")

        if stop_event.is_set():
            REPORTER.status("⛔ Остановка получена при сканировании — выходим, индекс сохранён.")
            return result

        # ФУНКЦИЯ-СПАСАТЕЛЬ (Disk Source of Truth): ДО плана сверяем диск с бессмертной БД
        # и заносим в неё готовые файлы, которые там ещё не значатся (например, «забытые»
        # откатом транзакции после сетевого сбоя). Запускаем ВСЕГДА — даже без дедупа, иначе
        # сверка не увидела бы готовое и начала бы перекачку. download_state.db не трогается
        # --rescan, так что прогресс бессмертен.
        recovered = recover_state_from_disk(conn, dest, media)
        if recovered:
            REPORTER.status(f"🛟 Восстановлено с диска в download_state.db: +{recovered} "
                            f"готовых файл(ов) — перекачивать их не нужно.")
            log(f"RECOVER +{recovered} from disk")

        # ── ПРЕДПРОСМОТР ──
        REPORTER.status("📊 Анализирую, что нужно скачать…")
        items = await build_plan(app, chat.id, msg_ids, allow_kinds, skip_kinds,
                                 dest, media, analyzed, conn)

        # ── ДВУХПРОХОДНАЯ РЕЗОЛЮЦИЯ КАЧЕСТВА (Pre-flight, до обращения к диску) ──
        # Группируем медиа по «отпечатку» (имя+длительность), оставляем лучшую (самую
        # крупную) версию, худшие исключаем из плана. force_include защищает возвращённые
        # пользователем уникальные файлы от исключения.
        losers: list = []
        if use_quality:
            items, losers = resolve_quality(items, dest, force_include=force_include)
            if losers:
                REPORTER.status(f"🏆 Резолюция качества: исключено худших версий-дубликатов "
                                f"{len(losers)} (мастеров к обработке: {len(items)}). "
                                f"Подробности → quality_report.txt")
                log(f"QUALITY masters={len(items)} losers={len(losers)}")

        # ── РЕЖИМ АУДИТА (--verify): только чтение, без скачивания ──
        if verify:
            REPORTER.status("🔍 Режим аудита (--verify): сверяю файлы на диске, сеть не используется…")
            v = verify_export(items, dest, conn)
            REPORTER.status("─" * 70)
            REPORTER.status("🧾 АУДИТ ЭКСПОРТА (только чтение)")
            REPORTER.status(f"   Всего файлов в плане : {len(items)}")
            REPORTER.status(f"   ✅ COMPLETE          : {v['COMPLETE']}")
            REPORTER.status(f"   ⏳ PARTIAL           : {v['PARTIAL']}")
            REPORTER.status(f"   ❌ MISSING           : {v['MISSING']}")
            REPORTER.status(f"   🔧 OVERSIZE          : {v['OVERSIZE']}")
            REPORTER.status(f"   📄 Отчёт             : {v['report']}")
            REPORTER.status("─" * 70)
            log(f"VERIFY complete={v['COMPLETE']} partial={v['PARTIAL']} "
                f"missing={v['MISSING']} oversize={v['OVERSIZE']}")
            return {"verify": v}
        s = summarize_plan(items, dest, conn, use_dedup, force_include=force_include)
        preview = _build_preview(items, s, losers, workers, use_dedup)
        result["preview"] = preview
        REPORTER.plan_preview(preview)

        REPORTER.status("─" * 70)
        REPORTER.status("📋 ПРЕДПРОСМОТР ЭКСПОРТА")
        REPORTER.status(f"   Всего медиафайлов в канале : {s['total_files']}  "
                        f"({human_gb(s['total_size']):.2f} ГБ)")
        REPORTER.status(f"   Уже на диске               : {s['have_files']}"
                        + (f" | дубликатов: {s['dup_files']}" if use_dedup else ""))
        REPORTER.status(f"   📥 К ЗАГРУЗКЕ               : {s['need_files']} файлов, "
                        f"{human_gb(s['need_size']):.2f} ГБ")
        if s["need_size"]:
            REPORTER.status(f"   ⏱  Оценка времени:")
            REPORTER.status(f"        • средняя 850 КБ/с (1 поток) : ~{fmt_duration(preview['eta_avg'])}")
            REPORTER.status(f"        • пиковая 1.1 МБ/с (1 поток) : ~{fmt_duration(preview['eta_peak'])}")
            if workers > 1:
                REPORTER.status(f"        • {workers} воркера(ов) @850 КБ/с   : ~{fmt_duration(preview['eta_par'])} "
                                f"(оптимистично, если Telegram не лимитирует)")
        REPORTER.status("─" * 70)
        log(f"PLAN total={s['total_files']}/{human_gb(s['total_size']):.2f}GB "
            f"need={s['need_files']}/{human_gb(s['need_size']):.2f}GB")

        # Только предпросмотр (экран «Сканировать» в GUI) — загрузку не начинаем.
        if options.preview_only:
            return result

        stats = Stats()
        # ── Загрузка прикреплённого медиа (если оно ещё не на диске) ──
        if s["need_files"] == 0:
            REPORTER.status("✅ Всё прикреплённое медиа уже скачано — новых файлов нет.")
        else:
            REPORTER.status("")
            mlock = asyncio.Lock()
            inprog: set[str] = set()
            # ВНИМАНИЕ: используем общий stop_event (создан выше и привязан к обработчику
            # сигналов в CLI / к request_stop в GUI) — не пересоздаём.
            queue: asyncio.Queue = asyncio.Queue(maxsize=workers * 4)

            feed = asyncio.create_task(feeder(items, queue, workers, stop_event))
            tasks = [asyncio.create_task(
                worker(f"w{i+1}", app, dest, queue, conn, mlock, inprog,
                       stats, stop_event, use_dedup, use_fast, connections,
                       force_include))
                for i in range(workers)]
            # Отдаём задачи обработчику сигналов: при повторном Ctrl+C он их отменит.
            sig_holder["feed"] = feed
            sig_holder["tasks"] = tasks

            try:
                await feed
                await asyncio.gather(*tasks)
            except (KeyboardInterrupt, asyncio.CancelledError):
                # Ctrl-C: гасим всё аккуратно, чтобы не было «Task exception was never retrieved»
                stop_event.set()
                raise
            finally:
                feed.cancel()
                for t in tasks:
                    t.cancel()
                await asyncio.gather(feed, *tasks, return_exceptions=True)
                await close_all_pools(app)
                conn.commit()

        # ── Фаза ссылок/комиксов: качаем внешние ссылки (web_page / URL в тексте) ──
        # Прикреплённые .pdf/.cbz движок берёт сам (kind=document); здесь — то, что лежит
        # ВНЕ Telegram, по ссылке: страницы-галереи собираются в один PDF/CBZ.
        if options.download_links and not stop_event.is_set():
            try:
                import link_export
                result["link_stats"] = await link_export.run_link_phase(
                    app, chat.id, dest, options.link_format, REPORTER, stop_event, log)
            except Exception as e:                       # noqa: BLE001 — фаза ссылок не валит экспорт
                log_error(0, f"link phase error: {e}")
                REPORTER.status(f"⚠️  Фаза ссылок прервана ошибкой: {e}")

        summary = (f"скачано: {stats.downloaded} ({human_gb(stats.bytes_done):.2f} ГБ), "
                   f"уже было: {stats.skipped}, докачано/починено: {stats.repaired}, "
                   f"дубликатов: {stats.dedup}, ошибок: {stats.failed}.")
        ls = result.get("link_stats")
        if ls:
            summary += (f" Ссылки: +{ls['saved']} (уже было {ls['skipped']}, "
                        f"ошибок {ls['failed']}).")
        REPORTER.status(f"\n✅ Готово. {summary}")
        REPORTER.finished(summary, stop_event.is_set())
        log(f"=== DONE {summary}")
        if stop_event.is_set():
            REPORTER.status("⚠️  Остановлено досрочно (сигнал/диск/ошибка). "
                            "Перезапуск продолжит с места.")
        result["stats"] = vars(stats)
        result["stopped"] = stop_event.is_set()
    finally:
        conn.commit()
        conn.close()
    return result


async def scan_preview(cfg, options: "Options", reporter: "Reporter | None" = None,
                       stop_event=None) -> dict:
    """Скан канала + план БЕЗ загрузки (экран «Сканировать» в GUI). Возвращает preview-dict
    (всего/на диске/к загрузке/ETA + данные ревью losers). Тонкая обёртка над run_export."""
    opts = replace(options, preview_only=True)
    return await run_export(cfg, opts, reporter=reporter, stop_event=stop_event,
                            manage_signals=False)


def parse_types(s):
    if not s:
        return None
    types = {x.strip().lower() for x in s.split(",") if x.strip()}
    bad = types - set(KNOWN_KINDS)
    if bad:
        sys.exit(f"❌ Неизвестные типы: {', '.join(bad)}. Доступно: {', '.join(KNOWN_KINDS)}")
    return types


def parse_args():
    p = argparse.ArgumentParser(description="Экспорт медиа из Telegram на внешний SSD (macOS).")
    p.add_argument("--reset", action="store_true", help="сбросить config.json")
    p.add_argument("--workers", type=int, default=None,
                   help="параллельных файлов = размер семафора конкурентности "
                        "(по умолчанию 1 при многопотоке, иначе 3; для параллельной "
                        "загрузки 3-5 файлов укажите, напр., --workers 3)")
    p.add_argument("--no-dedup", action="store_true", help="отключить дедуп по содержимому")
    p.add_argument("--no-quality", action="store_true",
                   help="отключить двухпроходную резолюцию качества (качать все версии дублей)")
    p.add_argument("--no-fast", action="store_true",
                   help="отключить многопоточную загрузку файла (только последовательно)")
    p.add_argument("--disable-faststart", "--no-faststart", action="store_true",
                   dest="no_faststart",
                   help="отключить пост-процессинг FFmpeg: не перепаковывать видео под Apple "
                        "Quick Look (-movflags +faststart). subprocess пропускается, в "
                        "download_state.db пишется оригинальный размер скачанного файла")
    p.add_argument("--connections", type=int, default=DEFAULT_CONNECTIONS,
                   help=f"соединений на файл в многопотоке (по умолчанию {DEFAULT_CONNECTIONS})")
    p.add_argument("--rescan", action="store_true", help="игнорировать кэш ID, скан заново")
    p.add_argument("--verify", action="store_true",
                   help="аудит: сверить файлы на диске (verify_report.txt) и выйти, без скачивания")
    p.add_argument("--only", help="только эти типы: video,document,photo,audio,voice,...")
    p.add_argument("--skip", help="пропускать эти типы")
    p.add_argument("--links", action="store_true",
                   help="дополнительно качать комиксы/файлы по ССЫЛКАМ из сообщений "
                        "(web_page и URL в тексте), а не только прикреплённое медиа")
    p.add_argument("--links-format", choices=("cbz", "pdf"), default="cbz",
                   help="формат сборки комиксов из страниц-галерей (по умолчанию cbz)")
    # ── Upload Pipeline (отправка папки в канал) ──
    p.add_argument("--upload", metavar="ПАПКА",
                   help="режим ОТПРАВКИ: рекурсивно загрузить файлы из ПАПКИ в канал "
                        "(по умолчанию канал из config.json; см. --to)")
    p.add_argument("--to", metavar="ЧАТ",
                   help="цель для --upload: @username / id / ссылка (по умолчанию канал из config)")
    p.add_argument("--upload-as-document", action="store_true",
                   help="--upload: слать ВСЁ документами (без send_video — не будет превью)")
    p.add_argument("--upload-no-recursive", action="store_true",
                   help="--upload: только верхний уровень папки, без вложенных подпапок")
    p.add_argument("--upload-preview", action="store_true",
                   help="--upload: ТОЛЬКО показать, что будет отправлено (dry-run), ничего не слать")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    # Глушим внутренний логгер Pyrogram до CRITICAL. На лимитируемом аккаунте он сыплет в
    # консоль трейсбэки ОЖИДАЕМЫХ и УЖЕ ОБРАБАТЫВАЕМЫХ нами событий: тихий
    # FileReferenceExpired в stream_media (мы ловим его по нулевой докачке и обновляем
    # ссылку) и «Send exception … Broken pipe» при сбросе сокетов (пул их сам переподнимает).
    # Свой ход экспорта мы пишем в export_log.txt и error_export.log — терминал держим чистым.
    logging.getLogger("pyrogram").setLevel(logging.CRITICAL)
    # Отдельно глушим логгер стандартного asyncio до ERROR. При обрыве/Ctrl+C он шлёт
    # WARNING «socket.send() raised exception.» (selector_events.py): после потери соединения
    # asyncio ещё несколько раз пытается писать в уже закрытый сокет. Это НЕ ошибка — файл
    # дозавершается на пересозданных сессиях. Логгер «asyncio» не входит в иерархию «pyrogram»,
    # поэтому строкой выше не накрывался. ERROR оставляет видимыми реальные сбои asyncio.
    logging.getLogger("asyncio").setLevel(logging.ERROR)
    if args.reset:
        reset_config()
    cfg = load_config()
    if cfg is None:
        cfg = interactive_setup()
        save_config(cfg)
    else:
        print("⚙️  Конфигурация найдена — запускаюсь в автоматическом режиме.")

    # ── Режим ОТПРАВКИ (--upload): ветка Upload Pipeline, мимо логики скачивания ──
    if args.upload:
        import uploader                      # ленивый импорт (uploader импортирует нас)
        up_opts = uploader.UploadOptions(
            recursive=not args.upload_no_recursive,
            native_media=not args.upload_as_document)
        try:
            if args.upload_preview:
                # Dry-run: только предпросмотр (что/сколько отправилось бы), без отправки.
                asyncio.run(uploader.scan_upload(cfg, args.upload, chat=args.to,
                                                 options=up_opts))
            else:
                asyncio.run(uploader.run_upload(cfg, args.upload, chat=args.to,
                                                options=up_opts))
        except ExportError as e:
            print(str(e))
            sys.exit(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n⛔ Прервано пользователем. Уже отправленное сохранено, можно перезапустить.")
        return

    allow_kinds = parse_types(args.only)
    skip_kinds = parse_types(args.skip)
    use_fast = not args.no_fast
    connections = max(1, args.connections)
    # При многопотоке параллелим внутри файла → по файлам по умолчанию 1 воркер,
    # чтобы не плодить workers×connections сессий и не злить лимиты Telegram.
    if args.workers is not None:
        workers = max(1, args.workers)
    else:
        workers = 1 if use_fast else DEFAULT_WORKERS
    options = Options(
        workers=workers, use_dedup=not args.no_dedup, rescan=args.rescan,
        allow_kinds=allow_kinds, skip_kinds=skip_kinds, use_fast=use_fast,
        connections=connections, verify=args.verify, use_quality=not args.no_quality,
        faststart=not args.no_faststart,
        download_links=args.links, link_format=args.links_format)
    try:
        # CLI: Reporter по умолчанию (CliReporter), движок сам ставит SIGINT-обработчик.
        asyncio.run(run_export(cfg, options))
    except ExportError as e:
        # Фатальная (не аварийная) ошибка движка: нет тома/доступа/места. В CLI — печать+выход.
        print(str(e))
        sys.exit(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        # CancelledError долетает сюда при принудительной остановке (повторный Ctrl+C):
        # обработчик сигнала отменяет feed/воркеры, отмена всплывает из asyncio.run.
        # Перехватываем здесь — иначе пользователь видит длинный трейсбэк CancelledError.
        print("\n⛔ Прервано пользователем. Прогресс сохранён, можно перезапустить.")


if __name__ == "__main__":
    main()

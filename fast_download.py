#!/usr/bin/env python3.12
# -*- coding: utf-8 -*-
"""
Многопоточная (multi-connection) загрузка ОДНОГО файла из Telegram для Pyrogram 2.x.

Идея (подход FastTelethon):
  * к дата-центру файла открывается несколько media-сессий;
  * файл бьётся на части по 1 МБ, части раздаются сессиям параллельно;
  * куски пишутся в нужные смещения файла (seek+write).

КЛЮЧЕВОЕ отличие от наивной реализации: media-сессии создаются ОДИН раз на весь
прогон и переиспользуются между файлами (пул). Это критично, когда канал лежит не
на «домашнем» DC: иначе каждая сессия дёргает auth.ExportAuthorization, и после
сотни файлов прилетает FLOOD_WAIT на ExportAuthorization → рвутся сокеты (Broken
pipe). С пулом ExportAuthorization выполняется один раз на DC за весь запуск.

Гарантии:
  * Zero-In-Memory: в памяти одновременно только активные чанки (N×1 МБ), не весь файл.
  * Докачка: прогресс хранится в карте частей (<dest>.dl + <dest>.dlmap); в финальное
    имя файл переименовывается только при 100%.
  * При CDN-редиректе/ошибке бросается FastUnavailable — вызывающий код делает откат
    на штатный последовательный загрузчик Pyrogram.

Внимание: используются внутренние механизмы Pyrogram (raw, Session, Auth) — это
版本-зависимо. Перед боевым прогоном проверьте на одном файле (см. test_fast.py).
"""

import asyncio
import json
import os
from typing import Callable, Optional, Any

from pyrogram import Client, raw, utils
from pyrogram.errors import FloodWait, FileReferenceExpired
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.session import Auth, Session

PART_SIZE = 1024 * 1024            # размер части (1 МБ; кратно 4096, ≤ 1 МБ)
DEFAULT_CONNECTIONS = 8           # соединений на файл по умолчанию
MAX_CONNECTIONS = 16
MAP_FLUSH_EVERY = 16              # как часто сбрасывать карту частей на диск
PART_RETRIES = 6                  # попыток на одну часть при обрыве соединения

# Сетевые/транзиентные ошибки: рвётся сокет (Broken pipe, reset, таймаут) — лечится
# пересозданием одной media-сессии (auth_key переиспользуется), а не откатом файла.
_TRANSIENT = (OSError, asyncio.TimeoutError, ConnectionError, EOFError)


class FastUnavailable(Exception):
    """Многопоточный путь невозможен — нужен откат на обычный загрузчик."""


def _build_location(file_id_obj: FileId):
    """Строит InputFileLocation из декодированного file_id (как делает Client.get_file)."""
    ft = file_id_obj.file_type
    if ft == FileType.CHAT_PHOTO:
        if file_id_obj.chat_id > 0:
            peer: Any = raw.types.InputPeerUser(
                user_id=file_id_obj.chat_id, access_hash=file_id_obj.chat_access_hash)
        elif file_id_obj.chat_access_hash == 0:
            peer = raw.types.InputPeerChat(chat_id=-file_id_obj.chat_id)
        else:
            peer = raw.types.InputPeerChannel(
                channel_id=utils.get_channel_id(file_id_obj.chat_id),
                access_hash=file_id_obj.chat_access_hash)
        return raw.types.InputPeerPhotoFileLocation(
            peer=peer, photo_id=file_id_obj.media_id,
            big=file_id_obj.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG)
    if ft == FileType.PHOTO:
        return raw.types.InputPhotoFileLocation(
            id=file_id_obj.media_id, access_hash=file_id_obj.access_hash,
            file_reference=file_id_obj.file_reference,
            thumb_size=file_id_obj.thumbnail_size)
    return raw.types.InputDocumentFileLocation(
        id=file_id_obj.media_id, access_hash=file_id_obj.access_hash,
        file_reference=file_id_obj.file_reference,
        thumb_size=file_id_obj.thumbnail_size)


def _connection_count(file_size: int) -> int:
    # мелким файлам — мало соединений, крупным — больше (но не выше MAX)
    by_size = max(1, (file_size + 8 * PART_SIZE - 1) // (8 * PART_SIZE))
    return max(1, min(DEFAULT_CONNECTIONS, MAX_CONNECTIONS, by_size))


# ── Пул переиспользуемых media-сессий ───────────────────────────────────────────
class _DCPool:
    """
    Пул долгоживущих media-сессий к одному DC. Все сессии делят ОДИН auth_key и
    одну авторизацию (ExportAuthorization/ImportAuthorization выполняются один раз).
    Слоты выдаются в аренду (lease) и возвращаются (release); упавшую сессию можно
    пересоздать (recreate_slot) бесплатно — auth_key уже авторизован.
    """

    def __init__(self, client: Client, dc_id: int):
        self.client = client
        self.dc_id = dc_id
        self.auth_key: Optional[bytes] = None
        self.test_mode = False
        self.is_remote = False
        self.size = 0
        self._free: asyncio.Queue = asyncio.Queue()
        self._lock = asyncio.Lock()

    async def _make_session(self) -> Session:
        session = Session(self.client, self.dc_id, self.auth_key or b"",
                          self.test_mode, is_media=True)
        await session.start()
        return session

    async def ensure(self, size: int) -> None:
        """Гарантирует, что в пуле не меньше `size` сессий (создаёт недостающие)."""
        async with self._lock:
            if self.auth_key is None:
                self.test_mode = await self.client.storage.test_mode()
                home_dc = await self.client.storage.dc_id()
                self.is_remote = self.dc_id != home_dc
                if self.is_remote:
                    self.auth_key = await self._with_flood(
                        lambda: Auth(self.client, self.dc_id, self.test_mode).create())
                else:
                    self.auth_key = await self.client.storage.auth_key()
                # одна авторизация auth_key на весь пул (важно: только для remote DC)
                if self.is_remote:
                    exported = await self._with_flood(lambda: self.client.invoke(
                        raw.functions.auth.ExportAuthorization(dc_id=self.dc_id)))
                    first = await self._make_session()
                    await self._with_flood(lambda: first.invoke(
                        raw.functions.auth.ImportAuthorization(
                            id=exported.id, bytes=exported.bytes)))
                    self._free.put_nowait([0, first])
                    self.size = 1
            need = size - self.size
            if need > 0:                             # остальные сессии — параллельно
                new = await asyncio.gather(
                    *(self._make_session() for _ in range(need)))
                for s in new:
                    self._free.put_nowait([self.size, s])
                    self.size += 1

    @staticmethod
    async def _with_flood(make_coro):
        """Выполняет корутину (из фабрики), пережидая FloodWait — для init-вызовов пула."""
        while True:
            try:
                return await make_coro()
            except FloodWait as e:
                await asyncio.sleep(int(getattr(e, "value", 0) or 0) + 1)

    async def lease(self, n: int) -> list:
        return [await self._free.get() for _ in range(n)]

    def release(self, slots: list) -> None:
        for slot in slots:
            self._free.put_nowait(slot)

    async def recreate_slot(self, slot: list) -> Session:
        _, old = slot
        try:
            await old.stop()
        except Exception:                            # noqa: BLE001
            pass
        slot[1] = await self._make_session()
        return slot[1]

    async def close(self) -> None:
        async with self._lock:
            drained = []
            while not self._free.empty():
                drained.append(self._free.get_nowait())
            for _, s in drained:
                try:
                    await s.stop()
                except Exception:                    # noqa: BLE001
                    pass
            self.size = 0
            self.auth_key = None


_POOLS: dict = {}


async def get_pool(client: Client, dc_id: int, size: int) -> _DCPool:
    key = (id(client), dc_id)
    pool = _POOLS.get(key)
    if pool is None:
        pool = _DCPool(client, dc_id)
        _POOLS[key] = pool
    await pool.ensure(size)
    return pool


async def close_all_pools(client: Optional[Client] = None) -> None:
    """Закрывает пулы (вызывать в finally прогона). При client=None — все."""
    keys = [k for k in list(_POOLS) if client is None or k[0] == id(client)]
    for k in keys:
        pool = _POOLS.pop(k, None)
        if pool is not None:
            await pool.close()


def _part_len(idx: int, file_size: int) -> int:
    start = idx * PART_SIZE
    return min(PART_SIZE, file_size - start)


def _load_map(map_path: str) -> set[int]:
    if not os.path.exists(map_path):
        return set()
    try:
        with open(map_path, "r", encoding="utf-8") as fh:
            return set(json.load(fh))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_map(map_path: str, done: set[int]) -> None:
    tmp = map_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(sorted(done), fh)
    os.replace(tmp, map_path)


async def fast_download_file(
    client: Client,
    file_id: str,
    dest_path: str,
    file_size: int,
    progress: Optional[Callable[[int], None]] = None,
    connections: int = 0,
    pool_size: int = 0,
) -> None:
    """
    Качает файл в dest_path несколькими соединениями из общего пула сессий.
    Бросает FastUnavailable, если многопоточный путь неприменим (CDN, неизвестный
    тип), либо FileReferenceExpired — чтобы вызывающий обновил ссылку и повторил.

    pool_size — общий размер пула сессий к DC (по умолчанию = connections). При
    нескольких воркерах передавайте workers*connections, чтобы хватало на всех.
    """
    if not file_size or file_size <= 0:
        raise FastUnavailable("неизвестный размер файла")

    try:
        file_id_obj = FileId.decode(file_id)
        location = _build_location(file_id_obj)
        dc_id = file_id_obj.dc_id
    except Exception as e:                       # noqa: BLE001
        raise FastUnavailable(f"не удалось разобрать file_id: {e}")

    tmp_path = dest_path + ".dl"
    map_path = dest_path + ".dlmap"
    total_parts = (file_size + PART_SIZE - 1) // PART_SIZE
    conns = connections or _connection_count(file_size)
    conns = max(1, min(conns, total_parts, MAX_CONNECTIONS))

    # подготовка временного файла нужного размера
    if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) != file_size:
        with open(tmp_path, "wb") as fh:
            if file_size:
                fh.truncate(file_size)

    done = _load_map(map_path)
    done = {i for i in done if 0 <= i < total_parts}
    done_bytes = sum(_part_len(i, file_size) for i in done)

    pending: asyncio.Queue = asyncio.Queue()
    for i in range(total_parts):
        if i not in done:
            pending.put_nowait(i)

    if pending.empty():                          # всё уже скачано ранее
        _finalize(tmp_path, dest_path, map_path)
        if progress:
            progress(file_size)
        return

    lock = asyncio.Lock()
    state: dict[str, Any] = {"done_bytes": done_bytes, "since_flush": 0, "fatal": None}

    async def fetch_part(session: Session, idx: int):
        offset = idx * PART_SIZE
        while True:
            try:
                result = await session.invoke(raw.functions.upload.GetFile(
                    location=location, offset=offset, limit=PART_SIZE,
                    precise=False, cdn_supported=False))
            except FloodWait as e:
                await asyncio.sleep(int(getattr(e, "value", 0) or 0) + 1)
                continue
            if not isinstance(result, raw.types.upload.File):
                raise FastUnavailable("CDN-редирект или неподдерживаемый ответ")
            data = result.bytes
            expected = _part_len(idx, file_size)
            if len(data) != expected:
                # Короткий/битый ответ (обрыв в момент чтения). Писать его НЕЛЬЗЯ: tmp уже
                # растянут truncate(file_size), поэтому короткая часть оставит внутри файла
                # «дыру» из нулей — файл выйдет правильного размера, но повреждённым, и
                # проверка по размеру это не поймает. EOFError входит в _TRANSIENT → часть
                # будет перекачана заново с пересозданием сессии.
                raise EOFError(f"короткая часть {idx}: {len(data)}/{expected} байт")
            # seek+write без await между ними — безопасно в asyncio
            fh.seek(offset)
            fh.write(data)
            return len(data)

    async def worker(slot: list):
        while True:
            if state["fatal"] is not None:
                return
            try:
                idx = pending.get_nowait()
            except asyncio.QueueEmpty:
                return

            n = None
            last_err: Exception | None = None
            for attempt in range(PART_RETRIES):
                if state["fatal"] is not None:
                    return
                try:
                    n = await fetch_part(slot[1], idx)
                    break
                except FastUnavailable as e:         # CDN/неподдерживаемый ответ — откат
                    state["fatal"] = e
                    return
                except FileReferenceExpired as e:    # ссылку обновит вызывающий код
                    state["fatal"] = e
                    return
                except _TRANSIENT as e:              # обрыв сокета — пересоздаём сессию
                    last_err = e
                    await asyncio.sleep(min(2 ** attempt, 8))
                    try:
                        await pool.recreate_slot(slot)
                    except Exception as e2:          # noqa: BLE001
                        last_err = e2
                except Exception as e:               # noqa: BLE001
                    state["fatal"] = FastUnavailable(f"ошибка части {idx}: {e}")
                    return

            if n is None:
                pending.put_nowait(idx)              # вернём часть в очередь
                state["fatal"] = FastUnavailable(
                    f"часть {idx} не скачана за {PART_RETRIES} попыток: {last_err}")
                return

            async with lock:
                done.add(idx)
                state["done_bytes"] += n
                state["since_flush"] += 1
                if state["since_flush"] >= MAP_FLUSH_EVERY:
                    _save_map(map_path, done)
                    state["since_flush"] = 0
                if progress:
                    progress(state["done_bytes"])

    pool = await get_pool(client, dc_id, pool_size or conns)
    slots = await pool.lease(conns)
    # Файл открываем ТОЛЬКО после успешной аренды сессий: если get_pool/lease упадут
    # (типично при флапающей сети), дескриптор файла не повиснет. release слотов — во
    # внешнем finally, чтобы сессии вернулись в пул даже если open() не смог открыть tmp.
    try:
        fh = open(tmp_path, "r+b")  # type: ignore[assignment]
        try:
            await asyncio.gather(*(worker(s) for s in slots))
        finally:
            fh.flush()
            os.fsync(fh.fileno())
            fh.close()
            _save_map(map_path, done)
    finally:
        pool.release(slots)

    if state["fatal"] is not None:
        raise state["fatal"]

    if len(done) != total_parts:
        raise FastUnavailable("скачаны не все части")

    _finalize(tmp_path, dest_path, map_path)
    if progress:
        progress(file_size)


def _finalize(tmp_path: str, dest_path: str, map_path: str) -> None:
    os.replace(tmp_path, dest_path)
    if os.path.exists(map_path):
        os.remove(map_path)

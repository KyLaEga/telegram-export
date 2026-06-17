"""EngineController — мост Qt ↔ asyncio (GUI-сторона, живёт в главном потоке).

Владеет:
  • фоновым потоком с собственным asyncio event-loop (там крутится движок pyrogram);
  • одной `QtReporter` + потокобезопасной очередью её событий;
  • `QTimer` (~20 Гц), который дренирует очередь В ГЛАВНОМ ПОТОКЕ и переизлучает Qt-сигналы.

Публичные методы (вызывать ТОЛЬКО из GUI-потока):
  • login(cfg) / submit_code(code) / submit_password(pwd) / resend_code() / cancel_login()
  • scan(cfg, options) / start_export(cfg, options) / start_upload(cfg, folder, chat, options)
  • stop()  — мягкая остановка текущего прогона (эквивалент первого Ctrl+C)
  • shutdown() — остановить loop/поток при выходе из приложения

Все ответы движка приходят как Qt-сигналы (см. ниже). Виджеты НИКОГДА не трогаются из
потока движка — единственный канал наружу это очередь, разбираемая `_drain` по таймеру.
"""

from __future__ import annotations

import asyncio
import queue
import threading

from PySide6.QtCore import QObject, QTimer, Signal

import export_media as em


class EngineController(QObject):
    # Низкоуровневый поток событий движка: (имя_события, кортеж_аргументов).
    # Сюда же мапятся все методы QtReporter — экраны Phase 2 подпишутся точечно.
    event = Signal(str, object)

    # Высокоуровневые завершения заданий (эмитятся СТРОГО после слива очереди событий).
    scan_done = Signal(dict)        # результат scan_preview (preview-dict)
    run_done = Signal(dict)         # результат run_export / run_upload
    failed = Signal(str)            # ExportError или непредвиденная ошибка (текст)
    busy_changed = Signal(bool)     # True пока выполняется задание

    # Интерактивный логин.
    login_code_sent = Signal(str)   # человекочитаемое описание способа доставки кода
    login_need_password = Signal()  # требуется облачный пароль (2FA)
    login_ok = Signal(str)          # успешный вход; строка-приветствие (имя/username/id)
    login_failed = Signal(str)      # ошибка логина (текст для показа)
    login_cancelled = Signal()

    DRAIN_HZ = 20

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._q: "queue.Queue" = queue.Queue()
        # QtReporter импортируем лениво, чтобы цепочка импортов gui→reporter→em была явной.
        from .reporter import QtReporter
        self._reporter = QtReporter(self._q)
        self._busy = False
        self._stop_event: "asyncio.Event | None" = None     # активного задания
        self._auth_inbox: "asyncio.Queue | None" = None      # канал ввода кода/пароля в логин

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="engine-asyncio", daemon=True)
        self._thread.start()

        # Таймер дренажа держим включённым всё время жизни контроллера: на 20 Гц это
        # пренебрежимая нагрузка, зато и логин, и задания шлют события одним каналом.
        self._timer = QTimer(self)
        self._timer.setInterval(max(1, 1000 // self.DRAIN_HZ))
        self._timer.timeout.connect(self._drain)
        self._timer.start()

    # ── фоновой asyncio-loop ───────────────────────────────────────────────
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _spawn(self, coro) -> None:
        """Запустить корутину в loop движка из GUI-потока."""
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ── слив очереди (ГЛАВНЫЙ ПОТОК, по таймеру) ────────────────────────────
    def _drain(self) -> None:
        try:
            while True:
                name, args = self._q.get_nowait()
                if name == "__done__":
                    self._on_job_done(*args)
                elif name == "__error__":
                    self._on_job_error(args[0])
                elif name == "__login__":
                    self._on_login_event(*args)
                else:
                    self.event.emit(name, args)
        except queue.Empty:
            pass

    def _on_job_done(self, kind: str, result: dict) -> None:
        self._busy = False
        self._stop_event = None
        self.busy_changed.emit(False)
        if kind == "scan":
            self.scan_done.emit(result or {})
        else:
            self.run_done.emit(result or {})

    def _on_job_error(self, message: str) -> None:
        self._busy = False
        self._stop_event = None
        self.busy_changed.emit(False)
        self.failed.emit(message)

    def _on_login_event(self, kind: str, value) -> None:
        if kind == "code_sent":
            self.login_code_sent.emit(value)
        elif kind == "need_password":
            self.login_need_password.emit()
        elif kind == "ok":
            self.login_ok.emit(value)
        elif kind == "error":
            self.login_failed.emit(value)
        elif kind == "cancelled":
            self.login_cancelled.emit()

    # ── публичный API заданий ───────────────────────────────────────────────
    @property
    def busy(self) -> bool:
        return self._busy

    def scan(self, cfg: dict, options: "em.Options") -> bool:
        return self._submit(
            "scan",
            lambda se: em.scan_preview(cfg, options, reporter=self._reporter, stop_event=se))

    def start_export(self, cfg: dict, options: "em.Options") -> bool:
        return self._submit(
            "run",
            lambda se: em.run_export(cfg, options, reporter=self._reporter,
                                     stop_event=se, manage_signals=False))

    def start_upload(self, cfg: dict, folder: str, chat, options) -> bool:
        import uploader
        return self._submit(
            "run",
            lambda se: uploader.run_upload(cfg, folder, chat, options,
                                           reporter=self._reporter, stop_event=se,
                                           manage_signals=False))

    def _submit(self, kind: str, make_coro) -> bool:
        """make_coro(stop_event) -> корутина движка. Возвращает False, если занят."""
        if self._busy:
            return False
        self._busy = True
        self.busy_changed.emit(True)
        self._spawn(self._job(kind, make_coro))
        return True

    async def _job(self, kind: str, make_coro) -> None:
        # stop_event создаём ВНУТРИ loop движка — иначе он привяжется к чужому циклу.
        self._stop_event = asyncio.Event()
        try:
            result = await make_coro(self._stop_event)
            self._q.put(("__done__", (kind, result)))
        except em.ExportError as e:
            self._q.put(("__error__", (str(e),)))
        except Exception as e:  # noqa: BLE001 — наружу отдаём как текст ошибки
            self._q.put(("__error__", (f"Непредвиденная ошибка: {e}",)))

    def stop(self) -> None:
        """Мягкая остановка текущего задания (дозагрузить начатое, новых не брать)."""
        if self._stop_event is not None:
            em.request_stop(self._loop, self._stop_event)

    # ── интерактивный логин ────────────────────────────────────────────────
    def login(self, cfg: dict) -> None:
        """Подключиться и при необходимости запросить код. Идемпотентно: если сессия
        уже авторизована — сразу login_ok."""
        self._spawn(self._auth(cfg))

    def submit_code(self, code: str) -> None:
        self._put_auth(("code", code))

    def submit_password(self, pwd: str) -> None:
        self._put_auth(("password", pwd))

    def resend_code(self) -> None:
        self._put_auth(("resend", None))

    def cancel_login(self) -> None:
        self._put_auth(("cancel", None))

    def _put_auth(self, item) -> None:
        """Положить ввод пользователя в asyncio.Queue логина (из GUI-потока)."""
        inbox = self._auth_inbox
        if inbox is not None:
            self._loop.call_soon_threadsafe(inbox.put_nowait, item)

    async def _auth(self, cfg: dict) -> None:
        from pyrogram import Client
        from pyrogram.errors import (
            SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired, FloodWait,
        )
        from .i18n import t

        # SentCodeType → a human "where the code went" clause (shown after "Code sent — ").
        _DELIVERY = {
            "APP": "delivery_app", "SMS": "delivery_sms", "CALL": "delivery_call",
            "MISSED_CALL": "delivery_missed_call", "FLASH_CALL": "delivery_flash_call",
            "FRAGMENT_SMS": "delivery_fragment", "EMAIL_CODE": "delivery_email",
        }

        def describe(sent) -> str:
            name = str(sent.type).split(".")[-1] if sent.type else ""
            key = _DELIVERY.get(name)
            return t(key) if key else t("delivery_other", type=name or "?")

        self._auth_inbox = asyncio.Queue()
        inbox = self._auth_inbox
        app = Client(em.SESSION_NAME, api_id=cfg["api_id"], api_hash=cfg["api_hash"],
                     workdir=em.DATA_DIR)
        try:
            await app.connect()
            # Уже авторизованы? get_me() вернёт пользователя без запроса кода.
            try:
                me = await app.get_me()
                self._q.put(("__login__", ("ok", self._greet(me))))
                return
            except Exception:
                pass  # не авторизованы — идём за кодом

            sent = await app.send_code(cfg["phone"])
            self._q.put(("__login__", ("code_sent", describe(sent))))

            while True:
                action, value = await inbox.get()
                if action == "cancel":
                    self._q.put(("__login__", ("cancelled", None)))
                    return
                if action == "resend":
                    # A failed resend must NOT abort login: the first code may already be
                    # in the user's Telegram app. Telegram returns 406 SEND_CODE_UNAVAILABLE
                    # for API logins (it won't escalate to SMS); keep the loop alive so the
                    # user can still enter the code from their app.
                    try:
                        sent = await app.resend_code(cfg["phone"], sent.phone_code_hash)
                        self._q.put(("__login__", ("code_sent", describe(sent))))
                    except FloodWait as e:
                        self._q.put(("__login__", ("error", t("login_flood", sec=e.value))))
                    except Exception as e:  # noqa: BLE001
                        key = ("login_resend_unavailable"
                               if "SEND_CODE_UNAVAILABLE" in str(e)
                               else "login_resend_failed")
                        self._q.put(("__login__", ("error", t(key, err=e))))
                    continue
                if action == "password":
                    try:
                        await app.check_password(value)
                        me = await app.get_me()
                        self._q.put(("__login__", ("ok", self._greet(me))))
                        return
                    except Exception as e:  # noqa: BLE001
                        self._q.put(("__login__", ("error", t("login_bad_password", err=e))))
                    continue
                if action == "code":
                    code = (value or "").replace(" ", "")
                    if not code.isdigit():
                        self._q.put(("__login__", ("error", t("login_code_digits"))))
                        continue
                    try:
                        await app.sign_in(cfg["phone"], sent.phone_code_hash, code)
                    except PhoneCodeInvalid:
                        self._q.put(("__login__", ("error", t("login_wrong_code"))))
                        continue
                    except PhoneCodeExpired:
                        self._q.put(("__login__", ("error", t("login_code_expired"))))
                        continue
                    except SessionPasswordNeeded:
                        self._q.put(("__login__", ("need_password", None)))
                        continue
                    me = await app.get_me()
                    self._q.put(("__login__", ("ok", self._greet(me))))
                    return
        except Exception as e:  # noqa: BLE001
            self._q.put(("__login__", ("error", t("login_conn_fail", err=e))))
        finally:
            self._auth_inbox = None
            try:
                await app.disconnect()
            except Exception:
                pass

    @staticmethod
    def _greet(me) -> str:
        uname = f" @{me.username}" if getattr(me, "username", None) else ""
        return f"{me.first_name}{uname} (id={me.id})"

    # ── завершение работы ──
    def shutdown(self) -> None:
        self._timer.stop()
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)

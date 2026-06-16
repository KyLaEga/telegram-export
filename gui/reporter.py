"""QtReporter — сторона ДВИЖКА (фоновый поток).

Каждый метод-событие складывает кортеж `(name, args)` в потокобезопасную очередь.
GUI-сторона (`EngineController`) дренирует её по таймеру и переизлучает Qt-сигналы.

ВАЖНО: здесь НЕЛЬЗЯ трогать ни один Qt-виджет — мы в чужом потоке. Только queue.put().
Сигнатуры методов 1:1 повторяют `export_media.Reporter` (см. там docstring каждого).
"""

from __future__ import annotations

import queue

import export_media as em


class QtReporter(em.Reporter):
    def __init__(self, q: "queue.Queue") -> None:
        self._q = q

    def _emit(self, name: str, *args) -> None:
        self._q.put((name, args))

    # ── текстовые статусы ──
    def status(self, text: str = "") -> None:
        self._emit("status", text)

    def status_inline(self, text: str) -> None:
        self._emit("status_inline", text)

    def status_clear(self) -> None:
        self._emit("status_clear")

    # ── живой прогресс одного файла ──
    # Составной сигнал прогресса: worker (ID потока из progress_args) + current + total.
    # По worker дашборд находит индивидуальную полосу в Multi-Worker View.
    def file_progress(self, worker, msg_id, current, total, speed) -> None:
        self._emit("file_progress", worker, msg_id, current, total, speed)

    def progress_done(self, worker, msg_id) -> None:
        self._emit("progress_done", worker, msg_id)

    # ── жизненный цикл файла ──
    def file_started(self, worker, msg_id, fn, size) -> None:
        self._emit("file_started", worker, msg_id, fn, size)

    def file_done(self, worker, msg_id, size, speed) -> None:
        self._emit("file_done", worker, msg_id, size, speed)

    def file_skipped(self, msg_id) -> None:
        self._emit("file_skipped", msg_id)

    def file_dedup(self, msg_id, dup_fn) -> None:
        self._emit("file_dedup", msg_id, dup_fn)

    def file_repair_oversize(self, msg_id) -> None:
        self._emit("file_repair_oversize", msg_id)

    def file_failed(self, msg_id, retries, fn) -> None:
        self._emit("file_failed", msg_id, retries, fn)

    # ── структурные данные для UI ──
    def plan_preview(self, summary: dict) -> None:
        self._emit("plan_preview", summary)

    def stats(self, snapshot: dict) -> None:
        self._emit("stats", snapshot)

    def finished(self, summary: str, stopped: bool) -> None:
        self._emit("finished", summary, stopped)

    # ── Upload Pipeline ──
    def upload_started(self, name, size) -> None:
        self._emit("upload_started", name, size)

    def upload_progress(self, name, current, total, speed) -> None:
        self._emit("upload_progress", name, current, total, speed)

    def upload_done(self, name, size, speed, message_id) -> None:
        self._emit("upload_done", name, size, speed, message_id)

    def upload_skipped(self, name) -> None:
        self._emit("upload_skipped", name)

    def upload_failed(self, name, err) -> None:
        self._emit("upload_failed", name, err)

    def upload_preview(self, summary: dict) -> None:
        self._emit("upload_preview", summary)

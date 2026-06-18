"""Screen 4 — Download dashboard.

Subscribes to the single `EngineController.event(name, args)` stream (engine events
arrive from the thread-safe QtReporter queue, drained by a QTimer → widgets are
touched only in the GUI thread).

Shows:
  • plan/progress counters — ✅ Downloaded / 📥 Remaining / 🟢 Already on disk /
    ♻️ Duplicates / 🔧 Repaired / ❌ Errors. The source of "Remaining" and "Already on
    disk" is `plan_preview` (need_files / have_files), not runtime `failed`, so a
    missing on-disk file does NOT look like an error;
  • a grid of active workers — one progress bar per thread (w1, w2, …), each with its
    own file name and speed;
  • an operations log — only live events (done/repair/network errors); the big static
    "EXPORT PREVIEW" block from the previous step is filtered out.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar,
    QPushButton, QVBoxLayout, QWidget,
)

from .. import theme
from ..i18n import t
from ._fmt import elide_middle, fmt_speed, human_size

# Status-line substrings belonging to the static preview/decor from the previous
# screen — kept out of the download log (see export_media.run_export).
_PREVIEW_NOISE = (
    "─", "ПРЕДПРОСМОТР", "PREVIEW", "Всего медиафайлов", "Уже на диске", "К ЗАГРУЗКЕ",
    "Оценка времени", "средняя", "пиковая", "воркера(ов) @", "Всё уже скачано",
)


class WorkerRow(QWidget):
    """One active-worker bar: tag+file, progress, speed."""

    def __init__(self, worker: str) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(theme.SPACING)
        self.name = QLabel(t("worker_wait_file", w=worker))
        self.name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.name.setStyleSheet(
            "font-family:'SF Mono','Menlo','Courier New',monospace;font-size:11px;")
        self.speed = QLabel("—")
        self.speed.setStyleSheet(f"font-weight:600;color:{theme.SUCCESS};")
        head.addWidget(self.name, 1)
        head.addWidget(self.speed, 0)
        lay.addLayout(head)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        lay.addWidget(self.bar)

    def set_file(self, worker: str, msg_id, fn: str, size: int) -> None:
        # Long names are middle-elided — otherwise the row stretches on a narrow
        # window; the full name stays available in the tooltip.
        self.name.setText(f"[{worker}] msg {msg_id} → {elide_middle(fn, 56)}  "
                          f"({human_size(size)})")
        self.name.setToolTip(fn)
        self.bar.setValue(0)
        self.speed.setText("…")

    def set_progress(self, current: int, total: int, speed: float) -> None:
        self.bar.setValue(int(1000 * current / total) if total else 0)
        self.speed.setText(fmt_speed(speed))

    def set_done(self, worker: str) -> None:
        self.bar.setValue(1000)
        self.name.setText(t("worker_done", w=worker))
        self.speed.setText("—")


class DownloadDashboard(QWidget):
    finished = Signal(dict)   # forward totals upward (for the reports/back screen)
    back = Signal()

    def __init__(self, controller, parent=None) -> None:
        super().__init__(parent)
        # Screen safety floor (anti-collapse): props up minimumSizeHint of the
        # QStackedWidget so the Multi-Worker View and log don't collapse vertically.
        self.setMinimumSize(760, 480)
        self._ctl = controller
        self._need = 0          # plan: files to download (need_files)
        self._have = 0          # plan: already on disk (have_files)
        self._rows: "dict[str, WorkerRow]" = {}   # worker_id → its own bar
        self._build()
        # Single engine event channel. Filtered by name in _on_event.
        self._ctl.event.connect(self._on_event)
        self._ctl.run_done.connect(self._on_run_done)
        self._ctl.failed.connect(self._on_failed)
        self._ctl.busy_changed.connect(self._on_busy)

    # ── layout ──────────────────────────────────────────────────────────────
    def _build(self) -> None:
        root = theme.page_layout(self)
        self.title = theme.title_label(t("dl_title"))
        root.addWidget(self.title)

        # ── status counters (plan + progress) ──
        self.cards_box, cards_lay = theme.section(t("sec_counters"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(theme.SPACING)
        grid.setVerticalSpacing(theme.SPACING)
        self.card_done = theme.StatCard(t("card_downloaded"), theme.SUCCESS)
        self.card_remaining = theme.StatCard(t("card_remaining"), theme.PRIMARY)
        self.card_on_disk = theme.StatCard(t("card_on_disk"), theme.TEAL)
        self.card_dedup = theme.StatCard(t("card_dedup"), theme.INFO)
        self.card_repaired = theme.StatCard(t("card_repaired"), theme.PURPLE)
        self.card_failed = theme.StatCard(t("card_failed"), theme.DANGER)
        cells = [self.card_done, self.card_remaining, self.card_on_disk,
                 self.card_dedup, self.card_repaired, self.card_failed]
        for i, card in enumerate(cells):
            grid.addWidget(card, i // 3, i % 3)
        cards_lay.addLayout(grid)
        root.addWidget(self.cards_box)

        # ── active-workers grid (dynamic progress bars) ──
        self.workers_box, self.workers_lay = theme.section(t("sec_active_workers"))
        self.workers_hint = QLabel(t("workers_waiting"))
        self.workers_hint.setWordWrap(True)
        self.workers_hint.setStyleSheet(f"color:{theme.TEXT_MUTED};")
        self.workers_lay.addWidget(self.workers_hint)
        root.addWidget(self.workers_box)

        # ── operations log ──
        self.log_header = QLabel(t("log_header_dl"))
        self.log_header.setStyleSheet("font-weight: 700;")
        root.addWidget(self.log_header)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)   # don't grow memory forever
        self.log.setStyleSheet(theme.CONSOLE_QSS)
        root.addWidget(self.log, 1)

        # ── actions ──
        actions = QHBoxLayout()
        actions.setSpacing(theme.SPACING)
        self.back_btn = QPushButton()
        self.back_btn.clicked.connect(self.back.emit)
        self.stop_btn = QPushButton()
        self.stop_btn.setProperty("danger", True)
        self.stop_btn.clicked.connect(self._on_stop)
        actions.addWidget(self.back_btn)
        actions.addStretch(1)
        actions.addWidget(self.stop_btn)
        root.addLayout(actions)

        self.retranslate()

    def retranslate(self) -> None:
        self.title.setText(t("dl_title"))
        self.cards_box.setTitle(t("sec_counters"))
        self.card_done.set_caption(t("card_downloaded"))
        self.card_remaining.set_caption(t("card_remaining"))
        self.card_on_disk.set_caption(t("card_on_disk"))
        self.card_dedup.set_caption(t("card_dedup"))
        self.card_repaired.set_caption(t("card_repaired"))
        self.card_failed.set_caption(t("card_failed"))
        self.workers_box.setTitle(t("sec_active_workers"))
        self.workers_hint.setText(t("workers_waiting"))
        self.log_header.setText(t("log_header_dl"))
        self.back_btn.setText(t("nav_back_params"))
        self.stop_btn.setText(t("btn_stop"))

    # ── run lifecycle ───────────────────────────────────────────────────────────
    def reset(self) -> None:
        """Reset before a new run (called by MainWindow on entering the screen)."""
        self._need = self._have = 0
        self._clear_workers()
        for card in (self.card_done, self.card_remaining, self.card_on_disk,
                     self.card_dedup, self.card_repaired, self.card_failed):
            card.set_value(0)
        self.log.clear()

    def _clear_workers(self) -> None:
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self.workers_hint.show()

    def _worker_row(self, worker: str) -> WorkerRow:
        """Lazily create a bar for a worker the first time it appears."""
        row = self._rows.get(worker)
        if row is None:
            self.workers_hint.hide()
            row = WorkerRow(worker)
            self._rows[worker] = row
            # Keep rows in a stable order w1, w2, …
            for w in sorted(self._rows, key=self._worker_key):
                self.workers_lay.removeWidget(self._rows[w])
            for w in sorted(self._rows, key=self._worker_key):
                self.workers_lay.addWidget(self._rows[w])
        return row

    @staticmethod
    def _worker_key(name: str):
        digits = "".join(ch for ch in name if ch.isdigit())
        return (int(digits) if digits else 0, name)

    def _on_stop(self) -> None:
        self.stop_btn.setEnabled(False)
        self._append(t("dash_stop_soft"))
        self._ctl.stop()

    def _on_busy(self, busy: bool) -> None:
        self.stop_btn.setEnabled(busy)
        self.back_btn.setEnabled(not busy)

    # ── engine events ───────────────────────────────────────────────────────────
    def _on_event(self, name: str, args: tuple) -> None:
        # The event channel is shared across screens — react only when we're active,
        # otherwise the dashboard would log another job's events (e.g. an upload).
        if not self.isVisible():
            return
        if name == "plan_preview":
            self._apply_plan(args[0])
        elif name == "file_started":
            worker, msg_id, fn, size = args
            self._worker_row(worker).set_file(worker, msg_id, fn, size)
        elif name == "file_progress":
            worker, msg_id, current, total, speed = args
            if worker is not None:
                self._worker_row(worker).set_progress(current, total, speed)
        elif name == "progress_done":
            worker, msg_id = args
            if worker is not None and worker in self._rows:
                self._rows[worker].bar.setValue(1000)
        elif name == "file_done":
            worker, msg_id, size, speed = args
            if worker in self._rows:
                self._rows[worker].set_done(worker)
            self._append(t("dash_file_done", w=worker, msg=msg_id,
                           size=human_size(size), speed=fmt_speed(speed)))
        elif name == "file_skipped":
            # Already on disk (COMPLETE) — counters updated by stats; don't spam the log.
            pass
        elif name == "file_dedup":
            msg_id, dup_fn = args
            self._append(t("dash_dedup", msg=msg_id, dup=dup_fn))
        elif name == "file_repair_oversize":
            (msg_id,) = args
            self._append(t("dash_repair", msg=msg_id))
        elif name == "file_failed":
            msg_id, retries, fn = args
            self._append(t("dash_file_failed", msg=msg_id, retries=retries, fn=fn))
        elif name == "stats":
            self._apply_stats(args[0])
        elif name in ("status", "status_inline"):
            self._log_status(args[0] if args else "")
        elif name == "finished":
            summary, stopped = args
            mark = t("dash_finished_stopped") if stopped else t("dash_finished_ok")
            self._append(t("dash_finished", mark=mark, summary=summary))

    def _apply_plan(self, plan: dict) -> None:
        """plan_preview: need_files (remaining) and have_files (already on disk)."""
        plan = plan or {}
        self._need = int(plan.get("need_files", 0))
        self._have = int(plan.get("have_files", 0))
        self.card_remaining.set_value(self._need)
        self.card_on_disk.set_value(self._have)

    def _apply_stats(self, snap: dict) -> None:
        downloaded = snap.get("downloaded", 0)
        self.card_done.set_value(downloaded)
        # "Remaining" shrinks as we download; "already on disk" — plan + runtime skips.
        self.card_remaining.set_value(max(0, self._need - downloaded))
        self.card_on_disk.set_value(max(self._have, snap.get("skipped", 0)))
        self.card_dedup.set_value(snap.get("dedup", 0))
        self.card_repaired.set_value(snap.get("repaired", 0))
        self.card_failed.set_value(snap.get("failed", 0))

    def _log_status(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        # Static-preview filter — only live events in the log.
        if any(token in text for token in _PREVIEW_NOISE):
            return
        self._append(text)

    def _on_run_done(self, result: dict) -> None:
        if not self.isVisible():
            return
        self.stop_btn.setEnabled(False)
        stats = (result or {}).get("stats")
        if stats:
            self._apply_stats(stats)
        self._append(t("dash_run_done"))
        self.finished.emit(result or {})

    def _on_failed(self, message: str) -> None:
        if not self.isVisible():
            return
        self.stop_btn.setEnabled(False)
        self._append(t("err_prefix", message=message))

    def _append(self, line: str) -> None:
        self.log.appendPlainText(line)
        self.log.moveCursor(QTextCursor.End)

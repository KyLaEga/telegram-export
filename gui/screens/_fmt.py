"""Маленькие форматтеры для экранов (МБ/с, размеры, длительность медиа).

Тяжёлую логику не дублируем — числовые хелперы берём из движка (`export_media`),
здесь только то, чего там нет (например mm:ss для колонки длительности в ревью).
"""

from __future__ import annotations

import os

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from export_media import fmt_speed, human_gb, human_mb  # noqa: F401 (re-export)
from ..i18n import t


def open_path(path: str) -> bool:
    """Открыть файл/папку в системном приложении (Finder/просмотрщик). True — успех.

    Кросс-платформенно через Qt (QDesktopServices) — на macOS откроет Finder для папки
    или дефолтную программу для *.txt/*.log. Несуществующий путь → False (вызывающий
    покажет предупреждение, а не молчит)."""
    if not path or not os.path.exists(path):
        return False
    return QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(path)))


def reveal_in_finder(path: str) -> bool:
    """Показать файл/папку в Finder. Для файла открываем его родительский каталог."""
    if not path or not os.path.exists(path):
        return False
    target = path if os.path.isdir(path) else os.path.dirname(path)
    return open_path(target)


def elide_middle(text: str, limit: int = 64) -> str:
    """Усечь длинное имя файла по середине ('начало…хвост'), сохранив расширение видимым.

    QLabel сам не эллиптирует — поэтому длинные имена в строках воркеров режем здесь,
    иначе они распирают строку и ломают вёрстку на узком окне."""
    text = text or ""
    if len(text) <= limit:
        return text
    keep = limit - 1
    head = keep // 2
    tail = keep - head
    return f"{text[:head]}…{text[-tail:]}"


def human_size(n: int) -> str:
    """Human-readable size: KB / MB / GB — for tables and labels (localised units)."""
    n = int(n or 0)
    if n >= 1024 ** 3:
        return f"{human_gb(n):.2f} {t('unit_gb')}"
    if n >= 1024 ** 2:
        return f"{human_mb(n):.1f} {t('unit_mb')}"
    if n >= 1024:
        return f"{n / 1024:.0f} {t('unit_kb')}"
    return f"{n} {t('unit_b')}"


def media_duration(sec: int) -> str:
    """Длительность медиа как mm:ss / h:mm:ss (для колонки «Длительность»)."""
    sec = int(sec or 0)
    if sec <= 0:
        return "—"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

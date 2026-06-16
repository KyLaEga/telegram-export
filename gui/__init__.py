"""Native PySide6 GUI поверх проверенного движка export_media.py / uploader.py.

Архитектура моста Qt ↔ asyncio (Phase 1):
  • Движок (pyrogram/asyncio) крутится в ОТДЕЛЬНОМ фоновом потоке со своим event-loop.
  • Любое событие движка идёт через `QtReporter` → потокобезопасная `queue.Queue`.
  • `EngineController` (живёт в GUI-потоке) дренирует очередь по `QTimer` (~20 Гц) и
    переизлучает Qt-сигналы. Виджеты Qt НИКОГДА не трогаются из потока движка.
"""

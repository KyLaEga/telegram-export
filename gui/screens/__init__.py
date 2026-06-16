"""Экраны мастера (QStackedWidget).

Phase 1: login.
Phase 2: options (параметры + «Скачать всё») → review (ревью дублей/losers) →
dashboard (прогресс загрузки) → upload (Upload Pipeline). Навигацию координирует
MainWindow в gui/app.py; экраны общаются с движком через сигналы EngineController.
"""

"""Localisation engine for the GUI (English default + Russian).

A tiny singleton translation dispatcher modelled on the proven design used in the
sibling TensorMedia app: one ``QObject`` holds the active language, exposes a
``language_changed`` signal, and persists the choice via ``QSettings`` so the UI
opens in the language the user last picked.

Screens never hard-code user-facing text — they pull it through the module-level
``t(key, **fmt)`` helper. On a language switch ``MainWindow`` calls each screen's
``retranslate()`` so every label/button/tooltip is re-rendered live.

Keys are short and grouped by screen with comments; an unknown key degrades to the
key itself (so a missing translation is visible, never a crash).
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QSettings, Signal


class TranslationEngine(QObject):
    """Process-wide singleton holding the active language."""

    language_changed = Signal()
    _instance: "TranslationEngine | None" = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls, *args, **kwargs)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        super().__init__()
        self._initialized = True

        self.settings = QSettings("TelegramExport", "GUI")
        saved = self.settings.value("language", "en")
        self.current_lang = saved if saved in ("en", "ru") else "en"
        self.dictionaries = _DICTIONARIES

    # ── lookup ──────────────────────────────────────────────────────────────
    def tr(self, key: str, **fmt) -> str:
        table = self.dictionaries.get(self.current_lang, self.dictionaries["en"])
        text = table.get(key)
        if text is None:
            # Fall back to English, then to the raw key — never raise.
            text = self.dictionaries["en"].get(key, key)
        if fmt:
            try:
                return text.format(**fmt)
            except (KeyError, IndexError, ValueError):
                return text
        return text

    def set_language(self, lang_code: str) -> None:
        if lang_code in self.dictionaries and lang_code != self.current_lang:
            self.current_lang = lang_code
            self.settings.setValue("language", lang_code)
            self.language_changed.emit()


# ════════════════════════════════════════════════════════════════════════════
# Dictionaries. EN is the source of truth (default language); RU mirrors it.
# ════════════════════════════════════════════════════════════════════════════
_EN = {
    # ── app chrome / top bar ──
    "app_title": "Telegram Export",
    "topbar_theme": "Theme:",
    "topbar_lang": "Language:",
    "theme_dark": "Dark",
    "theme_light": "Light",
    "lang_en": "English",
    "lang_ru": "Русский",

    # ── shared ──
    "sec_config": "Configuration",
    "sec_processing": "Processing options",
    "btn_browse": "Browse…",
    "nav_back": "← Back",
    "nav_back_params": "← To settings",
    "warn_title": "Warning",

    # ── units (formatters) ──
    "unit_gb": "GB",
    "unit_mb": "MB",
    "unit_kb": "KB",
    "unit_b": "B",
    "unit_speed": "KB/s",
    "unit_speed_mb": "MB/s",

    # ── login ──
    "login_title": "Telegram Export — Sign in",
    "lbl_api_id": "API ID:",
    "lbl_api_hash": "API Hash:",
    "lbl_phone": "Phone:",
    "lbl_channel": "Channel:",
    "lbl_dest": "SSD folder:",
    "ph_api_id": "e.g. 12345678",
    "ph_api_hash": "from my.telegram.org",
    "ph_phone": "+1 555 123 4567",
    "ph_channel": "@username, t.me/… or id",
    "ph_dest": "/Volumes/SSD/telegram_export",
    "volumes_placeholder": "Volumes…",
    "btn_save": "Save",
    "btn_login": "Sign in to Telegram",
    "ph_code": "code from Telegram / SMS",
    "ph_password": "cloud password (2FA)",
    "btn_confirm": "Confirm",
    "btn_resend": "Resend",
    "btn_cancel": "Cancel",
    "dlg_dest_title": "Destination folder",
    "msg_fill_fields": "Please fill in: {fields}",
    "msg_api_id_numeric": "API ID must be a number.",
    "msg_config_saved": "Configuration saved: {path}",
    "msg_connecting": "Connecting to Telegram…",
    "msg_code_sent": "Code sent — {descr}",
    "msg_need_password": "2FA is enabled — enter your cloud password.",
    "msg_login_ok": "✅ Signed in: {greet}",
    "msg_login_cancelled": "Login cancelled.",

    # ── options ──
    "opt_title": "Export settings",
    "cb_download_all": "Download everything (with duplicate review before start)",
    "tip_download_all": (
        "When enabled, the app first scans the channel and shows a review screen\n"
        "of skipped duplicates / worse versions — there you can bring needed files\n"
        "back into the plan. Disabled — the download starts right from the current index."),
    "hint_download_all": (
        "Plan-review mode before downloading: handy when you need to bring individual "
        "skipped versions back. Otherwise the export starts immediately."),
    "cb_keep_raw_video": "Keep video unprocessed (Fast, but no streaming)",
    "tip_keep_raw_video": (
        "Normally the app lightly repackages video (faststart) so spacebar preview in "
        "macOS and seeking in the player work instantly, without buffering. Tick this to "
        "keep video exactly as in Telegram — slightly faster download, but preview and "
        "instant seeking may not work."),
    "cb_dedup": "Skip byte-identical files (Deduplication)",
    "tip_dedup": (
        "If the same file was posted to the channel several times, it is downloaded only "
        "once, protecting the SSD from clutter. A match is determined by name and size "
        "(and, for video, also by duration)."),
    "cb_quality": "Automatically pick the highest resolution and bitrate",
    "tip_quality": (
        "When the same video exists in several qualities, the app keeps the best version "
        "(higher resolution and bitrate) and skips the compressed copies. Untick only "
        "for debugging."),
    "hint_processing": (
        "Deduplication and best-quality selection save disk space by filtering out repeats "
        "and compressed copies. Unprocessed video is the fastest, 'raw' mode."),
    "sec_threads": "Threads and speed",
    "cb_fast": "Multi-threaded download (several connections per file)",
    "tip_fast": "Download large files over several TCP connections at once (FastTelethon).",
    "tip_workers": (
        "How many files to download at one time. More is faster, but puts more load on the "
        "network and raises the risk that Telegram temporarily throttles the speed."),
    "tip_connections": (
        "How many parallel streams a single large file's download is split into. Speeds up "
        "big videos; makes no difference for small files."),
    "lbl_workers": "How many files to download at once:",
    "lbl_conns": "Download streams per file:",
    "hint_threads": "Recommended for a rate-limited account: 1 worker × 4 connections.",
    "warn_conn": "Total {total} conn. to DC ({workers} worker(s) × {per} conn.).",
    "warn_conn_high": " Telegram may start throttling — keep ≤ 6.",
    "warn_conn_ok": " Gentle mode, within limits.",
    "btn_upload_panel": "Upload panel →",
    "btn_run_export": "Start export",
    "status_scanning": "Scanning channel…",
    "status_starting": "Starting download…",

    # ── review ──
    "review_title": "Duplicate review — what to bring back",
    "col_check": "✓",
    "col_msg_id": "msg_id",
    "col_name": "File name",
    "col_size": "Size",
    "col_duration": "Duration",
    "col_reason": "Skip reason",
    "btn_select_all": "Select all",
    "btn_select_none": "Deselect all",
    "lbl_selected": "Selected: {n}",
    "btn_apply_run": "Apply selection and start download",
    "review_summary": (
        "{need} file(s) already selected for the plan. Below are {n} skipped "
        "duplicates / worse versions. Tick the ones you also want to download."),
    "review_summary_none": "  No duplicates found — you can start as is.",
    "reason_worse": "worse version (master msg {id})",
    "reason_dup": "duplicate",
    "review_scanning": "Scanning the channel and building the plan… please wait for the preview.",
    "review_error": "Scan error: {msg}",

    # ── dashboard ──
    "dl_title": "Download",
    "sec_counters": "Counters",
    "card_downloaded": "✅ Downloaded",
    "card_remaining": "📥 Remaining",
    "card_on_disk": "🟢 Already on disk (Skipped)",
    "card_dedup": "♻️ Duplicates",
    "card_repaired": "🔧 Repaired",
    "card_failed": "❌ Errors",
    "sec_active_workers": "Active threads",
    "workers_waiting": "Waiting for the download to start…",
    "log_header_dl": "📋 Download log (events and network errors):",
    "btn_stop": "Stop",
    "worker_wait_file": "[{w}] waiting for a file…",
    "worker_done": "[{w}] ✓ done, waiting for next…",
    "dash_stop_soft": "⏹ Soft stop requested — finishing current downloads, taking no new ones…",
    "dash_file_done": "[{w}] msg {msg} done ({size}, {speed})",
    "dash_dedup": "♻️ msg {msg} duplicate → {dup}",
    "dash_repair": "🔧 msg {msg} bigger than master — re-downloading",
    "dash_file_failed": "❌ msg {msg}: not completed after {retries} attempts ({fn})",
    "dash_finished_stopped": "⚠️ stopped early",
    "dash_finished_ok": "✅ finished",
    "dash_finished": "{mark}: {summary}",
    "dash_run_done": "— run finished —",
    "err_prefix": "🛑 ERROR: {message}",

    # ── upload ──
    "up_title": "Upload (Upload Pipeline)",
    "up_idle": "⚪ Waiting to start the upload pipeline…",
    "lbl_src_folder": "Source folder:",
    "lbl_target": "Target channel:",
    "ph_target": "@username, t.me/… or id (--target)",
    "cb_skip_uploaded": "Skip already-uploaded (checks upload_state.db)",
    "tip_skip_uploaded": (
        "Before sending, checks the file against the upload_state.db by path and hash.\n"
        "If it was already uploaded to this channel — it is not sent again.\n"
        "Lets you safely restart the pipeline from the same place."),
    "cb_recursive": "Recurse into subfolders (including nested folders)",
    "tip_recursive": (
        "Descend into every nested folder of the source directory and send files from them —\n"
        "handy when the export is split into subfolders. Disabled — only top-level files of\n"
        "the chosen folder are taken, and nested directories are ignored."),
    "cb_native": "Send as native media (video with preview)",
    "tip_native": (
        "Video goes via send_video with a thumbnail, photo as a photo:\n"
        "they play natively in the channel. Disabled — everything is sent as a document file\n"
        "(no preview, but no metadata re-encoding)."),
    "cb_caption": "Alphabetical order + file name in caption",
    "tip_caption": (
        "Files are sent strictly in alphabetical order, and the file name is put in the "
        "caption —\nso the channel keeps its order and messages stay indexable."),
    "sec_current_upload": "Current upload",
    "up_counts": "sent: {sent} · skipped: {skipped} · errors: {failed}",
    "log_header_up": "📋 Upload operation log:",
    "btn_start_upload": "Start upload",
    "btn_stop_upload": "Stop",
    "dlg_upload_folder": "Folder to upload",
    "up_need_folder": "⚠️ Specify a source folder.",
    "up_busy": "⚠️ The engine is busy with another job — please wait for it to finish.",
    "up_start_status": "🚀 Starting the upload pipeline…",
    "up_start_log": "▶ Upload start: {folder} → {target}",
    "up_stopping": "⏹ Stopping the pipeline…",
    "up_stop_requested": "⏹ Upload stop requested…",
    "up_sending": "🚀 Sending file: {fname}",
    "up_done_line": "✓ {fname} ({size}, {speed}) → msg {msg}",
    "up_skipped_line": "· {fname} — already uploaded, skipped",
    "up_failed_line": "❌ {fname}: {err}",
    "up_preview": "Found to send: {n} file(s) in “{chat}”.",
    "up_finished": "✅ Upload finished · sent {sent}, skipped {skipped}, errors {failed}",
    "up_finished_log": "— upload finished —",
    "up_failed_status": "🛑 Pipeline stopped with an error",

    # ── reports ──
    "rep_title": "Reports and summary",
    "rep_not_done": "Run not finished yet.",
    "sec_dl_summary": "Download summary",
    "card_already": "🟢 Already present",
    "sec_dest": "Destination folder",
    "btn_open_folder": "📂 Open folder",
    "btn_reveal": "🔎 Show in Finder",
    "sec_text_reports": "Text reports",
    "rep_none_short": "No reports yet.",
    "rep_none_long": (
        "No text reports in the folder (they appear after an export with "
        "dedup/quality or a --verify audit)."),
    "btn_open": "Open",
    "btn_new_export": "🔄 New export",
    "rep_done": "✅ Run finished · downloaded {n} file(s) ({gb:.2f} GB), {failed} errors.",
    "rep_stopped": "⚠️ Run stopped early · downloaded {n} file(s) ({gb:.2f} GB), {failed} errors.",
    "rep_open_folder_fail": "⚠️ Couldn't open the folder — path is unavailable.",
    "rep_reveal_fail": "⚠️ Couldn't reveal the folder in Finder.",
    "rep_open_file_fail": "⚠️ Couldn't open the file: {path}",
    "report_export_title": "📄 Export log",
    "report_export_desc": "Full chronology of the run (events, network, FS).",
    "report_quality_title": "🏆 Quality report",
    "report_quality_desc": "Which duplicate versions were skipped and in favour of which master.",
    "report_verify_title": "🔍 Audit report",
    "report_verify_desc": "Result of reconciling on-disk files with the plan (--verify mode).",
    "report_error_title": "⚠️ Error log",
    "report_error_desc": "Quiet log of critical errors and skips by msg_id.",
}

_RU = {
    # ── app chrome / top bar ──
    "app_title": "Telegram Export",
    "topbar_theme": "Тема:",
    "topbar_lang": "Язык:",
    "theme_dark": "Тёмная",
    "theme_light": "Светлая",
    "lang_en": "English",
    "lang_ru": "Русский",

    # ── shared ──
    "sec_config": "Конфигурация",
    "sec_processing": "Параметры обработки",
    "btn_browse": "Обзор…",
    "nav_back": "← Назад",
    "nav_back_params": "← К параметрам",
    "warn_title": "Внимание",

    # ── units (formatters) ──
    "unit_gb": "ГБ",
    "unit_mb": "МБ",
    "unit_kb": "КБ",
    "unit_b": "Б",
    "unit_speed": "КБ/с",
    "unit_speed_mb": "МБ/с",

    # ── login ──
    "login_title": "Telegram Export — вход",
    "lbl_api_id": "API ID:",
    "lbl_api_hash": "API Hash:",
    "lbl_phone": "Телефон:",
    "lbl_channel": "Канал:",
    "lbl_dest": "Папка на SSD:",
    "ph_api_id": "например 12345678",
    "ph_api_hash": "из my.telegram.org",
    "ph_phone": "+79991234567",
    "ph_channel": "@username, t.me/… или id",
    "ph_dest": "/Volumes/SSD/telegram_export",
    "volumes_placeholder": "Тома…",
    "btn_save": "Сохранить",
    "btn_login": "Войти в Telegram",
    "ph_code": "код из Telegram / SMS",
    "ph_password": "облачный пароль (2FA)",
    "btn_confirm": "Подтвердить",
    "btn_resend": "Переотправить",
    "btn_cancel": "Отмена",
    "dlg_dest_title": "Папка назначения",
    "msg_fill_fields": "Заполните поля: {fields}",
    "msg_api_id_numeric": "API ID должен быть числом.",
    "msg_config_saved": "Конфигурация сохранена: {path}",
    "msg_connecting": "Подключаюсь к Telegram…",
    "msg_code_sent": "Код отправлен — {descr}",
    "msg_need_password": "Включена 2FA — введите облачный пароль.",
    "msg_login_ok": "✅ Вход выполнен: {greet}",
    "msg_login_cancelled": "Логин отменён.",

    # ── options ──
    "opt_title": "Параметры экспорта",
    "cb_download_all": "Скачать всё (с ревью дублей перед стартом)",
    "tip_download_all": (
        "При включении приложение сперва просканирует канал и покажет экран ревью\n"
        "пропускаемых дублей/худших версий — там можно вернуть нужные файлы в план.\n"
        "Выключено — загрузка стартует сразу по текущему индексу."),
    "hint_download_all": (
        "Режим обзора плана перед загрузкой: удобно, когда нужно вернуть отдельные "
        "пропускаемые версии. Иначе экспорт начинается немедленно."),
    "cb_keep_raw_video": "Сохранять видео без обработки (Быстро, но без стриминга)",
    "tip_keep_raw_video": (
        "Обычно приложение слегка переупаковывает видео (faststart), чтобы превью по "
        "пробелу в macOS и перемотка в плеере работали сразу, без докачки. Поставьте "
        "галочку, чтобы сохранять видео ровно как в Telegram — загрузка чуть быстрее, "
        "но предпросмотр и мгновенная перемотка могут не работать."),
    "cb_dedup": "Пропускать абсолютно одинаковые файлы (Дедупликация)",
    "tip_dedup": (
        "Если один и тот же файл был скинут в канал несколько раз, скрипт скачает его "
        "только один раз, защищая SSD от мусора. Совпадение определяется по имени и "
        "размеру (а для видео — ещё и по длительности)."),
    "cb_quality": "Автоматически выбирать максимальное разрешение и битрейт",
    "tip_quality": (
        "Когда одно и то же видео есть в нескольких качествах, приложение само оставит "
        "самую качественную версию (больше разрешение и битрейт) и пропустит сжатые "
        "копии. Снимайте галочку только для отладки."),
    "hint_processing": (
        "Дедупликация и выбор лучшего качества берегут место на диске, отсеивая "
        "повторы и сжатые копии. Без обработки видео — самый быстрый, но «сырой» режим."),
    "sec_threads": "Потоки и скорость",
    "cb_fast": "Многопоточная загрузка (несколько соединений на файл)",
    "tip_fast": "Качать крупные файлы в несколько TCP-соединений одновременно (FastTelethon).",
    "tip_workers": (
        "Сколько файлов скачивать в один момент времени. Больше — быстрее, но выше "
        "нагрузка на сеть и риск, что Telegram временно ограничит скорость."),
    "tip_connections": (
        "На сколько параллельных потоков дробится загрузка одного крупного файла. "
        "Ускоряет скачивание больших видео; для маленьких файлов значения не имеет."),
    "lbl_workers": "Сколько файлов скачивать одновременно:",
    "lbl_conns": "Потоков загрузки на каждый файл:",
    "hint_threads": "Рекомендация для лимитируемого аккаунта: 1 воркер × 4 соединения.",
    "warn_conn": "Суммарно {total} соед. к DC ({workers} воркер(ов) × {per} соед.).",
    "warn_conn_high": " Telegram может начать лимитировать скорость — держите ≤ 6.",
    "warn_conn_ok": " Щадящий режим, в пределах нормы.",
    "btn_upload_panel": "Панель отгрузки →",
    "btn_run_export": "Запустить экспорт",
    "status_scanning": "Сканирую канал…",
    "status_starting": "Запускаю загрузку…",

    # ── review ──
    "review_title": "Ревью дублей — что вернуть в загрузку",
    "col_check": "✓",
    "col_msg_id": "msg_id",
    "col_name": "Имя файла",
    "col_size": "Размер",
    "col_duration": "Длительность",
    "col_reason": "Причина пропуска",
    "btn_select_all": "Выбрать все",
    "btn_select_none": "Снять все",
    "lbl_selected": "Выбрано: {n}",
    "btn_apply_run": "Применить выбор и начать загрузку",
    "review_summary": (
        "В план уже отобрано {need} файл(ов). Ниже — {n} пропускаемых дублей/худших "
        "версий. Отметьте те, что нужно скачать дополнительно."),
    "review_summary_none": "  Дублей не найдено — можно запускать как есть.",
    "reason_worse": "худшая версия (мастер msg {id})",
    "reason_dup": "дубликат",
    "review_scanning": "Сканирую канал и строю план… дождитесь предпросмотра.",
    "review_error": "Ошибка скана: {msg}",

    # ── dashboard ──
    "dl_title": "Загрузка",
    "sec_counters": "Счётчики",
    "card_downloaded": "✅ Скачано",
    "card_remaining": "📥 Осталось загрузить",
    "card_on_disk": "🟢 Уже на диске (Пропущено)",
    "card_dedup": "♻️ Дубли",
    "card_repaired": "🔧 Починено",
    "card_failed": "❌ Ошибки",
    "sec_active_workers": "Активные потоки",
    "workers_waiting": "Ожидание запуска загрузки…",
    "log_header_dl": "📋 Журнал загрузки (события и сетевые сбои):",
    "btn_stop": "Остановить",
    "worker_wait_file": "[{w}] ожидание файла…",
    "worker_done": "[{w}] ✓ готово, ждёт следующий…",
    "dash_stop_soft": "⏹ Запрошена мягкая остановка — докачиваю начатое, новых не беру…",
    "dash_file_done": "[{w}] msg {msg} готово ({size}, {speed})",
    "dash_dedup": "♻️ msg {msg} дубликат → {dup}",
    "dash_repair": "🔧 msg {msg} больше эталона — перекачиваю",
    "dash_file_failed": "❌ msg {msg}: не докачан за {retries} попыток ({fn})",
    "dash_finished_stopped": "⚠️ остановлено досрочно",
    "dash_finished_ok": "✅ завершено",
    "dash_finished": "{mark}: {summary}",
    "dash_run_done": "— прогон завершён —",
    "err_prefix": "🛑 ОШИБКА: {message}",

    # ── upload ──
    "up_title": "Отгрузка (Upload Pipeline)",
    "up_idle": "⚪ Ожидание запуска конвейера отправки…",
    "lbl_src_folder": "Исходная папка:",
    "lbl_target": "Целевой канал:",
    "ph_target": "@username, t.me/… или id (--target)",
    "cb_skip_uploaded": "Пропускать уже отправленное (проверка upload_state.db)",
    "tip_skip_uploaded": (
        "Перед отправкой сверяет файл с базой upload_state.db по пути и хэшу.\n"
        "Если он уже был успешно отгружен в этот канал — повторно не отправляет.\n"
        "Позволяет безопасно перезапускать конвейер с того же места."),
    "cb_recursive": "Рекурсивный обход подпапок (включая вложенные папки)",
    "tip_recursive": (
        "Заходить во все вложенные папки исходного каталога и отправлять файлы из них —\n"
        "удобно, когда экспорт разложен по подпапкам. Выключено — берутся только файлы\n"
        "верхнего уровня выбранной папки, а вложенные каталоги игнорируются."),
    "cb_native": "Отправлять как нативное медиа (видео с превью)",
    "tip_native": (
        "Видео уходит через send_video с генерацией превью-эскиза, фото — как фото:\n"
        "в канале они проигрываются нативно. Выключено — всё уйдёт как документ-файл\n"
        "(без превью, зато без перекодирования метаданных)."),
    "cb_caption": "Алфавитная сортировка + имя файла в подпись",
    "tip_caption": (
        "Файлы отправляются строго по алфавиту, а имя файла кладётся в подпись —\n"
        "так в канале сохраняется порядок и сообщения остаются индексируемыми."),
    "sec_current_upload": "Текущая отгрузка",
    "up_counts": "отправлено: {sent} · пропущено: {skipped} · ошибок: {failed}",
    "log_header_up": "📋 Операционный лог отгрузки:",
    "btn_start_upload": "Начать отгрузку",
    "btn_stop_upload": "Остановить",
    "dlg_upload_folder": "Папка для отгрузки",
    "up_need_folder": "⚠️ Укажите исходную папку.",
    "up_busy": "⚠️ Движок занят другим заданием — дождитесь завершения.",
    "up_start_status": "🚀 Запуск конвейера отправки…",
    "up_start_log": "▶ Старт отгрузки: {folder} → {target}",
    "up_stopping": "⏹ Останавливаю конвейер…",
    "up_stop_requested": "⏹ Запрошена остановка отгрузки…",
    "up_sending": "🚀 Отправка файла: {fname}",
    "up_done_line": "✓ {fname} ({size}, {speed}) → msg {msg}",
    "up_skipped_line": "· {fname} — уже загружен, пропуск",
    "up_failed_line": "❌ {fname}: {err}",
    "up_preview": "Найдено к отправке: {n} файл(ов) в «{chat}».",
    "up_finished": "✅ Отгрузка завершена · отправлено {sent}, пропущено {skipped}, ошибок {failed}",
    "up_finished_log": "— отгрузка завершена —",
    "up_failed_status": "🛑 Конвейер остановлен с ошибкой",

    # ── reports ──
    "rep_title": "Отчёты и итоги",
    "rep_not_done": "Прогон ещё не завершён.",
    "sec_dl_summary": "Итоги загрузки",
    "card_already": "🟢 Уже было",
    "sec_dest": "Папка назначения",
    "btn_open_folder": "📂 Открыть папку",
    "btn_reveal": "🔎 Показать в Finder",
    "sec_text_reports": "Текстовые отчёты",
    "rep_none_short": "Отчётов пока нет.",
    "rep_none_long": (
        "Текстовых отчётов в папке нет (они появляются после экспорта "
        "с дедупом/качеством или аудита --verify)."),
    "btn_open": "Открыть",
    "btn_new_export": "🔄 Новый экспорт",
    "rep_done": "✅ Прогон завершён · скачано {n} файл(ов) ({gb:.2f} ГБ), ошибок {failed}.",
    "rep_stopped": "⚠️ Прогон остановлен досрочно · скачано {n} файл(ов) ({gb:.2f} ГБ), ошибок {failed}.",
    "rep_open_folder_fail": "⚠️ Не удалось открыть папку — путь недоступен.",
    "rep_reveal_fail": "⚠️ Не удалось показать папку в Finder.",
    "rep_open_file_fail": "⚠️ Не удалось открыть файл: {path}",
    "report_export_title": "📄 Журнал экспорта",
    "report_export_desc": "Полная хронология прогона (события, сеть, ФС).",
    "report_quality_title": "🏆 Отчёт по качеству",
    "report_quality_desc": "Какие версии-дубликаты пропущены и в пользу какого мастера.",
    "report_verify_title": "🔍 Отчёт аудита",
    "report_verify_desc": "Результат сверки файлов на диске с планом (режим --verify).",
    "report_error_title": "⚠️ Лог ошибок",
    "report_error_desc": "Тихий лог критических ошибок и пропусков по msg_id.",
}

_DICTIONARIES = {"en": _EN, "ru": _RU}


# Module-level singleton + convenience helper (the only API screens use). Created
# after the dictionaries exist so __init__ can bind them.
translator = TranslationEngine()


def t(key: str, **fmt) -> str:
    return translator.tr(key, **fmt)

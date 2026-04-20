import sys
import os
import asyncio
import logging
import webbrowser

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTextEdit, QLabel, QSystemTrayIcon, QMenu,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QTextCursor

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_icon() -> QIcon:
    path = os.path.join(_BASE_DIR, "icon.png")
    if os.path.exists(path):
        return QIcon(path)
    # fallback — зелёный круг
    px = QPixmap(64, 64)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#2ecc71"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(6, 6, 52, 52)
    p.end()
    return QIcon(px)

from logger import bot_logger, GMT3Formatter, QtLogHandler
from main import TradingBot


class BotWorker(QThread):
    """Запускает TradingBot в отдельном потоке с собственным asyncio event loop."""

    started_signal = pyqtSignal()
    stopped_signal = pyqtSignal()
    error_signal = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self._loop: asyncio.AbstractEventLoop | None = None

    def run(self) -> None:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        bot = TradingBot()
        self.started_signal.emit()
        try:
            self._loop.run_until_complete(bot.run())
        except Exception as e:
            self.error_signal.emit(str(e))
        finally:
            self._loop.close()
            self._loop = None
            self.stopped_signal.emit()

    def request_stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._cancel_all)

    def _cancel_all(self) -> None:
        for task in asyncio.all_tasks(self._loop):
            task.cancel()


class MainWindow(QMainWindow):
    _log_signal = pyqtSignal(int, str)

    def __init__(self) -> None:
        super().__init__()
        self._worker: BotWorker | None = None
        self._running = False
        self._pending_restart = False

        self._icon = _load_icon()

        self._build_ui()
        self._build_tray()
        self._install_log_handler()
        self._set_state(False)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        self.setWindowTitle("TG Parser Bot")
        self.setMinimumSize(720, 500)
        self.resize(860, 580)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Статус + кнопки
        bar = QHBoxLayout()
        self._lbl_status = QLabel()
        self._lbl_status.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        bar.addWidget(self._lbl_status)
        bar.addStretch()

        self._btn_start     = QPushButton("▶  Старт")
        self._btn_stop      = QPushButton("■  Стоп")
        self._btn_restart   = QPushButton("↺  Перезапуск")
        self._btn_dashboard = QPushButton("🌐  Дашборд")

        for btn in (self._btn_start, self._btn_stop, self._btn_restart, self._btn_dashboard):
            btn.setFixedHeight(30)
            btn.setMinimumWidth(110)
            bar.addWidget(btn)

        self._btn_start.clicked.connect(self.start_bot)
        self._btn_stop.clicked.connect(self.stop_bot)
        self._btn_restart.clicked.connect(self.restart_bot)
        self._btn_dashboard.clicked.connect(self.open_dashboard)
        layout.addLayout(bar)

        # Лог
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont("Consolas", 9))
        self._log.setStyleSheet(
            "QTextEdit { background:#1e1e1e; color:#d4d4d4; border:1px solid #3c3c3c; }"
        )
        layout.addWidget(self._log)

        self._log_signal.connect(self._append_log)

    def _build_tray(self) -> None:
        self._tray = QSystemTrayIcon(self._icon, self)
        self._tray.setToolTip("TG Parser — Остановлен")

        menu = QMenu()
        self._tray_status = menu.addAction("● Остановлен")
        self._tray_status.setEnabled(False)
        menu.addSeparator()
        self._tray_start   = menu.addAction("▶  Старт")
        self._tray_stop    = menu.addAction("■  Стоп")
        self._tray_restart = menu.addAction("↺  Перезапуск")
        menu.addSeparator()
        menu.addAction("🌐  Открыть дашборд").triggered.connect(self.open_dashboard)
        menu.addSeparator()
        menu.addAction("Показать / Скрыть").triggered.connect(self._toggle_window)
        menu.addSeparator()
        menu.addAction("Выход").triggered.connect(self._quit)

        self._tray_start.triggered.connect(self.start_bot)
        self._tray_stop.triggered.connect(self.stop_bot)
        self._tray_restart.triggered.connect(self.restart_bot)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_click)
        self._tray.show()

    def _install_log_handler(self) -> None:
        # Убираем консольный handler — терминал не нужен
        bot_logger.handlers = [h for h in bot_logger.handlers
                                if not isinstance(h, logging.StreamHandler)
                                or isinstance(h, logging.FileHandler)]

        fmt = GMT3Formatter(
            fmt="[%(asctime)s] [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        )
        handler = QtLogHandler(self._log_signal.emit)
        handler.setFormatter(fmt)
        handler.setLevel(logging.INFO)
        bot_logger.addHandler(handler)

    # ------------------------------------------------------------------ Лог

    def _append_log(self, level: int, msg: str) -> None:
        if level >= logging.ERROR:
            color = "#e74c3c"
        elif level >= logging.WARNING:
            color = "#f0a500"
        else:
            color = "#d4d4d4"
        escaped = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self._log.append(f'<span style="color:{color};">{escaped}</span>')
        self._log.moveCursor(QTextCursor.MoveOperation.End)

    # ------------------------------------------------------------------ Бот

    def start_bot(self) -> None:
        if self._running:
            return
        self._worker = BotWorker()
        self._worker.started_signal.connect(self._on_started)
        self._worker.stopped_signal.connect(self._on_stopped)
        self._worker.error_signal.connect(
            lambda e: bot_logger.error(f"Критическая ошибка бота: {e}")
        )
        self._worker.start()

    def stop_bot(self) -> None:
        if self._worker:
            self._btn_stop.setEnabled(False)
            self._btn_restart.setEnabled(False)
            self._worker.request_stop()

    def restart_bot(self) -> None:
        if self._running:
            self._pending_restart = True
            self.stop_bot()
        else:
            self.start_bot()

    def open_dashboard(self) -> None:
        import config
        webbrowser.open(f"http://{config.WEB_HOST}:{config.WEB_PORT}")

    def _on_started(self) -> None:
        self._running = True
        self._set_state(True)

    def _on_stopped(self) -> None:
        self._running = False
        self._set_state(False)
        if self._pending_restart:
            self._pending_restart = False
            self.start_bot()

    # ------------------------------------------------------------------ Состояние UI

    def _set_state(self, running: bool) -> None:
        if running:
            text, color, tip = "● Работает", "#2ecc71", "TG Parser — Работает"
        else:
            text, color, tip = "● Остановлен", "#e74c3c", "TG Parser — Остановлен"

        self._lbl_status.setText(text)
        self._lbl_status.setStyleSheet(f"color:{color};")
        self._tray.setToolTip(tip)
        self._tray_status.setText(text)
        self.setWindowIcon(self._icon)

        self._btn_start.setEnabled(not running)
        self._btn_stop.setEnabled(running)
        self._btn_restart.setEnabled(running)
        self._tray_start.setEnabled(not running)
        self._tray_stop.setEnabled(running)
        self._tray_restart.setEnabled(running)

    # ------------------------------------------------------------------ Окно / трей

    def _toggle_window(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def _on_tray_click(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_window()

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()
        self._tray.showMessage(
            "TG Parser",
            "Приложение скрыто в трей. Для выхода — меню трея → Выход.",
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )

    def _quit(self) -> None:
        if self._running:
            self.stop_bot()
            if self._worker:
                self._worker.wait(5000)
        QApplication.quit()


def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("Системный трей недоступен.")
        sys.exit(1)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

from __future__ import annotations

import importlib
import sys
import threading
from dataclasses import dataclass
from typing import Any, NoReturn

import structlog

LOGGER = structlog.get_logger(__name__)


@dataclass(frozen=True)
class QtBindings:
    qt: Any
    color: Any
    font: Any
    palette: Any
    application: Any
    frame: Any
    horizontal_layout: Any
    label: Any
    main_window: Any
    push_button: Any
    vertical_layout: Any
    widget: Any


def _load_pyqt6() -> QtBindings:
    try:
        qt_core: Any = importlib.import_module("PyQt6.QtCore")
        qt_gui: Any = importlib.import_module("PyQt6.QtGui")
        qt_widgets: Any = importlib.import_module("PyQt6.QtWidgets")
    except ImportError as exc:
        raise RuntimeError("PyQt6 is required to launch the Maxwell desktop app.") from exc

    return QtBindings(
        qt=qt_core.Qt,
        color=qt_gui.QColor,
        font=qt_gui.QFont,
        palette=qt_gui.QPalette,
        application=qt_widgets.QApplication,
        frame=qt_widgets.QFrame,
        horizontal_layout=qt_widgets.QHBoxLayout,
        label=qt_widgets.QLabel,
        main_window=qt_widgets.QMainWindow,
        push_button=qt_widgets.QPushButton,
        vertical_layout=qt_widgets.QVBoxLayout,
        widget=qt_widgets.QWidget,
    )


class DaemonServerThread(threading.Thread):
    """Background thread to run the FastAPI daemon without blocking the GUI."""

    def run(self) -> None:
        import uvicorn

        from maxwell_daemon.api import create_app
        from maxwell_daemon.config import load_config
        from maxwell_daemon.daemon import Daemon

        try:
            cfg = load_config()
            daemon_instance = Daemon(cfg)
            fastapi_app = create_app(daemon_instance)
            uvicorn.run(fastapi_app, host="127.0.0.1", port=8000, log_level="error")
        except Exception:
            LOGGER.exception("daemon_server_start_failed")


def _apply_dark_theme(window: Any, bindings: QtBindings) -> None:
    palette = bindings.palette()
    palette.setColor(bindings.palette.ColorRole.Window, bindings.color("#15161E"))
    palette.setColor(bindings.palette.ColorRole.WindowText, bindings.color("#FFFFFF"))
    window.setPalette(palette)


def _build_sidebar(bindings: QtBindings) -> Any:
    sidebar = bindings.frame()
    sidebar.setFixedWidth(260)
    sidebar.setStyleSheet("background-color: #1A1B26; border-right: 1px solid #292E42;")
    sidebar_layout = bindings.vertical_layout(sidebar)
    sidebar_layout.setContentsMargins(15, 30, 15, 30)

    title_label = bindings.label("⚡ Maxwell-Daemon")
    title_label.setStyleSheet(
        "color: #7AA2F7; font-size: 22px; font-weight: bold; margin-bottom: 30px; border: none;"
    )
    sidebar_layout.addWidget(title_label)

    nav_items = (
        "Dashboard",
        "Cognitive Pipeline",
        "Role Players",
        "Memory Annealer",
        "Execution Sandbox",
    )
    for item in nav_items:
        btn = bindings.push_button(item)
        btn.setCursor(bindings.qt.CursorShape.PointingHandCursor)
        if item == "Dashboard":
            btn.setStyleSheet(
                """
                QPushButton {
                    background-color: #24283B;
                    color: #7AA2F7;
                    text-align: left;
                    padding: 12px 20px;
                    font-size: 14px;
                    font-weight: bold;
                    border: none;
                    border-radius: 8px;
                }
                """
            )
        else:
            btn.setStyleSheet(
                """
                QPushButton {
                    background-color: transparent;
                    color: #A9B1D6;
                    text-align: left;
                    padding: 12px 20px;
                    font-size: 14px;
                    border: none;
                    border-radius: 8px;
                }
                QPushButton:hover {
                    background-color: #1F2335;
                    color: #FFFFFF;
                }
                """
            )
        sidebar_layout.addWidget(btn)

    sidebar_layout.addStretch()
    return sidebar


def _build_pipeline_visualizer(bindings: QtBindings) -> Any:
    pipeline_frame = bindings.frame()
    pipeline_frame.setStyleSheet(
        """
        QFrame {
            background-color: rgba(36, 40, 59, 0.7);
            border-radius: 16px;
            border: 1px solid rgba(122, 162, 247, 0.2);
        }
        """
    )
    pipeline_frame.setFixedHeight(150)
    pipe_layout = bindings.horizontal_layout(pipeline_frame)

    stages = (
        ("Strategist", "#BB9AF7", "Active"),
        ("Implementer", "#A9B1D6", "Waiting"),
        ("Maxwell Crucible", "#A9B1D6", "Waiting"),
    )

    for title, color, status in stages:
        stage_widget = bindings.widget()
        stage_layout = bindings.vertical_layout(stage_widget)

        lbl_title = bindings.label(title)
        lbl_title.setStyleSheet(
            f"color: {color}; font-weight: bold; font-size: 18px; "
            "border: none; background: transparent;"
        )
        lbl_title.setAlignment(bindings.qt.AlignmentFlag.AlignCenter)

        lbl_status = bindings.label(status)
        status_color = "#73DACA" if status == "Active" else "#565F89"
        lbl_status.setStyleSheet(
            f"color: {status_color}; font-size: 12px; border: none; background: transparent;"
        )
        lbl_status.setAlignment(bindings.qt.AlignmentFlag.AlignCenter)

        stage_layout.addWidget(lbl_title)
        stage_layout.addWidget(lbl_status)
        pipe_layout.addWidget(stage_widget)

    return pipeline_frame


def _build_main_window(bindings: QtBindings) -> Any:
    window = bindings.main_window()
    window.setWindowTitle("Maxwell-Daemon")
    window.resize(1200, 750)
    _apply_dark_theme(window, bindings)

    central_widget = bindings.widget()
    window.setCentralWidget(central_widget)
    main_layout = bindings.horizontal_layout(central_widget)
    main_layout.setContentsMargins(0, 0, 0, 0)
    main_layout.setSpacing(0)

    content = bindings.frame()
    content.setStyleSheet("background-color: #15161E;")
    content_layout = bindings.vertical_layout(content)
    content_layout.setContentsMargins(50, 40, 50, 40)

    header = bindings.label("Pipeline Status")
    header.setStyleSheet("color: #FFFFFF; font-size: 28px; font-weight: bold;")
    content_layout.addWidget(header)
    content_layout.addWidget(_build_pipeline_visualizer(bindings))
    content_layout.addStretch()

    main_layout.addWidget(_build_sidebar(bindings))
    main_layout.addWidget(content)

    window.server_thread = DaemonServerThread(daemon=True)
    window.server_thread.start()
    return window


def launch() -> NoReturn:
    bindings = _load_pyqt6()
    app = bindings.application(sys.argv)
    font = bindings.font("Segoe UI Variable", 10)
    font.setStyleHint(bindings.font.StyleHint.SansSerif)
    app.setFont(font)

    window = _build_main_window(bindings)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    launch()

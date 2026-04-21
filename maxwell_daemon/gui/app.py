import sys
import threading

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class DaemonServerThread(threading.Thread):
    """Background thread to run the FastAPI daemon without blocking the GUI."""

    def run(self):
        import uvicorn

        from maxwell_daemon.cli.main import create_app
        from maxwell_daemon.config import load_config
        from maxwell_daemon.daemon import Daemon

        try:
            cfg = load_config()
            daemon_instance = Daemon(cfg)
            fastapi_app = create_app(daemon_instance)
            uvicorn.run(fastapi_app, host="127.0.0.1", port=8000, log_level="error")
        except Exception as e:
            print(f"Daemon failed to start: {e}")


class MaxwellDesktopApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Maxwell-Daemon")
        self.resize(1200, 750)
        self._apply_dark_theme()

        # Main Layout (Borderless container)
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Left Sidebar
        sidebar = QFrame()
        sidebar.setFixedWidth(260)
        sidebar.setStyleSheet(
            "background-color: #1A1B26; border-right: 1px solid #292E42;"
        )
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(15, 30, 15, 30)

        title_label = QLabel("⚡ Maxwell-Daemon")
        title_label.setStyleSheet(
            "color: #7AA2F7; font-size: 22px; font-weight: bold; margin-bottom: 30px; border: none;"
        )
        sidebar_layout.addWidget(title_label)

        nav_items = [
            "Dashboard",
            "Cognitive Pipeline",
            "Role Players",
            "Memory Annealer",
            "Execution Sandbox",
        ]
        for item in nav_items:
            btn = QPushButton(item)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            if item == "Dashboard":
                btn.setStyleSheet("""
                    QPushButton { background-color: #24283B; color: #7AA2F7; text-align: left; padding: 12px 20px; font-size: 14px; font-weight: bold; border: none; border-radius: 8px; }
                """)
            else:
                btn.setStyleSheet("""
                    QPushButton { background-color: transparent; color: #A9B1D6; text-align: left; padding: 12px 20px; font-size: 14px; border: none; border-radius: 8px; }
                    QPushButton:hover { background-color: #1F2335; color: #FFFFFF; }
                """)
            sidebar_layout.addWidget(btn)

        sidebar_layout.addStretch()

        # Right Content Area
        content = QFrame()
        content.setStyleSheet("background-color: #15161E;")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(50, 40, 50, 40)

        header = QLabel("Pipeline Status")
        header.setStyleSheet("color: #FFFFFF; font-size: 28px; font-weight: bold;")
        content_layout.addWidget(header)

        # Glassmorphic Pipeline Visualizer
        pipeline_frame = QFrame()
        pipeline_frame.setStyleSheet("""
            QFrame {
                background-color: rgba(36, 40, 59, 0.7);
                border-radius: 16px;
                border: 1px solid rgba(122, 162, 247, 0.2);
            }
        """)
        pipeline_frame.setFixedHeight(150)
        pipe_layout = QHBoxLayout(pipeline_frame)

        stages = [
            ("Strategist", "#BB9AF7", "Active"),
            ("Implementer", "#A9B1D6", "Waiting"),
            ("Maxwell Crucible", "#A9B1D6", "Waiting"),
        ]

        for title, color, status in stages:
            stage_widget = QWidget()
            stage_layout = QVBoxLayout(stage_widget)

            lbl_title = QLabel(title)
            lbl_title.setStyleSheet(
                f"color: {color}; font-weight: bold; font-size: 18px; border: none; background: transparent;"
            )
            lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)

            lbl_status = QLabel(status)
            lbl_status.setStyleSheet(
                f"color: {'#73DACA' if status == 'Active' else '#565F89'}; font-size: 12px; border: none; background: transparent;"
            )
            lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)

            stage_layout.addWidget(lbl_title)
            stage_layout.addWidget(lbl_status)
            pipe_layout.addWidget(stage_widget)

        content_layout.addWidget(pipeline_frame)
        content_layout.addStretch()

        main_layout.addWidget(sidebar)
        main_layout.addWidget(content)

        # Start daemon in background
        self.server_thread = DaemonServerThread(daemon=True)
        self.server_thread.start()

    def _apply_dark_theme(self):
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#15161E"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#FFFFFF"))
        self.setPalette(palette)


def launch():
    app = QApplication(sys.argv)
    # Use modern typography
    font = QFont("Segoe UI Variable", 10)
    font.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(font)

    window = MaxwellDesktopApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    launch()

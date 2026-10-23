from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractButton, QLabel, QSizePolicy


INK = "#233e32"
MUTED = "#5b7063"
GREEN = "#286046"
PATHS = {
    "folder": '<path d="M3 7V5h6l2 2h10v12H3Z"/>',
    "search": '<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "refresh": '<path d="M20 7v5h-5M4 17v-5h5M5 8a8 8 0 0 1 13-3l2 3M4 16l2 3a8 8 0 0 0 13-3"/>',
    "terminal": '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m7 9 3 3-3 3M13 15h4"/>',
    "code": '<path d="m8 6-6 6 6 6m8-12 6 6-6 6M14 4l-4 16"/>',
    "copilot": '<path d="M5 18v-5a7 7 0 0 1 14 0v5"/><rect x="3" y="13" width="18" height="7" rx="3"/><circle cx="9" cy="16.5" r="1"/><circle cx="15" cy="16.5" r="1"/>',
    "editor": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M8 4v16"/>',
    "app": '<rect x="4" y="4" width="16" height="16" rx="4"/><path d="M9 12h6M12 9v6"/>',
    "resume": '<path d="M4 5v6h6M4 11a8 8 0 1 1 2 7M12 7v5l3 2"/>',
    "arrow": '<path d="M4 12h16m-6-6 6 6-6 6"/>',
    "copy": '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V3H3v13h5"/>',
    "more": '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
    "import": '<path d="M12 3v12m-5-5 5 5 5-5M4 15v6h16v-6"/>',
    "check": '<path d="m5 12 4 4L19 6"/>',
}


def icon(name: str, color: str = INK, size: int = 20) -> QIcon:
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{PATHS[name]}</svg>'
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode())).render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


class ElidedLabel(QLabel):
    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.full_text = text
        self.setAccessibleName(text)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setToolTip(text)

    def setText(self, text: str) -> None:
        self.full_text = text
        self.setAccessibleName(text)
        self.setToolTip(text)
        self.updateGeometry()
        self.update()

    def sizeHint(self):
        return QSize(180, self.fontMetrics().height() + 4)

    def minimumSizeHint(self):
        return QSize(0, self.fontMetrics().height() + 4)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        text = self.fontMetrics().elidedText(self.full_text, Qt.TextElideMode.ElideMiddle, self.width())
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)


class CliButton(QAbstractButton):
    def __init__(self, title: str, subtitle: str, symbol: str, parent=None):
        super().__init__(parent)
        self.title = title
        self.subtitle = subtitle
        self.symbol = symbol
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)
        self.setAccessibleDescription(subtitle)
        self.setMinimumHeight(76)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        return QSize(240, 76)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = self.isChecked()
        background = "#e3eddf" if active else "#ffffff"
        if self.underMouse() and not active:
            background = "#eef2e9"
        painter.setBrush(QColor(background))
        painter.setPen(QPen(QColor(GREEN if active or self.hasFocus() else "#d8e1d7"), 2 if self.hasFocus() else 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 12, 12)
        icon(self.symbol, GREEN, 25).paint(painter, 19, 24, 25, 25)
        painter.setPen(QColor(INK))
        painter.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        painter.drawText(58, 30, self.title)
        painter.setFont(QFont("Segoe UI", 9))
        painter.setPen(QColor(MUTED))
        painter.drawText(58, 51, self.subtitle)
        if active:
            icon("check", GREEN, 17).paint(painter, self.width() - 32, 17, 17, 17)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
        else:
            super().keyPressEvent(event)


STYLE = """
QMainWindow, QDialog { background: #f5f6f1; }
QWidget { font-family: 'Segoe UI'; font-size: 13px; color: #233e32; }
QLabel { background: transparent; }
QLabel#muted { color: #5b7063; }
QLabel#muted[bypass="true"] { color: #7e4216; }
QLabel#title { font-size: 29px; font-weight: 600; }
QLabel#section { font-size: 14px; font-weight: 600; }
QLabel#status { color: #286046; background: #e3eddf; padding: 7px 12px; border-radius: 12px; }
QLabel#status[offline="true"] { color: #7e4216; background: #f4e5ce; }
QLabel#notice { color: #7e4216; background: #f4e5ce; padding: 10px 14px; border-radius: 8px; }
QFrame#sidebar { background: #193d32; }
QFrame#sidebar QLabel { color: #d6e4d6; }
QFrame#sidebar QLabel#brand { color: #f2f7ec; font-size: 19px; font-weight: 600; }
QFrame#sidebar QLabel#railMuted { color: #b5cdbf; font-size: 12px; }
QLineEdit, QComboBox { background: #ffffff; border: 1px solid #d5ded1; border-radius: 8px; padding: 9px 11px; selection-background-color: #d5e6cf; color: #233e32; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #286046; }
QLineEdit { placeholder-text-color: #60725f; }
QComboBox { min-width: 102px; padding-right: 25px; }
QComboBox::drop-down { border: 0; width: 25px; }
QComboBox QAbstractItemView { background: white; selection-background-color: #e3eddf; selection-color: #233e32; padding: 4px; border: 1px solid #d5ded1; }
QFrame#sidebar QLineEdit { background: #264c3f; color: #f0f5e9; border: 1px solid #50715d; selection-background-color: #4c735a; }
QFrame#sidebar QLineEdit:focus { border: 1px solid #bbd7a8; }
QFrame#sidebar QLineEdit { placeholder-text-color: #c4d8c6; }
QFrame#sidebar QComboBox { background: #264c3f; color: #f0f5e9; border: 1px solid #50715d; }
QPushButton, QToolButton { background: #ffffff; border: 1px solid #d5ded1; border-radius: 8px; padding: 9px 14px; font-weight: 600; }
QPushButton:hover, QToolButton:hover { background: #e9efdf; border-color: #abc19f; }
QPushButton:pressed, QToolButton:pressed { background: #dce8d5; }
QPushButton:focus, QToolButton:focus { border: 2px solid #286046; }
QPushButton:disabled, QToolButton:disabled { background: #ebeee7; color: #687566; border-color: #dbe0d4; }
QPushButton#primary { background: #286046; color: #ffffff; border-color: #286046; padding: 12px 22px; }
QPushButton#primary:hover { background: #1c4c35; }
QPushButton#primary:disabled { background: #d4dfd0; color: #51674f; border-color: #d4dfd0; }
QPushButton#primary:focus { border: 2px solid #99bd7e; }
QPushButton#ghost, QToolButton#ghost { background: transparent; border-color: transparent; font-weight: 400; }
QPushButton#ghost:hover, QToolButton#ghost:hover { background: #e6ecdf; }
QFrame#sidebar QPushButton { color: #edf5e6; background: #305942; border-color: #547954; }
QFrame#sidebar QPushButton:hover { background: #41684c; }
QFrame#sidebar QPushButton#ghost { background: transparent; color: #cfdfce; border-color: transparent; }
QFrame#sidebar QListWidget { background: transparent; border: 0; outline: 0; color: #d6e4d6; }
QFrame#sidebar QListWidget::item { padding: 12px 10px; margin-bottom: 3px; border-radius: 8px; }
QFrame#sidebar QListWidget::item:hover { background: #2a5141; }
QFrame#sidebar QListWidget::item:selected { background: #dcebcf; color: #1b3d2b; }
QTableWidget { background: #ffffff; alternate-background-color: #fafbf7; border: 1px solid #d5ded1; border-radius: 10px; selection-background-color: #dfecda; selection-color: #193d2b; gridline-color: transparent; outline: 0; }
QTableWidget::item { padding: 0 12px; border-bottom: 1px solid #eef1e9; }
QTableWidget::item:selected { background: #dfecda; color: #193d2b; }
QTableWidget::item:focus { border: 1px solid #749965; }
QHeaderView::section { background: #edf1e7; color: #51664f; border: 0; border-bottom: 1px solid #d5ded1; padding: 10px 12px; font-weight: 600; text-align: left; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 3px; }
QScrollBar::handle:vertical { background: #adc0aa; min-height: 28px; border-radius: 3px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QMenu { background: #ffffff; border: 1px solid #d5ded1; padding: 5px; }
QMenu::item { padding: 9px 20px; }
QMenu::item:selected { background: #dfecda; }
QToolTip { background: #233e32; color: #ffffff; padding: 6px; border: 0; }
"""

CHECK_ICON = (Path(__file__).resolve().parents[2] / "assets" / "check-white.svg").as_posix()
STYLE += f"""
QCheckBox {{ spacing: 9px; }}
QCheckBox::indicator {{ width: 20px; height: 20px; background: #ffffff; border: 1px solid #749965; border-radius: 4px; }}
QCheckBox::indicator:hover {{ border-color: #286046; }}
QCheckBox::indicator:checked {{ background: #286046; border-color: #286046; image: url("{CHECK_ICON}"); }}
QCheckBox::indicator:disabled {{ background: #ebeee7; border-color: #b8c5b6; }}
QCheckBox:disabled {{ color: #687566; }}
"""

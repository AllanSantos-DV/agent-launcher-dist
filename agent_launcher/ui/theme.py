from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractButton, QComboBox, QGridLayout, QLabel, QLayout, QSizePolicy, QStyledItemDelegate, QWidget,
)


INK = "#233e32"
MUTED = "#526659"
GREEN = "#286046"
PALE = "#a3b3a8"
MIN_TEXT_PX = 13
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
    """Cartão de uma ferramenta. Pronta: marcável, como sempre. Não instalada: ícone apagado e "clique para instalar".
    Instalando: barra no rodapé e ícone pintado na mesma fração; sem fração conhecida (instalador do fabricante em
    silêncio) a barra e o ícone mostram movimento sem fingir porcentagem."""

    INSTALLED_HOLD_MS = 900
    HEIGHT = 88
    TEXT_LEFT = 58

    def __init__(self, title: str, subtitle: str, symbol: str, parent=None):
        super().__init__(parent)
        self.title = title
        self.subtitle = subtitle
        self.symbol = symbol
        self.version = ""
        self.installed = True
        self.installing = False
        self.fraction: float | None = None
        self.stage = ""
        self.phase = 0.0
        self.animation = QTimer(self)
        self.animation.setInterval(40)
        self.animation.timeout.connect(self._advance)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)
        self.setAccessibleDescription(subtitle)
        self.setMinimumHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_version(self, text: str) -> None:
        self.version = text
        self._describe()
        self.update()

    def set_installed(self, installed: bool) -> None:
        self.installed = installed
        self.setCheckable(installed)
        self._describe()
        self.update()

    def set_progress(self, fraction: float | None, stage: str) -> None:
        self.installing = True
        self.fraction = None if fraction is None else max(0.0, min(float(fraction), 1.0))
        self.stage = stage
        if self.fraction is None:
            self.animation.start()
        else:
            self.animation.stop()
        self._describe()
        self.update()

    def finish_install(self, installed: bool) -> None:
        """Instalado: a barra completa fica à vista um instante antes de o cartão voltar ao normal."""
        self.animation.stop()
        if installed:
            self.fraction, self.stage = 1.0, "Instalado"
            self.update()
            QTimer.singleShot(self.INSTALLED_HOLD_MS, self._stop_installing)
        else:
            self._stop_installing()
        self.set_installed(installed)

    def _stop_installing(self) -> None:
        self.installing, self.fraction, self.stage = False, None, ""
        self._describe()
        self.update()

    def _advance(self) -> None:
        self.phase = (self.phase + 0.02) % 1.0
        self.update()

    def _describe(self) -> None:
        state = (self.stage if self.installing else self.version if self.installed
                 else "Não instalado. Clique para instalar")
        self.setAccessibleDescription(f"{self.subtitle}. {state}" if state else self.subtitle)

    def sizeHint(self):
        return QSize(240, self.HEIGHT)

    def minimumSizeHint(self):
        return QSize(CardGrid.MIN_CARD, self.HEIGHT)

    @staticmethod
    def card_font(pixels: int, weight=QFont.Weight.Normal) -> QFont:
        font = QFont("Segoe UI")
        font.setPixelSize(pixels)
        font.setWeight(weight)
        return font

    def _text(self, painter: QPainter, font: QFont, color: str, baseline: int, text: str, right: int) -> None:
        painter.setFont(font)
        painter.setPen(QColor(color))
        room = max(0, right - self.TEXT_LEFT)
        shown = painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, room)
        painter.drawText(self.TEXT_LEFT, baseline, shown)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = self.isChecked()
        background = "#e3eddf" if active else "#ffffff"
        if self.underMouse() and not active:
            background = "#eef2e9"
        painter.setBrush(QColor(background))
        width = 3 if self.hasFocus() else 2 if active else 1
        painter.setPen(QPen(QColor(GREEN if active or self.hasFocus() else "#d8e1d7"), width))
        inset = width / 2 + 0.5
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(inset, inset, -inset, -inset), 12, 12)
        top = 30
        if self.installed and not self.installing:
            icon(self.symbol, GREEN, 25).paint(painter, 19, top, 25, 25)
        else:
            icon(self.symbol, PALE, 25).paint(painter, 19, top, 25, 25)
            if self.installing:
                painter.save()
                if self.fraction is None:
                    painter.setClipRect(QRectF(19 - 10 + 35 * self.phase, top, 10, 25))
                else:
                    painter.setClipRect(QRectF(19, top + 25 * (1 - self.fraction), 25, 25 * self.fraction))
                icon(self.symbol, GREEN, 25).paint(painter, 19, top, 25, 25)
                painter.restore()
        right = self.width() - (40 if active else 14)
        self._text(painter, self.card_font(15, QFont.Weight.DemiBold), INK, 31, self.title, right)
        self._text(painter, self.card_font(MIN_TEXT_PX), MUTED, 54, self.subtitle, self.width() - 14)
        line, color = ((self.stage, GREEN) if self.installing else
                       (self.version, GREEN if "disponível" in self.version else MUTED) if self.installed else
                       ("Não instalado · clique para instalar", MUTED))
        if line:
            self._text(painter, self.card_font(MIN_TEXT_PX), color, 74, line, self.width() - 14)
        if self.installing:
            track = QRectF(14, self.height() - 7, self.width() - 28, 3)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#dfe7dc"))
            painter.drawRoundedRect(track, 1.5, 1.5)
            if self.fraction is None:
                width = track.width() * 0.28
                left = track.left() - width + (track.width() + width) * self.phase
                painter.setClipRect(track)
                fill = QRectF(left, track.top(), width, track.height())
            else:
                fill = QRectF(track.left(), track.top(), track.width() * self.fraction, track.height())
            painter.setBrush(QColor(GREEN))
            painter.drawRoundedRect(fill, 1.5, 1.5)
            painter.setClipping(False)
        if active:
            icon("check", GREEN, 17).paint(painter, self.width() - 32, 17, 17, 17)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
        else:
            super().keyPressEvent(event)


class CurrentMark(QStyledItemDelegate):
    """List row painter that marks the combo's current item with "✓ Em uso", so it is not told apart by colour alone."""

    MARK = "✓ Em uso"
    ROW = 40
    PADDING = 12
    GAP = 16

    def __init__(self, combo: QComboBox):
        super().__init__(combo)
        self.combo = combo

    def mark_font(self) -> QFont:
        return CliButton.card_font(MIN_TEXT_PX, QFont.Weight.DemiBold)

    def mark_width(self) -> int:
        return QFontMetrics(self.mark_font()).horizontalAdvance(self.MARK) + self.GAP + self.PADDING

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(size.width() + self.mark_width(), max(size.height(), self.ROW))

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        if index.row() == self.combo.currentIndex():
            room = option.rect.width() - self.mark_width() - self.PADDING
            option.text = option.fontMetrics.elidedText(option.text, Qt.TextElideMode.ElideRight, max(room, 0))

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if index.row() != self.combo.currentIndex():
            return
        painter.save()
        painter.setFont(self.mark_font())
        painter.setPen(QColor(GREEN))
        painter.drawText(option.rect.adjusted(0, 0, -self.PADDING, 0), Qt.AlignRight | Qt.AlignVCenter, self.MARK)
        painter.restore()


class ReadableCombo(QComboBox):
    """Combo box whose open list follows the stylesheet item rules (dark text on white, 40 px rows) and is wide enough
    for every name next to the "Em uso" mark; the default menu-style delegate paints the list with the combo's own text
    colour, unreadable on the dark rail."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setItemDelegate(CurrentMark(self))

    def showPopup(self):
        view = self.view()
        view.ensurePolished()
        metrics = QFontMetrics(view.font())
        widest = max((metrics.horizontalAdvance(self.itemText(row)) for row in range(self.count())), default=0)
        view.setMinimumWidth(widest + 2 * CurrentMark.PADDING + self.itemDelegate().mark_width() + 8)
        super().showPopup()


class CardGrid(QWidget):
    """Tool cards in three columns, or two when the area is too narrow for three readable cards."""

    MIN_CARD = 200
    SPACING = 12

    def __init__(self, cards: list, parent=None):
        super().__init__(parent)
        self.cards = cards
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(self.SPACING)
        self.grid.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(2 * self.MIN_CARD + self.SPACING)
        self.columns = 0
        self.arrange(3)

    @classmethod
    def columns_for(cls, width: int) -> int:
        return 3 if width >= 3 * cls.MIN_CARD + 2 * cls.SPACING else 2

    def arrange(self, columns: int) -> None:
        if columns != self.columns:
            self.columns = columns
            for card in self.cards:
                self.grid.removeWidget(card)
            for index, card in enumerate(self.cards):
                self.grid.addWidget(card, index // columns, index % columns)
            for column in range(3):
                self.grid.setColumnStretch(column, 1 if column < columns else 0)
        rows = -(-len(self.cards) // columns)
        self.setFixedHeight(rows * CliButton.HEIGHT + (rows - 1) * self.SPACING)

    def sizeHint(self):
        return QSize(3 * 240 + 2 * self.SPACING, self.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.arrange(self.columns_for(event.size().width()))


STYLE = """
QMainWindow, QDialog { background: #f5f6f1; }
QWidget { font-family: 'Segoe UI'; font-size: 14px; color: #233e32; }
QLabel { background: transparent; }
QLabel#muted { color: #526659; }
QLabel#muted[bypass="true"] { color: #7e4216; }
QLabel#title { font-size: 29px; font-weight: 600; }
QLabel#section { font-size: 14px; font-weight: 600; }
QLabel#status { color: #286046; background: #e3eddf; padding: 7px 12px; border-radius: 12px; }
QLabel#status[offline="true"] { color: #7e4216; background: #f4e5ce; }
QLabel#notice { color: #7e4216; background: #f4e5ce; padding: 10px 14px; border-radius: 8px; }
QFrame#sidebar { background: #193d32; }
QFrame#sidebar QLabel { color: #d6e4d6; }
QFrame#sidebar QLabel#brand { color: #f2f7ec; font-size: 19px; font-weight: 600; }
QFrame#sidebar QLabel#railMuted { color: #b5cdbf; font-size: 13px; }
QLineEdit, QComboBox { background: #ffffff; border: 1px solid #d5ded1; border-radius: 8px; padding: 9px 11px; selection-background-color: #d5e6cf; color: #233e32; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #286046; }
QLineEdit { placeholder-text-color: #60725f; }
QComboBox { min-width: 102px; padding-right: 25px; }
QComboBox::drop-down { border: 0; width: 25px; }
QComboBox QAbstractItemView, QFrame#sidebar QComboBox QAbstractItemView { background: #ffffff; color: #233e32; font-size: 14px; selection-background-color: #e3eddf; selection-color: #193d2b; padding: 4px; border: 1px solid #d5ded1; outline: 0; }
QComboBox QAbstractItemView::item, QFrame#sidebar QComboBox QAbstractItemView::item { min-height: 40px; padding: 0 12px; color: #233e32; }
QComboBox QAbstractItemView::item:selected, QFrame#sidebar QComboBox QAbstractItemView::item:selected { background: #e3eddf; color: #193d2b; }
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
QListWidget { background: #ffffff; color: #233e32; border: 1px solid #d5ded1; border-radius: 8px; outline: 0; }
QListWidget::item:selected { background: #dfecda; color: #193d2b; }
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
QMenu { background: #ffffff; color: #233e32; font-size: 14px; border: 1px solid #d5ded1; border-radius: 8px; padding: 6px; }
QMenu::item { padding: 8px 24px 8px 12px; min-height: 20px; border-radius: 6px; }
QMenu::item:selected { background: #e3eddf; color: #193d2b; }
QMenu::item:disabled { color: #687566; }
QMenu::separator { height: 1px; background: #d5ded1; margin: 5px 8px; }
QMenu::indicator { width: 18px; height: 18px; padding-left: 6px; }
QScrollArea#page, QWidget#page, QScrollArea#page > QWidget#qt_scrollarea_viewport { background: #f5f6f1; border: 0; }
QFrame#actionsBar { background: #f5f6f1; border-top: 1px solid #d5ded1; }
QFrame#sidebar QScrollArea#rail, QFrame#sidebar QWidget#railPage, QScrollArea#rail > QWidget#qt_scrollarea_viewport { background: transparent; border: 0; }
QFrame#sidebar QScrollBar::handle:vertical { background: #50715d; }
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

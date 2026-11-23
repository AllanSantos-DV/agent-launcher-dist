"""Keeps windows reachable and popups at their own size when the window moves between monitors.

Windows sends WM_DPICHANGED with a suggested rectangle scaled for the old monitor; Qt applies it as is. A menu
opened on a 150% monitor and shown on a 100% one ends up at two thirds of its size, and a main window whose
minimum size is larger than the new monitor's work area hangs off the screen.
"""
from __future__ import annotations

import sys
from ctypes import wintypes

import shiboken6
from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QMargins, QObject, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QDialog, QFrame, QMenu, QScrollArea, QVBoxLayout, QWidget

PREFERRED_MINIMUM = QSize(800, 600)
TITLE_GRIP = 48
SETTLE_MS = 350
POPUP_EVENTS = (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Resize, QEvent.Type.ScreenChangeInternal,
                QEvent.Type.DevicePixelRatioChange)
WM_DPICHANGED = 0x02E0
VISIBLE_EDGE = 1 if sys.platform == "win32" else 0
KEEPERS: dict[int, "WindowKeeper"] = {}


def screen_for(rect: QRect):
    """Screen holding the centre of rect, else the one sharing the largest area with it, else the primary."""
    application = QGuiApplication.instance()
    found = application.screenAt(rect.center())
    if found is not None:
        return found
    best, best_area = application.primaryScreen(), 0
    for screen in application.screens():
        overlap = screen.availableGeometry().intersected(rect)
        area = overlap.width() * overlap.height() if overlap.isValid() else 0
        if area > best_area:
            best, best_area = screen, area
    if best_area == 0:
        centre = rect.center()
        best = min(application.screens(), key=lambda s: (s.availableGeometry().center() - centre).manhattanLength())
    return best


def minimum_for(available: QRect, border: QSize = QSize(0, 0)) -> QSize:
    """Minimum client size: 800x600, or less when the area minus the title bar and borders is smaller."""
    return PREFERRED_MINIMUM.boundedTo(available.size() - border)


def work_area(screen) -> QRect:
    """Available area minus the 1 px border Windows 11 paints outside the frame Qt reports (left, right, bottom)."""
    return screen.availableGeometry().adjusted(VISIBLE_EDGE, 0, -VISIBLE_EDGE, -VISIBLE_EDGE)


def popup_area(screen) -> QRect:
    """Available area for popups: one logical pixel smaller at the right and bottom on a fractional scale, where the
    physical rectangle Qt derives from the rounded logical one can end a pixel past the area."""
    area = screen.availableGeometry()
    if screen.devicePixelRatio() == int(screen.devicePixelRatio()):
        return area
    return area.adjusted(0, 0, -1, -1)


def fitted(frame: QRect, available: QRect) -> QRect:
    """Frame shrunk to the available area and moved fully inside it when any part is outside."""
    result = QRect(frame)
    result.setWidth(min(frame.width(), available.width()))
    result.setHeight(min(frame.height(), available.height()))
    if not available.contains(result):
        result.moveLeft(max(available.left(), min(result.left(), available.right() - result.width() + 1)))
        result.moveTop(max(available.top(), min(result.top(), available.bottom() - result.height() + 1)))
    return result


def reachable(frame: QRect, available: QRect) -> bool:
    """True while the window fits the area and its title bar can still be grabbed."""
    if frame.width() > available.width() or frame.height() > available.height():
        return False
    grip = QRect(frame.left(), frame.top(), frame.width(), TITLE_GRIP)
    visible = grip.intersected(available)
    return frame.top() >= available.top() and visible.width() >= min(160, frame.width()) and visible.height() >= 8


class WindowKeeper(QObject):
    """Refits a top-level window once it settles on a new screen, after a DPI change or a work-area change."""

    def __init__(self, window: QWidget):
        super().__init__(window)
        self.window = window
        self.pending = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(SETTLE_MS)
        self.timer.timeout.connect(self.fit)
        self.handle = None
        window.installEventFilter(self)
        application = QGuiApplication.instance()
        application.screenAdded.connect(self._watch_screen)
        application.screenRemoved.connect(self.request)
        for screen in application.screens():
            self._watch_screen(screen)

    def _watch_screen(self, screen) -> None:
        screen.availableGeometryChanged.connect(self.request)
        screen.logicalDotsPerInchChanged.connect(self.request)

    def request(self, *_changed) -> None:
        """Refits once the window stops moving; signal arguments are ignored."""
        self.pending = True
        self.timer.start()

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QEvent.Type.Show:
            handle = self.window.windowHandle()
            if handle is not None and handle is not self.handle:
                self.handle = handle
                KEEPERS[int(self.window.winId())] = self
                handle.screenChanged.connect(self.request)
            self.fit()
            self.request()
        elif kind in (QEvent.Type.ScreenChangeInternal, QEvent.Type.DevicePixelRatioChange):
            self.request()
        elif kind in (QEvent.Type.Move, QEvent.Type.Resize) and self.pending:
            self.timer.start()
        return False

    def fit(self) -> None:
        """Shrinks and moves the window into the work area of its screen; minimum size follows that area."""
        self.pending = False
        window = self.window
        frame = window.frameGeometry()
        available = work_area(screen_for(frame))
        border = frame.size() - window.geometry().size()
        window.setMinimumSize(minimum_for(available, border))
        if window.windowState() & (Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen
                                   | Qt.WindowState.WindowMinimized):
            return
        if reachable(frame, available):
            return
        target = fitted(frame, available)
        window.resize(target.size() - border)
        window.move(target.topLeft())


class PopupKeeper(QObject):
    """Application-wide filter: a menu or popup list keeps the size it opened with and stays on its screen."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sizes: dict[int, QSize] = {}
        self.fixing: set[int] = set()

    @staticmethod
    def is_popup(widget) -> bool:
        """True for menus and combo-box lists: top-level widgets of the Qt popup window type."""
        return isinstance(widget, QWidget) and widget.windowType() == Qt.WindowType.Popup

    def eventFilter(self, watched, event):
        kind = event.type()
        if isinstance(watched, QDialog) and watched.isWindow():
            if kind == QEvent.Type.Show:
                QTimer.singleShot(0, lambda w=watched: _clamp_dialog(w))
            elif kind in (QEvent.Type.ScreenChangeInternal, QEvent.Type.DevicePixelRatioChange):
                QTimer.singleShot(SETTLE_MS, lambda w=watched: _clamp_dialog(w))
        if kind not in POPUP_EVENTS or not self.is_popup(watched):
            return False
        key = shiboken6.getCppPointer(watched)[0]
        if kind == QEvent.Type.Show:
            wanted = watched.size()
            if isinstance(watched, QMenu):
                wanted = wanted.expandedTo(watched.sizeHint())
            self.sizes[key] = wanted
            QTimer.singleShot(0, lambda w=watched: self.restore(w))
        elif kind == QEvent.Type.Hide:
            self.sizes.pop(key, None)
        elif kind in (QEvent.Type.Resize, QEvent.Type.ScreenChangeInternal, QEvent.Type.DevicePixelRatioChange):
            wanted = self.sizes.get(key)
            if wanted is not None and key not in self.fixing and (
                    watched.width() < wanted.width() or watched.height() < wanted.height()):
                QTimer.singleShot(0, lambda w=watched: self.restore(w))
        return False

    def restore(self, popup: QWidget) -> None:
        """Puts the popup back at its opening size and inside the work area of the screen it is on."""
        key = shiboken6.getCppPointer(popup)[0]
        wanted = self.sizes.get(key)
        if wanted is None or not popup.isVisible():
            return
        self.fixing.add(key)
        try:
            frame = QRect(popup.pos(), wanted)
            target = fitted(frame, popup_area(screen_for(frame)))
            if popup.size() != target.size():
                popup.resize(target.size())
            if popup.pos() != target.topLeft():
                popup.move(target.topLeft())
        finally:
            self.fixing.discard(key)


def _clamp_dialog(dialog: QDialog, whole: bool = False) -> None:
    """Fits a dialog into the work area of its screen; its minimum size never exceeds that area. A dialog that is only
    reachable stays where it is, unless `whole` asks for the entire frame inside the area."""
    if not shiboken6.isValid(dialog) or not dialog.isVisible():
        return
    frame = dialog.frameGeometry()
    available = work_area(screen_for(frame))
    border = frame.size() - dialog.geometry().size()
    dialog.setMinimumSize(dialog.minimumSize().boundedTo(available.size() - border))
    if available.contains(frame) if whole else reachable(frame, available):
        return
    target = fitted(frame, available)
    dialog.resize(target.size() - border)
    dialog.move(target.topLeft())


def reveal(widget: QWidget) -> None:
    """Scrolls the body of the dialog holding `widget` to it, once the layout has placed it (a banner that just appeared)."""
    dialog = widget.window()
    if isinstance(dialog, GrowingDialog):
        QTimer.singleShot(0, lambda: shiboken6.isValid(widget) and widget.isVisible()
                          and dialog.body_scroll.ensureWidgetVisible(widget, 0, dialog.SPACING))


class GrowingDialog(QDialog):
    """Dialog whose minimum size follows its layout and never drops below `floor`: a panel, banner or message that
    appears makes it grow instead of squeezing buttons and tables.

    The content goes in `body_layout`, inside a frameless area that scrolls vertically only, and the closing row in
    `footer_layout`, which never scrolls. While the body fits the work area of the screen the minimum size follows it
    and there is no scroll bar; when it does not, the dialog takes the whole area and the body scrolls, so no button
    ends up smaller than its text and the footer stays in view."""

    MARGINS = QMargins(24, 22, 24, 20)
    SPACING = 12

    def __init__(self, parent=None, floor: QSize = QSize(0, 0)):
        super().__init__(parent)
        self.floor = floor
        self.setMinimumSize(floor)
        margins = self.MARGINS
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.body_scroll = QScrollArea()
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body_host = QWidget()
        self.body_layout = QVBoxLayout(self.body_host)
        self.body_layout.setContentsMargins(margins.left(), margins.top(), margins.right(), 0)
        self.body_layout.setSpacing(self.SPACING)
        self.body_scroll.setWidget(self.body_host)
        self.body_host.setAutoFillBackground(False)
        self.body_scroll.viewport().setAutoFillBackground(False)
        self.body_host.installEventFilter(self)
        outer.addWidget(self.body_scroll, 1)
        self.footer_host = QWidget()
        self.footer_layout = QVBoxLayout(self.footer_host)
        self.footer_layout.setContentsMargins(margins.left(), self.SPACING, margins.right(), margins.bottom())
        self.footer_layout.setSpacing(self.SPACING)
        outer.addWidget(self.footer_host)

    def content_floor(self) -> QSize:
        """Minimum size the dialog keeps regardless of content; subclasses whose floor depends on fonts override it."""
        return self.floor

    def required_size(self) -> QSize:
        """Smallest size the content needs at the current width (`content_floor` is applied by the caller)."""
        return self._minimum_of(self.layout(), self.width())

    @staticmethod
    def _minimum_of(layout, width: int) -> QSize:
        """Minimum size of a layout, with the height it needs at `width` when its content wraps."""
        need = layout.totalMinimumSize()
        if layout.hasHeightForWidth():
            need.setHeight(max(need.height(), layout.totalMinimumHeightForWidth(width)))
        return need

    def fit_body(self, limit: QSize) -> None:
        """Gives the scroll area the minimum the body needs; when body and footer together exceed `limit`, only what the
        footer leaves, and the bar takes its width from the body."""
        width = self.body_scroll.viewport().width() or self.width()
        body = self._minimum_of(self.body_host.layout(), width)
        footer = self._minimum_of(self.footer_host.layout(), self.width())
        room = max(limit.height() - footer.height(), 0)
        scrolls = body.height() > room
        bar = self.body_scroll.verticalScrollBar().sizeHint().width() if scrolls else 0
        wanted = QSize(min(body.width() + bar, limit.width()), room if scrolls else body.height())
        if self.body_scroll.minimumSize() != wanted:
            self.body_scroll.setMinimumSize(wanted)

    def keep_room(self) -> None:
        """Raises (or lowers) the minimum size to what the content needs; a window smaller than that grows, and is moved
        back inside the work area when the growth pushed it out."""
        if not self.isVisible():
            return
        frame = self.frameGeometry()
        border = frame.size() - self.geometry().size()
        limit = work_area(screen_for(frame)).size() - border
        self.fit_body(limit)
        wanted = self.content_floor().expandedTo(self.required_size()).boundedTo(limit)
        if self.minimumSize() != wanted:
            self.setMinimumSize(wanted)
            QTimer.singleShot(0, lambda: _clamp_dialog(self, whole=True))

    def eventFilter(self, watched, event) -> bool:
        if watched is self.body_host and event.type() == QEvent.Type.LayoutRequest:
            self.keep_room()
        return super().eventFilter(watched, event)

    def event(self, event) -> bool:
        handled = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest:
            self.keep_room()
        return handled

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.keep_room()


class DpiMessages(QAbstractNativeEventFilter):
    """WM_DPICHANGED for a kept window asks it to refit once it settles (Qt applies the suggested rectangle as is)."""

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_DPICHANGED:
                keeper = KEEPERS.get(int(msg.hWnd or 0))
                if keeper is not None and shiboken6.isValid(keeper):
                    keeper.request()
        return False, 0


def install(application: QApplication) -> tuple[PopupKeeper, DpiMessages | None]:
    """Installs the popup guard (and, on Windows, the DPI message watcher); the caller keeps both alive."""
    keeper = PopupKeeper(application)
    application.installEventFilter(keeper)
    messages = None
    if sys.platform == "win32":
        messages = DpiMessages()
        application.installNativeEventFilter(messages)
    return keeper, messages


def initial_geometry(available: QRect, wanted: QSize) -> QRect:
    """Window rectangle for the first show: the wanted size bounded by the work area, centred on it."""
    size = wanted.boundedTo(QSize(available.width() - 40, available.height() - 40)).expandedTo(
        minimum_for(available))
    size = size.boundedTo(available.size())
    rect = QRect(QPoint(0, 0), size)
    rect.moveCenter(available.center())
    return rect

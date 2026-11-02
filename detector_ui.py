"""
The Hallows' Eve detector's window, built with Qt (PySide6).

hallows_eve_detector.py does the watching (capture, OCR, equipping, rebirths,
Discord); this file is only the window around it. It's started from there with
run(module, interval, debug), so both share one copy of the detector module.

Layout: a sidebar (photo, status, the watched windows, session numbers, Discord)
next to the main page, which is either the window picker or the open window's
panel (live view, automation switches, hotbar, private server) above the activity
feed. Worker threads hand results to the window through a queue it empties ten
times a second.
"""

import json
import os
import queue
import re
import sys
import threading
import time

from PIL import Image
from PySide6.QtCore import (QByteArray, QEasingCurve, QPoint, QPointF, QPropertyAnimation, QRectF,
                            QSize, Qt, QTimer, Property, Signal)
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QIcon, QImage, QPainter, QPainterPath,
                           QPalette, QPen, QPixmap)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QAbstractButton, QApplication, QDialog, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
                               QPushButton, QScrollArea, QSizePolicy, QSlider, QStackedWidget,
                               QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget)

import updater

core = None  # the detector module (hallows_eve_detector); set by run() or App()

# --------------------------------------------------------------------------- #
# Look
# --------------------------------------------------------------------------- #

BG = "#0b0d12"
SIDEBAR = "#0f1218"
CARD = "#151922"
CARD_2 = "#1b202b"
BORDER = "#232a37"
HOVER = "#242b39"
TEXT = "#eef1f6"
MUTED = "#8d95a8"
SUBTLE = "#5d6577"
ACCENT = "#ff7a1a"
ACCENT_HOVER = "#ff9140"
ACCENT_SOFT = "#2b1a0e"
ON_ACCENT = "#1a0e05"
GOOD = "#34d399"
GOOD_SOFT = "#0f2a21"
WARN = "#fbbf24"
WARN_SOFT = "#2b2410"
DANGER = "#f87171"
DANGER_SOFT = "#2e1717"

THUMB_MAX = 720  # widest live preview kept (pixels); the view scales it smoothly
TRIM_EVERY = 120  # seconds between RAM trims while the switch is on

# Lucide-style line icons (24x24, drawn with the stroke colour).
ICONS = {
    "eye": '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "eye-off": '<path d="M3 3l18 18"/><path d="M10.6 5.1A10.9 10.9 0 0 1 12 5c6.5 0 10 7 10 7a17.6 17.6 '
               '0 0 1-3.2 4.2"/><path d="M6.6 6.6C3.9 8.4 2 12 2 12s3.5 7 10 7a9.7 9.7 0 0 0 5.4-1.6"/>'
               '<path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 4v5h-5"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/>',
    "repeat": '<path d="M17 2l4 4-4 4"/><path d="M3 11V9a3 3 0 0 1 3-3h15"/><path d="M7 22l-4-4 4-4"/>'
              '<path d="M21 13v2a3 3 0 0 1-3 3H3"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/>'
            '<path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
    "copy": '<rect x="9" y="9" width="12" height="12" rx="2"/>'
            '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "play": '<path d="M7 4l13 8-13 8z"/>',
    "list": '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
    "grid": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/>'
            '<rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    "pencil": '<path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/>',
    "trash": '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/>',
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
    "camera": '<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/>'
              '<circle cx="12" cy="13" r="4"/>',
    "check": '<path d="M20 6L9 17l-5-5"/>',
    "backpack": '<path d="M4 10a4 4 0 0 1 4-4h8a4 4 0 0 1 4 4v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/>'
                '<path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/><path d="M8 14h8"/>',
    "pumpkin": '<path d="M12 7c-5 0-9 2.6-9 6.5S7 20 12 20s9-2.6 9-6.5S17 7 12 7z"/>'
               '<path d="M12 7c-1.8 0-3 2.9-3 6.5S10.2 20 12 20s3-2.9 3-6.5S13.8 7 12 7z"/>'
               '<path d="M12 7V4.5c0-1 .8-1.5 2-1.5"/>',
    "chat": '<path d="M21 12a8 8 0 0 1-11.6 7.1L3 21l1.9-6.4A8 8 0 1 1 21 12z"/>',
    "alert": '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 '
             '1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "x": '<path d="M18 6L6 18M6 6l12 12"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/>'
                '<path d="M12 15V3"/>',
    "chip": '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/>'
            '<path d="M9 2v3M15 2v3M9 19v3M15 19v3M19 9h3M19 15h3M2 9h3M2 15h3"/>',
}

_icon_cache = {}


def icon_pixmap(name, color=TEXT, size=16, scale=3.0):
    key = (name, color, size, scale)
    if key not in _icon_cache:
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
               f'stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
               f'{ICONS[name]}</svg>')
        pixmap = QPixmap(int(size * scale), int(size * scale))
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        QSvgRenderer(QByteArray(svg.encode())).render(painter)
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        _icon_cache[key] = pixmap
    return _icon_cache[key]


def icon(name, color=TEXT, size=16):
    return QIcon(icon_pixmap(name, color, size))


def pick_font():
    families = set(QFontDatabase.families())
    for family in ("Segoe UI Variable Text", "Segoe UI", "Inter"):
        if family in families:
            return family
    return QApplication.font().family()


def stylesheet(font):
    return f"""
    * {{ font-family: "{font}"; }}
    QWidget {{ color: {TEXT}; font-size: 13px; }}
    QMainWindow, QDialog, #page {{ background: {BG}; }}
    #sidebar {{ background: {SIDEBAR}; border-right: 1px solid {BORDER}; }}
    QLabel {{ background: transparent; }}
    QFrame#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; }}
    QFrame#card[active="true"] {{ border: 1px solid {ACCENT}; }}
    QFrame#tile {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; }}
    QFrame#tile:hover {{ border-color: #33405a; }}
    QFrame#tile[selected="true"] {{ border: 2px solid {ACCENT}; background: #181a20; }}
    QFrame#nav {{ background: transparent; border-radius: 10px; border: 1px solid transparent; }}
    QFrame#nav:hover {{ background: {HOVER}; }}
    QFrame#nav[selected="true"] {{ background: {CARD_2}; border: 1px solid {BORDER}; }}
    QFrame#divider {{ background: {BORDER}; border: none; }}
    QFrame#stat {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 10px; }}
    QLabel#h1 {{ font-size: 24px; font-weight: 600; }}
    QLabel#h2 {{ font-size: 16px; font-weight: 600; }}
    QLabel#title {{ font-size: 13.5px; font-weight: 600; }}
    QLabel#brand {{ font-size: 15px; font-weight: 700; }}
    QLabel#section {{ color: {SUBTLE}; font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
    QLabel#muted {{ color: {MUTED}; }}
    QLabel#small {{ color: {MUTED}; font-size: 12px; }}
    QLabel#warning {{ color: {DANGER}; font-size: 12.5px; }}
    QLabel#statvalue {{ font-size: 18px; font-weight: 700; }}
    QLabel#countdown {{ font-size: 36px; font-weight: 600; color: {SUBTLE}; }}
    QLabel#countdown[active="true"] {{ color: {ACCENT}; }}
    QPushButton {{ background: {CARD_2}; border: 1px solid {BORDER}; border-radius: 9px;
                  padding: 7px 14px; color: {TEXT}; }}
    QPushButton:hover {{ background: {HOVER}; border-color: #2f3849; }}
    QPushButton:pressed {{ background: #171b24; }}
    QPushButton:disabled {{ color: {SUBTLE}; background: {CARD}; }}
    QPushButton#primary {{ background: {ACCENT}; color: {ON_ACCENT}; border: none; font-weight: 600;
                          padding: 11px 22px; font-size: 14px; }}
    QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
    QPushButton#primary:disabled {{ background: #3a2a1f; color: #7d6553; }}
    QPushButton#ghost {{ background: transparent; border: 1px solid transparent; color: {MUTED};
                        padding: 6px 10px; }}
    QPushButton#ghost:hover {{ background: {HOVER}; color: {TEXT}; }}
    QPushButton#danger {{ color: {DANGER}; }}
    QPushButton#danger:hover {{ background: {DANGER_SOFT}; border-color: #4a2424; }}
    QPushButton#chip {{ min-width: 34px; max-width: 34px; min-height: 34px; max-height: 34px;
                       padding: 0; border-radius: 10px; font-weight: 700; font-size: 14px; }}
    QPushButton#chip:checked {{ background: {ACCENT}; color: {ON_ACCENT}; border: none; }}
    QLineEdit {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 9px;
                padding: 9px 12px; selection-background-color: {ACCENT}; selection-color: {ON_ACCENT}; }}
    QLineEdit:focus {{ border-color: {ACCENT}; }}
    QLineEdit#rename {{ padding: 2px 6px; font-weight: 600; border-radius: 6px; }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
    QScrollBar::handle:vertical {{ background: #2a3140; border-radius: 3px; min-height: 36px; }}
    QScrollBar::handle:vertical:hover {{ background: #3a4356; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
    QSlider {{ min-height: 22px; }}
    QSlider::groove:horizontal {{ height: 4px; background: {HOVER}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: {TEXT}; width: 14px; height: 14px; margin: -6px 0;
                                 border-radius: 8px; border: 2px solid {ACCENT}; }}
    QSlider::handle:horizontal:hover {{ background: #ffffff; }}
    QToolTip {{ background: {CARD_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px 8px; }}
    QTextEdit#feed {{ background: transparent; border: none; font-size: 12.5px; }}
    QTableWidget {{ background: transparent; border: none; outline: none; }}
    QTableWidget::item {{ padding: 4px 6px; border-bottom: 1px solid {BORDER}; }}
    QHeaderView, QTableCornerButton::section {{ background: transparent; border: none; }}
    QHeaderView::section {{ background: transparent; color: {SUBTLE}; border: none;
                           border-bottom: 1px solid {BORDER}; font-size: 11px; font-weight: 700;
                           padding: 6px 6px; }}
    """


def dark_title_bar(widget):
    """Dark Windows title bar in the app's background colour (Windows 10/11; ignored elsewhere)."""
    try:
        import ctypes
        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi
        dark = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        r, g, b = (int(BG[i:i + 2], 16) for i in (1, 3, 5))
        colour = ctypes.c_int(r | g << 8 | b << 16)
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(colour), ctypes.sizeof(colour))
    except Exception:
        pass


def repolish(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def label(text="", name=None, wrap=False):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def button(text, command=None, name=None, icon_name=None, icon_color=None, tip=None):
    widget = QPushButton(text)
    widget.setCursor(Qt.PointingHandCursor)
    if name:
        widget.setObjectName(name)
    if icon_name:
        widget.setIcon(icon(icon_name, icon_color or (ON_ACCENT if name == "primary" else MUTED)))
        widget.setIconSize(QSize(15, 15))
    if command:
        widget.clicked.connect(lambda *_: command())
    if tip:
        widget.setToolTip(tip)
    return widget


def card(margins=(20, 18, 20, 18), spacing=12):
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    return frame, layout


def divider():
    line = QFrame()
    line.setObjectName("divider")
    line.setFixedHeight(1)
    return line


def pil_to_qimage(img):
    img = img.convert("RGB")
    data = img.tobytes()
    return QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()


def shrink(img, width=THUMB_MAX):
    """A copy of a screenshot no wider than `width` (done on the worker thread)."""
    if img.width <= width:
        return img.copy()
    return img.resize((width, max(1, int(img.height * width / img.width))), Image.BILINEAR)


def hotbar_crop(img, slots):
    """The hotbar with the names above it, cut from a screenshot: (picture, slot boxes in it)."""
    size = slots[0][3] - slots[0][1]
    x0 = max(0, min(b[0] for b in slots) - int(size * 0.35))
    x1 = min(img.width, max(b[2] for b in slots) + int(size * 0.35))
    y0 = max(0, slots[0][1] - int(size * 0.7))
    y1 = min(img.height, slots[0][3] + int(size * 0.2))
    return img.crop((x0, y0, x1, y1)), [(bx0 - x0, by0 - y0, bx1 - x0, by1 - y0)
                                        for bx0, by0, bx1, by1 in slots]


def slot_at(boxes, x, y, pad):
    for i, (x0, y0, x1, y1) in enumerate(boxes, 1):
        if x0 - pad <= x <= x1 + pad and y0 - pad <= y <= y1 + pad:
            return i
    return None


# --------------------------------------------------------------------------- #
# Widgets
# --------------------------------------------------------------------------- #

class Toggle(QAbstractButton):
    """An on/off switch with a sliding knob. `clicked` fires only when the user flips it."""

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(42, 24)
        self._knob = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._slide)

    def _get_knob(self):
        return self._knob

    def _set_knob(self, value):
        self._knob = value
        self.update()

    knob = Property(float, _get_knob, _set_knob)

    def _slide(self, on):
        self._anim.stop()
        self._anim.setStartValue(self._knob)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.4)
        off, on = QColor("#2c3443"), QColor(ACCENT)
        t = self._knob
        track = QColor(int(off.red() + (on.red() - off.red()) * t),
                       int(off.green() + (on.green() - off.green()) * t),
                       int(off.blue() + (on.blue() - off.blue()) * t))
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 12, 12)
        r = 9
        x = 3 + r + (self.width() - 6 - 2 * r) * t
        p.setBrush(QColor(0, 0, 0, 60))
        p.drawEllipse(QPointF(x, self.height() / 2 + 1), r, r)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(x, self.height() / 2), r, r)

    def sizeHint(self):
        return QSize(42, 24)


class Dot(QWidget):
    """A small status dot, with a soft glow while it's lit."""

    def __init__(self, color=SUBTLE, size=10):
        super().__init__()
        self.color, self.glow = color, False
        self.setFixedSize(size + 8, size + 8)
        self.size = size

    def set(self, color, glow=False):
        self.color, self.glow = color, glow
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2)
        p.setPen(Qt.NoPen)
        if self.glow:
            halo = QColor(self.color)
            halo.setAlpha(60)
            p.setBrush(halo)
            p.drawEllipse(c, self.size / 2 + 4, self.size / 2 + 4)
        p.setBrush(QColor(self.color))
        p.drawEllipse(c, self.size / 2, self.size / 2)


class Pill(QLabel):
    """A rounded status label: '● No event'."""

    def __init__(self, text="", color=MUTED, bg=CARD_2):
        super().__init__()
        self.setAlignment(Qt.AlignCenter)
        self.set(text, color, bg)

    def set(self, text, color, bg):
        self.plain = text
        self.setText(f'<span style="color:{color}">●</span>&nbsp;&nbsp;{text}')
        self.setStyleSheet(f"background:{bg}; color:{color}; border-radius:12px; padding:5px 12px;"
                           f"font-weight:600; font-size:12.5px;")


class IconBadge(QLabel):
    """A line icon in a small rounded square."""

    def __init__(self, name, color=MUTED, size=34):
        super().__init__()
        self.name = name
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(f"background:{CARD_2}; border-radius:10px;")
        self.set_color(color)

    def set_color(self, color):
        self.setPixmap(icon_pixmap(self.name, color, 17))


class ImageView(QWidget):
    """A picture in rounded corners, scaled smoothly to fill (cover) or fit, keeping a fixed
    aspect ratio when `ratio` is set."""

    def __init__(self, placeholder="", ratio=None, radius=12, cover=True, focus=(0.5, 0.5)):
        super().__init__()
        self.placeholder, self.ratio, self.radius = placeholder, ratio, radius
        self.cover, self.focus = cover, focus
        self.image = None
        self._scaled = None
        if ratio:
            policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            policy.setHeightForWidth(True)
            self.setSizePolicy(policy)
            self.setMinimumHeight(80)

    def hasHeightForWidth(self):
        return bool(self.ratio)

    def heightForWidth(self, width):
        return int(width / self.ratio) if self.ratio else super().heightForWidth(width)

    def sizeHint(self):
        return QSize(320, int(320 / self.ratio) if self.ratio else 180)

    def set_image(self, image):
        self.image = image if image is not None and not image.isNull() else None
        self._scaled = None
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        r = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(r, self.radius, self.radius)
        p.setClipPath(path)
        p.fillRect(r, QColor(CARD_2))
        if self.image is None:
            p.setPen(QColor(SUBTLE))
            p.drawText(r, Qt.AlignCenter | Qt.TextWordWrap, self.placeholder)
            return
        iw, ih = self.image.width(), self.image.height()
        s = (max if self.cover else min)(r.width() / iw, r.height() / ih)
        w, h = iw * s, ih * s
        dpr = self.devicePixelRatioF()
        target = (int(w * dpr), int(h * dpr))
        if self._scaled is None or (self._scaled.width(), self._scaled.height()) != target:
            self._scaled = self.image.scaled(target[0], target[1], Qt.IgnoreAspectRatio,
                                             Qt.SmoothTransformation)
            self._scaled.setDevicePixelRatio(dpr)
        x = r.x() + (r.width() - w) * self.focus[0]
        y = r.y() + (r.height() - h) * self.focus[1]
        p.drawImage(QPointF(x, y), self._scaled)


class HotbarStrip(QWidget):
    """A window's hotbar, cut from its screenshot. Each slot shows its key; chosen slots are
    outlined in orange with their priority (1st, 2nd, ...), slots whose character already has
    Dracula get a red D, and clicking a slot emits slot_clicked."""

    slot_clicked = Signal(int)

    def __init__(self, height=96, placeholder="Looking for the hotbar…", fit_height=False):
        super().__init__()
        self.setFixedHeight(height)
        self.base_height, self.fit_height = height, fit_height
        self.setMouseTracking(True)
        self.placeholder = placeholder
        self.image = None
        self.boxes = []  # in image pixels
        self.chosen, self.mutated, self.hover = [], set(), None
        self._shown = []  # boxes in widget coordinates
        self._scaled = None

    def set_picture(self, image, boxes):
        self.image, self.boxes = image, boxes
        self._scaled = None
        self._fit()
        self.update()

    def _fit(self):
        """With fit_height, as tall as the picture is at this width (no empty bands)."""
        if not self.fit_height:
            return
        if self.image is None:
            height = self.base_height
        else:
            height = int(min(self.width() / self.image.width(), 3.5) * self.image.height())
            height = max(60, min(self.base_height, height))
        if height != self.height():
            self.setFixedHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def set_marks(self, chosen=None, mutated=None):
        if chosen is not None:
            self.chosen = list(chosen)
        if mutated is not None:
            self.mutated = set(mutated)
        self.update()

    def _layout(self):
        if self.image is None:
            return None
        iw, ih = self.image.width(), self.image.height()
        s = min(self.width() / iw, self.height() / ih, 3.5)
        w, h = iw * s, ih * s
        x, y = (self.width() - w) / 2, (self.height() - h) / 2
        return x, y, w, h, s

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform
                         | QPainter.TextAntialiasing)
        fit = self._layout()
        if fit is None:
            r = QRectF(self.rect())
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(CARD_2))
            p.drawRoundedRect(r, 12, 12)
            p.setPen(QColor(SUBTLE))
            p.drawText(r, Qt.AlignCenter | Qt.TextWordWrap, self.placeholder)
            self._shown = []
            return
        x, y, w, h, s = fit
        dpr = self.devicePixelRatioF()
        target = (int(w * dpr), int(h * dpr))
        if self._scaled is None or (self._scaled.width(), self._scaled.height()) != target:
            self._scaled = self.image.scaled(target[0], target[1], Qt.IgnoreAspectRatio,
                                             Qt.SmoothTransformation)
            self._scaled.setDevicePixelRatio(dpr)
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, w, h), 10, 10)
        p.save()
        p.setClipPath(path)
        p.drawImage(QPointF(x, y), self._scaled)
        p.restore()
        self._shown = [(x + bx0 * s, y + by0 * s, x + bx1 * s, y + by1 * s)
                       for bx0, by0, bx1, by1 in self.boxes]
        if self._shown:
            side = self._shown[0][2] - self._shown[0][0]
            self._paint_marks(p, max(7.0, min(11.0, side * 0.17)))

    def _paint_marks(self, p, r):
        font = QFont(self.font())
        font.setBold(True)
        font.setPixelSize(max(9, int(r * 1.15)))
        p.setFont(font)
        for i, (x0, y0, x1, y1) in enumerate(self._shown, 1):
            chosen, hover = i in self.chosen, i == self.hover
            if chosen or hover:
                p.setPen(QPen(QColor(ACCENT if chosen else ACCENT_HOVER), 3 if chosen else 2))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(QRectF(x0 - 3, y0 - 3, x1 - x0 + 6, y1 - y0 + 6), 7, 7)
            if i in self.mutated:
                c = QPointF(x1 - r + 2, y0 + r - 2)
                p.setPen(QPen(QColor(BG), 1.5))
                p.setBrush(QColor(DANGER))
                p.drawEllipse(c, r, r)
                p.setPen(QColor("#ffffff"))
                p.drawText(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), Qt.AlignCenter, "D")
            c = QPointF(x0 + r - 2, y0 + r - 2)
            p.setPen(QPen(QColor(ACCENT if (chosen or hover) else "#5d6577"), 1.3))
            p.setBrush(QColor(ACCENT if chosen else BG))
            p.drawEllipse(c, r, r)
            p.setPen(QColor(ON_ACCENT if chosen else TEXT))
            p.drawText(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), Qt.AlignCenter, str(i % 10))
            if chosen and len(self.chosen) > 1:
                text = core.ordinal(self.chosen.index(i) + 1)
                w = r * 3.2
                box = QRectF((x0 + x1) / 2 - w / 2, y1 - r * 1.9, w, r * 1.7)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(ACCENT))
                p.drawRoundedRect(box, r * 0.85, r * 0.85)
                p.setPen(QColor(ON_ACCENT))
                p.drawText(box, Qt.AlignCenter, text)

    def mouseMoveEvent(self, event):
        slot = slot_at(self._shown, event.position().x(), event.position().y(), 3)
        self.setCursor(Qt.PointingHandCursor if slot else Qt.ArrowCursor)
        if slot != self.hover:
            self.hover = slot
            self.update()
            self.hover_changed(slot)

    def hover_changed(self, slot):
        pass

    def leaveEvent(self, _):
        if self.hover is not None:
            self.hover = None
            self.update()
            self.hover_changed(None)

    def mouseReleaseEvent(self, event):
        slot = slot_at(self._shown, event.position().x(), event.position().y(), 3)
        if slot and event.button() == Qt.LeftButton:
            self.slot_clicked.emit(slot)

    def click_slot(self, slot):
        """For tests: the same as clicking that slot."""
        self.slot_clicked.emit(slot)


class StepSlider(QSlider):
    """A slider over a list of stops that jumps to where it's clicked. `released` fires once
    the user lets go (after a drag or a click)."""

    released = Signal(int)

    def __init__(self, count, index):
        super().__init__(Qt.Horizontal)
        self.setRange(0, count - 1)
        self.setPageStep(1)
        self.setValue(index)
        self.setCursor(Qt.PointingHandCursor)

    def _value_at(self, x):
        handle = 14
        share = (x - handle / 2) / max(1, self.width() - handle)
        return round(min(1.0, max(0.0, share)) * self.maximum())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setValue(self._value_at(event.position().x()))
            self.setSliderDown(True)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            self.setValue(self._value_at(event.position().x()))

    def mouseReleaseEvent(self, event):
        if self.isSliderDown():
            self.setSliderDown(False)
            self.released.emit(self.value())


class NavItem(QFrame):
    """A watched window in the sidebar: status dot, name and status line. Click to open it,
    double-click (or the pencil in its panel) to rename it in place."""

    clicked = Signal()
    renamed = Signal(str)

    def __init__(self, name):
        super().__init__()
        self.setObjectName("nav")
        self.setAttribute(Qt.WA_Hover)
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("selected", False)
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 8, 10, 8)
        row.setSpacing(8)
        self.dot = Dot(SUBTLE, 8)
        row.addWidget(self.dot, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.name = label(name, "title")
        self.sub = label("Starting…", "small")
        col.addWidget(self.name)
        col.addWidget(self.sub)
        row.addLayout(col, 1)
        self.countdown = label("", "title")
        self.countdown.setStyleSheet(f"color:{ACCENT};")
        row.addWidget(self.countdown)
        self.edit = None

    def set_selected(self, on):
        self.setProperty("selected", bool(on))
        repolish(self)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.edit is None:
            self.clicked.emit()

    def mouseDoubleClickEvent(self, _):
        self.start_rename()

    def start_rename(self):
        if self.edit is not None:
            return
        self.edit = QLineEdit(self.name.text())
        self.edit.setObjectName("rename")
        self.edit.selectAll()
        self.name.hide()
        self.name.parentWidget().layout().itemAt(1).layout().insertWidget(0, self.edit)
        self.edit.returnPressed.connect(lambda: self.finish_rename(True))
        self.edit.editingFinished.connect(lambda: self.finish_rename(True))
        self.edit.installEventFilter(self)
        self.edit.setFocus()

    def eventFilter(self, obj, event):
        if obj is self.edit and event.type() == event.Type.KeyPress and event.key() == Qt.Key_Escape:
            self.finish_rename(False)
            return True
        return False

    def finish_rename(self, keep):
        edit = self.edit
        if edit is None:
            return  # already finished (Enter, then the focus leaving)
        self.edit = None
        text = edit.text()
        edit.removeEventFilter(self)
        edit.hide()
        edit.deleteLater()
        self.name.show()
        if keep:
            self.renamed.emit(text)


class SettingRow(QWidget):
    """Icon, title and explanation on the left, a switch on the right."""

    def __init__(self, icon_name, title, subtitle, toggle):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 8, 0, 8)
        row.setSpacing(12)
        self.badge = IconBadge(icon_name)
        row.addWidget(self.badge)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(label(title, "title"))
        self.subtitle = label(subtitle, "small", wrap=True)
        col.addWidget(self.subtitle)
        row.addLayout(col, 1)
        row.addWidget(toggle)
        self.toggle = toggle

    def set_on(self, on):
        self.badge.set_color(ACCENT if on else MUTED)


class WindowTile(QFrame):
    """A Roblox window in the picker: preview, name, size and whether it'll be watched."""

    toggled = Signal()

    def __init__(self, name, meta, thumb):
        super().__init__()
        self.setObjectName("tile")
        self.setAttribute(Qt.WA_Hover)
        self.setCursor(Qt.PointingHandCursor)
        col = QVBoxLayout(self)
        col.setContentsMargins(12, 12, 12, 14)
        col.setSpacing(10)
        self.view = ImageView("No preview (minimised?)", ratio=16 / 9, radius=10)
        if thumb is not None:
            self.view.set_image(pil_to_qimage(thumb))
        col.addWidget(self.view)
        row = QHBoxLayout()
        row.setSpacing(8)
        text = QVBoxLayout()
        text.setSpacing(1)
        text.addWidget(label(name, "title"))
        text.addWidget(label(meta, "small"))
        row.addLayout(text, 1)
        self.check = QLabel()
        self.check.setFixedSize(26, 26)
        self.check.setAlignment(Qt.AlignCenter)
        row.addWidget(self.check)
        col.addLayout(row)
        self.set_selected(True)

    def set_selected(self, on):
        self.selected = on
        self.setProperty("selected", on)
        if on:
            self.check.setPixmap(icon_pixmap("check", ON_ACCENT, 15))
            self.check.setStyleSheet(f"background:{ACCENT}; border-radius:13px;")
        else:
            self.check.setPixmap(QPixmap())
            self.check.setStyleSheet("background:transparent; border:2px solid #3a4356;"
                                     "border-radius:13px;")
        repolish(self)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.toggled.emit()


class Feed(QTextEdit):
    """The activity feed: time-stamped lines, newest at the bottom."""

    COLORS = {None: TEXT, "start": ACCENT, "end": MUTED, "error": DANGER}

    def __init__(self):
        super().__init__()
        self.setObjectName("feed")
        self.setReadOnly(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lines = []

    def add(self, text, tag=None):
        self.lines.append(text)
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.append(f'<span style="color:{SUBTLE}">{time.strftime("%H:%M:%S")}</span>'
                    f'&nbsp;&nbsp;&nbsp;<span style="color:{self.COLORS.get(tag, TEXT)}">{safe}</span>')
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())


# --------------------------------------------------------------------------- #
# Dialogs
# --------------------------------------------------------------------------- #

class Dialog(QDialog):
    def __init__(self, app, title):
        super().__init__(app)
        self.app = app
        self.setWindowTitle(title)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setModal(False)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(26, 22, 26, 22)
        self.body.setSpacing(14)

    def show_centred(self):
        self.adjustSize()
        frame = self.app.frameGeometry()
        self.move(frame.x() + (frame.width() - self.width()) // 2, frame.y() + 80)
        self.show()
        dark_title_bar(self)
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(0, self.adjustSize)  # again, once pictures have settled their size


class RebirthLogDialog(Dialog):
    """Every rebirth auto rebirth has done in one window: time, cash and cost."""

    def __init__(self, app, hwnd):
        self.label, self.pid = app.rows[hwnd]["label"], app.rows[hwnd]["pid"]
        super().__init__(app, f"Rebirths · {self.label}")
        self.setMinimumSize(560, 520)
        self.body.addWidget(label(f"Rebirths · {self.label}", "h2"))
        self.summary = label("", "muted", wrap=True)
        self.body.addWidget(self.summary)
        frame, inner = card((14, 8, 14, 10), 0)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["TIME", "CASH", "COST"])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.verticalHeader().setDefaultSectionSize(34)
        inner.addWidget(self.table)
        self.body.addWidget(frame, 1)
        buttons = QHBoxLayout()
        buttons.addWidget(button("Open full log file", self._open_file, icon_name="file"))
        self.clear_btn = button("Clear log", self._clear, "danger", "trash", DANGER)
        buttons.addWidget(self.clear_btn)
        buttons.addStretch(1)
        buttons.addWidget(button("Close", self.close, "primary"))
        self.body.addLayout(buttons)
        self._confirm = None
        self.refresh()

    def refresh(self):
        rows = core.read_rebirth_log(self.pid, self.label)
        if not rows:
            self.summary.setText(f"PID {self.pid} · no rebirths yet. They show up here as "
                                 "auto rebirth does them.")
        else:
            times = [time.mktime(time.strptime(r["time"], "%Y-%m-%d %H:%M:%S")) for r in rows]
            text = (f"PID {self.pid} · {core.plural(len(rows), 'rebirth')} · last at "
                    f"{rows[-1]['time'][11:16]}")
            if len(rows) > 1:
                every = (times[-1] - times[0]) / (len(rows) - 1)
                text += (f" · about every {every / 60:.0f} min" if every >= 60
                         else f" · about every {every:.0f} s")
            self.summary.setText(text)
        today = time.strftime("%Y-%m-%d")
        self.table.setRowCount(len(rows))
        for i, row in enumerate(reversed(rows)):  # newest first
            day, clock = row["time"][:10], row["time"][11:]
            cells = [(clock if day == today else f"{day} {clock[:5]}", TEXT),
                     (row["cash"], GOOD), (row["cost"], MUTED)]
            for j, (text, color) in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setForeground(QColor(color))
                self.table.setItem(i, j, item)

    def _open_file(self):
        if core.REBIRTH_LOG_PATH.exists():
            os.startfile(core.REBIRTH_LOG_PATH)

    def _clear(self):
        """First click asks; a second click within a few seconds clears this window's rebirths
        (other windows' stay in the file)."""
        count = len(core.read_rebirth_log(self.pid, self.label))
        if not count:
            return
        if self._confirm is None:
            self.clear_btn.setText(f"Click again to delete {core.plural(count, 'rebirth')}")
            self._confirm = QTimer(self)
            self._confirm.setSingleShot(True)
            self._confirm.timeout.connect(self._cancel_clear)
            self._confirm.start(4000)
            return
        self._cancel_clear()
        removed = core.clear_rebirth_log(self.pid, self.label)
        self.app._log(f"{self.label}: cleared {core.plural(removed, 'rebirth')} from the rebirth log.")
        self.refresh()

    def _cancel_clear(self):
        if self._confirm is not None:
            self._confirm.stop()
        self._confirm = None
        self.clear_btn.setText("Clear log")


class PickerStrip(HotbarStrip):
    def __init__(self, picker):
        super().__init__(220, "", fit_height=True)
        self.picker = picker

    def hover_changed(self, slot):
        self.picker._show_hint(slot)


class SlotPickerDialog(Dialog):
    """Choose a window's characters by clicking them in a screenshot of its hotbar. The
    choice is saved on every click. Key buttons do the same when the hotbar can't be seen."""

    def __init__(self, app, hwnd):
        self.hwnd = hwnd
        self.label = app.rows[hwnd]["label"]
        super().__init__(app, f"Auto equip · {self.label}")
        self.setMinimumWidth(820)
        self.chosen = list(app.slots.get(hwnd, []))
        self.mutated = set(app.mutated.get(hwnd, set()))
        self.body.addWidget(label(f"Which characters should {self.label} equip?", "h2"))
        self.body.addWidget(label(
            "Click characters in the hotbar to choose them, and click again to remove them. "
            "During Hallows' Eve the first one you picked without Dracula is held, then swapped "
            "for the next once it gets Dracula.", "muted", wrap=True))
        frame, inner = card((14, 14, 14, 12), 8)
        self.strip = PickerStrip(self)
        self.strip.slot_clicked.connect(self.select)
        inner.addWidget(self.strip)
        self.hint = label("", "small")
        inner.addWidget(self.hint)
        self.warning = label("", "warning")
        inner.addWidget(self.warning)
        self.body.addWidget(frame)

        keys = QHBoxLayout()
        keys.setSpacing(6)
        keys.addWidget(label("OR PICK KEYS", "section"))
        keys.addSpacing(6)
        self.chips = {}
        for slot in range(1, 11):
            chip = QPushButton(str(slot % 10))
            chip.setObjectName("chip")
            chip.setCheckable(True)
            chip.setCursor(Qt.PointingHandCursor)
            chip.clicked.connect(lambda _=False, s=slot: self.select(s))
            keys.addWidget(chip)
            self.chips[slot] = chip
        keys.addStretch(1)
        self.body.addLayout(keys)

        buttons = QHBoxLayout()
        buttons.addWidget(button("New screenshot", self.capture, icon_name="camera"))
        buttons.addWidget(button("Don't equip anything", self.clear, icon_name="x"))
        buttons.addStretch(1)
        self.test_btn = button(f"Test for {core.TEST_EVENT_SECONDS} s", self._test, icon_name="play")
        buttons.addWidget(self.test_btn)
        buttons.addWidget(button("Done", self.close, "primary"))
        self.body.addLayout(buttons)
        self.capture()
        self._style()

    def capture(self):
        """Take a fresh screenshot of the window and find its hotbar."""
        stream = core.get_stream(self.hwnd)
        img = stream.latest() if stream else None
        slots = core.find_hotbar_slots(img) if img else []
        if not slots:
            reason = ("Can't see the window right now (minimised?)." if img is None
                      else "Couldn't find the hotbar in this window.")
            self.strip.placeholder = f"{reason}\nPick the keys below instead."
            self.strip.set_picture(None, [])
            self.hint.setText("")
            return
        # Read the labels above the slots now, so Dracula marks are up to date.
        self.mutated = {i for i, text in enumerate(core.read_slot_labels(img, slots), 1)
                        if core.is_dracula(text)}
        self.app._apply_mutations(self.hwnd, self.mutated)
        picture, boxes = hotbar_crop(img, slots)
        self.strip.set_picture(pil_to_qimage(picture), boxes)
        self._style()

    def _show_hint(self, slot):
        if not self.strip.boxes:
            return
        if slot:
            parts = [f"Slot {slot}", f"key {slot % 10}"]
            if slot in self.mutated:
                parts.append("has Dracula")
            parts.append("click to remove" if slot in self.chosen else "click to add")
            self.hint.setText("  ·  ".join(parts))
        else:
            self.hint.setText(f"Found {core.plural(len(self.strip.boxes), 'slot')} in "
                              f"{self.label}'s hotbar. Hover to see each key; D = already has Dracula.")

    def select(self, slot):
        """Add the slot to the choice, or take it out if it's already chosen."""
        self.chosen = core.toggle_slot(self.chosen, slot)
        self.app._set_slots(self.hwnd, self.chosen)
        self._style()

    def clear(self):
        self.chosen = []
        self.app._set_slots(self.hwnd, [])
        self._style()

    def _style(self):
        self.strip.set_marks(self.chosen, self.mutated)
        clash = bool(set(self.chosen) & self.mutated)
        self.warning.setText(f"⚠  {core.MUTATED_WARNING}" if clash else "")
        self.warning.setVisible(clash)
        for slot, chip in self.chips.items():
            chip.setChecked(slot in self.chosen)
        self._show_hint(self.strip.hover)

    def _test(self):
        self.app._simulate(self.hwnd)
        self.close()


class UpdateDialog(Dialog):
    """Check GitHub for a newer version and install it (then the app restarts)."""

    def __init__(self, app):
        super().__init__(app, "Updates")
        self.setMinimumWidth(520)
        self.newest = None
        head = QHBoxLayout()
        head.setSpacing(12)
        self.badge = IconBadge("download", MUTED, 40)
        head.addWidget(self.badge)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title = label("Checking for updates…", "h2")
        titles.addWidget(self.title)
        self.version = label("", "small")
        titles.addWidget(self.version)
        head.addLayout(titles, 1)
        self.body.addLayout(head)
        self.detail = label("", "muted", wrap=True)
        self.body.addWidget(self.detail)
        buttons = QHBoxLayout()
        self.again_btn = button("Check again", self.check, icon_name="refresh")
        buttons.addWidget(self.again_btn)
        buttons.addStretch(1)
        buttons.addWidget(button("Close", self.close))
        self.install_btn = button("Install and restart", self.install, "primary", "download")
        self.install_btn.hide()
        buttons.addWidget(self.install_btn)
        self.body.addLayout(buttons)
        mine = updater.installed()
        self.version.setText(f"This copy: {updater.short(mine['sha'])}" if mine
                             else "This copy: version not known")

    def check(self):
        self.title.setText("Checking for updates…")
        self.detail.setText("")
        self.install_btn.hide()
        self.again_btn.setEnabled(False)
        self.app._check_for_updates(quiet=False)

    def show_result(self, state, mine, newest, error):
        self.again_btn.setEnabled(True)
        self.newest = newest
        if error:
            self.title.setText("Couldn't check for updates")
            self.detail.setText(f"{error}.")
            self.badge.set_color(WARN)
            return
        what = f"{updater.short(newest['sha'])} from {newest['date']}: {newest['message']}"
        if state == "up_to_date":
            self.title.setText("You're up to date")
            self.detail.setText(f"This is the newest version ({what}).")
            self.badge.set_color(GOOD)
        else:
            self.title.setText("Update available" if state == "available" else "Newest version")
            self.detail.setText(f"Newest version: {what}." + (
                "" if state == "available" else
                " This copy's version isn't known (it was probably downloaded by hand), so "
                "installing the newest makes sure you have it.") +
                " Your settings, logs and links are kept. Watching stops for a moment while "
                "the app restarts.")
            self.badge.set_color(ACCENT)
            self.install_btn.show()
        self.adjustSize()

    def install(self):
        if not self.newest:
            return
        self.install_btn.setEnabled(False)
        self.again_btn.setEnabled(False)
        self.title.setText("Installing the update…")
        self.app._install_update(self.newest)

    def show_progress(self, text):
        self.detail.setText(text)

    def show_done(self, ok, text):
        if ok:
            self.title.setText("Restarting…")
            self.detail.setText(text)
            self.badge.set_color(GOOD)
        else:
            self.title.setText("The update didn't install")
            self.detail.setText(f"{text}. Nothing else changed; you can keep using this version.")
            self.badge.set_color(DANGER)
            self.install_btn.setEnabled(True)
            self.again_btn.setEnabled(True)


# --------------------------------------------------------------------------- #
# The main window
# --------------------------------------------------------------------------- #

class App(QMainWindow):
    def __init__(self, interval=1.0, debug=False, core_module=None):
        global core
        if core_module is not None:
            core = core_module
        super().__init__()
        self.interval, self.debug = interval, debug
        self.events = queue.Queue()  # worker threads -> window
        self.run_id = 0  # events from an earlier scan or watch are ignored
        self.stop_event = None
        self.windows = []  # [(hwnd, pid, title, PIL thumbnail or None)]
        self.chosen = {}  # hwnd -> ticked in the picker
        self.tiles = {}  # hwnd -> WindowTile
        self.rows = {}  # hwnd -> widgets and facts of its panel
        self.active = set()
        self.events_seen = 0
        self.discord_on = True
        self.detecting = {}  # hwnd -> look for Hallows' Eve in it (on by default)
        self.anti_disconnect = {}  # hwnd -> reconnect / close the "Welcome Back~" popup
        # Kept by Roblox process ID while the app is open (e.g. across Change windows).
        self.session_detect_off = set()
        self.session_anti_off = set()
        self.private_servers = core.load_private_servers()  # pid -> private server link
        self.popups = self.notifier = self.equipper = self.rebirther = None
        self.rebirth_every = {}  # hwnd -> seconds between auto rebirth checks
        self.rebirth_on = {}  # hwnd -> auto rebirth switched on
        # Equip slots and auto rebirth start off at every launch; these keep them (by
        # Roblox process ID) only while the app is open, e.g. across Change windows.
        self.session_slots = {}
        self.session_rebirth = set()
        self.session_rebirth_every = {}  # pid -> seconds; back to REBIRTH_EVERY every launch
        self.window_names = core.load_window_names()  # pid -> name the user gave the window
        self.names = {}  # hwnd -> name, read by the monitor thread for its messages
        self.default_names = {}
        self.current_tab = None  # pid of the window whose panel is open
        self.log_windows = {}  # hwnd -> its open RebirthLogDialog
        self.slots = {}  # hwnd -> hotbar slots to equip while the event is on
        self.simulated = {}  # hwnd -> when its test event ends (time.monotonic())
        self.mutated = {}  # hwnd -> hotbar slots last seen with Dracula
        self.slot_picker = None
        self.feed = None
        self.update_dialog = None
        self.update_state = None  # 'up_to_date' / 'available' / 'unknown' after a check

        self.setWindowTitle("Hallows' Eve Detector")
        if core.ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(core.ICON_PATH)))
        self.setMinimumSize(900, 620)
        self._build()
        self._restore_geometry()
        self.show_picker()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(100)
        QTimer.singleShot(3000, lambda: self._check_for_updates(quiet=True))

    # ------------------------------------------------------------------ layout

    def _build(self):
        root = QWidget()
        root.setObjectName("page")
        self.setCentralWidget(root)
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._build_sidebar())
        self.pages = QStackedWidget()
        row.addWidget(self.pages, 1)

    def _build_sidebar(self):
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(272)
        col = QVBoxLayout(side)
        col.setContentsMargins(18, 20, 18, 18)
        col.setSpacing(0)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        logo = QLabel()
        logo.setFixedSize(38, 38)
        logo.setAlignment(Qt.AlignCenter)
        logo.setPixmap(icon_pixmap("pumpkin", ACCENT, 22))
        logo.setStyleSheet(f"background:{ACCENT_SOFT}; border-radius:11px;")
        brand.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(label("Hallows' Eve", "brand"))
        names.addWidget(label("Event detector", "small"))
        brand.addLayout(names, 1)
        col.addLayout(brand)
        col.addSpacing(18)

        self.photo = ImageView(radius=14, focus=(0.72, 0.5))
        self.photo.setFixedHeight(150)
        if core.PHOTO_PATH.exists():
            self.photo.set_image(QImage(str(core.PHOTO_PATH)))
        col.addWidget(self.photo)
        col.addSpacing(14)
        self.status = Pill("Choose windows", MUTED, CARD_2)
        col.addWidget(self.status, 0, Qt.AlignLeft)
        col.addSpacing(22)

        head = QHBoxLayout()
        head.addWidget(label("WINDOWS", "section"))
        head.addStretch(1)
        self.manage_btn = button("Manage", self.show_picker, "ghost", "grid",
                                 tip="Choose which Roblox windows to watch")
        head.addWidget(self.manage_btn)
        col.addLayout(head)
        col.addSpacing(6)
        self.nav_box = QVBoxLayout()
        self.nav_box.setSpacing(4)
        col.addLayout(self.nav_box)
        self.nav_empty = label("Pick the Roblox windows to watch.", "small", wrap=True)
        col.addWidget(self.nav_empty)
        col.addStretch(1)

        trim = QFrame()
        trim.setObjectName("stat")
        trim.setToolTip("Frees the memory your Roblox windows aren't using right now, every "
                        f"{TRIM_EVERY // 60} minutes (what the F6 ram dump hotkey does)")
        trow = QHBoxLayout(trim)
        trow.setContentsMargins(12, 10, 12, 10)
        trow.setSpacing(10)
        self.trim_badge = IconBadge("chip", MUTED, 30)
        trow.addWidget(self.trim_badge)
        tcol = QVBoxLayout()
        tcol.setSpacing(0)
        tcol.addWidget(label("Trim Roblox RAM", "title"))
        self.trim_sub = label("Off", "small")
        tcol.addWidget(self.trim_sub)
        trow.addLayout(tcol, 1)
        self.trim_toggle = Toggle(False)
        self.trim_toggle.clicked.connect(lambda *_: self._toggle_trim())
        trow.addWidget(self.trim_toggle)
        col.addWidget(trim)
        col.addSpacing(12)
        self.trim_timer = QTimer(self)
        self.trim_timer.setInterval(TRIM_EVERY * 1000)
        self.trim_timer.timeout.connect(self._trim_ram)
        self.trim_busy = False

        stats = QHBoxLayout()
        stats.setSpacing(8)
        self.stats = {}
        for key, title in [("watching", "Watching"), ("active", "Active"), ("seen", "Events")]:
            box = QFrame()
            box.setObjectName("stat")
            inner = QVBoxLayout(box)
            inner.setContentsMargins(10, 8, 10, 8)
            inner.setSpacing(0)
            value = label("—", "statvalue")
            inner.addWidget(value)
            inner.addWidget(label(title, "small"))
            stats.addWidget(box)
            self.stats[key] = value
        col.addLayout(stats)
        col.addSpacing(12)

        discord = QFrame()
        discord.setObjectName("stat")
        drow = QHBoxLayout(discord)
        drow.setContentsMargins(12, 10, 12, 10)
        drow.setSpacing(10)
        self.discord_badge = IconBadge("chat", MUTED, 30)
        drow.addWidget(self.discord_badge)
        dcol = QVBoxLayout()
        dcol.setSpacing(0)
        dcol.addWidget(label("Discord pings", "title"))
        self.discord_sub = label("Starts with watching", "small")
        dcol.addWidget(self.discord_sub)
        drow.addLayout(dcol, 1)
        self.discord_toggle = Toggle(True)
        self.discord_toggle.clicked.connect(lambda *_: self._toggle_discord())
        self.discord_toggle.setEnabled(False)
        drow.addWidget(self.discord_toggle)
        col.addWidget(discord)
        col.addSpacing(8)
        urow = QHBoxLayout()
        urow.setSpacing(4)
        mine = updater.installed()
        self.version_label = label(f"Version {updater.short(mine['sha'])}" if mine else "Version —",
                                   "small")
        urow.addWidget(self.version_label)
        urow.addStretch(1)
        self.update_btn = button("Check for updates", self._show_updates, "ghost", "refresh",
                                 tip="See if there's a newer version on GitHub")
        urow.addWidget(self.update_btn)
        col.addLayout(urow)
        return side

    def _scroll_page(self, margins=(34, 30, 34, 30), spacing=18):
        """A page that scrolls when the window is too short for it."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        content.setObjectName("page")
        col = QVBoxLayout(content)
        col.setContentsMargins(*margins)
        col.setSpacing(spacing)
        scroll.setWidget(content)
        return scroll, content, col

    def _set_page(self, widget):
        old = self.pages.currentWidget()
        self.pages.addWidget(widget)
        self.pages.setCurrentWidget(widget)
        if old is not None:
            self.pages.removeWidget(old)
            old.deleteLater()

    def _restore_geometry(self):
        """Last session's window size and position (old 'WxH+X+Y' settings work too)."""
        try:
            saved = json.loads(core.UI_SETTINGS_PATH.read_text())
            if "qt_geometry" in saved:
                if self.restoreGeometry(QByteArray.fromBase64(saved["qt_geometry"].encode())):
                    return
            w, h, x, y = (int(v) for v in re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)",
                                                     saved["geometry"]).groups())
            if QApplication.screenAt(QPoint(x + 60, y + 20)) is not None:  # still on a screen
                self.setGeometry(x, y, w, h)
                if saved.get("maximized"):
                    self.setWindowState(Qt.WindowMaximized)
                return
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            pass
        screen = QApplication.primaryScreen().availableGeometry()
        w, h = min(1280, screen.width() - 80), min(860, screen.height() - 80)
        self.setGeometry(screen.x() + (screen.width() - w) // 2,
                         screen.y() + (screen.height() - h) // 2, w, h)

    def _save_geometry(self):
        try:
            normal = self.normalGeometry()
            core.UI_SETTINGS_PATH.write_text(json.dumps({
                "qt_geometry": bytes(self.saveGeometry().toBase64()).decode(),
                # Also in the old form, so the Tkinter backup opens in the same place.
                "geometry": f"{normal.width()}x{normal.height()}+{normal.x()}+{normal.y()}",
                "maximized": self.isMaximized()}))
        except OSError:
            pass

    def _set_status(self, text, color, bg=CARD_2):
        self.status.set(text, color, bg)

    def _update_stats(self):
        watching = len(self.rows)
        self.stats["watching"].setText(str(watching) if watching else "—")
        self.stats["active"].setText(str(len(self.active)) if watching else "—")
        self.stats["active"].setStyleSheet(f"color:{ACCENT};" if self.active else "")
        self.stats["seen"].setText(str(self.events_seen))
        if self.active:
            lefts = [self.rows[h].get("left_s") for h in self.active if h in self.rows]
            lefts = [s for s in lefts if s is not None]
            prefix = f"{core.fmt_clock(min(lefts))} · " if lefts else ""
            self.setWindowTitle(f"{prefix}Hallows' Eve active · Hallows' Eve Detector")
        else:
            self.setWindowTitle("Hallows' Eve Detector")

    def _clear_nav(self):
        while self.nav_box.count():
            item = self.nav_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.nav_empty.show()

    # ------------------------------------------------------------------ picker

    def show_picker(self):
        self._close_slot_picker()
        self._close_log_windows()
        self._stop_watching()
        core.show_all_hidden()
        self.rows, self.active = {}, set()
        self.feed = None
        self._clear_nav()
        self.manage_btn.setEnabled(False)
        self._update_stats()
        self._set_status("Choose windows", MUTED)
        self.discord_toggle.setEnabled(False)
        self.discord_sub.setText("Starts with watching")

        page, content, col = self._scroll_page()
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(label("Choose windows", "h1"))
        titles.addWidget(label("Click a window to include or skip it. Windows can sit behind "
                               "others, just not minimised.", "muted", wrap=True))
        head.addLayout(titles, 1)
        self.refresh_btn = button("Refresh", self.refresh, icon_name="refresh")
        head.addWidget(self.refresh_btn, 0, Qt.AlignTop)
        col.addLayout(head)
        col.addSpacing(4)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(16)
        col.addWidget(self.grid_host)
        col.addStretch(1)

        wrapper = QWidget()
        wrapper.setObjectName("page")
        outer = QVBoxLayout(wrapper)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(page, 1)
        bar = QFrame()
        bar.setStyleSheet(f"QFrame {{ background:{BG}; border-top:1px solid {BORDER}; }}")
        brow = QHBoxLayout(bar)
        brow.setContentsMargins(34, 14, 34, 16)
        self.picker_note = label("", "small")
        brow.addWidget(self.picker_note, 1)
        self.start_btn = button("Start watching", self.start_watching, "primary", "play")
        brow.addWidget(self.start_btn)
        outer.addWidget(bar)
        self._set_page(wrapper)
        self.refresh()

    def refresh(self):
        self.run_id += 1
        run = self.run_id
        self.refresh_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self._grid_message("Looking for Roblox windows…")

        def scan():
            # Each snapshot waits for a captured frame, so keep this off the UI thread.
            found = []
            with core.open_screen() as sct:
                for hwnd, pid, title in core.find_roblox_windows():
                    shot = core.snapshot(hwnd, sct)
                    found.append((hwnd, pid, title, shrink(shot, 640) if shot else None))
            self.events.put(("windows", run, found))

        threading.Thread(target=scan, daemon=True).start()

    def _grid_message(self, text):
        self._clear_grid()
        message = label(text, "muted")
        message.setAlignment(Qt.AlignCenter)
        message.setMinimumHeight(220)
        self.grid.addWidget(message, 0, 0)

    def _clear_grid(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _show_windows(self, found):
        self.windows = found
        self.chosen = {hwnd: self.chosen.get(hwnd, True) for hwnd, *_ in found}
        self.tiles = {}
        self.refresh_btn.setEnabled(True)
        if not found:
            self._grid_message("No Roblox windows found.\nOpen Roblox, then press Refresh.")
            self._update_start()
            return
        self._clear_grid()
        for i, (hwnd, pid, title, thumb) in enumerate(found):
            w, h = core.client_size(hwnd)
            tile = WindowTile(self.window_names.get(pid, f"Window {i + 1}"),
                              f"{w} × {h}  ·  PID {pid}", thumb)
            tile.toggled.connect(lambda h=hwnd: self._toggle(h))
            tile.set_selected(self.chosen[hwnd])
            self.tiles[hwnd] = tile
        self.grid_columns = 0
        self._layout_tiles()
        self._update_start()

    def _layout_tiles(self):
        """As many columns as fit (tiles at least ~300 px wide)."""
        if not self.tiles or not hasattr(self, "grid_host"):
            return
        width = max(1, self.grid_host.width())
        columns = max(1, min(3, width // 300))
        if columns == getattr(self, "grid_columns", 0) and self.grid.count() == len(self.tiles):
            return
        self.grid_columns = columns
        for tile in self.tiles.values():
            self.grid.removeWidget(tile)
        for col in range(3):
            self.grid.setColumnStretch(col, 1 if col < columns else 0)
        for i, tile in enumerate(self.tiles.values()):
            self.grid.addWidget(tile, i // columns, i % columns)

    def _toggle(self, hwnd):
        self.chosen[hwnd] = not self.chosen[hwnd]
        self.tiles[hwnd].set_selected(self.chosen[hwnd])
        self._update_start()

    def _update_start(self):
        count = sum(self.chosen.values())
        self.start_btn.setText(f"Start watching {core.plural(count, 'window')}"
                               if count else "Pick at least one window")
        self.start_btn.setEnabled(count > 0)
        total = len(self.windows)
        self.picker_note.setText(f"{count} of {core.plural(total, 'window')} selected" if total else "")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._layout_tiles)
        QTimer.singleShot(0, self._layout_panels)

    # ------------------------------------------------------------------ watching

    def start_watching(self):
        selected = [(hwnd, self.window_names.get(pid, f"Window {i}"), pid)
                    for i, (hwnd, pid, _, _) in enumerate(self.windows, 1) if self.chosen.get(hwnd)]
        self.default_names = {hwnd: f"Window {i}"
                              for i, (hwnd, _, _, _) in enumerate(self.windows, 1)}
        # Forget names of Roblox windows that have closed.
        running = {pid for _, pid, _, _ in self.windows}
        if any(pid not in running for pid in self.window_names):
            self.window_names = {p: n for p, n in self.window_names.items() if p in running}
            core.save_window_names(self.window_names)
        if not selected:
            return
        thumbs = {hwnd: thumb for hwnd, _, _, thumb in self.windows}
        self.active = set()
        self.names = {hwnd: name for hwnd, name, _ in selected}
        self.rows, self.slots, self.simulated, self.mutated = {}, {}, {}, {}
        self.rebirth_on, self.detecting, self.anti_disconnect = {}, {}, {}
        self._clear_nav()
        self.nav_empty.hide()
        self.manage_btn.setEnabled(True)

        page, content, col = self._scroll_page(spacing=16)
        self.panels = QStackedWidget()
        col.addWidget(self.panels)
        feed_card, feed_col = card((20, 16, 20, 14), 8)
        feed_col.addWidget(label("ACTIVITY", "section"))
        self.feed = Feed()
        self.feed.setMinimumHeight(150)
        feed_col.addWidget(self.feed)
        col.addWidget(feed_card)
        col.addStretch(1)

        for hwnd, name, pid in selected:
            self._build_panel(hwnd, name, pid, thumbs.get(hwnd))
            # Off at every launch; only kept while the app stays open (e.g. Change windows).
            self.slots[hwnd] = list(self.session_slots.get(pid, []))
            self.rebirth_on[hwnd] = pid in self.session_rebirth
            self.detecting[hwnd] = pid not in self.session_detect_off
            self.anti_disconnect[hwnd] = pid not in self.session_anti_off
            self._style_detection(hwnd)
            self._style_popups(hwnd)
            self._style_hide(hwnd)
            self._style_slot(hwnd)
            self._style_rebirth(hwnd)
        self._set_page(page)
        by_pid = {w["pid"]: hwnd for hwnd, w in self.rows.items()}
        self._open_tab(by_pid.get(self.current_tab, next(iter(self.rows))))
        QTimer.singleShot(0, self._layout_panels)

        self._log(f"Started watching {core.plural(len(selected), 'window')}.")
        self._update_stats()
        self._watching_status()

        self.run_id += 1
        run = self.run_id
        self.stop_event = stop = threading.Event()
        last_thumb = {}

        self.equipper = core.HotbarEquipper(
            on_action=lambda ok, text: self.events.put(("hotbar", run, ok, text)))
        self.popups = core.PopupCloser(
            on_action=lambda ok, text: self.events.put(("hotbar", run, ok, text)))
        self.popups.enabled_for = dict(self.anti_disconnect)
        self.rebirther = core.AutoRebirth(
            on_action=lambda ok, text: self.events.put(("rebirth", run, ok, text)),
            on_reading=lambda hwnd, text: self.events.put(("rebirth_reading", run, hwnd, text)))
        self.rebirther.on_stopped = lambda hwnd: self.events.put(("rebirth_stopped", run, hwnd))
        self.rebirther.every = dict(self.rebirth_every)
        for hwnd, on in self.rebirth_on.items():
            self.rebirther.set_enabled(hwnd, on)
        if any(self.rebirth_on.values()):
            self._log("Auto rebirth on for " + ", ".join(
                f"{self.rows[h]['label']} (every {core.duration(self.rebirth_every[h])})"
                for h, on in self.rebirth_on.items() if on) + ".")
        for hwnd, chosen in self.slots.items():
            self.equipper.set_slots(hwnd, chosen)
        if any(self.slots.values()):
            self._log("Hotbar: " + "; ".join(
                f"{self.rows[h]['label']} equips {core.slot_list(c)}"
                for h, c in self.slots.items() if c)
                + " while the event is on (in the order picked, skipping Dracula).")

        self.notifier, reason = core.DiscordNotifier.from_config(
            on_result=lambda ok, text: self.events.put(("discord", run, ok, text)))
        if self.notifier:
            self.notifier.enabled = self.discord_on
            self.notifier.link_for = self._private_server_of

            def check_discord(notifier=self.notifier):
                # Look up the channel and role now, so setup problems show up before an event.
                try:
                    self.events.put(("discord", run, True, f"Discord ready. {notifier.resolve()}."))
                except core.DiscordError as e:
                    self.events.put(("discord", run, False, f"Discord not ready: {e}"))

            threading.Thread(target=check_discord, daemon=True).start()
        else:
            self._log(f"Discord pings off: {reason}.", "end")
        self._style_discord()

        def on_status(hwnd, active, left, img, mutated=None, slots=None):
            # Runs on the worker thread, so pictures are cut and shrunk here, not in the UI.
            # A new Dracula reading always comes with a new picture from the same screenshot.
            thumb = strip = None
            due = time.monotonic() - last_thumb.get(hwnd, 0) >= core.THUMB_EVERY
            if img is not None and (due or mutated is not None):
                last_thumb[hwnd] = time.monotonic()
                thumb = shrink(img)
                if slots:
                    strip = hotbar_crop(img, slots)
            self.events.put(("status", run, hwnd, active, left, thumb, strip, mutated))

        def on_change(hwnd, name, active, left):
            self.events.put(("change", run, hwnd, active, left))

        threading.Thread(target=core.monitor, daemon=True,
                         args=([(h, n) for h, n, _ in selected], self.interval, self.debug,
                               stop, on_status, on_change, self.notifier,
                               self.equipper, self.simulated, self.detecting,
                               self.popups, self.rebirther, self.names)).start()

    def _build_panel(self, hwnd, name, pid, thumb):
        panel = QWidget()
        col = QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(16)

        # Header: name, facts, status and countdown.
        head = QHBoxLayout()
        head.setSpacing(14)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        title = label(name, "h1")
        name_row.addWidget(title)
        rename = button("", lambda h=hwnd: self._rename_tab(h), "ghost", "pencil",
                        tip="Rename this window (or double-click it in the sidebar)")
        name_row.addWidget(rename)
        name_row.addStretch(1)
        titles.addLayout(name_row)
        w, h = core.client_size(hwnd)
        titles.addWidget(label(f"PID {pid}  ·  {w} × {h}", "muted"))
        badge = Pill("Starting…", MUTED, CARD_2)
        titles.addSpacing(4)
        titles.addWidget(badge, 0, Qt.AlignLeft)
        head.addLayout(titles, 1)
        timer = QVBoxLayout()
        timer.setSpacing(0)
        left = label("", "countdown")
        left.setAlignment(Qt.AlignRight)
        left.hide()
        caption = label("", "section")
        caption.setAlignment(Qt.AlignRight)
        caption.hide()
        timer.addWidget(left)
        timer.addWidget(caption)
        head.addLayout(timer)
        col.addLayout(head)

        # Live view and automation, side by side when there's room.
        grid = QGridLayout()
        grid.setSpacing(16)
        live, live_col = card((16, 14, 16, 16), 10)
        lhead = QHBoxLayout()
        lhead.addWidget(label("LIVE VIEW", "section"))
        lhead.addStretch(1)
        hide = button("", lambda h=hwnd: self._toggle_hidden(h), "ghost", "eye-off")
        lhead.addWidget(hide)
        live_col.addLayout(lhead)
        image = ImageView("Waiting for the first picture…", ratio=16 / 9, radius=10)
        if thumb is not None:
            image.set_image(pil_to_qimage(thumb))
        live_col.addWidget(image)
        live_col.addStretch(1)

        auto, auto_col = card((20, 14, 20, 16), 2)
        auto_col.addWidget(label("AUTOMATION", "section"))
        auto_col.addSpacing(4)
        detect = Toggle(True)
        detect.clicked.connect(lambda *_, h=hwnd: self._toggle_detection(h))
        detect_row = SettingRow("target", "Hallows' Eve detection",
                                "Watch this window for the event", detect)
        auto_col.addWidget(detect_row)
        auto_col.addWidget(divider())
        anti = Toggle(True)
        anti.clicked.connect(lambda *_, h=hwnd: self._toggle_popups(h))
        anti_row = SettingRow("shield", "Anti Disconnect",
                              "Reconnect, and close the Welcome Back popup", anti)
        auto_col.addWidget(anti_row)
        auto_col.addWidget(divider())
        rebirth = Toggle(False)
        rebirth.clicked.connect(lambda *_, h=hwnd: self._toggle_rebirth(h))
        rebirth_row = SettingRow("repeat", "Auto rebirth", "Rebirth whenever the cash covers it",
                                 rebirth)
        auto_col.addWidget(rebirth_row)
        self.rebirth_every[hwnd] = self.session_rebirth_every.get(pid, core.REBIRTH_EVERY)
        every = QHBoxLayout()
        every.setContentsMargins(46, 0, 0, 0)
        every.setSpacing(10)
        every.addWidget(label("Check every", "small"))
        stops = core.REBIRTH_INTERVALS
        at = min(range(len(stops)), key=lambda i: abs(stops[i] - self.rebirth_every[hwnd]))
        slider = StepSlider(len(stops), at)
        slider.valueChanged.connect(lambda i, h=hwnd: self._set_rebirth_every(h, stops[i]))
        slider.released.connect(lambda _i, h=hwnd: self._log(
            f"{self.rows[h]['label']}: auto rebirth now checks every "
            f"{core.duration(self.rebirth_every[h])}."))
        every.addWidget(slider, 1)
        every_label = label(core.duration(self.rebirth_every[hwnd]), "title")
        every_label.setMinimumWidth(56)
        every.addWidget(every_label)
        auto_col.addLayout(every)
        info_row = QHBoxLayout()
        info_row.setContentsMargins(46, 6, 0, 0)
        rebirth_info = label("", "small", wrap=True)
        info_row.addWidget(rebirth_info, 1)
        info_row.addWidget(button("Rebirth log", lambda h=hwnd: self._show_rebirth_log(h),
                                  icon_name="list"))
        auto_col.addLayout(info_row)
        auto_col.addStretch(1)
        grid.addWidget(live, 0, 0)
        grid.addWidget(auto, 0, 1)
        col.addLayout(grid)

        # Hotbar: click characters to choose them.
        bar, bar_col = card((20, 14, 20, 16), 10)
        bhead = QHBoxLayout()
        bhead.setSpacing(8)
        bhead.addWidget(label("HOTBAR  ·  AUTO EQUIP", "section"))
        bhead.addStretch(1)
        bhead.addWidget(button(f"Test {core.TEST_EVENT_SECONDS} s", lambda h=hwnd: self._simulate(h),
                               icon_name="play",
                               tip="Act as if Hallows' Eve were on here (no Discord ping)"))
        slot = button("Choose slots", lambda h=hwnd: self._slot_menu(h), icon_name="backpack")
        bhead.addWidget(slot)
        bar_col.addLayout(bhead)
        strip = HotbarStrip(104)
        strip.slot_clicked.connect(lambda s, h=hwnd: self._strip_click(h, s))
        bar_col.addWidget(strip)
        summary = label("", "small", wrap=True)
        bar_col.addWidget(summary)
        warning = label("", "warning")
        warning.hide()
        bar_col.addWidget(warning)
        col.addWidget(bar)

        # Private server link, sent with the Discord ping.
        server, server_col = card((20, 14, 20, 16), 8)
        server_col.addWidget(label("PRIVATE SERVER", "section"))
        srow = QHBoxLayout()
        srow.setSpacing(8)
        server_entry = QLineEdit(self.private_servers.get(pid, ""))
        server_entry.setPlaceholderText("Paste this window's private server link "
                                        "(roblox.com/share?code=…)")
        server_entry.addAction(icon("link", MUTED), QLineEdit.LeadingPosition)
        server_entry.textChanged.connect(lambda _t, h=hwnd: self._set_private_server(h))
        srow.addWidget(server_entry, 1)
        srow.addWidget(button("Copy", lambda e=server_entry: QApplication.clipboard().setText(
            e.text().strip()), icon_name="copy"))
        server_col.addLayout(srow)
        server_hint = label("Sent with the Discord ping when Hallows' Eve starts in this window.",
                            "small")
        server_col.addWidget(server_hint)
        col.addWidget(server)

        nav = NavItem(name)
        nav.clicked.connect(lambda h=hwnd: self._open_tab(h))
        nav.renamed.connect(lambda text, h=hwnd: self._set_window_name(h, text))
        self.nav_box.addWidget(nav)
        self.panels.addWidget(panel)
        self.rows[hwnd] = {
            "panel": panel, "label": name, "pid": pid, "title": title, "badge": badge,
            "left": left, "caption": caption, "left_s": None, "image": image, "hide": hide,
            "live": live, "grid": grid, "auto": auto, "detect": detect, "detect_row": detect_row,
            "anti": anti, "anti_row": anti_row, "rebirth": rebirth, "rebirth_row": rebirth_row,
            "slider": slider, "every_label": every_label, "rebirth_info": rebirth_info,
            "slot": slot, "strip": strip, "summary": summary, "warning": warning,
            "server_entry": server_entry, "server_hint": server_hint, "nav": nav, "wide": None}

    def _layout_panels(self):
        """Live view and automation side by side in wide windows, stacked in narrow ones."""
        if not self.rows:
            return
        wide = self.pages.width() >= 900
        for widgets in self.rows.values():
            if widgets["wide"] == wide:
                continue
            widgets["wide"] = wide
            grid = widgets["grid"]
            grid.removeWidget(widgets["live"])
            grid.removeWidget(widgets["auto"])
            if wide:
                grid.addWidget(widgets["live"], 0, 0)
                grid.addWidget(widgets["auto"], 0, 1)
                grid.setColumnStretch(0, 5)
                grid.setColumnStretch(1, 6)
            else:
                grid.addWidget(widgets["live"], 0, 0)
                grid.addWidget(widgets["auto"], 1, 0)
                grid.setColumnStretch(0, 1)
                grid.setColumnStretch(1, 0)

    def _apply_status(self, hwnd, active, left, thumb, strip=None, mutated=None):
        widgets = self.rows.get(hwnd)
        if not widgets:
            return
        if mutated is not None:
            self._apply_mutations(hwnd, mutated)
        if thumb is not None:
            widgets["image"].set_image(pil_to_qimage(thumb))
        if strip is not None:  # keep the last good one when the hotbar is covered
            picture, boxes = strip
            widgets["strip"].set_picture(pil_to_qimage(picture), boxes)
        if active:
            self.active.add(hwnd)
            text, fg, bg, short = "Hallows' Eve active", ACCENT, ACCENT_SOFT, "Hallows' Eve"
        else:
            self.active.discard(hwnd)
            if active is None and not core.user32.IsWindow(hwnd):
                text, fg, bg, short = "Window closed", DANGER, DANGER_SOFT, "Window closed"
            elif active is None:
                text, fg, bg, short = "Can't see it (minimised?)", WARN, WARN_SOFT, "Can't see it"
            elif not self.detecting.get(hwnd, True):
                text, fg, bg, short = "Not looking (Hallows' Eve off)", MUTED, CARD_2, "Not looking"
            else:
                text, fg, bg, short = "No event", MUTED, CARD_2, "No event"
        widgets["badge"].set(text, fg, bg)
        widgets["left_s"] = left if active else None
        nav = widgets["nav"]
        nav.sub.setText(short)
        nav.dot.set(fg if fg != MUTED else (GOOD if active is False and self.detecting.get(hwnd, True)
                                            else SUBTLE), glow=bool(active))
        clock = (core.fmt_clock(left) if left is not None else "?:??") if active else ""
        nav.countdown.setText(clock)
        widgets["left"].setText(clock)
        widgets["left"].setProperty("active", bool(active))
        repolish(widgets["left"])
        widgets["left"].setVisible(bool(active))
        widgets["caption"].setText("LEFT" if active else "")
        widgets["caption"].setVisible(bool(active))
        if widgets["live"].property("active") != bool(active):
            widgets["live"].setProperty("active", bool(active))
            repolish(widgets["live"])
        self._update_stats()
        self._watching_status()

    def _apply_change(self, hwnd, active, left):
        widgets = self.rows.get(hwnd)
        if not widgets:
            return
        test = hwnd in self.simulated
        if active:
            if not test:
                self.events_seen += 1
            extra = f"  ·  {core.fmt_clock(left)} left" if left is not None else ""
            self._log(f"{widgets['label']}: {'TEST ' * test}Hallows' Eve started{extra}", "start")
            QApplication.alert(self)  # flash the taskbar button (no sound)
        else:
            self.simulated.pop(hwnd, None)
            self._log(f"{widgets['label']}: {'TEST ' * test}Hallows' Eve ended", "end")
        self._update_stats()

    def _log(self, text, tag=None):
        if self.feed is not None:
            self.feed.add(text, tag)

    def log_text(self):
        """Everything in the activity feed (for tests)."""
        return "\n".join(self.feed.lines) if self.feed else ""

    def _toggle_hidden(self, hwnd):
        name = self.rows[hwnd]["label"]
        if core.is_hidden(hwnd):
            core.show_window(hwnd)
            self._log(f"{name} shown.")
        else:
            core.hide_window(hwnd)
            self._log(f"{name} hidden at the edge of your right screen; still watched.")
        self._style_hide(hwnd)

    def _style_hide(self, hwnd):
        hidden = core.is_hidden(hwnd)
        b = self.rows[hwnd]["hide"]
        b.setText("Show window" if hidden else "Hide window")
        b.setIcon(icon("eye" if hidden else "eye-off", MUTED))
        b.setToolTip("Bring the window back" if hidden
                     else "Move the Roblox window out of the way (it keeps being watched)")

    # ------------------------------------------------------------------ tabs (sidebar)

    def _open_tab(self, hwnd):
        if hwnd not in self.rows:
            return
        self.panels.setCurrentWidget(self.rows[hwnd]["panel"])
        self.current_tab = self.rows[hwnd]["pid"]
        for other, widgets in self.rows.items():
            widgets["nav"].set_selected(other == hwnd)

    def _rename_tab(self, hwnd):
        self.rows[hwnd]["nav"].start_rename()

    def _set_window_name(self, hwnd, text):
        widgets = self.rows.get(hwnd)
        if not widgets:
            return
        old = widgets["label"]
        name = core.clean_window_name(text) or self.default_names.get(hwnd, old)
        if name == old:
            return
        if name == self.default_names.get(hwnd):
            self.window_names.pop(widgets["pid"], None)
        else:
            self.window_names[widgets["pid"]] = name
        core.save_window_names(self.window_names)
        widgets["label"] = name
        self.names[hwnd] = name
        widgets["title"].setText(name)
        widgets["nav"].name.setText(name)
        self._log(f"{old} renamed to {name}.")

    # ------------------------------------------------------------------ hotbar

    def _strip_click(self, hwnd, slot):
        self._set_slots(hwnd, core.toggle_slot(self.slots.get(hwnd, []), slot))

    def _slot_menu(self, hwnd):
        self._close_slot_picker()
        self.slot_picker = SlotPickerDialog(self, hwnd)
        self.slot_picker.destroyed.connect(lambda *_: setattr(self, "slot_picker", None))
        self.slot_picker.show_centred()

    def _close_slot_picker(self):
        if self.slot_picker is not None:
            try:
                self.slot_picker.close()
            except RuntimeError:
                pass
        self.slot_picker = None

    def _simulate(self, hwnd, seconds=None):
        """Make the window count as showing Hallows' Eve for a while: equips and
        unequips like a real event, but sends no Discord ping."""
        seconds = seconds or core.TEST_EVENT_SECONDS
        self.simulated[hwnd] = time.monotonic() + seconds
        chosen = self.slots.get(hwnd)
        what = (f"equips {core.slot_list(chosen)}, in the order picked, skipping Dracula, and "
                "unequips as soon as it ends") if chosen \
            else "no hotbar slot chosen, so nothing gets equipped"
        self._log(f"{self.rows[hwnd]['label']}: test Hallows' Eve for {seconds} s ({what}; "
                  "no Discord ping).")

    def _set_slots(self, hwnd, chosen):
        chosen = list(dict.fromkeys(chosen))
        self.slots[hwnd] = chosen
        self.equipper.set_slots(hwnd, chosen)
        self.session_slots[self.rows[hwnd]["pid"]] = chosen
        self._style_slot(hwnd)
        name = self.rows[hwnd]["label"]
        if chosen:
            self._log(f"{name}: will equip {core.slot_list(chosen)} while Hallows' Eve is on "
                      "(in the order picked, skipping Dracula), and unequip after.")
        else:
            self._log(f"{name}: won't equip anything.")

    def _apply_mutations(self, hwnd, mutated):
        """New reading of which slots have Dracula: redraw marks, update the warning."""
        if hwnd not in self.rows:
            return
        self.mutated[hwnd] = set(mutated)
        self._style_slot(hwnd)

    def _style_slot(self, hwnd):
        widgets = self.rows[hwnd]
        chosen = self.slots.get(hwnd, [])
        widgets["strip"].set_marks(chosen, self.mutated.get(hwnd, set()))
        widgets["slot"].setText("Choose slots" if not chosen
                                else "Equip " + ", ".join(str(s) for s in chosen))
        widgets["slot"].setIcon(icon("backpack", ACCENT if chosen else MUTED))
        widgets["slot"].setStyleSheet(f"color:{ACCENT};" if chosen else "")
        if chosen:
            order = " then ".join(f"slot {s}" for s in chosen)
            widgets["summary"].setText(f"During Hallows' Eve this window equips {order}, skipping "
                                       "any that already have Dracula, and unequips when it ends.")
        else:
            widgets["summary"].setText("Auto equip is off. Click characters in the hotbar to "
                                       "choose which to hold during Hallows' Eve.")
        clash = set(chosen) & self.mutated.get(hwnd, set())
        widgets["warning"].setText(f"⚠  {core.MUTATED_WARNING}" if clash else "")
        widgets["warning"].setVisible(bool(clash))

    # ------------------------------------------------------------------ switches

    def _toggle_detection(self, hwnd):
        on = self.detecting[hwnd] = not self.detecting.get(hwnd, True)
        pid, name = self.rows[hwnd]["pid"], self.rows[hwnd]["label"]
        (self.session_detect_off.discard if on else self.session_detect_off.add)(pid)
        if on:
            self._log(f"{name}: looking for Hallows' Eve again.")
        else:
            self._log(f"{name}: stopped looking for Hallows' Eve. Nothing is detected, pinged "
                      "or equipped there until it's switched back on (a held character is "
                      "unequipped).", "end")
        self._style_detection(hwnd)
        self._watching_status()

    def _style_detection(self, hwnd):
        on = self.detecting.get(hwnd, True)
        widgets = self.rows[hwnd]
        widgets["detect"].setChecked(on)
        widgets["detect_row"].set_on(on)
        widgets["detect_row"].subtitle.setText("Watching this window for the event" if on
                                               else "Off: this window isn't checked")

    def _toggle_popups(self, hwnd):
        on = self.anti_disconnect[hwnd] = not self.anti_disconnect.get(hwnd, True)
        pid, name = self.rows[hwnd]["pid"], self.rows[hwnd]["label"]
        (self.session_anti_off.discard if on else self.session_anti_off.add)(pid)
        if self.popups:
            self.popups.enabled_for[hwnd] = on
        self._log(f"{name}: Anti Disconnect on: reconnecting after disconnects and closing the "
                  "'Welcome Back' popup automatically." if on
                  else f"{name}: Anti Disconnect off: not reconnecting or closing the 'Welcome "
                       "Back' popup (it hides the hotbar, so equipping can't work while it's open).",
                  None if on else "end")
        self._style_popups(hwnd)

    def _style_popups(self, hwnd):
        on = self.anti_disconnect.get(hwnd, True)
        widgets = self.rows[hwnd]
        widgets["anti"].setChecked(on)
        widgets["anti_row"].set_on(on)

    def _toggle_rebirth(self, hwnd):
        on = not self.rebirth_on.get(hwnd)
        self.rebirth_on[hwnd] = on
        self.rebirther.set_enabled(hwnd, on)
        (self.session_rebirth.add if on else self.session_rebirth.discard)(self.rows[hwnd]["pid"])
        name = self.rows[hwnd]["label"]
        self._log(f"{name}: auto rebirth on (checks every {core.duration(self.rebirth_every[hwnd])} "
                  "and rebirths when the cash covers it)." if on else f"{name}: auto rebirth off.")
        self._style_rebirth(hwnd)

    def _style_rebirth(self, hwnd):
        on = bool(self.rebirth_on.get(hwnd))
        widgets = self.rows[hwnd]
        widgets["rebirth"].setChecked(on)
        widgets["rebirth_row"].set_on(on)
        if not on:
            widgets["rebirth_info"].setText("")

    def _set_rebirth_every(self, hwnd, seconds):
        self.rebirth_every[hwnd] = float(seconds)
        if hwnd in self.rows:
            self.session_rebirth_every[self.rows[hwnd]["pid"]] = float(seconds)
            self.rows[hwnd]["every_label"].setText(core.duration(seconds))
        if self.rebirther:
            self.rebirther.every[hwnd] = float(seconds)

    def _show_rebirth_log(self, hwnd):
        window = self.log_windows.get(hwnd)
        try:
            if window is not None and window.isVisible():
                window.refresh()
                window.raise_()
                window.activateWindow()
                return
        except RuntimeError:  # already deleted
            pass
        window = RebirthLogDialog(self, hwnd)
        window.destroyed.connect(lambda *_, h=hwnd: self.log_windows.pop(h, None))
        self.log_windows[hwnd] = window
        window.show_centred()

    def _close_log_windows(self):
        for window in list(self.log_windows.values()):
            try:
                window.close()
            except RuntimeError:
                pass
        self.log_windows = {}

    def _private_server_of(self, name):
        """The private server link typed in for the window with this name ('' if none).
        Called from the Discord thread."""
        for widgets in list(self.rows.values()):
            if widgets["label"] == name:
                return self.private_servers.get(widgets["pid"], "")
        return ""

    def _set_private_server(self, hwnd):
        widgets = self.rows.get(hwnd)
        if not widgets:
            return
        link = widgets["server_entry"].text().strip()
        if link:
            self.private_servers[widgets["pid"]] = link
        else:
            self.private_servers.pop(widgets["pid"], None)
        core.save_private_servers(self.private_servers)
        looks_wrong = bool(link) and "roblox.com" not in link.lower()
        widgets["server_hint"].setText(
            "That doesn't look like a Roblox link, but it's saved and sent as it is."
            if looks_wrong else
            "Sent with the Discord ping when Hallows' Eve starts in this window.")
        widgets["server_hint"].setStyleSheet(f"color:{WARN};" if looks_wrong else "")

    def _watching_status(self):
        if self.active:
            self._set_status(f"Hallows' Eve in {core.plural(len(self.active), 'window')}",
                             ACCENT, ACCENT_SOFT)
        elif any(self.detecting.get(hwnd, True) for hwnd in self.rows):
            self._set_status("Watching", GOOD, GOOD_SOFT)
        else:
            self._set_status("Paused: Hallows' Eve off", WARN, WARN_SOFT)

    def _toggle_discord(self):
        if self.notifier:
            self.discord_on = self.notifier.enabled = not self.discord_on
        self._style_discord()

    def _style_discord(self):
        if not self.notifier:
            self.discord_toggle.setChecked(False)
            self.discord_toggle.setEnabled(False)
            self.discord_sub.setText("Not set up")
            self.discord_badge.set_color(SUBTLE)
            return
        self.discord_toggle.setEnabled(True)
        self.discord_toggle.setChecked(self.discord_on)
        role = self.notifier.role_label or f"@{self.notifier.role}"
        self.discord_sub.setText(f"Pings {role}" if self.discord_on else "Paused")
        self.discord_badge.set_color(ACCENT if self.discord_on else MUTED)

    # ------------------------------------------------------------------ RAM trim

    def _toggle_trim(self):
        """On: trim every Roblox client's memory now and then every TRIM_EVERY seconds."""
        on = self.trim_toggle.isChecked()
        self.trim_badge.set_color(ACCENT if on else MUTED)
        if on:
            self.trim_timer.start()
            self.trim_sub.setText(f"Every {TRIM_EVERY // 60} min")
            self._log(f"Trim Roblox RAM on: every {TRIM_EVERY // 60} minutes.")
            self._trim_ram()
        else:
            self.trim_timer.stop()
            self.trim_sub.setText("Off")
            self._log("Trim Roblox RAM off.", "end")

    def _trim_ram(self):
        """Same as the F6 hotkey: trim every Roblox client's memory (on a worker thread)."""
        if self.trim_busy:
            return
        self.trim_busy = True

        def work():
            try:
                found, trimmed = core.trim_roblox_memory()
                self.events.put(("trim_done", None, found, trimmed, None))
            except Exception as e:
                self.events.put(("trim_done", None, 0, 0, str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _trim_done(self, found, trimmed, error):
        """Show the last trim under the switch; only problems go in the activity feed, so a
        trim every two minutes doesn't flood it."""
        self.trim_busy = False
        if not self.trim_toggle.isChecked():
            return
        when = time.strftime("%H:%M")
        if error:
            self._log(f"RAM trim failed: {error}", "error")
            short = "last trim failed"
        elif not found:
            short = "no Roblox clients"
        elif trimmed == found:
            short = f"{found} clients trimmed"
        else:
            self._log(f"RAM trimmed for {trimmed} of {found} clients (the rest couldn't be "
                      "opened).", "error")
            short = f"{trimmed} of {found} trimmed"
        self.trim_sub.setText(f"Every {TRIM_EVERY // 60} min · {short} {when}")

    # ------------------------------------------------------------------ updates

    def _show_updates(self):
        try:
            if self.update_dialog is not None and self.update_dialog.isVisible():
                self.update_dialog.raise_()
                return
        except RuntimeError:
            pass
        self.update_dialog = UpdateDialog(self)
        self.update_dialog.destroyed.connect(lambda *_: setattr(self, "update_dialog", None))
        self.update_dialog.show_centred()
        self.update_dialog.check()

    def _check_for_updates(self, quiet):
        """Ask GitHub on a worker thread; the answer comes back as an 'update_checked' event.
        A quiet check (at startup) only marks the button when there's something new."""
        def work():
            try:
                state, mine, newest = updater.check()
                self.events.put(("update_checked", None, quiet, state, mine, newest, None))
            except updater.UpdateError as e:
                self.events.put(("update_checked", None, quiet, None, None, None, str(e)))
            except Exception as e:  # never let a check take the app down
                self.events.put(("update_checked", None, quiet, None, None, None,
                                 f"unexpected problem ({e})"))

        threading.Thread(target=work, daemon=True).start()

    def _install_update(self, newest):
        def work():
            try:
                text = updater.install(newest, progress=lambda t: self.events.put(
                    ("update_progress", None, t)))
                self.events.put(("update_done", None, True, text))
            except updater.UpdateError as e:
                self.events.put(("update_done", None, False, str(e)))
            except Exception as e:
                self.events.put(("update_done", None, False, f"unexpected problem ({e})"))

        threading.Thread(target=work, daemon=True).start()

    def _update_event(self, kind, data):
        dialog = self.update_dialog
        try:
            dialog = dialog if dialog is not None and dialog.isVisible() else None
        except RuntimeError:
            dialog = None
        if kind == "update_checked":
            quiet, state, mine, newest, error = data
            if not error:
                self.update_state = state
                if state == "available":
                    self.update_btn.setText("Update available")
                    self.update_btn.setIcon(icon("download", ACCENT))
                    self.update_btn.setStyleSheet(f"color:{ACCENT};")
                else:
                    self.update_btn.setText("Check for updates")
                    self.update_btn.setIcon(icon("refresh", MUTED))
                    self.update_btn.setStyleSheet("")
            if dialog is not None and not quiet:
                dialog.show_result(state, mine, newest, error)
        elif kind == "update_progress" and dialog is not None:
            dialog.show_progress(data[0])
        elif kind == "update_done":
            ok, text = data
            self._log(text if ok else f"Update failed: {text}", None if ok else "error")
            if dialog is not None:
                dialog.show_done(ok, text)
            if ok:
                QTimer.singleShot(1200, self._restart_after_update)

    def _restart_after_update(self):
        self.on_close()
        updater.restart()
        core.quit_now()

    def _stop_watching(self):
        if self.stop_event:
            self.stop_event.set()
            self.stop_event = None

    # ------------------------------------------------------------------ plumbing

    def _poll(self):
        try:
            while True:
                kind, run, *data = self.events.get_nowait()
                if kind.startswith("update_"):
                    self._update_event(kind, data)
                    continue
                if kind == "trim_done":
                    self._trim_done(*data)
                    continue
                if run != self.run_id:
                    continue
                if kind == "windows":
                    self._show_windows(*data)
                elif kind == "status":
                    self._apply_status(*data)
                elif kind == "change":
                    self._apply_change(*data)
                elif kind in ("discord", "hotbar"):
                    ok, text = data
                    self._log(text, None if ok else "error")
                    if kind == "discord":
                        self._style_discord()
                elif kind == "rebirth":
                    ok, text = data
                    self._log(text, "error" if not ok else "start" if "REBIRTHED" in text else None)
                    if "REBIRTHED" in text:  # keep open log windows up to date
                        for window in list(self.log_windows.values()):
                            try:
                                window.refresh()
                            except RuntimeError:
                                pass
                elif kind == "rebirth_stopped":
                    (hwnd,) = data
                    if hwnd in self.rows:
                        self.rebirth_on[hwnd] = False
                        self.session_rebirth.discard(self.rows[hwnd]["pid"])
                        self._style_rebirth(hwnd)
                elif kind == "rebirth_reading":
                    hwnd, text = data
                    if hwnd in self.rows and self.rebirth_on.get(hwnd):
                        self.rows[hwnd]["rebirth_info"].setText(f"{text}  ({time.strftime('%H:%M')})")
        except queue.Empty:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        dark_title_bar(self)

    def closeEvent(self, event):
        self.on_close()
        event.accept()

    def on_close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self.timer.stop()
        self.trim_timer.stop()
        self._save_geometry()
        self._close_slot_picker()
        self._close_log_windows()
        try:
            if self.update_dialog is not None:
                self.update_dialog.close()
        except RuntimeError:
            pass
        self._stop_watching()
        core.show_all_hidden()


def setup_application(app):
    """Dark Fusion style, the app font and the stylesheet."""
    app.setStyle("Fusion")
    palette = QPalette()
    for role, colour in [(QPalette.Window, BG), (QPalette.WindowText, TEXT), (QPalette.Base, BG),
                         (QPalette.AlternateBase, CARD), (QPalette.Text, TEXT),
                         (QPalette.Button, CARD_2), (QPalette.ButtonText, TEXT),
                         (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, ON_ACCENT),
                         (QPalette.ToolTipBase, CARD_2), (QPalette.ToolTipText, TEXT),
                         (QPalette.PlaceholderText, SUBTLE)]:
        palette.setColor(role, QColor(colour))
    app.setPalette(palette)
    family = pick_font()
    font = QFont(family)
    font.setPixelSize(13)
    font.setHintingPreference(QFont.PreferNoHinting)
    app.setFont(font)
    app.setStyleSheet(stylesheet(family))


def make_application():
    """The QApplication (one per process), set up for this app."""
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.window=false")  # DPI-awareness notice
    app = QApplication.instance()
    if app is None:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        app = QApplication(sys.argv[:1])
        setup_application(app)
    return app


def run(core_module, interval, debug):
    """Open the window and run until it's closed."""
    global core
    core = core_module
    app = make_application()
    if core.ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(core.ICON_PATH)))
    window = App(interval, debug)
    window.show()
    app.exec()

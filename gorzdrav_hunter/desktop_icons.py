"""Отрисовка локальных SVG Lucide в цветах темы Qt Quick."""
from pathlib import Path
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtQuick import QQuickImageProvider
from PySide6.QtSvg import QSvgRenderer


ICONS = Path(__file__).with_name('qml') / 'icons'


class LucideIcons(QQuickImageProvider):
    def __init__(self):
        super().__init__(QQuickImageProvider.Image)

    def requestImage(self, identifier, size, requested_size):
        match = re.fullmatch(r'([a-z0-9-]+)/([0-9a-f]{6}|[0-9a-f]{8})', identifier)
        if not match:
            return QImage()
        name, hex_color = match.groups()
        try:
            svg = (ICONS / (name + '.svg')).read_bytes()
        except FileNotFoundError:
            return QImage()

        color = QColor('#' + hex_color)
        renderer = QSvgRenderer(svg.replace(b'currentColor', color.name().encode('ascii')))
        if not renderer.isValid():
            return QImage()
        natural_size = renderer.defaultSize()
        if size is not None:
            size.setWidth(natural_size.width())
            size.setHeight(natural_size.height())
        target = natural_size if requested_size.isEmpty() else requested_size
        image = QImage(target, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        with QPainter(image) as painter:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setOpacity(color.alphaF())
            renderer.render(painter)
        return image

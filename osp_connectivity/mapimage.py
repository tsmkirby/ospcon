"""Isolated map for a circuit card: background layers plus the traced plant and handholes."""

from __future__ import annotations

from qgis.PyQt.QtCore import QBuffer, QIODevice, QSize, Qt
from qgis.PyQt.QtGui import QColor, QFont, QImage, QPainter, QPen
from qgis.core import (
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsGeometry,
    QgsMapLayer,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsPointXY,
    QgsRectangle,
    QgsUnitTypes,
    QgsWkbTypes,
)

from .core.cards import fit_extent


IMAGE_WIDTH = 1200
IMAGE_HEIGHT = 679

_STYLES = {
    "idfs": QColor(20, 140, 60),
    "cables": QColor(230, 120, 20),
    "handholes": QColor(140, 90, 20),
    "enclosures": QColor(180, 30, 160),
    "devices": QColor(20, 90, 200),
}


def render_circuit_jpegs(project, canvas, reports: list[dict], osp_layer_ids: set) -> list:
    """One JPEG per report. None when that circuit has no geometry to draw."""
    layers = {layer.id(): layer for layer in project.mapLayers().values()}
    background = [layer for layer in canvas.layers() if _is_background(layer, osp_layer_ids)]
    images = []
    for report in reports:
        images.append(_one(project, layers, background, report.get("highlights") or {}))
    return images


def _one(project, layers, background, highlights) -> bytes | None:
    drawn = _geometries(project, layers, highlights)
    if not drawn:
        return None
    boxes = []
    for _kind, geometry in drawn:
        rect = geometry.boundingBox()
        boxes.append((rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()))
    units = project.crs().mapUnits()
    min_span = 0.002 if units == QgsUnitTypes.DistanceDegrees else 60
    extent = fit_extent(boxes, IMAGE_WIDTH, IMAGE_HEIGHT, min_span=min_span)
    if extent is None:
        return None
    rectangle = QgsRectangle(extent[0], extent[1], extent[2], extent[3])
    image = _render_background(project, background, rectangle)
    _paint(image, drawn, rectangle, project)
    return _jpeg(image)


def _geometries(project, layers, highlights) -> list:
    found = []
    order = ("idfs", "cables", "handholes", "enclosures", "devices")
    for kind in order:
        for layer_id, fid in highlights.get(kind) or []:
            layer = layers.get(layer_id)
            if layer is None or not hasattr(layer, "getFeatures"):
                continue
            feature = next(layer.getFeatures(QgsFeatureRequest(fid)), None)
            if feature is None or not feature.hasGeometry():
                continue
            geometry = QgsGeometry(feature.geometry())
            if layer.crs() != project.crs():
                geometry.transform(QgsCoordinateTransform(layer.crs(), project.crs(), project))
            if not geometry.isEmpty():
                found.append((kind, geometry))
    return found


def _is_background(layer, osp_layer_ids: set) -> bool:
    if layer.id() in osp_layer_ids:
        return False
    if layer.type() != QgsMapLayer.VectorLayer:
        return True
    provider = (layer.providerType() or "").lower()
    if provider in {"wms", "xyz", "arcgismapserver", "vectortile", "tiledscene"}:
        return True
    tokens = set("".join(ch if ch.isalnum() else " " for ch in (layer.name() or "").casefold()).split())
    return bool(tokens & {"osm", "satellite", "imagery", "basemap", "background", "google", "esri", "aerial"})


def _render_background(project, background, rectangle) -> QImage:
    settings = QgsMapSettings()
    settings.setDestinationCrs(project.crs())
    settings.setLayers(list(background))
    settings.setExtent(rectangle)
    settings.setOutputSize(QSize(IMAGE_WIDTH, IMAGE_HEIGHT))
    settings.setBackgroundColor(QColor(244, 246, 248))
    settings.setFlag(QgsMapSettings.Antialiasing, True)
    if background:
        job = QgsMapRendererParallelJob(settings)
        job.start()
        job.waitForFinished()
        image = job.renderedImage()
        if not image.isNull():
            return image.copy()
    image = QImage(IMAGE_WIDTH, IMAGE_HEIGHT, QImage.Format_RGB32)
    image.fill(QColor(244, 246, 248))
    return image


def _paint(image, drawn, rectangle, project) -> None:
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    scale_x = IMAGE_WIDTH / rectangle.width() if rectangle.width() else 1
    scale_y = IMAGE_HEIGHT / rectangle.height() if rectangle.height() else 1

    def to_pixel(point):
        return (
            (point.x() - rectangle.xMinimum()) * scale_x,
            IMAGE_HEIGHT - (point.y() - rectangle.yMinimum()) * scale_y,
        )

    for kind, geometry in drawn:
        color = _STYLES[kind]
        pen = QPen(color)
        pen.setWidth(6 if kind == "cables" else 2)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        fill = QColor(color)
        fill.setAlpha(70 if kind == "idfs" else 255)
        painter.setBrush(fill if kind != "cables" else Qt.NoBrush)
        _draw_geometry(painter, geometry, to_pixel, 8 if kind == "handholes" else 7)
    _scale_bar(painter, rectangle, project)
    painter.end()


def _draw_geometry(painter, geometry, to_pixel, radius) -> None:
    kind = geometry.type()
    if kind == QgsWkbTypes.LineGeometry:
        lines = geometry.asMultiPolyline() if geometry.isMultipart() else [geometry.asPolyline()]
        for line in lines:
            if len(line) < 2:
                continue
            last = None
            for point in line:
                pixel = to_pixel(point)
                if last is not None:
                    painter.drawLine(int(last[0]), int(last[1]), int(pixel[0]), int(pixel[1]))
                last = pixel
        return
    if kind == QgsWkbTypes.PolygonGeometry:
        polygons = geometry.asMultiPolygon() if geometry.isMultipart() else [geometry.asPolygon()]
        for polygon in polygons:
            for ring in polygon:
                if len(ring) < 2:
                    continue
                last = None
                first = to_pixel(ring[0])
                for point in ring:
                    pixel = to_pixel(point)
                    if last is not None:
                        painter.drawLine(int(last[0]), int(last[1]), int(pixel[0]), int(pixel[1]))
                    last = pixel
                if last is not None:
                    painter.drawLine(int(last[0]), int(last[1]), int(first[0]), int(first[1]))
        return
    point = geometry.asPoint() if not geometry.isMultipart() else (geometry.asMultiPoint() or [None])[0]
    if point is None:
        return
    pixel = to_pixel(point)
    painter.drawEllipse(int(pixel[0] - radius), int(pixel[1] - radius), radius * 2, radius * 2)


def _scale_bar(painter, rectangle, project) -> None:
    if project.crs().mapUnits() != QgsUnitTypes.DistanceMeters or rectangle.width() <= 0:
        return
    meters_per_pixel = rectangle.width() / IMAGE_WIDTH
    raw = 180 * meters_per_pixel
    magnitude = 10 ** int(len(str(int(max(raw, 1)))) - 1)
    for step in (1, 2, 5, 10):
        choice = step * magnitude
        if choice >= raw:
            break
    else:
        choice = raw
    pixels = choice / meters_per_pixel
    painter.setPen(QPen(QColor(20, 30, 40), 2))
    painter.setBrush(Qt.NoBrush)
    painter.drawLine(36, IMAGE_HEIGHT - 36, int(36 + pixels), IMAGE_HEIGHT - 36)
    painter.setFont(QFont("Helvetica", 14))
    painter.drawText(36, IMAGE_HEIGHT - 44, f"{int(choice)} m")


def _jpeg(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "JPG", 82)
    return bytes(buffer.data())

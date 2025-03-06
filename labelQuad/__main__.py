import argparse
import base64
import codecs
import copy
from functools import partial
from glob import glob
import html
import math
import os
import os.path as osp
import sys
from loguru import logger
import yaml
import io
import json
import re
import shutil
from typing import Optional
import PIL.Image
from PyQt5.QtCore import *
from PyQt5.QtWidgets import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import QMessageBox as QMB
import imgviz
from loguru import logger
import natsort
import numpy as np
import yaml
import skimage.measure


PIL.Image.MAX_IMAGE_PIXELS = None


__appname__ = 'labelQuad'
__version__ = '1.0.0'


LABEL_COLORMAP = imgviz.label_colormap()
ZOOM_MODE_FIT_WINDOW = 0
ZOOM_MODE_FIT_WIDTH = 1
ZOOM_MODE_MANUAL_ZOOM = 2
MAX_RECENT_FILES = 7
CURSOR_DEFAULT = Qt.ArrowCursor
CURSOR_POINT = Qt.PointingHandCursor
CURSOR_DRAW = Qt.CrossCursor
CURSOR_MOVE = Qt.ClosedHandCursor
CURSOR_GRAB = Qt.OpenHandCursor
MOVE_SPEED = 5.0


class ToolBar(QToolBar):

    def __init__(self, title):
        super(ToolBar, self).__init__(title)
        layout = self.layout()
        m = (0, 0, 0, 0)
        layout.setSpacing(0)
        layout.setContentsMargins(*m)
        self.setContentsMargins(*m)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)

    def addAction(self, action):
        if isinstance(action, QWidgetAction):
            return super(ToolBar, self).addAction(action)
        btn = QToolButton()
        btn.setDefaultAction(action)
        btn.setToolButtonStyle(self.toolButtonStyle())
        self.addWidget(btn)

        # center align
        for i in range(self.layout().count()):
            if isinstance(self.layout().itemAt(i).widget(), QToolButton):
                self.layout().itemAt(i).setAlignment(Qt.AlignCenter)


class ZoomWidget(QSpinBox):

    def __init__(self, value=100):
        super(ZoomWidget, self).__init__()
        self.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.setRange(1, 1000)
        self.setSuffix(" %")
        self.setValue(value)
        self.setToolTip("Zoom Level")
        self.setStatusTip(self.toolTip())
        self.setAlignment(Qt.AlignCenter)

    def minimumSizeHint(self):
        height = super(ZoomWidget, self).minimumSizeHint().height()
        fm = QFontMetrics(self.font())
        width = fm.width(str(self.maximum()))
        return QSize(width, height)


class Shape(object):
    # Render handles as squares
    P_SQUARE = 0

    # Render handles as circles
    P_ROUND = 1

    # Flag for the handles we would move if dragging
    MOVE_VERTEX = 0

    # Flag for all other handles on the current shape
    NEAR_VERTEX = 1

    PEN_WIDTH = 2

    # The following class variables influence the drawing of all shape objects.
    line_color = None
    fill_color = None
    select_line_color = None
    select_fill_color = None
    vertex_fill_color = None
    hvertex_fill_color = None
    point_type = P_ROUND
    point_size = 8
    scale = 1.0

    def __init__(
        self,
        label=None,
        line_color=None,
        shape_type=None,
        flags=None,
        group_id=None,
        description=None,
        mask=None,
    ):
        self.label = label
        self.group_id = group_id
        self.points = []
        self.point_labels = []
        self.shape_type = shape_type
        self._shape_raw = None
        self._points_raw = []
        self._shape_type_raw = None
        self.fill = False
        self.selected = False
        self.flags = flags
        self.description = description
        self.other_data = {}
        self.mask = mask

        self._highlightIndex = None
        self._highlightMode = self.NEAR_VERTEX
        self._highlightSettings = {
            self.NEAR_VERTEX: (4, self.P_ROUND),
            self.MOVE_VERTEX: (1.5, self.P_SQUARE),
        }

        self._closed = False

        if line_color is not None:
            # Override the class line_color attribute
            # with an object attribute. Currently this
            # is used for drawing the pending line a different color.
            self.line_color = line_color

    def _scale_point(self, point: QPointF) -> QPointF:
        return QPointF(point.x() * self.scale, point.y() * self.scale)

    def setShapeRefined(self, shape_type, points, point_labels, mask=None):
        self._shape_raw = (self.shape_type, self.points, self.point_labels)
        self.shape_type = shape_type
        self.points = points
        self.point_labels = point_labels
        self.mask = mask

    def restoreShapeRaw(self):
        if self._shape_raw is None:
            return
        self.shape_type, self.points, self.point_labels = self._shape_raw
        self._shape_raw = None

    @property
    def shape_type(self):
        return self._shape_type

    @shape_type.setter
    def shape_type(self, value):
        if value is None:
            value = "polygon"
        if value not in [
            "polygon",
            "point",
            "line",
            "circle",
            "linestrip",
            "points",
            "mask",
        ]:
            raise ValueError("Unexpected shape_type: {}".format(value))
        self._shape_type = value

    def close(self):
        self._closed = True

    def addPoint(self, point, label=1):
        if self.points and point == self.points[0]:
            self.close()
        else:
            self.points.append(point)
            self.point_labels.append(label)

    def canAddPoint(self):
        return self.shape_type in ["polygon", "linestrip"]

    def popPoint(self):
        if self.points:
            if self.point_labels:
                self.point_labels.pop()
            return self.points.pop()
        return None

    def insertPoint(self, i, point, label=1):
        self.points.insert(i, point)
        self.point_labels.insert(i, label)

    def removePoint(self, i):
        if not self.canAddPoint():
            logger.warning(
                "Cannot remove point from: shape_type=%r",
                self.shape_type,
            )
            return

        if self.shape_type == "polygon" and len(self.points) <= 3:
            logger.warning(
                "Cannot remove point from: shape_type=%r, len(points)=%d",
                self.shape_type,
                len(self.points),
            )
            return

        if self.shape_type == "linestrip" and len(self.points) <= 2:
            logger.warning(
                "Cannot remove point from: shape_type=%r, len(points)=%d",
                self.shape_type,
                len(self.points),
            )
            return

        self.points.pop(i)
        self.point_labels.pop(i)

    def isClosed(self):
        return self._closed

    def setOpen(self):
        self._closed = False

    def paint(self, painter):
        if self.mask is None and not self.points:
            return

        color = self.select_line_color if self.selected else self.line_color
        pen = QPen(color)
        # Try using integer sizes for smoother drawing(?)
        pen.setWidth(self.PEN_WIDTH)
        painter.setPen(pen)

        if self.mask is not None:
            image_to_draw = np.zeros(self.mask.shape + (4,), dtype=np.uint8)
            fill_color = (
                self.select_fill_color.getRgb()
                if self.selected
                else self.fill_color.getRgb()
            )
            image_to_draw[self.mask] = fill_color
            qimage = QImage.fromData(img_arr_to_data(image_to_draw))
            qimage = qimage.scaled(
                qimage.size() * self.scale,
                Qt.IgnoreAspectRatio,
                Qt.SmoothTransformation,
            )

            painter.drawImage(self._scale_point(point=self.points[0]), qimage)

            line_path = QPainterPath()
            contours = skimage.measure.find_contours(np.pad(self.mask, pad_width=1))
            for contour in contours:
                contour += [self.points[0].y(), self.points[0].x()]
                line_path.moveTo(
                    self._scale_point(QPointF(contour[0, 1], contour[0, 0]))
                )
                for point in contour[1:]:
                    line_path.lineTo(
                        self._scale_point(QPointF(point[1], point[0]))
                    )
            painter.drawPath(line_path)

        if self.points:
            line_path = QPainterPath()
            vrtx_path = QPainterPath()
            negative_vrtx_path = QPainterPath()

            if self.shape_type == "circle":
                assert len(self.points) in [1, 2]
                if len(self.points) == 2:
                    raidus = distance(
                        self._scale_point(self.points[0] - self.points[1])
                    )
                    line_path.addEllipse(
                        self._scale_point(self.points[0]), raidus, raidus
                    )
                for i in range(len(self.points)):
                    self.drawVertex(vrtx_path, i)
            elif self.shape_type == "linestrip":
                line_path.moveTo(self._scale_point(self.points[0]))
                for i, p in enumerate(self.points):
                    line_path.lineTo(self._scale_point(p))
                    self.drawVertex(vrtx_path, i)
            elif self.shape_type == "points":
                assert len(self.points) == len(self.point_labels)
                for i, point_label in enumerate(self.point_labels):
                    if point_label == 1:
                        self.drawVertex(vrtx_path, i)
                    else:
                        self.drawVertex(negative_vrtx_path, i)
            else:
                line_path.moveTo(self._scale_point(self.points[0]))
                # Uncommenting the following line will draw 2 paths
                # for the 1st vertex, and make it non-filled, which
                # may be desirable.
                # self.drawVertex(vrtx_path, 0)

                for i, p in enumerate(self.points):
                    line_path.lineTo(self._scale_point(p))
                    self.drawVertex(vrtx_path, i)
                if self.isClosed():
                    line_path.lineTo(self._scale_point(self.points[0]))

            painter.drawPath(line_path)
            if vrtx_path.length() > 0:
                painter.drawPath(vrtx_path)
                painter.fillPath(vrtx_path, self._vertex_fill_color)
            if self.fill and self.mask is None:
                color = self.select_fill_color if self.selected else self.fill_color
                painter.fillPath(line_path, color)

            pen.setColor(QColor(255, 0, 0, 255))
            painter.setPen(pen)
            painter.drawPath(negative_vrtx_path)
            painter.fillPath(negative_vrtx_path, QColor(255, 0, 0, 255))

    def drawVertex(self, path, i):
        d = self.point_size
        shape = self.point_type
        point = self._scale_point(self.points[i])
        if i == self._highlightIndex:
            size, shape = self._highlightSettings[self._highlightMode]
            d *= size
        if self._highlightIndex is not None:
            self._vertex_fill_color = self.hvertex_fill_color
        else:
            self._vertex_fill_color = self.vertex_fill_color
        if shape == self.P_SQUARE:
            path.addRect(point.x() - d / 2, point.y() - d / 2, d, d)
        elif shape == self.P_ROUND:
            path.addEllipse(point, d / 2.0, d / 2.0)
        else:
            assert False, "unsupported vertex shape"

    def nearestVertex(self, point, epsilon):
        min_distance = float("inf")
        min_i = None
        point = QPointF(point.x() * self.scale, point.y() * self.scale)
        for i, p in enumerate(self.points):
            p = QPointF(p.x() * self.scale, p.y() * self.scale)
            dist = distance(p - point)
            if dist <= epsilon and dist < min_distance:
                min_distance = dist
                min_i = i
        return min_i

    def nearestEdge(self, point, epsilon):
        min_distance = float("inf")
        post_i = None
        point = QPointF(point.x() * self.scale, point.y() * self.scale)
        for i in range(len(self.points)):
            start = self.points[i - 1]
            end = self.points[i]
            start = QPointF(start.x() * self.scale, start.y() * self.scale)
            end = QPointF(end.x() * self.scale, end.y() * self.scale)
            line = [start, end]
            dist = distancetoline(point, line)
            if dist <= epsilon and dist < min_distance:
                min_distance = dist
                post_i = i
        return post_i

    def containsPoint(self, point):
        if self.mask is not None:
            y = np.clip(
                int(round(point.y() - self.points[0].y())),
                0,
                self.mask.shape[0] - 1,
            )
            x = np.clip(
                int(round(point.x() - self.points[0].x())),
                0,
                self.mask.shape[1] - 1,
            )
            return self.mask[y, x]
        return self.makePath().contains(point)

    def makePath(self):
        if self.shape_type == "circle":
            path = QPainterPath()
            if len(self.points) == 2:
                raidus = distance(self.points[0] - self.points[1])
                path.addEllipse(self.points[0], raidus, raidus)
        else:
            path = QPainterPath(self.points[0])
            for p in self.points[1:]:
                path.lineTo(p)
        return path

    def boundingRect(self):
        return self.makePath().boundingRect()

    def moveBy(self, offset):
        self.points = [p + offset for p in self.points]

    def moveVertexBy(self, i, offset):
        self.points[i] = self.points[i] + offset

    def highlightVertex(self, i, action):
        """Highlight a vertex appropriately based on the current action

        Args:
            i (int): The vertex index
            action (int): The action
            (see Shape.NEAR_VERTEX and Shape.MOVE_VERTEX)
        """
        self._highlightIndex = i
        self._highlightMode = action

    def highlightClear(self):
        """Clear the highlighted point"""
        self._highlightIndex = None

    def copy(self):
        return copy.deepcopy(self)

    def __len__(self):
        return len(self.points)

    def __getitem__(self, key):
        return self.points[key]

    def __setitem__(self, key, value):
        self.points[key] = value


class Canvas(QWidget):
    zoomRequest = pyqtSignal(int, QPoint)
    scrollRequest = pyqtSignal(int, int)
    newShape = pyqtSignal()
    selectionChanged = pyqtSignal(list)
    shapeMoved = pyqtSignal()
    drawingPolygon = pyqtSignal(bool)
    vertexSelected = pyqtSignal(bool)
    mouseMoved = pyqtSignal(QPointF)

    CREATE, EDIT = 0, 1

    _createMode = "polygon"

    _fill_drawing = False

    def __init__(self, *args, **kwargs):
        self.epsilon = kwargs.pop("epsilon", 10.0)
        self.double_click = kwargs.pop("double_click", "close")
        if self.double_click not in [None, "close"]:
            raise ValueError(
                "Unexpected value for double_click event: {}".format(self.double_click)
            )
        self.num_backups = kwargs.pop("num_backups", 10)
        self._crosshair = kwargs.pop(
            "crosshair",
            {
                "polygon": False,
                "circle": False,
                "line": False,
                "point": False,
                "linestrip": False,
                "ai_mask": False,
            },
        )
        super(Canvas, self).__init__(*args, **kwargs)
        # Initialise local state.
        self.mode = self.EDIT
        self.shapes = []
        self.shapesBackups = []
        self.current = None
        self.selectedShapes = []  # save the selected shapes here
        self.selectedShapesCopy = []
        # self.line represents:
        #   - createMode == 'polygon': edge from last point to current
        #   - createMode == 'line': the line
        #   - createMode == 'point': the point
        self.line = Shape()
        self.prevPoint = QPoint()
        self.prevMovePoint = QPoint()
        self.offsets = QPoint(), QPoint()
        self.scale = 1.0
        self.pixmap = QPixmap()
        self.visible = {}
        self._hideBackround = False
        self.hideBackround = False
        self.hShape = None
        self.prevhShape = None
        self.hVertex = None
        self.prevhVertex = None
        self.hEdge = None
        self.prevhEdge = None
        self.movingShape = False
        self.snapping = True
        self.hShapeIsSelected = False
        self._painter = QPainter()
        self._cursor = CURSOR_DEFAULT
        # Menus:
        # 0: right-click without selection and dragging of shapes
        # 1: right-click with selection and dragging of shapes
        self.menus = [QMenu(), QMenu()]
        # Set widget options.
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.WheelFocus)

        self._ai_model = None

    def fillDrawing(self):
        return self._fill_drawing

    def setFillDrawing(self, value):
        self._fill_drawing = value

    @property
    def createMode(self):
        return self._createMode

    @createMode.setter
    def createMode(self, value):
        if value not in [
            "polygon",
            "circle",
            "line",
            "point",
            "linestrip",
            "ai_mask",
        ]:
            raise ValueError("Unsupported createMode: %s" % value)
        self._createMode = value

    def storeShapes(self):
        shapesBackup = []
        for shape in self.shapes:
            shapesBackup.append(shape.copy())
        if len(self.shapesBackups) > self.num_backups:
            self.shapesBackups = self.shapesBackups[-self.num_backups - 1 :]
        self.shapesBackups.append(shapesBackup)

    @property
    def isShapeRestorable(self):
        # We save the state AFTER each edit (not before) so for an
        # edit to be undoable, we expect the CURRENT and the PREVIOUS state
        # to be in the undo stack.
        if len(self.shapesBackups) < 2:
            return False
        return True

    def restoreShape(self):
        # This does _part_ of the job of restoring shapes.
        # The complete process is also done in app.py::undoShapeEdit
        # and app.py::loadShapes and our own Canvas::loadShapes function.
        if not self.isShapeRestorable:
            return
        self.shapesBackups.pop()  # latest

        # The application will eventually call Canvas.loadShapes which will
        # push this right back onto the stack.
        shapesBackup = self.shapesBackups.pop()
        self.shapes = shapesBackup
        self.selectedShapes = []
        for shape in self.shapes:
            shape.selected = False
        self.update()

    def enterEvent(self, ev):
        self.overrideCursor(self._cursor)

    def leaveEvent(self, ev):
        self.unHighlight()
        self.restoreCursor()

    def focusOutEvent(self, ev):
        self.restoreCursor()

    def isVisible(self, shape):
        return self.visible.get(shape, True)

    def drawing(self):
        return self.mode == self.CREATE

    def editing(self):
        return self.mode == self.EDIT

    def setEditing(self, value=True):
        self.mode = self.EDIT if value else self.CREATE
        if self.mode == self.EDIT:
            # CREATE -> EDIT
            self.repaint()  # clear crosshair
        else:
            # EDIT -> CREATE
            self.unHighlight()
            self.deSelectShape()

    def unHighlight(self):
        if self.hShape:
            self.hShape.highlightClear()
            self.update()
        self.prevhShape = self.hShape
        self.prevhVertex = self.hVertex
        self.prevhEdge = self.hEdge
        self.hShape = self.hVertex = self.hEdge = None

    def selectedVertex(self):
        return self.hVertex is not None

    def selectedEdge(self):
        return self.hEdge is not None

    def mouseMoveEvent(self, ev):
        """Update line with last point and current coordinates."""
        try:
            pos = self.transformPos(ev.localPos())
        except AttributeError:
            return

        self.mouseMoved.emit(pos)

        self.prevMovePoint = pos
        self.restoreCursor()

        is_shift_pressed = ev.modifiers() & Qt.ShiftModifier

        # Polygon drawing.
        if self.drawing():
            self.line.shape_type = self.createMode

            self.overrideCursor(CURSOR_DRAW)
            if not self.current:
                self.repaint()  # draw crosshair
                return

            if self.outOfPixmap(pos):
                # Don't allow the user to draw outside the pixmap.
                # Project the point to the pixmap's edges.
                pos = self.intersectionPoint(self.current[-1], pos)
            elif (
                self.snapping
                and len(self.current) > 1
                and self.createMode == "polygon"
                and self.closeEnough(pos, self.current[0])
            ):
                # Attract line to starting point and
                # colorise to alert the user.
                pos = self.current[0]
                self.overrideCursor(CURSOR_POINT)
                self.current.highlightVertex(0, Shape.NEAR_VERTEX)
            if self.createMode in ["polygon", "linestrip"]:
                self.line.points = [self.current[-1], pos]
                self.line.point_labels = [1, 1]
            elif self.createMode == "circle":
                self.line.points = [self.current[0], pos]
                self.line.point_labels = [1, 1]
                self.line.shape_type = "circle"
            elif self.createMode == "line":
                self.line.points = [self.current[0], pos]
                self.line.point_labels = [1, 1]
                self.line.close()
            elif self.createMode == "point":
                self.line.points = [self.current[0]]
                self.line.point_labels = [1]
                self.line.close()
            assert len(self.line.points) == len(self.line.point_labels)
            self.repaint()
            self.current.highlightClear()
            return

        # Polygon copy moving.
        if Qt.RightButton & ev.buttons():
            if self.selectedShapesCopy and self.prevPoint:
                self.overrideCursor(CURSOR_MOVE)
                self.boundedMoveShapes(self.selectedShapesCopy, pos)
                self.repaint()
            elif self.selectedShapes:
                self.selectedShapesCopy = [s.copy() for s in self.selectedShapes]
                self.repaint()
            return

        # Polygon/Vertex moving.
        if Qt.LeftButton & ev.buttons():
            if self.selectedVertex():
                self.boundedMoveVertex(pos)
                self.repaint()
                self.movingShape = True
            elif self.selectedShapes and self.prevPoint:
                self.overrideCursor(CURSOR_MOVE)
                self.boundedMoveShapes(self.selectedShapes, pos)
                self.repaint()
                self.movingShape = True
            return

        # Just hovering over the canvas, 2 possibilities:
        # - Highlight shapes
        # - Highlight vertex
        # Update shape/vertex fill and tooltip value accordingly.
        self.setToolTip(self.tr("Image"))
        for shape in reversed([s for s in self.shapes if self.isVisible(s)]):
            # Look for a nearby vertex to highlight. If that fails,
            # check if we happen to be inside a shape.
            index = shape.nearestVertex(pos, self.epsilon)
            index_edge = shape.nearestEdge(pos, self.epsilon)
            if index is not None:
                if self.selectedVertex():
                    self.hShape.highlightClear()
                self.prevhVertex = self.hVertex = index
                self.prevhShape = self.hShape = shape
                self.prevhEdge = self.hEdge
                self.hEdge = None
                shape.highlightVertex(index, shape.MOVE_VERTEX)
                self.overrideCursor(CURSOR_POINT)
                self.setToolTip(
                    self.tr(
                        "Click & Drag to move point\n"
                        "ALT + SHIFT + Click to delete point"
                    )
                )
                self.setStatusTip(self.toolTip())
                self.update()
                break
            elif index_edge is not None and shape.canAddPoint():
                if self.selectedVertex():
                    self.hShape.highlightClear()
                self.prevhVertex = self.hVertex
                self.hVertex = None
                self.prevhShape = self.hShape = shape
                self.prevhEdge = self.hEdge = index_edge
                self.overrideCursor(CURSOR_POINT)
                self.setToolTip(self.tr("ALT + Click to create point"))
                self.setStatusTip(self.toolTip())
                self.update()
                break
            elif shape.containsPoint(pos):
                if self.selectedVertex():
                    self.hShape.highlightClear()
                self.prevhVertex = self.hVertex
                self.hVertex = None
                self.prevhShape = self.hShape = shape
                self.prevhEdge = self.hEdge
                self.hEdge = None
                self.setToolTip(
                    self.tr("Click & drag to move shape '%s'") % shape.label
                )
                self.setStatusTip(self.toolTip())
                self.overrideCursor(CURSOR_GRAB)
                self.update()
                break
        else:  # Nothing found, clear highlights, reset state.
            self.unHighlight()
        self.vertexSelected.emit(self.hVertex is not None)

    def addPointToEdge(self):
        shape = self.prevhShape
        index = self.prevhEdge
        point = self.prevMovePoint
        if shape is None or index is None or point is None:
            return
        shape.insertPoint(index, point)
        shape.highlightVertex(index, shape.MOVE_VERTEX)
        self.hShape = shape
        self.hVertex = index
        self.hEdge = None
        self.movingShape = True

    def removeSelectedPoint(self):
        shape = self.prevhShape
        index = self.prevhVertex
        if shape is None or index is None:
            return
        shape.removePoint(index)
        shape.highlightClear()
        self.hShape = shape
        self.prevhVertex = None
        self.movingShape = True  # Save changes

    def mousePressEvent(self, ev):
        pos = self.transformPos(ev.localPos())
        
        is_shift_pressed = ev.modifiers() & Qt.ShiftModifier

        if ev.button() == Qt.LeftButton:
            if self.drawing():
                if self.current:
                    # Add point to existing shape.
                    if self.createMode == "polygon":
                        self.current.addPoint(self.line[1])
                        self.line[0] = self.current[-1]
                        if len(self.current.points) == 4:
                            self.finalise()
                    elif self.createMode == "linestrip":
                        self.current.addPoint(self.line[1])
                        self.line[0] = self.current[-1]
                        if int(ev.modifiers()) == Qt.ControlModifier:
                            self.finalise()
                elif not self.outOfPixmap(pos):
                    # Create new shape.
                    self.current = Shape(
                        shape_type=self.createMode
                    )
                    self.current.addPoint(pos, label=0 if is_shift_pressed else 1)
                    if self.createMode == "point":
                        self.finalise()
                    else:
                        if self.createMode == "circle":
                            self.current.shape_type = "circle"
                        self.line.points = [pos, pos]
                        self.line.point_labels = [1, 1]
                        self.setHiding()
                        self.drawingPolygon.emit(True)
                        self.update()
            elif self.editing():
                if self.selectedEdge() and ev.modifiers() == Qt.AltModifier:
                    self.addPointToEdge()
                elif self.selectedVertex() and ev.modifiers() == (
                    Qt.AltModifier | Qt.ShiftModifier
                ):
                    self.removeSelectedPoint()

                group_mode = int(ev.modifiers()) == Qt.ControlModifier
                self.selectShapePoint(pos, multiple_selection_mode=group_mode)
                self.prevPoint = pos
                self.repaint()
        elif ev.button() == Qt.RightButton and self.editing():
            group_mode = int(ev.modifiers()) == Qt.ControlModifier
            if not self.selectedShapes or (
                self.hShape is not None and self.hShape not in self.selectedShapes
            ):
                self.selectShapePoint(pos, multiple_selection_mode=group_mode)
                self.repaint()
            self.prevPoint = pos

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.RightButton:
            menu = self.menus[len(self.selectedShapesCopy) > 0]
            self.restoreCursor()
            if isinstance(menu, QMenu):
                if not menu.exec_(self.mapToGlobal(ev.pos())) and self.selectedShapesCopy:
                    # Cancel the move by deleting the shadow copy.
                    self.selectedShapesCopy = []
                    self.repaint()
            else:
                menu()
        elif ev.button() == Qt.LeftButton:
            if self.editing():
                if (
                    self.hShape is not None
                    and self.hShapeIsSelected
                    and not self.movingShape
                ):
                    self.selectionChanged.emit(
                        [x for x in self.selectedShapes if x != self.hShape]
                    )

        if self.movingShape and self.hShape:
            index = self.shapes.index(self.hShape)
            if self.shapesBackups[-1][index].points != self.shapes[index].points:
                self.storeShapes()
                self.shapeMoved.emit()

            self.movingShape = False

    def endMove(self, copy):
        assert self.selectedShapes and self.selectedShapesCopy
        assert len(self.selectedShapesCopy) == len(self.selectedShapes)
        if copy:
            for i, shape in enumerate(self.selectedShapesCopy):
                self.shapes.append(shape)
                self.selectedShapes[i].selected = False
                self.selectedShapes[i] = shape
        else:
            for i, shape in enumerate(self.selectedShapesCopy):
                self.selectedShapes[i].points = shape.points
        self.selectedShapesCopy = []
        self.repaint()
        self.storeShapes()
        return True

    def hideBackroundShapes(self, value):
        self.hideBackround = value
        if self.selectedShapes:
            # Only hide other shapes if there is a current selection.
            # Otherwise the user will not be able to select a shape.
            self.setHiding(True)
            self.update()

    def setHiding(self, enable=True):
        self._hideBackround = self.hideBackround if enable else False

    def canCloseShape(self):
        return self.drawing() and (
            (self.current and len(self.current) > 2)
        )

    def mouseDoubleClickEvent(self, ev):
        if self.double_click != "close":
            return

        if (self.createMode == "polygon" and self.canCloseShape()):
            self.finalise()

    def selectShapes(self, shapes):
        self.setHiding()
        self.selectionChanged.emit(shapes)
        self.update()

    def selectShapePoint(self, point, multiple_selection_mode):
        """Select the first shape created which contains this point."""
        if self.selectedVertex():  # A vertex is marked for selection.
            index, shape = self.hVertex, self.hShape
            shape.highlightVertex(index, shape.MOVE_VERTEX)
        else:
            for shape in reversed(self.shapes):
                if self.isVisible(shape) and shape.containsPoint(point):
                    self.setHiding()
                    if shape not in self.selectedShapes:
                        if multiple_selection_mode:
                            self.selectionChanged.emit(self.selectedShapes + [shape])
                        else:
                            self.selectionChanged.emit([shape])
                        self.hShapeIsSelected = False
                    else:
                        self.hShapeIsSelected = True
                    self.calculateOffsets(point)
                    return
        self.deSelectShape()

    def calculateOffsets(self, point):
        left = self.pixmap.width() - 1
        right = 0
        top = self.pixmap.height() - 1
        bottom = 0
        for s in self.selectedShapes:
            rect = s.boundingRect()
            if rect.left() < left:
                left = rect.left()
            if rect.right() > right:
                right = rect.right()
            if rect.top() < top:
                top = rect.top()
            if rect.bottom() > bottom:
                bottom = rect.bottom()

        x1 = left - point.x()
        y1 = top - point.y()
        x2 = right - point.x()
        y2 = bottom - point.y()
        self.offsets = QPointF(x1, y1), QPointF(x2, y2)

    def boundedMoveVertex(self, pos):
        index, shape = self.hVertex, self.hShape
        point = shape[index]
        if self.outOfPixmap(pos):
            pos = self.intersectionPoint(point, pos)
        shape.moveVertexBy(index, pos - point)

    def boundedMoveShapes(self, shapes, pos):
        if self.outOfPixmap(pos):
            return False  # No need to move
        o1 = pos + self.offsets[0]
        if self.outOfPixmap(o1):
            pos -= QPointF(min(0, o1.x()), min(0, o1.y()))
        o2 = pos + self.offsets[1]
        if self.outOfPixmap(o2):
            pos += QPointF(
                min(0, self.pixmap.width() - o2.x()),
                min(0, self.pixmap.height() - o2.y()),
            )
        # XXX: The next line tracks the new position of the cursor
        # relative to the shape, but also results in making it
        # a bit "shaky" when nearing the border and allows it to
        # go outside of the shape's area for some reason.
        # self.calculateOffsets(self.selectedShapes, pos)
        dp = pos - self.prevPoint
        if dp:
            for shape in shapes:
                shape.moveBy(dp)
            self.prevPoint = pos
            return True
        return False

    def deSelectShape(self):
        if self.selectedShapes:
            self.setHiding(False)
            self.selectionChanged.emit([])
            self.hShapeIsSelected = False
            self.update()

    def deleteSelected(self):
        deleted_shapes = []
        if self.selectedShapes:
            for shape in self.selectedShapes:
                self.shapes.remove(shape)
                deleted_shapes.append(shape)
            self.storeShapes()
            self.selectedShapes = []
            self.update()
        return deleted_shapes

    def deleteShape(self, shape):
        if shape in self.selectedShapes:
            self.selectedShapes.remove(shape)
        if shape in self.shapes:
            self.shapes.remove(shape)
        self.storeShapes()
        self.update()

    def paintEvent(self, event):
        if not self.pixmap:
            return super(Canvas, self).paintEvent(event)

        p = self._painter
        p.begin(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.HighQualityAntialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        p.scale(self.scale, self.scale)
        p.translate(self.offsetToCenter())

        p.drawPixmap(0, 0, self.pixmap)

        p.scale(1 / self.scale, 1 / self.scale)

        # draw crosshair
        if (
            self._crosshair[self._createMode]
            and self.drawing()
            and self.prevMovePoint
            and not self.outOfPixmap(self.prevMovePoint)
        ):
            p.setPen(QColor(0, 0, 0))
            p.drawLine(
                0,
                int(self.prevMovePoint.y() * self.scale),
                self.width() - 1,
                int(self.prevMovePoint.y() * self.scale),
            )
            p.drawLine(
                int(self.prevMovePoint.x() * self.scale),
                0,
                int(self.prevMovePoint.x() * self.scale),
                self.height() - 1,
            )

        Shape.scale = self.scale
        for shape in self.shapes:
            if (shape.selected or not self._hideBackround) and self.isVisible(shape):
                shape.fill = shape.selected or shape == self.hShape
                shape.paint(p)
        if self.current:
            self.current.paint(p)
            assert len(self.line.points) == len(self.line.point_labels)
            self.line.paint(p)
        if self.selectedShapesCopy:
            for s in self.selectedShapesCopy:
                s.paint(p)

        if (
            self.fillDrawing()
            and self.createMode == "polygon"
            and self.current is not None
            and len(self.current.points) >= 2
        ):
            drawing_shape = self.current.copy()
            if drawing_shape.fill_color.getRgb()[3] == 0:
                logger.warning(
                    "fill_drawing=true, but fill_color is transparent,"
                    " so forcing to be opaque."
                )
                drawing_shape.fill_color.setAlpha(64)
            drawing_shape.addPoint(self.line[1])
            drawing_shape.fill = True
            drawing_shape.paint(p)

        p.end()

    def transformPos(self, point):
        """Convert from widget-logical coordinates to painter-logical ones."""
        return point / self.scale - self.offsetToCenter()

    def offsetToCenter(self):
        s = self.scale
        area = super(Canvas, self).size()
        w, h = self.pixmap.width() * s, self.pixmap.height() * s
        aw, ah = area.width(), area.height()
        x = (aw - w) / (2 * s) if aw > w else 0
        y = (ah - h) / (2 * s) if ah > h else 0
        return QPointF(x, y)

    def outOfPixmap(self, p):
        w, h = self.pixmap.width(), self.pixmap.height()
        return not (0 <= p.x() <= w - 1 and 0 <= p.y() <= h - 1)

    def finalise(self):
        assert self.current
        self.current.close()

        self.shapes.append(self.current)
        self.storeShapes()
        self.current = None
        self.setHiding(False)
        self.newShape.emit()
        self.update()

    def closeEnough(self, p1, p2):
        # d = distance(p1 - p2)
        # m = (p1-p2).manhattanLength()
        # print "d %.2f, m %d, %.2f" % (d, m, d - m)
        # divide by scale to allow more precision when zoomed in
        return distance(p1 - p2) < (self.epsilon / self.scale)

    def intersectionPoint(self, p1, p2):
        # Cycle through each image edge in clockwise fashion,
        # and find the one intersecting the current line segment.
        # http://paulbourke.net/geometry/lineline2d/
        size = self.pixmap.size()
        points = [
            (0, 0),
            (size.width() - 1, 0),
            (size.width() - 1, size.height() - 1),
            (0, size.height() - 1),
        ]
        # x1, y1 should be in the pixmap, x2, y2 should be out of the pixmap
        x1 = min(max(p1.x(), 0), size.width() - 1)
        y1 = min(max(p1.y(), 0), size.height() - 1)
        x2, y2 = p2.x(), p2.y()
        d, i, (x, y) = min(self.intersectingEdges((x1, y1), (x2, y2), points))
        x3, y3 = points[i]
        x4, y4 = points[(i + 1) % 4]
        if (x, y) == (x1, y1):
            # Handle cases where previous point is on one of the edges.
            if x3 == x4:
                return QPointF(x3, min(max(0, y2), max(y3, y4)))
            else:  # y3 == y4
                return QPointF(min(max(0, x2), max(x3, x4)), y3)
        return QPointF(x, y)

    def intersectingEdges(self, point1, point2, points):
        """Find intersecting edges.

        For each edge formed by `points', yield the intersection
        with the line segment `(x1,y1) - (x2,y2)`, if it exists.
        Also return the distance of `(x2,y2)' to the middle of the
        edge along with its index, so that the one closest can be chosen.
        """
        (x1, y1) = point1
        (x2, y2) = point2
        for i in range(4):
            x3, y3 = points[i]
            x4, y4 = points[(i + 1) % 4]
            denom = (y4 - y3) * (x2 - x1) - (x4 - x3) * (y2 - y1)
            nua = (x4 - x3) * (y1 - y3) - (y4 - y3) * (x1 - x3)
            nub = (x2 - x1) * (y1 - y3) - (y2 - y1) * (x1 - x3)
            if denom == 0:
                # This covers two cases:
                #   nua == nub == 0: Coincident
                #   otherwise: Parallel
                continue
            ua, ub = nua / denom, nub / denom
            if 0 <= ua <= 1 and 0 <= ub <= 1:
                x = x1 + ua * (x2 - x1)
                y = y1 + ua * (y2 - y1)
                m = QPointF((x3 + x4) / 2, (y3 + y4) / 2)
                d = distance(m - QPointF(x2, y2))
                yield d, i, (x, y)

    # These two, along with a call to adjustSize are required for the
    # scroll area.
    def sizeHint(self):
        return self.minimumSizeHint()

    def minimumSizeHint(self):
        if self.pixmap:
            return self.scale * self.pixmap.size()
        return super(Canvas, self).minimumSizeHint()

    def wheelEvent(self, ev):
        mods = ev.modifiers()
        delta = ev.angleDelta()
        if Qt.ControlModifier == int(mods):
            # with Ctrl/Command key
            # zoom
            self.zoomRequest.emit(delta.y(), ev.pos())
        else:
            # scroll
            self.scrollRequest.emit(delta.x(), Qt.Horizontal)
            self.scrollRequest.emit(delta.y(), Qt.Vertical)
        ev.accept()

    def moveByKeyboard(self, offset):
        if self.selectedShapes:
            self.boundedMoveShapes(self.selectedShapes, self.prevPoint + offset)
            self.repaint()
            self.movingShape = True

    def keyPressEvent(self, ev):
        modifiers = ev.modifiers()
        key = ev.key()
        if self.drawing():
            if key == Qt.Key_Escape and self.current:
                self.current = None
                self.drawingPolygon.emit(False)
                self.update()
            elif key == Qt.Key_Return and self.canCloseShape():
                self.finalise()
            elif modifiers == Qt.AltModifier:
                self.snapping = False
        elif self.editing():
            if key == Qt.Key_Up:
                self.moveByKeyboard(QPointF(0.0, -MOVE_SPEED))
            elif key == Qt.Key_Down:
                self.moveByKeyboard(QPointF(0.0, MOVE_SPEED))
            elif key == Qt.Key_Left:
                self.moveByKeyboard(QPointF(-MOVE_SPEED, 0.0))
            elif key == Qt.Key_Right:
                self.moveByKeyboard(QPointF(MOVE_SPEED, 0.0))

    def keyReleaseEvent(self, ev):
        modifiers = ev.modifiers()
        if self.drawing():
            if int(modifiers) == 0:
                self.snapping = True
        elif self.editing():
            if self.movingShape and self.selectedShapes:
                index = self.shapes.index(self.selectedShapes[0])
                if self.shapesBackups[-1][index].points != self.shapes[index].points:
                    self.storeShapes()
                    self.shapeMoved.emit()

                self.movingShape = False

    def setLastLabel(self, text, flags):
        assert text
        self.shapes[-1].label = text
        self.shapes[-1].flags = flags
        self.shapesBackups.pop()
        self.storeShapes()
        return self.shapes[-1]

    def undoLastLine(self):
        assert self.shapes
        self.current = self.shapes.pop()
        self.current.setOpen()
        self.current.restoreShapeRaw()
        if self.createMode in ["polygon", "linestrip"]:
            self.line.points = [self.current[-1], self.current[0]]
        elif self.createMode == "point":
            self.current = None
        self.drawingPolygon.emit(True)

    def undoLastPoint(self):
        if not self.current or self.current.isClosed():
            return
        self.current.popPoint()
        if len(self.current) > 0:
            self.line[0] = self.current[-1]
        else:
            self.current = None
            self.drawingPolygon.emit(False)
        self.update()

    def loadPixmap(self, pixmap, clear_shapes=True):
        self.pixmap = pixmap
        if self._ai_model:
            self._ai_model.set_image(
                image=img_qt_to_arr(self.pixmap.toImage())
            )
        if clear_shapes:
            self.shapes = []
        self.update()

    def loadShapes(self, shapes, replace=True):
        if replace:
            self.shapes = list(shapes)
        else:
            self.shapes.extend(shapes)
        self.storeShapes()
        self.current = None
        self.hShape = None
        self.hVertex = None
        self.hEdge = None
        self.update()

    def setShapeVisible(self, shape, value):
        self.visible[shape] = value
        self.update()

    def overrideCursor(self, cursor):
        self.restoreCursor()
        self._cursor = cursor
        QApplication.setOverrideCursor(cursor)

    def restoreCursor(self):
        QApplication.restoreOverrideCursor()

    def resetState(self):
        self.restoreCursor()
        self.pixmap = None
        self.shapesBackups = []
        self.update()


class LabelFileError(Exception):
    pass


class LabelFile(object):
    suffix = ".json"

    def __init__(self, filename=None):
        self.shapes = []
        self.imagePath = None
        self.imageData = None
        if filename is not None:
            self.load(filename)
        self.filename = filename

    @staticmethod
    def load_image_file(filename):
        try:
            image_pil = PIL.Image.open(filename)
        except IOError:
            logger.error("Failed opening image file: {}".format(filename))
            return

        # apply orientation to image according to exif
        image_pil = apply_exif_orientation(image_pil)

        with io.BytesIO() as f:
            ext = osp.splitext(filename)[1].lower()
            if ext in [".jpg", ".jpeg"]:
                format = "JPEG"
            else:
                format = "PNG"
            image_pil.save(f, format=format)
            f.seek(0)
            return f.read()

    def load(self, filename):
        keys = [
            "version",
            "imageData",
            "imagePath",
            "shapes",  # polygonal annotations
            "flags",  # image level flags
            "imageHeight",
            "imageWidth",
        ]
        shape_keys = [
            "label",
            "points",
            "group_id",
            "shape_type",
            "flags",
            "description",
            "mask",
        ]
        try:
            with open(filename, "r") as f:
                data = json.load(f)

            if data["imageData"] is not None:
                imageData = base64.b64decode(data["imageData"])
            else:
                # relative path from label file to relative path from cwd
                imagePath = osp.join(osp.dirname(filename), data["imagePath"])
                imageData = self.load_image_file(imagePath)
            flags = data.get("flags") or {}
            imagePath = data["imagePath"]
            self._check_image_height_and_width(
                base64.b64encode(imageData).decode("utf-8"),
                data.get("imageHeight"),
                data.get("imageWidth"),
            )
            shapes = [
                dict(
                    label=s["label"],
                    points=s["points"],
                    shape_type=s.get("shape_type", "polygon"),
                    flags=s.get("flags", {}),
                    description=s.get("description"),
                    group_id=s.get("group_id"),
                    mask=img_b64_to_arr(s["mask"]).astype(bool)
                    if s.get("mask")
                    else None,
                    other_data={k: v for k, v in s.items() if k not in shape_keys},
                )
                for s in data["shapes"]
            ]
        except Exception as e:
            raise LabelFileError(e)

        otherData = {}
        for key, value in data.items():
            if key not in keys:
                otherData[key] = value

        # Only replace data after everything is loaded.
        self.flags = flags
        self.shapes = shapes
        self.imagePath = imagePath
        self.imageData = imageData
        self.filename = filename
        self.otherData = otherData

    @staticmethod
    def _check_image_height_and_width(imageData, imageHeight, imageWidth):
        img_arr = img_b64_to_arr(imageData)
        if imageHeight is not None and img_arr.shape[0] != imageHeight:
            logger.error(
                "imageHeight does not match with imageData or imagePath, "
                "so getting imageHeight from actual image."
            )
            imageHeight = img_arr.shape[0]
        if imageWidth is not None and img_arr.shape[1] != imageWidth:
            logger.error(
                "imageWidth does not match with imageData or imagePath, "
                "so getting imageWidth from actual image."
            )
            imageWidth = img_arr.shape[1]
        return imageHeight, imageWidth

    def save(
        self,
        filename,
        shapes,
        imagePath,
        imageHeight,
        imageWidth,
        imageData=None,
        otherData=None,
        flags=None,
    ):
        if imageData is not None:
            imageData = base64.b64encode(imageData).decode("utf-8")
            imageHeight, imageWidth = self._check_image_height_and_width(
                imageData, imageHeight, imageWidth
            )
        if otherData is None:
            otherData = {}
        if flags is None:
            flags = {}
        data = dict(
            version=__version__,
            shapes=shapes,
            imagePath=imagePath,
            imageHeight=imageHeight,
            imageWidth=imageWidth,
        )
        for key, value in otherData.items():
            assert key not in data
            data[key] = value
        try:
            with open(filename, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.filename = filename
        except Exception as e:
            raise LabelFileError(e)

    @staticmethod
    def is_label_file(filename):
        return osp.splitext(filename)[1].lower() == LabelFile.suffix


class EscapableQListWidget(QListWidget):
    def keyPressEvent(self, event):
        super(EscapableQListWidget, self).keyPressEvent(event)
        if event.key() == Qt.Key_Escape:
            self.clearSelection()


class UniqueLabelQListWidget(EscapableQListWidget):

    def mousePressEvent(self, event):
        super(UniqueLabelQListWidget, self).mousePressEvent(event)
        if not self.indexAt(event.pos()).isValid():
            self.clearSelection()

    def findItemByLabel(self, label):
        for row in range(self.count()):
            item = self.item(row)
            if item.data(Qt.UserRole) == label:
                return item

    def createItemFromLabel(self, label):
        if self.findItemByLabel(label):
            raise ValueError("Item for label '{}' already exists".format(label))

        item = QListWidgetItem()
        item.setData(Qt.UserRole, label)
        return item

    def setItemLabel(self, item, label, color=None):
        qlabel = QLabel()
        if color is None:
            qlabel.setText("{}".format(label))
        else:
            qlabel.setText(
                '{} <font color="#{:02x}{:02x}{:02x}">●</font>'.format(
                    html.escape(label), *color
                )
            )
        qlabel.setAlignment(Qt.AlignBottom)

        item.setSizeHint(qlabel.sizeHint())

        self.setItemWidget(item, qlabel)


class LabelQLineEdit(QLineEdit):

    def setListWidget(self, list_widget):
        self.list_widget = list_widget

    def keyPressEvent(self, e):
        if e.key() in [Qt.Key_Up, Qt.Key_Down]:
            self.list_widget.keyPressEvent(e)
        else:
            super(LabelQLineEdit, self).keyPressEvent(e)


class HTMLDelegate(QStyledItemDelegate):

    def __init__(self, parent=None):
        super(HTMLDelegate, self).__init__()
        self.doc = QTextDocument(self)

    def paint(self, painter, option, index):
        painter.save()

        options = QStyleOptionViewItem(option)

        self.initStyleOption(options, index)
        self.doc.setHtml(options.text)
        options.text = ""

        style = (
            QApplication.style()
            if options.widget is None
            else options.widget.style()
        )
        style.drawControl(QStyle.CE_ItemViewItem, options, painter)

        ctx = QAbstractTextDocumentLayout.PaintContext()

        if option.state & QStyle.State_Selected:
            ctx.palette.setColor(
                QPalette.Text,
                option.palette.color(QPalette.Active, QPalette.HighlightedText),
            )
        else:
            ctx.palette.setColor(
                QPalette.Text,
                option.palette.color(QPalette.Active, QPalette.Text),
            )

        textRect = style.subElementRect(QStyle.SE_ItemViewItemText, options)

        if index.column() != 0:
            textRect.adjust(5, 0, 0, 0)

        thefuckyourshitup_constant = 4
        margin = (option.rect.height() - options.fontMetrics.height()) // 2
        margin = margin - thefuckyourshitup_constant
        textRect.setTop(textRect.top() + margin)

        painter.translate(textRect.topLeft())
        painter.setClipRect(textRect.translated(-textRect.topLeft()))
        self.doc.documentLayout().draw(painter, ctx)

        painter.restore()

    def sizeHint(self, option, index):
        thefuckyourshitup_constant = 4
        return QSize(
            int(self.doc.idealWidth()),
            int(self.doc.size().height() - thefuckyourshitup_constant),
        )


class StandardItemModel(QStandardItemModel):

    itemDropped = pyqtSignal()

    def removeRows(self, *args, **kwargs):
        ret = super().removeRows(*args, **kwargs)
        self.itemDropped.emit()
        return ret


class LabelListWidgetItem(QStandardItem):

    def __init__(self, text=None, shape=None):
        super(LabelListWidgetItem, self).__init__()
        self.setText(text or "")
        self.setShape(shape)

        self.setCheckable(True)
        self.setCheckState(Qt.Checked)
        self.setEditable(False)
        self.setTextAlignment(Qt.AlignBottom)

    def clone(self):
        return LabelListWidgetItem(self.text(), self.shape())

    def setShape(self, shape):
        self.setData(shape, Qt.UserRole)

    def shape(self):
        return self.data(Qt.UserRole)

    def __hash__(self):
        return id(self)

    def __repr__(self):
        return '{}("{}")'.format(self.__class__.__name__, self.text())


class LabelListWidget(QListView):

    itemDoubleClicked = pyqtSignal(LabelListWidgetItem)
    itemSelectionChanged = pyqtSignal(list, list)

    def __init__(self):
        super(LabelListWidget, self).__init__()
        self._selectedItems = []

        self.setWindowFlags(Qt.Window)
        self.setModel(StandardItemModel())
        self.model().setItemPrototype(LabelListWidgetItem())
        self.setItemDelegate(HTMLDelegate())
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)

        self.doubleClicked.connect(self.itemDoubleClickedEvent)
        self.selectionModel().selectionChanged.connect(self.itemSelectionChangedEvent)

    def __len__(self):
        return self.model().rowCount()

    def __getitem__(self, i):
        return self.model().item(i)

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    @property
    def itemDropped(self):
        return self.model().itemDropped

    @property
    def itemChanged(self):
        return self.model().itemChanged

    def itemSelectionChangedEvent(self, selected, deselected):
        selected = [self.model().itemFromIndex(i) for i in selected.indexes()]
        deselected = [self.model().itemFromIndex(i) for i in deselected.indexes()]
        self.itemSelectionChanged.emit(selected, deselected)

    def itemDoubleClickedEvent(self, index):
        self.itemDoubleClicked.emit(self.model().itemFromIndex(index))

    def selectedItems(self):
        return [self.model().itemFromIndex(i) for i in self.selectedIndexes()]

    def scrollToItem(self, item):
        self.scrollTo(self.model().indexFromItem(item))

    def addItem(self, item):
        if not isinstance(item, LabelListWidgetItem):
            raise TypeError("item must be LabelListWidgetItem")
        self.model().setItem(self.model().rowCount(), 0, item)
        item.setSizeHint(self.itemDelegate().sizeHint(None, None))

    def removeItem(self, item):
        index = self.model().indexFromItem(item)
        self.model().removeRows(index.row(), 1)

    def selectItem(self, item):
        index = self.model().indexFromItem(item)
        self.selectionModel().select(index, QItemSelectionModel.Select)

    def findItemByShape(self, shape):
        for row in range(self.model().rowCount()):
            item = self.model().item(row, 0)
            if item.shape() == shape:
                return item
        raise ValueError("cannot find shape: {}".format(shape))

    def clear(self):
        self.model().clear()


class LabelDialog(QDialog):
    def __init__(
        self,
        text="Enter object label",
        parent=None,
        labels=None,
        sort_labels=True,
        show_text_field=True,
        completion="startswith",
        fit_to_content=None,
        flags=None,
    ):
        if fit_to_content is None:
            fit_to_content = {"row": False, "column": True}
        self._fit_to_content = fit_to_content

        super(LabelDialog, self).__init__(parent)
        self.edit = LabelQLineEdit()
        self.edit.setPlaceholderText(text)
        self.edit.setValidator(labelValidator())
        self.edit.editingFinished.connect(self.postProcess)
        if flags:
            self.edit.textChanged.connect(self.updateFlags)
        self.edit_group_id = QLineEdit()
        self.edit_group_id.setPlaceholderText("Group ID")
        self.edit_group_id.setValidator(
            QRegExpValidator(QRegExp(r"\d*"), None)
        )
        layout = QVBoxLayout()
        if show_text_field:
            layout_edit = QHBoxLayout()
            layout_edit.addWidget(self.edit, 6)
            layout_edit.addWidget(self.edit_group_id, 2)
            layout.addLayout(layout_edit)
        # buttons
        self.buttonBox = bb = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
            Qt.Horizontal,
            self,
        )
        bb.button(bb.Ok).setIcon(newIcon("done"))
        bb.button(bb.Cancel).setIcon(newIcon("undo"))
        bb.accepted.connect(self.validate)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)
        # label_list
        self.labelList = QListWidget()
        if self._fit_to_content["row"]:
            self.labelList.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        if self._fit_to_content["column"]:
            self.labelList.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._sort_labels = sort_labels
        if labels:
            self.labelList.addItems(labels)
        if self._sort_labels:
            self.labelList.sortItems()
        else:
            self.labelList.setDragDropMode(QAbstractItemView.InternalMove)
        self.labelList.currentItemChanged.connect(self.labelSelected)
        self.labelList.itemDoubleClicked.connect(self.labelDoubleClicked)
        self.labelList.setFixedHeight(150)
        self.edit.setListWidget(self.labelList)
        layout.addWidget(self.labelList)
        # label_flags
        if flags is None:
            flags = {}
        self._flags = flags
        self.flagsLayout = QVBoxLayout()
        self.resetFlags()
        layout.addItem(self.flagsLayout)
        self.edit.textChanged.connect(self.updateFlags)
        # text edit
        self.editDescription = QTextEdit()
        self.editDescription.setPlaceholderText("Label description")
        self.editDescription.setFixedHeight(50)
        layout.addWidget(self.editDescription)
        self.setLayout(layout)
        # completion
        completer = QCompleter()
        if completion == "startswith":
            completer.setCompletionMode(QCompleter.InlineCompletion)
            # Default settings.
            # completer.setFilterMode(QtCore.Qt.MatchStartsWith)
        elif completion == "contains":
            completer.setCompletionMode(QCompleter.PopupCompletion)
            completer.setFilterMode(Qt.MatchContains)
        else:
            raise ValueError("Unsupported completion: {}".format(completion))
        completer.setModel(self.labelList.model())
        self.edit.setCompleter(completer)

    def addLabelHistory(self, label):
        if self.labelList.findItems(label, Qt.MatchExactly):
            return
        self.labelList.addItem(label)
        if self._sort_labels:
            self.labelList.sortItems()

    def labelSelected(self, item):
        self.edit.setText(item.text())

    def validate(self):
        if not self.edit.isEnabled():
            self.accept()
            return

        text = self.edit.text()
        if hasattr(text, "strip"):
            text = text.strip()
        else:
            text = text.trimmed()
        if text:
            self.accept()

    def labelDoubleClicked(self, item):
        self.validate()

    def postProcess(self):
        text = self.edit.text()
        if hasattr(text, "strip"):
            text = text.strip()
        else:
            text = text.trimmed()
        self.edit.setText(text)

    def updateFlags(self, label_new):
        # keep state of shared flags
        flags_old = self.getFlags()

        flags_new = {}
        for pattern, keys in self._flags.items():
            if re.match(pattern, label_new):
                for key in keys:
                    flags_new[key] = flags_old.get(key, False)
        self.setFlags(flags_new)

    def deleteFlags(self):
        for i in reversed(range(self.flagsLayout.count())):
            item = self.flagsLayout.itemAt(i).widget()
            self.flagsLayout.removeWidget(item)
            item.setParent(None)

    def resetFlags(self, label=""):
        flags = {}
        for pattern, keys in self._flags.items():
            if re.match(pattern, label):
                for key in keys:
                    flags[key] = False
        self.setFlags(flags)

    def setFlags(self, flags):
        self.deleteFlags()
        for key in flags:
            item = QCheckBox(key, self)
            item.setChecked(flags[key])
            self.flagsLayout.addWidget(item)
            item.show()

    def getFlags(self):
        flags = {}
        for i in range(self.flagsLayout.count()):
            item = self.flagsLayout.itemAt(i).widget()
            flags[item.text()] = item.isChecked()
        return flags

    def getGroupId(self):
        group_id = self.edit_group_id.text()
        if group_id:
            return int(group_id)
        return None

    def popUp(self, text=None, move=True, flags=None, group_id=None, description=None):
        if self._fit_to_content["row"]:
            self.labelList.setMinimumHeight(
                self.labelList.sizeHintForRow(0) * self.labelList.count() + 2
            )
        if self._fit_to_content["column"]:
            self.labelList.setMinimumWidth(self.labelList.sizeHintForColumn(0) + 2)
        # if text is None, the previous label in self.edit is kept
        if text is None:
            text = self.edit.text()
        # description is always initialized by empty text c.f., self.edit.text
        if description is None:
            description = ""
        self.editDescription.setPlainText(description)
        if flags:
            self.setFlags(flags)
        else:
            self.resetFlags(text)
        self.edit.setText(text)
        self.edit.setSelection(0, len(text))
        if group_id is None:
            self.edit_group_id.clear()
        else:
            self.edit_group_id.setText(str(group_id))
        items = self.labelList.findItems(text, Qt.MatchFixedString)
        if items:
            if len(items) != 1:
                logger.warning("Label list has duplicate '{}'".format(text))
            self.labelList.setCurrentItem(items[0])
            row = self.labelList.row(items[0])
            self.edit.completer().setCurrentRow(row)
        self.edit.setFocus(Qt.PopupFocusReason)
        if move:
            self.move(QCursor.pos())
        if self.exec_():
            return (
                self.edit.text(),
                self.getFlags(),
                self.getGroupId(),
                self.editDescription.toPlainText(),
            )
        else:
            return None, None, None, None


class BrightnessContrastDialog(QDialog):
    _base_value = 50

    def __init__(self, img, callback, parent=None):
        super(BrightnessContrastDialog, self).__init__(parent)
        self.setModal(True)
        self.setWindowTitle("Brightness/Contrast")

        sliders = {}
        layouts = {}
        for title in ["Brightness:", "Contrast:"]:
            layout = QHBoxLayout()
            title_label = QLabel(self.tr(title))
            title_label.setFixedWidth(75)
            layout.addWidget(title_label)
            #
            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, 3 * self._base_value)
            slider.setValue(self._base_value)
            layout.addWidget(slider)
            #
            value_label = QLabel(f"{slider.value() / self._base_value:.2f}")
            value_label.setAlignment(Qt.AlignRight)
            layout.addWidget(value_label)
            #
            slider.valueChanged.connect(self.onNewValue)
            slider.valueChanged.connect(
                lambda: value_label.setText(f"{slider.value() / self._base_value:.2f}")
            )
            layouts[title] = layout
            sliders[title] = slider

        self.slider_brightness = sliders["Brightness:"]
        self.slider_contrast = sliders["Contrast:"]
        del sliders

        layout = QVBoxLayout()
        layout.addLayout(layouts["Brightness:"])
        layout.addLayout(layouts["Contrast:"])
        del layouts
        self.setLayout(layout)

        assert isinstance(img, PIL.Image.Image)
        self.img = img
        self.callback = callback

    def onNewValue(self, _):
        brightness = self.slider_brightness.value() / self._base_value
        contrast = self.slider_contrast.value() / self._base_value

        img = self.img
        if brightness != 1:
            img = PIL.ImageEnhance.Brightness(img).enhance(brightness)
        if contrast != 1:
            img = PIL.ImageEnhance.Contrast(img).enhance(contrast)

        qimage = QImage(
            img.tobytes(), img.width, img.height, img.width * 3, QImage.Format_RGB888
        )
        self.callback(qimage)


class MainWindow(QMainWindow):

    def __init__(
            self,
            config=None,
            output=None,
            output_file=None,
            output_dir=None):
        if output is not None:
            logger.warning('argument output is deprecated, use output_file instead')
            if output_file is None:
                output_file = output

        if config is None:
            config = get_config()
        self._config = config

        Shape.line_color = QColor(*self._config['shape']['line_color'])
        Shape.fill_color = QColor(*self._config['shape']['fill_color'])
        Shape.select_line_color = QColor(*self._config['shape']['select_line_color'])
        Shape.select_fill_color = QColor(*self._config['shape']['select_fill_color'])
        Shape.vertex_fill_color = QColor(*self._config['shape']['vertex_fill_color'])
        Shape.hvertex_fill_color = QColor(*self._config['shape']['hvertex_fill_color'])
        Shape.point_size = self._config['shape']['point_size']

        super(MainWindow, self).__init__()
        self.setWindowTitle(__appname__)

        self.image_dir: Optional[str] = None
        self.annot_dir: Optional[str] = None
        self.dirty: bool = False
        self.image: QImage = QImage()
        self.image_path: Optional[str] = None
        self.image_data: Optional[bytes] = None
        self.zoom_mode = ZOOM_MODE_FIT_WINDOW
        self.zoom_level = 100
        self.zoom_values: dict[str, tuple[int, int]] = {}  # key=filename, value=(zoom_mode, zoom_value)
        self.recent_files: list[str] = []
        self.brightness_contrast_values = {}
        self.scroll_values = {
            Qt.Horizontal: {},
            Qt.Vertical: {}}
        self._noSelectionSlot = False
        self._copied_shapes = None

        self.label_dialog = LabelDialog(
            parent=self,
            labels=self._config['labels'],
            sort_labels=self._config['sort_labels'],
            show_text_field=self._config['show_label_text_field'],
            completion=self._config['label_completion'],
            fit_to_content=self._config['fit_to_content'])
        self.label_dialog.edit_group_id.setDisabled(True)
        self.label_dialog.editDescription.setDisabled(True)

        self.label_list = UniqueLabelQListWidget()
        if self._config['labels']:
            for label in self._config['labels']:
                item = self.label_list.createItemFromLabel(label)
                self.label_list.addItem(item)
                rgb = self.__get_rgb_by_label(label)
                self.label_list.setItemLabel(item, label, rgb)
        self.label_dock = QDockWidget(self.tr('Labels'), self)
        self.label_dock.setObjectName('Label List')
        self.label_dock.setFeatures(
            QDockWidget.DockWidgetFeatures() |
            QDockWidget.DockWidgetClosable |
            QDockWidget.DockWidgetFloatable |
            QDockWidget.DockWidgetMovable)
        self.label_dock.setWidget(self.label_list)

        self.quad_list = LabelListWidget()
        self.quad_list.itemSelectionChanged.connect(self.__label_selection_changed)
        self.quad_list.itemDoubleClicked.connect(self.__edit_label)
        self.quad_list.itemChanged.connect(self.__label_item_changed)
        self.quad_list.itemDropped.connect(self.__label_order_changed)
        self.quad_dock = QDockWidget(self.tr('Quads'), self)
        self.quad_dock.setObjectName('Labels')
        self.quad_dock.setFeatures(
            QDockWidget.DockWidgetFeatures() |
            QDockWidget.DockWidgetClosable |
            QDockWidget.DockWidgetFloatable |
            QDockWidget.DockWidgetMovable)
        self.quad_dock.setWidget(self.quad_list)

        self.file_search = QLineEdit()
        self.file_search.setPlaceholderText(self.tr('Search Filename'))
        self.file_search.textChanged.connect(self.__file_search_changed)
        self.file_list = QListWidget()
        self.file_list.itemSelectionChanged.connect(self.__file_selection_changed)
        file_list_layout = QVBoxLayout()
        file_list_layout.setContentsMargins(0, 0, 0, 0)
        file_list_layout.setSpacing(0)
        file_list_layout.addWidget(self.file_search)
        file_list_layout.addWidget(self.file_list)
        file_list_widget = QWidget()
        file_list_widget.setLayout(file_list_layout)
        self.file_dock = QDockWidget(self.tr('Files'), self)
        self.file_dock.setObjectName('Files')
        self.file_dock.setFeatures(
            QDockWidget.DockWidgetFeatures() |
            QDockWidget.DockWidgetClosable |
            QDockWidget.DockWidgetFloatable |
            QDockWidget.DockWidgetMovable)
        self.file_dock.setWidget(file_list_widget)

        self.setAcceptDrops(True)

        self.canvas = Canvas(
            epsilon=self._config['epsilon'],
            double_click=self._config['canvas']['double_click'],
            num_backups=self._config['canvas']['num_backups'],
            crosshair=self._config['canvas']['crosshair'])
        self.canvas.zoomRequest.connect(self.__zoom_request)
        self.canvas.mouseMoved.connect(lambda pos: self.__status(f'Mouse is at: x={pos.x()}, y={pos.y()}'))

        scroll_area = QScrollArea()
        scroll_area.setWidget(self.canvas)
        scroll_area.setWidgetResizable(True)
        self.scroll_bars = {
            Qt.Vertical: scroll_area.verticalScrollBar(),
            Qt.Horizontal: scroll_area.horizontalScrollBar()}
        self.canvas.scrollRequest.connect(self.__scroll_request)
        self.canvas.newShape.connect(self.__new_shape)
        self.canvas.shapeMoved.connect(self.__set_dirty)
        self.canvas.selectionChanged.connect(self.__shape_selection_changed)
        self.canvas.drawingPolygon.connect(self.__toggle_drawing_sensitive)

        self.setCentralWidget(scroll_area)

        self.addDockWidget(Qt.RightDockWidgetArea, self.label_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.quad_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.file_dock)

        shortcuts = self._config['shortcuts']

        self.action_quit = self.__new_action(self.tr('&Quit'), slot=self.close, shortcut=shortcuts['quit'], icon='quit', tip=self.tr('Quit application'))
        self.action_open_image_dir = self.__new_action(self.tr('Open Image Dir'), slot=self.__open_image_dir_dialog, icon='open', tip=self.tr('Open Image Dir'))
        self.action_open_annot_dir = self.__new_action(self.tr('Open Annot Dir'), slot=self.__open_annot_dir_dialog, icon='open', tip=self.tr('Open Image Dir'))
        self.action_open_next = self.__new_action(self.tr('&Next Image'), slot=self.__open_next, shortcut=shortcuts['open_next'], icon='next', tip=self.tr('Open next (hold Ctl+Shift to copy labels)'), enabled=False)
        self.action_open_prev = self.__new_action(self.tr('&Prev Image'), slot=self.__open_prev, shortcut=shortcuts['open_prev'], icon='prev', tip=self.tr('Open prev (hold Ctl+Shift to copy labels)'), enabled=False)
        self.action_save = self.__new_action(self.tr('&Save\n'), slot=self.__save, shortcut=shortcuts['save'], icon='save', tip=self.tr('Save labels to file'), enabled=False)
        self.action_save_auto = self.__new_action(self.tr('Save &Automatically'), slot=lambda x: self.action_save_auto.setChecked(x), icon='save', tip=self.tr('Save automatically'), checkable=True, enabled=True)
        self.action_save_auto.setChecked(self._config['auto_save'])
        self.action_close = self.__new_action(self.tr('&Close'), slot=self.__close_file, shortcut=shortcuts['close'], icon='close', tip=self.tr('Close current file'))
        self.action_create_mode = self.__new_action(self.tr('Create Polygons'), slot=partial(self.__toggle_draw_mode, False), shortcut=shortcuts['create_polygon'], icon='objects', tip=self.tr('Start drawing polygons'), enabled=False)
        self.action_edit_mode = self.__new_action(self.tr('Edit Polygons'), slot=self.__set_edit_mode, shortcut=shortcuts['edit_polygon'], icon='edit', tip=self.tr('Move and edit the selected polygons'), enabled=False)
        self.action_delete = self.__new_action(self.tr('Delete Polygons'), slot=self.__delete_selected_quad, shortcut=shortcuts['delete_polygon'], icon='cancel', tip=self.tr('Delete the selected polygons'), enabled=False)
        self.action_copy = self.__new_action(self.tr('Copy Polygons'), slot=self.__copy_selected_quad, shortcut=shortcuts['copy_polygon'], icon='copy_clipboard', tip=self.tr('Copy selected polygons to clipboard'), enabled=False)
        self.action_paste = self.__new_action(self.tr('Paste Polygons'), slot=self.__paste_selected_shape, shortcut=shortcuts['paste_polygon'], icon='paste', tip=self.tr('Paste copied polygons'), enabled=False)
        self.action_undo_last_point = self.__new_action(self.tr('Undo last point'), slot=self.canvas.undoLastPoint, shortcut=shortcuts['undo_last_point'], icon='undo', tip=self.tr('Undo last drawn point'), enabled=False)
        self.action_undo = self.__new_action(self.tr('Undo\n'), slot=self.__undo_shape_edit, shortcut=shortcuts['undo'], icon='undo', tip=self.tr('Undo last add and edit of shape'), enabled=False)
        self.action_hide_all = self.__new_action(self.tr('&Hide\nPolygons'), slot=partial(self.__toggle_polygons, False), shortcut=shortcuts['hide_all_polygons'], icon='eye', tip=self.tr('Hide all polygons'), enabled=False)
        self.action_show_all = self.__new_action(self.tr('&Show\nPolygons'), slot=partial(self.__toggle_polygons, True), shortcut=shortcuts['show_all_polygons'], icon='eye', tip=self.tr('Show all polygons'), enabled=False)
        self.action_toggle_all = self.__new_action(self.tr('&Toggle\nPolygons'), slot=partial(self.__toggle_polygons, None), shortcut=shortcuts['toggle_all_polygons'], icon='eye', tip=self.tr('Toggle all polygons'), enabled=False)

        self.zoom_widget = ZoomWidget()
        zoom_label = QLabel(self.tr('Zoom'))
        zoom_label.setAlignment(Qt.AlignCenter)
        zoom_box_layout = QVBoxLayout()
        zoom_box_layout.addWidget(zoom_label)
        zoom_box_layout.addWidget(self.zoom_widget)
        self.zoom = QWidgetAction(self)
        self.zoom.setDefaultWidget(QWidget())
        self.zoom.defaultWidget().setLayout(zoom_box_layout)
        self.zoom_widget.setWhatsThis(
            str(self.tr('Zoom in or out of the image. Also accessible with {} and {} from the canvas.'))
            .format(fmtShortcut('{},{}'.format(shortcuts['zoom_in'], shortcuts['zoom_out'])),
                    fmtShortcut(self.tr('Ctrl+Wheel'))))
        self.zoom_widget.setEnabled(False)

        self.action_zoom_in = self.__new_action(self.tr('Zoom &In'), slot=partial(self.__add_zoom, 1.1), shortcut=shortcuts['zoom_in'], icon='zoom-in', tip=self.tr('Increase zoom level'), enabled=False)
        self.action_zoom_out = self.__new_action(self.tr('&Zoom Out'), slot=partial(self.__add_zoom, 0.9), shortcut=shortcuts['zoom_out'], icon='zoom-out', tip=self.tr('Decrease zoom level'), enabled=False)
        self.action_zoom_org = self.__new_action(self.tr('&Original size'), slot=partial(self.__set_zoom, 100), shortcut=shortcuts['zoom_to_original'], icon='zoom', tip=self.tr('Zoom to original size'), enabled=False)
        self.action_keep_prev_scale = self.__new_action(self.tr('&Keep Previous Scale'), slot=self.__enable_keep_prev_scale, tip=self.tr('Keep previous zoom scale'), checkable=True, checked=self._config['keep_prev_scale'], enabled=True)
        self.action_fit_window = self.__new_action(self.tr('&Fit Window'), slot=self.__set_fit_window, shortcut=shortcuts['fit_window'], icon='fit-window', tip=self.tr('Zoom follows window size'), checkable=True, enabled=False)
        self.action_fit_width = self.__new_action(self.tr('Fit &Width'), slot=self.__set_fit_width, shortcut=shortcuts['fit_width'], icon='fit-width', tip=self.tr('Zoom follows window width'), checkable=True, enabled=False)
        self.action_brightness_contrast = self.__new_action(self.tr('&Brightness Contrast'), slot=self.__brightness_contrast, shortcut=None, icon='color', tip=self.tr('Adjust brightness and contrast'), enabled=False)

        self.action_fit_window.setChecked(Qt.Checked)
        self.scalers = {
            ZOOM_MODE_FIT_WINDOW: self.__scale_fit_window,
            ZOOM_MODE_FIT_WIDTH: self.__scale_fit_width,
            ZOOM_MODE_MANUAL_ZOOM: lambda: 1}

        self.action_edit = self.__new_action(self.tr('&Edit Label'), slot=self.__edit_label, shortcut=shortcuts['edit_label'], icon='edit', tip=self.tr('Modify the label of the selected polygon'), enabled=False)
        self.action_fill_drawing = self.__new_action(self.tr('Fill Drawing Polygon'), slot=self.canvas.setFillDrawing, shortcut=None, icon='color', tip=self.tr('Fill polygon while drawing'), checkable=True, enabled=True)
        if self._config['canvas']['fill_drawing']:
            self.action_fill_drawing.trigger()

        label_menu = QMenu()
        addActions(label_menu, (self.action_edit, self.action_delete))
        self.quad_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.quad_list.customContextMenuRequested.connect(self.__pop_label_list_menu)

        self.menu_file = self.menuBar().addMenu(self.tr('&File'))
        self.menu_edit = self.menuBar().addMenu(self.tr('&Edit'))
        self.menu_view = self.menuBar().addMenu(self.tr('&View'))
        self.menu_help = self.menuBar().addMenu(self.tr('&Help'))
        self.menu_recent_files = QMenu(self.tr('Open &Recent'))
        self.menu_label_list = label_menu

        addActions(
            self.menu_file,
            (self.action_open_image_dir,
             self.action_open_annot_dir,
             self.action_open_next,
             self.action_open_prev,
             self.menu_recent_files,
             self.action_save,
             self.action_save_auto,
             self.action_close,
             None,
             self.action_quit))
        addActions(self.menu_help, ())
        addActions(
            self.menu_view,
            (self.label_dock.toggleViewAction(),
             self.quad_dock.toggleViewAction(),
             self.file_dock.toggleViewAction(),
             None,
             self.action_fill_drawing,
             None,
             self.action_hide_all,
             self.action_show_all,
             self.action_toggle_all,
             None,
             self.action_zoom_in,
             self.action_zoom_out,
             self.action_zoom_org,
             self.action_keep_prev_scale,
             None,
             self.action_fit_window,
             self.action_fit_width,
             None,
             self.action_brightness_contrast))
        self.menu_file.aboutToShow.connect(self.updateFileMenu)

        addActions(
            self.canvas.menus[0],
            (self.action_create_mode,
             self.action_edit_mode,
             self.action_edit,
             self.action_copy,
             self.action_paste,
             self.action_delete,
             self.action_undo,
             self.action_undo_last_point))
        self.canvas.menus[1] = self.__copy_quad
        addActions(
            self.menu_edit,
            (self.action_create_mode,
             self.action_edit_mode,
             self.action_edit,
             self.action_copy,
             self.action_paste,
             self.action_delete,
             None,
             self.action_undo,
             self.action_undo_last_point,
             None,
             None))

        self.tools = ToolBar('Tools')
        self.tools.setObjectName('ToolBar')
        self.tools.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.addToolBar(Qt.TopToolBarArea, self.tools)

        addActions(
            self.tools,
            (self.action_open_image_dir,
             self.action_open_annot_dir,
             self.action_open_prev,
             self.action_open_next,
             self.action_save,
             None,
             self.action_create_mode,
             self.action_edit_mode,
             self.action_delete,
             self.action_undo,
             self.action_brightness_contrast,
             None,
             self.action_fit_window,
             self.zoom,
             None))
        self.actions_on_shapes_present = (
            self.action_hide_all,
            self.action_show_all,
            self.action_toggle_all)

        self.statusBar().showMessage(str(self.tr('%s started.')) % __appname__)
        self.statusBar().show()

        if config['file_search']:
            self.file_search.setText(config['file_search'])
            self.__file_search_changed()

        self.settings = QSettings('labelQuad', 'labelQuad')
        self.recent_files = self.settings.value('recent_files', []) or []
        size = self.settings.value('window/size', QSize(600, 500))
        position = self.settings.value('window/position', QPoint(0, 0))
        state = self.settings.value('window/state', QByteArray())
        self.resize(size)
        self.move(position)
        self.restoreState(state)

        self.updateFileMenu()

        self.zoom_widget.valueChanged.connect(self.__paint_canvas)

    def closeEvent(self, event):
        if not self.__may_continue():
            event.ignore()
        self.settings.setValue('filename', self.image_path if self.image_path else '')
        self.settings.setValue('window/size', self.size())
        self.settings.setValue('window/position', self.pos())
        self.settings.setValue('window/state', self.saveState())
        self.settings.setValue('recent_files', self.recent_files)

    def resizeEvent(self, event):
        if (self.canvas) and \
           (not self.image.isNull()) and \
           (self.zoom_mode != ZOOM_MODE_MANUAL_ZOOM):
            self.__adjust_scale()
        super(MainWindow, self).resizeEvent(event)

    def __load(self) -> None:
        image_path_prev = self.image_path
        image_path = self.__current_image_path()
        annot_path = self.__current_annot_path()
        if image_path is None:
            return
        self.__reset_state()
        self.canvas.setEnabled(False)
        if not QFile.exists(image_path):
            self.__error_message(
                self.tr(f'Error opening file'),
                self.tr(f'No such file: <b>{image_path}</b>'))
        self.__status(self.tr(f'Loading {image_path}...'))
        image_data = LabelFile.load_image_file(image_path)
        image = QImage.fromData(image_data)
        if image.isNull():
            self.__error_message(
                self.tr('Error opening file'),
                self.tr(f'<p>Make sure <i>{image_path}</i> is a valid image file.<br/>'))
            self.__status(self.tr('Error reading %s') % image_path)
        self.image = image
        self.image_path = image_path
        self.image_data = image_data
        self.canvas.loadPixmap(QPixmap.fromImage(self.image))

        if (annot_path is not None) and osp.exists(annot_path):
            with open(annot_path, 'r') as f:
                j = json.load(f)
            quads = []
            for shape in j['shapes']:
                label = shape['label']
                quad = Shape(label=label, shape_type='polygon')
                quad.addPoint(QPointF(shape['p1x'], shape['p1y']))
                quad.addPoint(QPointF(shape['p2x'], shape['p2y']))
                quad.addPoint(QPointF(shape['p3x'], shape['p3y']))
                quad.addPoint(QPointF(shape['p4x'], shape['p4y']))
                quad.close()
                quads.append(quad)
            self.__load_quads(quads)

        self.__set_clean()
        self.canvas.setEnabled(True)
        is_initial_load = not self.zoom_values
        if self.image_path in self.zoom_values:
            self.zoom_mode = self.zoom_values[self.image_path][0]
            self.__set_zoom(self.zoom_values[self.image_path][1])
        elif is_initial_load or not self._config['keep_prev_scale']:
            self.__adjust_scale(initial=True)
        for orientation in self.scroll_values:
            if self.image_path in self.scroll_values[orientation]:
                self.__set_scroll(orientation, self.scroll_values[orientation][self.image_path])
        dialog = BrightnessContrastDialog(
            img_data_to_pil(self.image_data),
            self.__on_new_brightness_contrast,
            parent=self)
        brightness, contrast = self.brightness_contrast_values.get(self.image_path, (None, None))
        if self._config['keep_prev_brightness'] and (image_path_prev is not None):
            brightness, _ = self.brightness_contrast_values.get(image_path_prev, (None, None))
        if self._config['keep_prev_contrast'] and self.recent_files:
            _, contrast = self.brightness_contrast_values.get(self.recent_files[0], (None, None))
        if brightness is not None:
            dialog.slider_brightness.setValue(brightness)
        if contrast is not None:
            dialog.slider_contrast.setValue(contrast)
        self.brightness_contrast_values[self.image_path] = (brightness, contrast)
        if brightness is not None or contrast is not None:
            dialog.onNewValue(None)

        self.__paint_canvas()
        self.__add_recent_file(self.image_path)
        self.__toggle_actions(True)
        self.canvas.setFocus()
        self.__status(self.tr(f'Loaded {image_path}'))

    def __save(self) -> None:
        if self.image_path is None:
            return
        image_path = self.image_path
        annot_path = osp.splitext(osp.basename(image_path))[0] + '.json'
        annot_path = osp.join(self.annot_dir, annot_path)
        def format_shape(s: Shape) -> dict:
            pts = s.points
            return dict(
                label=s.label,
                p1x=round(pts[0].x(), 2), p1y=round(pts[0].y(), 2),
                p2x=round(pts[1].x(), 2), p2y=round(pts[1].y(), 2),
                p3x=round(pts[2].x(), 2), p3y=round(pts[2].y(), 2),
                p4x=round(pts[3].x(), 2), p4y=round(pts[3].y(), 2))
        try:
            image_path = osp.relpath(annot_path, osp.dirname(image_path))
            if osp.dirname(annot_path) and not osp.exists(osp.dirname(annot_path)):
                os.makedirs(osp.dirname(annot_path))
            lf = LabelFile()
            lf.save(
                filename=annot_path,
                shapes=[format_shape(item.shape()) for item in self.quad_list],
                imagePath=image_path,
                imageHeight=self.image.height(),
                imageWidth=self.image.width())
            items = self.file_list.findItems(image_path, Qt.MatchExactly)
            if len(items) == 1:
                items[0].setCheckState(Qt.Checked)
            self.__set_clean()
        except LabelFileError as e:
            self.__error_message(self.tr('Error saving label data'), self.tr(f'<b>{e}</b>'))

    def __set_dirty(self) -> None:
        self.action_undo.setEnabled(self.canvas.isShapeRestorable)
        if self.action_save_auto.isChecked():
            self.__save()
            return
        self.dirty = True
        self.action_save.setEnabled(True)
        title = __appname__
        file = self.__current_image_path()
        if file is not None:
            title = f'{title} - {file}*'
        self.setWindowTitle(title)

    def __set_clean(self) -> None:
        self.dirty = False
        self.action_save.setEnabled(False)
        self.action_create_mode.setEnabled(True)
        title = __appname__
        file = self.__current_image_path()
        if file is not None:
            title = f'{title} - {file}'
        self.setWindowTitle(title)

    def __toggle_actions(self, value: bool = True) -> None:
        self.zoom_widget.setEnabled(value)
        self.action_zoom_in.setEnabled(value)
        self.action_zoom_out.setEnabled(value)
        self.action_zoom_org.setEnabled(value)
        self.action_fit_window.setEnabled(value)
        self.action_fit_width.setEnabled(value)
        self.action_close.setEnabled(value)
        self.action_create_mode.setEnabled(value)
        self.action_edit_mode.setEnabled(value)
        self.action_brightness_contrast.setEnabled(value)

    def __status(self, message: str, delay: int = 5000) -> None:
        self.statusBar().showMessage(message, delay)

    def __reset_state(self) -> None:
        self.quad_list.clear()
        self.image_path = None
        self.image_data = None
        self.canvas.resetState()

    def __add_recent_file(self, image_path: str) -> None:
        if image_path in self.recent_files:
            self.recent_files.remove(image_path)
        elif MAX_RECENT_FILES <= len(self.recent_files):
            self.recent_files.pop()
        self.recent_files.insert(0, image_path)

    def __undo_shape_edit(self) -> None:
        self.canvas.restoreShape()
        self.quad_list.clear()
        self.__load_quads(self.canvas.shapes)
        self.action_undo.setEnabled(self.canvas.isShapeRestorable)

    def __toggle_drawing_sensitive(self, drawing: bool = True) -> None:
        self.action_edit_mode.setEnabled(not drawing)
        self.action_undo_last_point.setEnabled(drawing)
        self.action_undo.setEnabled(not drawing)
        self.action_delete.setEnabled(not drawing)

    def __toggle_draw_mode(self, edit: bool = True) -> None:
        self.canvas.setEditing(edit)
        self.canvas.createMode = 'polygon'
        self.action_create_mode.setEnabled(edit)
        self.action_edit_mode.setEnabled(not edit)

    def __set_edit_mode(self):
        self.__toggle_draw_mode(True)

    def updateFileMenu(self):
        current = None
        if 0 <= self.file_list.currentRow():
            current = self.file_list.currentItem().text()

        def exists(filename):
            return osp.exists(str(filename))

        menu = self.menu_recent_files
        menu.clear()
        files = [x for x in self.recent_files if x != current and exists(x)]
        for i, f in enumerate(files):
            icon = newIcon('labels')
            action = QAction(icon, '&%d %s' % (i + 1, QFileInfo(f).fileName()), self)
            action.triggered.connect(partial(self.__load_recent, f))
            menu.addAction(action)

    def __pop_label_list_menu(self, point):
        self.menu_label_list.exec_(self.quad_list.mapToGlobal(point))

    def __edit_label(self) -> None:
        if not self.canvas.editing():
            return
        items = self.quad_list.selectedItems()
        if len(items) <= 0:
            logger.warning('No label is selected, so cannot edit label.')
            return
        item = items[0]
        quad: Shape = items[0].shape()
        text, _, _, _ = self.label_dialog.popUp(text=quad.label)
        if text is None:
            return
        self.canvas.storeShapes()
        quad.label = text
        self._update_shape_color(quad)
        item.setText('{} <font color="#{:02x}{:02x}{:02x}">●</font>'.format(
            html.escape(quad.label), *quad.fill_color.getRgb()[:3]))
        self.__set_dirty()
        if self.label_list.findItemByLabel(quad.label) is None:
            item = self.label_list.createItemFromLabel(quad.label)
            self.label_list.addItem(item)
            rgb = self.__get_rgb_by_label(quad.label)
            self.label_list.setItemLabel(item, quad.label, rgb)

    def __file_search_changed(self) -> None:
        self.__import_dir_images(
            self.image_dir,
            pattern=self.file_search.text(),
            load=False)

    def __file_selection_changed(self) -> None:
        if not self.__may_continue():
            return
        self.__load()

    def __shape_selection_changed(self, selected_shapes):
        self._noSelectionSlot = True
        for shape in self.canvas.selectedShapes:
            shape.selected = False
        self.quad_list.clearSelection()
        self.canvas.selectedShapes = selected_shapes
        for shape in self.canvas.selectedShapes:
            shape.selected = True
            item = self.quad_list.findItemByShape(shape)
            self.quad_list.selectItem(item)
            self.quad_list.scrollToItem(item)
        self._noSelectionSlot = False
        n_selected = len(selected_shapes)
        self.action_delete.setEnabled(n_selected)
        self.action_copy.setEnabled(n_selected)
        self.action_edit.setEnabled(n_selected)

    def __add_quad(self, quad: Shape) -> None:
        text = quad.label
        label_list_item = LabelListWidgetItem(text, quad)
        self.quad_list.addItem(label_list_item)
        if self.label_list.findItemByLabel(quad.label) is None:
            item = self.label_list.createItemFromLabel(quad.label)
            self.label_list.addItem(item)
            rgb = self.__get_rgb_by_label(quad.label)
            self.label_list.setItemLabel(item, quad.label, rgb)
        self.label_dialog.addLabelHistory(quad.label)
        for action in self.actions_on_shapes_present:
            action.setEnabled(True)
        self._update_shape_color(quad)
        label_list_item.setText(
            '{} <font color="#{:02x}{:02x}{:02x}">●</font>'.format(
                html.escape(text), *quad.fill_color.getRgb()[:3]))

    def _update_shape_color(self, shape):
        r, g, b = self.__get_rgb_by_label(shape.label)
        shape.line_color = QColor(r, g, b)
        shape.vertex_fill_color = QColor(r, g, b)
        shape.hvertex_fill_color = QColor(255, 255, 255)
        shape.fill_color = QColor(r, g, b, 128)
        shape.select_line_color = QColor(255, 255, 255)
        shape.select_fill_color = QColor(r, g, b, 155)

    def __get_rgb_by_label(self, label: str) -> tuple[int, int, int]:
        item = self.label_list.findItemByLabel(label)
        label_id = self.label_list.indexFromItem(item).row() + 1
        label_id += self._config['shift_auto_shape_color']
        return tuple(LABEL_COLORMAP[label_id % len(LABEL_COLORMAP)].tolist())

    def __load_quads(self, quads: list[Shape], replace: bool = True) -> None:
        self._noSelectionSlot = True
        for quad in quads:
            self.__add_quad(quad)
        self.quad_list.clearSelection()
        self._noSelectionSlot = False
        self.canvas.loadShapes(quads, replace=replace)

    def __remove_quads(self, quads: list[Shape]) -> None:
        for quad in quads:
            item = self.quad_list.findItemByShape(quad)
            self.quad_list.removeItem(item)

    def __delete_selected_quad(self) -> None:
        self.__remove_quads(self.canvas.deleteSelected())
        self.__set_dirty()
        if 0 <= len(self.quad_list):
            for action in self.actions_on_shapes_present:
                action.setEnabled(False)

    def __copy_selected_quad(self) -> None:
        self._copied_shapes = [s.copy() for s in self.canvas.selectedShapes]
        self.action_paste.setEnabled(len(self._copied_shapes) > 0)

    def __paste_selected_shape(self) -> None:
        self.__load_quads(self._copied_shapes, replace=False)
        self.__set_dirty()

    def __label_selection_changed(self) -> None:
        if self._noSelectionSlot:
            return
        if self.canvas.editing():
            selected_shapes = []
            for item in self.quad_list.selectedItems():
                selected_shapes.append(item.shape())
            if selected_shapes:
                self.canvas.selectShapes(selected_shapes)
            else:
                self.canvas.deSelectShape()

    def __label_item_changed(self, item) -> None:
        shape = item.shape()
        self.canvas.setShapeVisible(shape, item.checkState() == Qt.Checked)

    def __label_order_changed(self) -> None:
        self.__set_dirty()
        self.canvas.loadShapes([item.shape() for item in self.quad_list])

    def __new_shape(self) -> None:
        items = self.label_list.selectedItems()
        text = None
        if items:
            text = items[0].data(Qt.UserRole)
        if self._config['display_label_popup'] or not text:
            previous_text = self.label_dialog.edit.text()
            text, _, _, _ = self.label_dialog.popUp(text)
            if not text:
                self.label_dialog.edit.setText(previous_text)
        if text:
            self.quad_list.clearSelection()
            shape = self.canvas.setLastLabel(text, None)
            self.__add_quad(shape)
            self.action_edit_mode.setEnabled(True)
            self.action_undo_last_point.setEnabled(False)
            self.action_undo.setEnabled(True)
            self.__set_dirty()
        else:
            self.canvas.undoLastLine()
            self.canvas.shapesBackups.pop()

    def __scroll_request(self, delta, orientation) -> None:
        units = -delta * 0.1
        bar = self.scroll_bars[orientation]
        value = bar.value() + bar.singleStep() * units
        self.__set_scroll(orientation, value)

    def __set_scroll(self, orientation, value) -> None:
        self.scroll_bars[orientation].setValue(int(value))
        self.scroll_values[orientation][self.image_path] = value

    def __set_zoom(self, value) -> None:
        self.action_fit_width.setChecked(False)
        self.action_fit_window.setChecked(False)
        self.zoom_mode = ZOOM_MODE_MANUAL_ZOOM
        self.zoom_widget.setValue(value)
        self.zoom_values[self.image_path] = (self.zoom_mode, value)

    def __add_zoom(self, increment: float = 1.1) -> None:
        zoom_value = self.zoom_widget.value() * increment
        if increment > 1:
            zoom_value = math.ceil(zoom_value)
        else:
            zoom_value = math.floor(zoom_value)
        self.__set_zoom(zoom_value)

    def __zoom_request(self, delta, pos) -> None:
        canvas_width_old = self.canvas.width()
        units = 1.1
        if delta < 0:
            units = 0.9
        self.__add_zoom(units)
        canvas_width_new = self.canvas.width()
        if canvas_width_old != canvas_width_new:
            canvas_scale_factor = canvas_width_new / canvas_width_old
            x_shift = round(pos.x() * canvas_scale_factor) - pos.x()
            y_shift = round(pos.y() * canvas_scale_factor) - pos.y()
            self.__set_scroll(Qt.Horizontal, self.scroll_bars[Qt.Horizontal].value() + x_shift)
            self.__set_scroll(Qt.Vertical, self.scroll_bars[Qt.Vertical].value() + y_shift)

    def __set_fit_window(self) -> None:
        self.action_fit_width.setChecked(False)
        self.zoom_mode = ZOOM_MODE_FIT_WINDOW
        self.__adjust_scale()

    def __set_fit_width(self) -> None:
        self.action_fit_window.setChecked(False)
        self.zoom_mode = ZOOM_MODE_FIT_WIDTH
        self.__adjust_scale()

    def __enable_keep_prev_scale(self, enabled) -> None:
        self._config['keep_prev_scale'] = enabled
        self.action_keep_prev_scale.setChecked(enabled)

    def __on_new_brightness_contrast(self, qimage) -> None:
        self.canvas.loadPixmap(QPixmap.fromImage(qimage), clear_shapes=False)

    def __brightness_contrast(self, value) -> None:
        dialog = BrightnessContrastDialog(
            img_data_to_pil(self.image_data),
            self.__on_new_brightness_contrast,
            parent=self)
        brightness, contrast = self.brightness_contrast_values.get(self.image_path, (None, None))
        if brightness is not None:
            dialog.slider_brightness.setValue(brightness)
        if contrast is not None:
            dialog.slider_contrast.setValue(contrast)
        dialog.exec_()
        brightness = dialog.slider_brightness.value()
        contrast = dialog.slider_contrast.value()
        self.brightness_contrast_values[self.image_path] = (brightness, contrast)

    def __toggle_polygons(self, value) -> None:
        flag = value
        for item in self.quad_list:
            if value is None:
                flag = item.checkState() == Qt.Unchecked
            item.setCheckState(Qt.Checked if flag else Qt.Unchecked)

    def __paint_canvas(self) -> None:
        assert not self.image.isNull(), 'cannot paint null image'
        self.canvas.scale = 0.01 * self.zoom_widget.value()
        self.canvas.adjustSize()
        self.canvas.update()

    def __adjust_scale(self, initial: bool = False) -> None:
        value = self.scalers[ZOOM_MODE_FIT_WINDOW if initial else self.zoom_mode]()
        value = int(100 * value)
        self.zoom_widget.setValue(value)
        self.zoom_values[self.image_path] = (self.zoom_mode, value)

    def __scale_fit_window(self):
        e = 2.0
        w1 = self.centralWidget().width() - e
        h1 = self.centralWidget().height() - e
        a1 = w1 / h1
        w2 = self.canvas.pixmap.width() - 0.0
        h2 = self.canvas.pixmap.height() - 0.0
        a2 = w2 / h2
        return w1 / w2 if a2 >= a1 else h1 / h2

    def __scale_fit_width(self):
        w = self.centralWidget().width() - 2.0
        return w / self.canvas.pixmap.width()

    def __load_recent(self) -> None:
        if self.__may_continue():
            self.__load()

    def __open_next(self) -> None:
        if not self.__may_continue():
            return
        if self.file_list.count() <= 0:
            return
        size = self.file_list.count()
        row = self.file_list.currentRow()
        if row == -1:
            row = 0
        else:
            if row + 1 < size:
                row = row + 1
        self.file_list.setCurrentRow(row)
        self.__load()

    def __open_prev(self) -> None:
        if not self.__may_continue():
            return
        row = self.file_list.currentRow()
        if row < 1:
            return
        self.file_list.setCurrentRow(row - 1)
        self.__load()

    def __close_file(self) -> None:
        if not self.__may_continue():
            return
        self.__reset_state()
        self.__set_clean()
        self.__toggle_actions(False)
        self.canvas.setEnabled(False)

    def __may_continue(self) -> None:
        if not self.dirty:
            return True
        msg = self.tr('Save annotations to "{}" before closing?').format(self.image_path)
        answer = QMB.question(self, self.tr('Save annotations?'), msg, QMB.Save | QMB.Discard | QMB.Cancel, QMB.Save)
        if answer == QMB.Discard:
            return True
        elif answer == QMB.Save:
            self.__save()
            return True
        else:
            return False

    def __error_message(self, title: str, message: str) -> None:
        return QMessageBox.critical(self, title, f'<p><b>{title}</b></p>{message}')

    def __copy_quad(self) -> None:
        self.canvas.endMove(copy=True)
        for quad in self.canvas.selectedShapes:
            self.__add_quad(quad)
        self.quad_list.clearSelection()
        self.__set_dirty()

    def __open_image_dir_dialog(self) -> None:
        if not self.__may_continue():
            return
        dir_path = '.'
        if self.image_dir and osp.exists(self.image_dir):
            dir_path = self.image_dir
        dir_path = str(QFileDialog.getExistingDirectory(
            self, self.tr(f'{__appname__} - Open Image Directory'), dir_path,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks))
        self.__import_dir_images(dir_path)

    def __open_annot_dir_dialog(self) -> None:
        if not self.__may_continue():
            return
        dir_path = '.'
        if self.image_dir and osp.exists(self.image_dir):
            dir_path = self.image_dir
        self.annot_dir = str(QFileDialog.getExistingDirectory(
            self, self.tr(f'{__appname__} - Open Annot Directory'), dir_path,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks))
        self.__load()
        self.__refresh_file_check_state()

    def __import_dir_images(self, dirpath: str, pattern: Optional[str] = None) -> None:
        self.action_open_next.setEnabled(True)
        self.action_open_prev.setEnabled(True)
        if not self.__may_continue() or not dirpath:
            return
        self.image_dir = dirpath
        self.image_path = None
        image_paths = self.__scan_all_images(dirpath)
        if pattern:
            try:
                image_paths = [x for x in image_paths if re.search(pattern, x)]
            except re.error:
                pass
        qt_disconnect_signal_safely(
            self.file_list.itemSelectionChanged,
            self.__file_selection_changed)
        self.file_list.clear()
        for image_path in image_paths:
            item = QListWidgetItem(osp.basename(image_path))
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.file_list.addItem(item)
        qt_connect_signal_safely(
            self.file_list.itemSelectionChanged,
            self.__file_selection_changed)
        self.__refresh_file_check_state()
        self.__open_next()

    def __refresh_file_check_state(self) -> None:
        if (self.image_dir is None) or \
           (self.annot_dir is None):
            return
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            image_path = osp.join(self.image_dir, item.text())
            annot_path = osp.join(self.annot_dir, osp.splitext(osp.basename(image_path))[0] + '.json')
            if QFile.exists(annot_path) and LabelFile.is_label_file(annot_path):
                item.setCheckState(Qt.Checked)
            else:
                item.setCheckState(Qt.Unchecked)

    def __current_image_path(self) -> Optional[str]:
        if (self.file_list.currentRow() < 0) or \
           (self.image_dir is None):
            return None
        filename = self.file_list.currentItem().text()
        return osp.join(self.image_dir, filename)

    def __current_annot_path(self) -> Optional[str]:
        if (self.file_list.currentRow() < 0) or \
           (self.annot_dir is None):
            return None
        filename = self.file_list.currentItem().text()
        filename = osp.splitext(filename)[0] + '.json'
        return osp.join(self.annot_dir, filename)

    def __scan_all_images(self, dir_path: str) -> list[str]:
        extensions = ['.jpg', '.jpeg', '.png', '.JPG', '.JPEG', '.PNG']
        files = list_files_with_exts(dir_path, extensions)
        files = [osp.basename(x) for x in files]
        return natsort.os_sorted(files)

    def __new_action(
            self,
            text,
            slot=None,
            shortcut=None,
            icon: Optional[str] = None,
            tip: Optional[str] = None,
            checkable: bool = False,
            enabled: bool = True,
            checked: bool = False,
            ) -> QAction:
        a = QAction(text, self)
        if icon is not None:
            a.setIconText(text.replace(' ', '\n'))
            a.setIcon(self.__new_icon(icon))
        if shortcut is not None:
            if isinstance(shortcut, (list, tuple)):
                a.setShortcuts(shortcut)
            else:
                a.setShortcut(shortcut)
        if tip is not None:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        if slot is not None:
            a.triggered.connect(slot)
        if checkable:
            a.setCheckable(True)
        a.setEnabled(enabled)
        a.setChecked(checked)
        return a

    def __new_icon(self, icon: str) -> QIcon:
        path = osp.join('icon', icon)
        if hasattr(sys, '_MEIPASS'):
            path = osp.join(sys._MEIPASS, path)
        return QIcon(QPixmap(path))


def update_dict(target_dict, new_dict, validate_item=None):
    for key, value in new_dict.items():
        if validate_item:
            validate_item(key, value)
        if key not in target_dict:
            logger.warning("Skipping unexpected key in config: {}".format(key))
            continue
        if isinstance(target_dict[key], dict) and isinstance(value, dict):
            update_dict(target_dict[key], value, validate_item=validate_item)
        else:
            target_dict[key] = value


def get_default_config():
    config_file = 'default_config.yaml'
    if not osp.exists(config_file):
        with open(config_file, 'w') as f:
            f.writelines('\n'.join([
                "auto_save: false",
                "display_label_popup: true",
                "store_data: true",
                "keep_prev: false",
                "keep_prev_scale: false",
                "keep_prev_brightness: false",
                "keep_prev_contrast: false",
                "logger_level: info",
                "",
                "flags: null",
                "label_flags: null",
                "labels: null",
                "file_search: null",
                "sort_labels: true",
                "validate_label: null",
                "",
                "default_shape_color: [0, 255, 0]",
                "shape_color: auto  # null, 'auto', 'manual'",
                "shift_auto_shape_color: 0",
                "label_colors: null",
                "",
                "shape:",
                "  # drawing",
                "  line_color: [0, 255, 0, 128]",
                "  fill_color: [0, 0, 0, 64]",
                "  vertex_fill_color: [0, 255, 0, 255]",
                "  # selecting / hovering",
                "  select_line_color: [255, 255, 255, 255]",
                "  select_fill_color: [0, 255, 0, 64]",
                "  hvertex_fill_color: [255, 255, 255, 255]",
                "  point_size: 8",
                "",
                "ai:",
                "  default: 'EfficientSam (accuracy)'",
                "",
                "# main",
                "flag_dock:",
                "  show: true",
                "  closable: true",
                "  movable: true",
                "  floatable: true",
                "label_dock:",
                "  show: true",
                "  closable: true",
                "  movable: true",
                "  floatable: true",
                "shape_dock:",
                "  show: true",
                "  closable: true",
                "  movable: true",
                "  floatable: true",
                "file_dock:",
                "  show: true",
                "  closable: true",
                "  movable: true",
                "  floatable: true",
                "",
                "# label_dialog",
                "show_label_text_field: true",
                "label_completion: startswith",
                "fit_to_content:",
                "  column: true",
                "  row: false",
                "",
                "# canvas",
                "epsilon: 10.0",
                "canvas:",
                "  fill_drawing: true",
                "  # None: do nothing",
                "  # close: close polygon",
                "  double_click: close",
                "  # The max number of edits we can undo",
                "  num_backups: 10",
                "  # show crosshair",
                "  crosshair:",
                "    polygon: false",
                "    circle: false",
                "    line: false",
                "    point: false",
                "    linestrip: false",
                "    ai_mask: false",
                "",
                "shortcuts:",
                "  close: Ctrl+W",
                "  open: Ctrl+O",
                "  open_dir: Ctrl+U",
                "  quit: Ctrl+Q",
                "  save: Ctrl+S",
                "  save_as: Ctrl+Shift+S",
                "  save_to: null",
                "  delete_file: Ctrl+Delete",
                "",
                "  open_next: [D, Ctrl+Shift+D]",
                "  open_prev: [A, Ctrl+Shift+A]",
                "",
                "  zoom_in: [Ctrl++, Ctrl+=]",
                "  zoom_out: Ctrl+-",
                "  zoom_to_original: Ctrl+0",
                "  fit_window: Ctrl+F",
                "  fit_width: Ctrl+Shift+F",
                "",
                "  create_polygon: Ctrl+N",
                "  create_circle: null",
                "  create_line: null",
                "  create_point: null",
                "  create_linestrip: null",
                "  edit_polygon: Ctrl+J",
                "  delete_polygon: Delete",
                "  duplicate_polygon: Ctrl+D",
                "  copy_polygon: Ctrl+C",
                "  paste_polygon: Ctrl+V",
                "  undo: Ctrl+Z",
                "  undo_last_point: Ctrl+Z",
                "  add_point_to_edge: Ctrl+Shift+P",
                "  edit_label: Ctrl+E",
                "  toggle_keep_prev_mode: Ctrl+P",
                "  remove_selected_point: [Meta+H, Backspace]",
                "",
                "  show_all_polygons: null",
                "  hide_all_polygons: null",
                "  toggle_all_polygons: T\n",
            ]))
    with open(config_file) as f:
        config = yaml.safe_load(f)

    # save default config to ~/.labelQuadrc
    user_config_file = osp.join(osp.expanduser("~"), ".labelQuadrc")
    if not osp.exists(user_config_file):
        try:
            shutil.copy(config_file, user_config_file)
        except Exception:
            logger.warning("Failed to save config: {}".format(user_config_file))

    return config


def validate_config_item(key, value):
    if key == "validate_label" and value not in [None, "exact"]:
        raise ValueError(
            "Unexpected value for config key 'validate_label': {}".format(value)
        )
    if key == "shape_color" and value not in [None, "auto", "manual"]:
        raise ValueError(
            "Unexpected value for config key 'shape_color': {}".format(value)
        )
    if key == "labels" and value is not None and len(value) != len(set(value)):
        raise ValueError(
            "Duplicates are detected for config key 'labels': {}".format(value)
        )


def get_config(config_file_or_yaml=None, config_from_args=None):
    # 1. default config
    config = get_default_config()

    # 2. specified as file or yaml
    if config_file_or_yaml is not None:
        config_from_yaml = yaml.safe_load(config_file_or_yaml)
        if not isinstance(config_from_yaml, dict):
            with open(config_from_yaml) as f:
                logger.info("Loading config file from: {}".format(config_from_yaml))
                config_from_yaml = yaml.safe_load(f)
        update_dict(config, config_from_yaml, validate_item=validate_config_item)

    # 3. command line argument or specified config file
    if config_from_args is not None:
        update_dict(config, config_from_args, validate_item=validate_config_item)

    return config


def img_data_to_pil(img_data):
    f = io.BytesIO()
    f.write(img_data)
    img_pil = PIL.Image.open(f)
    return img_pil


def img_data_to_arr(img_data):
    img_pil = img_data_to_pil(img_data)
    img_arr = np.array(img_pil)
    return img_arr


def img_pil_to_data(img_pil):
    f = io.BytesIO()
    img_pil.save(f, format="PNG")
    img_data = f.getvalue()
    return img_data


def img_b64_to_arr(img_b64):
    img_data = base64.b64decode(img_b64)
    img_arr = img_data_to_arr(img_data)
    return img_arr


def img_arr_to_data(img_arr):
    img_pil = PIL.Image.fromarray(img_arr)
    img_data = img_pil_to_data(img_pil)
    return img_data


def newIcon(icon):
    path = osp.join('icon', icon)
    if hasattr(sys, '_MEIPASS'):
        path = osp.join(sys._MEIPASS, path)
    return QIcon(QPixmap(path))


def addActions(widget, actions):
    for action in actions:
        if action is None:
            widget.addSeparator()
        elif isinstance(action, QMenu):
            widget.addMenu(action)
        else:
            widget.addAction(action)


def fmtShortcut(text):
    mod, key = text.split("+", 1)
    return "<b>%s</b>+<b>%s</b>" % (mod, key)


def labelValidator():
    return QRegExpValidator(QRegExp(r"^[^ \t].+"), None)


def apply_exif_orientation(image):
    try:
        exif = image._getexif()
    except AttributeError:
        exif = None

    if exif is None:
        return image

    exif = {PIL.ExifTags.TAGS[k]: v for k, v in exif.items() if k in PIL.ExifTags.TAGS}

    orientation = exif.get("Orientation", None)

    if orientation == 1:
        # do nothing
        return image
    elif orientation == 2:
        # left-to-right mirror
        return PIL.ImageOps.mirror(image)
    elif orientation == 3:
        # rotate 180
        return image.transpose(PIL.Image.ROTATE_180)
    elif orientation == 4:
        # top-to-bottom mirror
        return PIL.ImageOps.flip(image)
    elif orientation == 5:
        # top-to-left mirror
        return PIL.ImageOps.mirror(image.transpose(PIL.Image.ROTATE_270))
    elif orientation == 6:
        # rotate 270
        return image.transpose(PIL.Image.ROTATE_270)
    elif orientation == 7:
        # top-to-right mirror
        return PIL.ImageOps.mirror(image.transpose(PIL.Image.ROTATE_90))
    elif orientation == 8:
        # rotate 90
        return image.transpose(PIL.Image.ROTATE_90)
    else:
        return image


def img_qt_to_arr(img_qt):
    w, h, d = img_qt.size().width(), img_qt.size().height(), img_qt.depth()
    bytes_ = img_qt.bits().asstring(w * h * d // 8)
    img_arr = np.frombuffer(bytes_, dtype=np.uint8).reshape((h, w, d // 8))
    return img_arr


def distance(p):
    return math.sqrt(p.x() * p.x() + p.y() * p.y())


def distancetoline(point, line):
    p1, p2 = line
    p1 = np.array([p1.x(), p1.y()])
    p2 = np.array([p2.x(), p2.y()])
    p3 = np.array([point.x(), point.y()])
    if np.dot((p3 - p1), (p2 - p1)) < 0:
        return np.linalg.norm(p3 - p1)
    if np.dot((p3 - p2), (p1 - p2)) < 0:
        return np.linalg.norm(p3 - p2)
    if np.linalg.norm(p2 - p1) == 0:
        return np.linalg.norm(p3 - p1)
    return np.linalg.norm(np.cross(p2 - p1, p1 - p3)) / np.linalg.norm(p2 - p1)


def list_files_with_exts(path: str, ext: str | list[str], recursive: bool = False) -> list[str]:
    if recursive:
        wildcard = osp.join('**', '*')
    else:
        wildcard = '*'
    if type(ext) is str:
        return glob(osp.join(path, f'{wildcard}{ext}'), recursive=recursive)
    paths = []
    for _ext in ext:
        paths += glob(osp.join(path, f'{wildcard}{_ext}'), recursive=recursive)
    return paths


def qt_connect_signal_safely(signal, handler):
    signal.connect(handler)


def qt_disconnect_signal_safely(signal, handler=None):
    try:
        if handler is not None:
            while True:
                signal.disconnect(handler)
        else:
            signal.disconnect()
    except TypeError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', '-V', action='store_true', help='show version')
    parser.add_argument('--reset-config', action='store_true', help='reset qt config')
    parser.add_argument('--output', '-O', '-o')
    default_config_file = os.path.join(os.path.expanduser('~'), '.labelQuadrc')
    parser.add_argument('--config', dest='config', default=default_config_file)
    parser.add_argument(
        '--flags',
        help='comma separated list of flags OR file containing flags',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--labels',
        help='comma separated list of labels OR file containing labels',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--validatelabel',
        dest='validate_label',
        choices=['exact'],
        help='label validation types',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--keep-prev',
        action='store_true',
        help='keep annotation of previous frame',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--epsilon',
        type=float,
        help='epsilon to find nearest vertex on canvas',
        default=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if args.version:
        print('{0} {1}'.format(__appname__, __version__))
        sys.exit(0)

    if hasattr(args, 'flags'):
        if os.path.isfile(args.flags):
            with codecs.open(args.flags, 'r', encoding='utf-8') as f:
                args.flags = [line.strip() for line in f if line.strip()]
        else:
            args.flags = [line for line in args.flags.split(',') if line]

    if hasattr(args, 'labels'):
        if os.path.isfile(args.labels):
            with codecs.open(args.labels, 'r', encoding='utf-8') as f:
                args.labels = [line.strip() for line in f if line.strip()]
        else:
            args.labels = [line for line in args.labels.split(',') if line]

    if hasattr(args, 'label_flags'):
        if os.path.isfile(args.label_flags):
            with codecs.open(args.label_flags, 'r', encoding='utf-8') as f:
                args.label_flags = yaml.safe_load(f)
        else:
            args.label_flags = yaml.safe_load(args.label_flags)

    config_from_args = args.__dict__
    config_from_args.pop('version')
    reset_config = config_from_args.pop('reset_config')
    output = config_from_args.pop('output')
    config_file_or_yaml = config_from_args.pop('config')
    config = get_config(config_file_or_yaml, config_from_args)

    if not config['labels'] and config['validate_label']:
        logger.error(
            '--labels must be specified with --validatelabel or '
            'validate_label: true in the config file '
            '(ex. ~/.labelQuadrc).'
        )
        sys.exit(1)

    output_file = None
    output_dir = None
    if output is not None:
        if output.endswith('.json'):
            output_file = output
        else:
            output_dir = output

    translator = QTranslator()
    translator.load(
        QLocale.system().name(),
        osp.dirname(osp.abspath(__file__)) + '/translate',
    )
    app = QApplication(sys.argv)
    app.setApplicationName(__appname__)
    app.setWindowIcon(newIcon('icon'))
    app.installTranslator(translator)
    win = MainWindow(
        config=config,
        output_file=output_file,
        output_dir=output_dir,
    )

    if reset_config:
        logger.info('Resetting Qt config: %s' % win.settings.fileName())
        win.settings.clear()
        sys.exit(0)

    win.show()
    win.raise_()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()

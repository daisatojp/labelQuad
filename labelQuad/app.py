import functools
import html
import math
import os
import os.path as osp
import re
from typing import Optional
from PyQt5.QtCore import *
from PyQt5.QtWidgets import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import QMessageBox as QMB
import imgviz
import natsort
import numpy as np
from loguru import logger
from labelme import __appname__
from labelme import utils
from labelme.config import get_config
from labelme.label_file import LabelFile
from labelme.label_file import LabelFileError
from labelme.shape import Shape
from labelme.widgets import BrightnessContrastDialog
from labelme.widgets import Canvas
from labelme.widgets import FileDialogPreview
from labelme.widgets import LabelDialog
from labelme.widgets import LabelListWidget
from labelme.widgets import LabelListWidgetItem
from labelme.widgets import ToolBar
from labelme.widgets import UniqueLabelQListWidget
from labelme.widgets import ZoomWidget


__appname__ = 'labelQuad'
__version__ = '1.0.0'


LABEL_COLORMAP = imgviz.label_colormap()


class MainWindow(QMainWindow):
    FIT_WINDOW, FIT_WIDTH, MANUAL_ZOOM = 0, 1, 2

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
        self._noSelectionSlot = False
        self._copied_shapes = None

        self.label_dialog = LabelDialog(
            parent=self,
            labels=self._config['labels'],
            sort_labels=self._config['sort_labels'],
            show_text_field=self._config['show_label_text_field'],
            completion=self._config['label_completion'],
            fit_to_content=self._config['fit_to_content'])

        self.label_list = LabelListWidget()
        self.label_list.itemSelectionChanged.connect(self.__label_selection_changed)
        self.label_list.itemDoubleClicked.connect(self.__edit_label)
        self.label_list.itemChanged.connect(self.__label_item_changed)
        self.label_list.itemDropped.connect(self.__label_order_changed)
        self.shape_dock = QDockWidget(self.tr('Polygon Labels'), self)
        self.shape_dock.setObjectName('Labels')
        self.shape_dock.setWidget(self.label_list)

        self.unique_label_list = UniqueLabelQListWidget()
        self.unique_label_list.setToolTip(self.tr('Select label to start annotating for it. ' 'Press "Esc" to deselect.'))
        if self._config['labels']:
            for label in self._config['labels']:
                item = self.unique_label_list.createItemFromLabel(label)
                self.unique_label_list.addItem(item)
                rgb = self._get_rgb_by_label(label)
                self.unique_label_list.setItemLabel(item, label, rgb)
        self.label_dock = QDockWidget(self.tr('Label List'), self)
        self.label_dock.setObjectName('Label List')
        self.label_dock.setWidget(self.unique_label_list)

        self.file_search = QLineEdit()
        self.file_search.setPlaceholderText(self.tr('Search Filename'))
        self.file_search.textChanged.connect(self.__file_search_changed)
        self.file_list_widget = QListWidget()
        self.file_list_widget.itemSelectionChanged.connect(self.__file_selection_changed)
        file_list_layout = QVBoxLayout()
        file_list_layout.setContentsMargins(0, 0, 0, 0)
        file_list_layout.setSpacing(0)
        file_list_layout.addWidget(self.file_search)
        file_list_layout.addWidget(self.file_list_widget)
        self.file_dock = QDockWidget(self.tr('File List'), self)
        self.file_dock.setObjectName('Files')
        file_list_widget = QWidget()
        file_list_widget.setLayout(file_list_layout)
        self.file_dock.setWidget(file_list_widget)

        self.setAcceptDrops(True)

        self.canvas = self.label_list.canvas = Canvas(
            epsilon=self._config['epsilon'],
            double_click=self._config['canvas']['double_click'],
            num_backups=self._config['canvas']['num_backups'],
            crosshair=self._config['canvas']['crosshair'])
        self.canvas.zoomRequest.connect(self.__zoom_request)
        self.canvas.mouseMoved.connect(lambda pos: self.status(f'Mouse is at: x={pos.x()}, y={pos.y()}'))

        scroll_area = QScrollArea()
        scroll_area.setWidget(self.canvas)
        scroll_area.setWidgetResizable(True)
        self.scroll_bars = {
            Qt.Vertical: scroll_area.verticalScrollBar(),
            Qt.Horizontal: scroll_area.horizontalScrollBar()}
        self.canvas.scrollRequest.connect(self.__scroll_request)
        self.canvas.newShape.connect(self.__new_shape)
        self.canvas.shapeMoved.connect(self.__set_dirty)
        self.canvas.selectionChanged.connect(self.shapeSelectionChanged)
        self.canvas.drawingPolygon.connect(self.toggleDrawingSensitive)

        self.setCentralWidget(scroll_area)

        features = QDockWidget.DockWidgetFeatures()
        for dock in ['label_dock', 'shape_dock', 'file_dock']:
            if self._config[dock]['closable']:
                features = features | QDockWidget.DockWidgetClosable
            if self._config[dock]['floatable']:
                features = features | QDockWidget.DockWidgetFloatable
            if self._config[dock]['movable']:
                features = features | QDockWidget.DockWidgetMovable
            getattr(self, dock).setFeatures(features)
            if self._config[dock]['show'] is False:
                getattr(self, dock).setVisible(False)

        self.addDockWidget(Qt.RightDockWidgetArea, self.label_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.shape_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.file_dock)

        shortcuts = self._config['shortcuts']

        self.action_quit = self.__new_action(self.tr('&Quit'), slot=self.close, shortcut=shortcuts['quit'], icon='quit', tip=self.tr('Quit application'))
        self.action_open_image_dir = self.__new_action(self.tr('Open Image Dir'), slot=self.__open_image_dir_dialog, icon='open', tip=self.tr('Open Image Dir'))
        self.action_open_annot_dir = self.__new_action(self.tr('Open Annot Dir'), slot=self.__open_annot_dir_dialog, icon='open', tip=self.tr('Open Image Dir'))
        self.action_open_next = self.__new_action(self.tr('&Next Image'), slot=self.__open_next, shortcut=shortcuts['open_next'], icon='next', tip=self.tr('Open next (hold Ctl+Shift to copy labels)'), enabled=False)
        self.action_open_prev = self.__new_action(self.tr('&Prev Image'), slot=self.__open_prev, shortcut=shortcuts['open_prev'], icon='prev', tip=self.tr('Open prev (hold Ctl+Shift to copy labels)'), enabled=False)
        self.action_save = self.__new_action(self.tr('&Save\n'), slot=self.__save_file, shortcut=shortcuts['save'], icon='save', tip=self.tr('Save labels to file'), enabled=False)
        self.action_save_auto = self.__new_action(self.tr('Save &Automatically'), slot=lambda x: self.action_save_auto.setChecked(x), icon='save', tip=self.tr('Save automatically'), checkable=True, enabled=True)
        self.action_save_auto.setChecked(self._config['auto_save'])
        self.action_close = self.__new_action(self.tr('&Close'), slot=self.closeFile, shortcut=shortcuts['close'], icon='close', tip=self.tr('Close current file'))
        self.action_toggle_keep_prev_mode = self.__new_action(self.tr('Keep Previous Annotation'), slot=self.toggleKeepPrevMode, shortcut=shortcuts['toggle_keep_prev_mode'], icon=None, tip=self.tr('Toggle "keep previous annotation" mode'), checkable=True)
        self.action_toggle_keep_prev_mode.setChecked(self._config['keep_prev'])
        self.action_create_mode = self.__new_action(self.tr('Create Polygons'), slot=lambda: self.toggleDrawMode(False, createMode='polygon'), shortcut=shortcuts['create_polygon'], icon='objects', tip=self.tr('Start drawing polygons'), enabled=False)
        self.action_edit_mode = self.__new_action(self.tr('Edit Polygons'), slot=self.setEditMode, shortcut=shortcuts['edit_polygon'], icon='edit', tip=self.tr('Move and edit the selected polygons'), enabled=False)
        self.action_delete = self.__new_action(self.tr('Delete Polygons'), slot=self.deleteSelectedShape, shortcut=shortcuts['delete_polygon'], icon='cancel', tip=self.tr('Delete the selected polygons'), enabled=False)
        self.action_copy = self.__new_action(self.tr('Copy Polygons'), slot=self.copySelectedShape, shortcut=shortcuts['copy_polygon'], icon='copy_clipboard', tip=self.tr('Copy selected polygons to clipboard'), enabled=False)
        self.action_paste = self.__new_action(self.tr('Paste Polygons'), slot=self.pasteSelectedShape, shortcut=shortcuts['paste_polygon'], icon='paste', tip=self.tr('Paste copied polygons'), enabled=False)
        self.action_undo_last_point = self.__new_action(self.tr('Undo last point'), slot=self.canvas.undoLastPoint, shortcut=shortcuts['undo_last_point'], icon='undo', tip=self.tr('Undo last drawn point'), enabled=False)
        self.action_undo = self.__new_action(self.tr('Undo\n'), slot=self.undoShapeEdit, shortcut=shortcuts['undo'], icon='undo', tip=self.tr('Undo last add and edit of shape'), enabled=False)
        self.action_hide_all = self.__new_action(self.tr('&Hide\nPolygons'), slot=functools.partial(self.__toggle_polygons, False), shortcut=shortcuts['hide_all_polygons'], icon='eye', tip=self.tr('Hide all polygons'), enabled=False)
        self.action_show_all = self.__new_action(self.tr('&Show\nPolygons'), slot=functools.partial(self.__toggle_polygons, True), shortcut=shortcuts['show_all_polygons'], icon='eye', tip=self.tr('Show all polygons'), enabled=False)
        self.action_toggle_all = self.__new_action(self.tr('&Toggle\nPolygons'), slot=functools.partial(self.__toggle_polygons, None), shortcut=shortcuts['toggle_all_polygons'], icon='eye', tip=self.tr('Toggle all polygons'), enabled=False)

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
            .format(utils.fmtShortcut('{},{}'.format(shortcuts['zoom_in'], shortcuts['zoom_out'])),
                    utils.fmtShortcut(self.tr('Ctrl+Wheel'))))
        self.zoom_widget.setEnabled(False)

        self.action_zoom_in = self.__new_action(self.tr('Zoom &In'), slot=functools.partial(self.__add_zoom, 1.1), shortcut=shortcuts['zoom_in'], icon='zoom-in', tip=self.tr('Increase zoom level'), enabled=False)
        self.action_zoom_out = self.__new_action(self.tr('&Zoom Out'), slot=functools.partial(self.__add_zoom, 0.9), shortcut=shortcuts['zoom_out'], icon='zoom-out', tip=self.tr('Decrease zoom level'), enabled=False)
        self.action_zoom_org = self.__new_action(self.tr('&Original size'), slot=functools.partial(self.__set_zoom, 100), shortcut=shortcuts['zoom_to_original'], icon='zoom', tip=self.tr('Zoom to original size'), enabled=False)
        self.action_keep_prev_scale = self.__new_action(self.tr('&Keep Previous Scale'), slot=self.__enable_keep_prev_scale, tip=self.tr('Keep previous zoom scale'), checkable=True, checked=self._config['keep_prev_scale'], enabled=True)
        self.action_fit_window = self.__new_action(self.tr('&Fit Window'), slot=self.__set_fit_window, shortcut=shortcuts['fit_window'], icon='fit-window', tip=self.tr('Zoom follows window size'), checkable=True, enabled=False)
        self.action_fit_width = self.__new_action(self.tr('Fit &Width'), slot=self.__set_fit_width, shortcut=shortcuts['fit_width'], icon='fit-width', tip=self.tr('Zoom follows window width'), checkable=True, enabled=False)
        self.action_brightness_contrast = self.__new_action(self.tr('&Brightness Contrast'), slot=self.__brightness_contrast, shortcut=None, icon='color', tip=self.tr('Adjust brightness and contrast'), enabled=False)

        self.zoomMode = self.FIT_WINDOW
        self.action_fit_window.setChecked(Qt.Checked)
        self.scalers = {
            self.FIT_WINDOW: self.__scale_fit_window,
            self.FIT_WIDTH: self.__scale_fit_width,
            self.MANUAL_ZOOM: lambda: 1}

        self.action_edit = self.__new_action(self.tr('&Edit Label'), slot=self.__edit_label, shortcut=shortcuts['edit_label'], icon='edit', tip=self.tr('Modify the label of the selected polygon'), enabled=False)
        self.action_fill_drawing = self.__new_action(self.tr('Fill Drawing Polygon'), slot=self.canvas.setFillDrawing, shortcut=None, icon='color', tip=self.tr('Fill polygon while drawing'), checkable=True, enabled=True)
        if self._config['canvas']['fill_drawing']:
            self.action_fill_drawing.trigger()

        label_menu = QMenu()
        utils.addActions(label_menu, (self.action_edit, self.action_delete))
        self.label_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.label_list.customContextMenuRequested.connect(self.popLabelListMenu)

        self.menu_file = self.menuBar().addMenu(self.tr('&File'))
        self.menu_edit = self.menuBar().addMenu(self.tr('&Edit'))
        self.menu_view = self.menuBar().addMenu(self.tr('&View'))
        self.menu_help = self.menuBar().addMenu(self.tr('&Help'))
        self.menu_recent_files = QMenu(self.tr('Open &Recent'))
        self.menu_label_list = label_menu

        utils.addActions(
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
        utils.addActions(self.menu_help, ())
        utils.addActions(
            self.menu_view,
            (self.label_dock.toggleViewAction(),
             self.shape_dock.toggleViewAction(),
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

        utils.addActions(
            self.canvas.menus[0],
            (self.action_create_mode,
             self.action_edit_mode,
             self.action_edit,
             self.action_copy,
             self.action_paste,
             self.action_delete,
             self.action_undo,
             self.action_undo_last_point))
        utils.addActions(
            self.canvas.menus[1],
            (self.__new_action('&Copy here', self.copyShape),
             self.__new_action('&Move here', self.moveShape)))

        self.tools = self.toolbar('Tools')
        self.actions_on_shapes_present = (
            self.action_hide_all,
            self.action_show_all,
            self.action_toggle_all)

        self.statusBar().showMessage(str(self.tr('%s started.')) % __appname__)
        self.statusBar().show()

        if output_file is not None and self._config['auto_save']:
            logger.warn(
                'If `auto_save` argument is True, `output_file` argument '
                'is ignored and output filename is automatically '
                'set as IMAGE_BASENAME.json.'
            )
        self.output_file = output_file
        self.output_dir = output_dir

        self.image = QImage()
        self.imagePath = None
        self.recentFiles = []
        self.maxRecent = 7
        self.otherData = None
        self.zoom_level = 100
        self.fit_window = False
        self.zoom_values = {}  # key=filename, value=(zoom_mode, zoom_value)
        self.brightness_contrast_values = {}
        self.scroll_values = {
            Qt.Horizontal: {},
            Qt.Vertical: {}}

        if config['file_search']:
            self.file_search.setText(config['file_search'])
            self.__file_search_changed()

        self.settings = QSettings('labelme', 'labelme')
        self.recentFiles = self.settings.value('recentFiles', []) or []
        size = self.settings.value('window/size', QSize(600, 500))
        position = self.settings.value('window/position', QPoint(0, 0))
        state = self.settings.value('window/state', QByteArray())
        self.resize(size)
        self.move(position)
        self.restoreState(state)

        self.updateFileMenu()

        self.zoom_widget.valueChanged.connect(self.paintCanvas)

        self.populateModeActions()

    def toolbar(self, title, actions=None):
        toolbar = ToolBar(title)
        toolbar.setObjectName('%sToolBar' % title)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        if actions:
            utils.addActions(toolbar, actions)
        self.addToolBar(Qt.TopToolBarArea, toolbar)
        return toolbar

    def noShapes(self):
        return not len(self.label_list)

    def populateModeActions(self):
        self.tools.clear()
        utils.addActions(
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
        self.canvas.menus[0].clear()
        utils.addActions(
            self.canvas.menus[0],
            (self.action_create_mode,
             self.action_edit_mode,
             self.action_edit,
             self.action_copy,
             self.action_paste,
             self.action_delete,
             self.action_undo,
             self.action_undo_last_point))
        self.menu_edit.clear()
        utils.addActions(
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
             None,
             self.action_toggle_keep_prev_mode))

    def __set_dirty(self):
        self.action_undo.setEnabled(self.canvas.isShapeRestorable)
        if self._config['auto_save'] or self.action_save_auto.isChecked():
            label_file = osp.splitext(self.imagePath)[0] + '.json'
            if self.output_dir:
                label_file_without_path = osp.basename(label_file)
                label_file = osp.join(self.output_dir, label_file_without_path)
            self.saveLabels(label_file)
            return
        self.dirty = True
        self.action_save.setEnabled(True)
        title = __appname__
        if self.filename is not None:
            title = '{} - {}*'.format(title, self.filename)
        self.setWindowTitle(title)

    def setClean(self):
        self.dirty = False
        self.action_save.setEnabled(False)
        self.action_create_mode.setEnabled(True)
        title = __appname__
        if self.filename is not None:
            title = '{} - {}'.format(title, self.filename)
        self.setWindowTitle(title)

    def toggleActions(self, value: bool = True):
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

    def queueEvent(self, function):
        QTimer.singleShot(0, function)

    def status(self, message, delay=5000):
        self.statusBar().showMessage(message, delay)

    def resetState(self):
        self.label_list.clear()
        self.filename = None
        self.imagePath = None
        self.imageData = None
        self.labelFile = None
        self.otherData = None
        self.canvas.resetState()

    def currentItem(self):
        items = self.label_list.selectedItems()
        if items:
            return items[0]
        return None

    def addRecentFile(self, filename):
        if filename in self.recentFiles:
            self.recentFiles.remove(filename)
        elif len(self.recentFiles) >= self.maxRecent:
            self.recentFiles.pop()
        self.recentFiles.insert(0, filename)

    def undoShapeEdit(self):
        self.canvas.restoreShape()
        self.label_list.clear()
        self.loadShapes(self.canvas.shapes)
        self.action_undo.setEnabled(self.canvas.isShapeRestorable)

    def toggleDrawingSensitive(self, drawing=True):
        """Toggle drawing sensitive.

        In the middle of drawing, toggling between modes should be disabled.
        """
        self.action_edit_mode.setEnabled(not drawing)
        self.action_undo_last_point.setEnabled(drawing)
        self.action_undo.setEnabled(not drawing)
        self.action_delete.setEnabled(not drawing)

    def toggleDrawMode(self, edit=True, createMode='polygon'):
        draw_actions = {
            'polygon': self.action_create_mode,
        }

        self.canvas.setEditing(edit)
        self.canvas.createMode = createMode
        if edit:
            for draw_action in draw_actions.values():
                draw_action.setEnabled(True)
        else:
            for draw_mode, draw_action in draw_actions.items():
                draw_action.setEnabled(createMode != draw_mode)
        self.action_edit_mode.setEnabled(not edit)

    def setEditMode(self):
        self.toggleDrawMode(True)

    def updateFileMenu(self):
        current = None
        if 0 <= self.file_list_widget.currentRow():
            current = self.file_list_widget.currentItem().text()

        def exists(filename):
            return osp.exists(str(filename))

        menu = self.menu_recent_files
        menu.clear()
        files = [f for f in self.recentFiles if f != current and exists(f)]
        for i, f in enumerate(files):
            icon = utils.newIcon('labels')
            action = QAction(icon, '&%d %s' % (i + 1, QFileInfo(f).fileName()), self)
            action.triggered.connect(functools.partial(self.__load_recent, f))
            menu.addAction(action)

    def popLabelListMenu(self, point):
        self.menu_label_list.exec_(self.label_list.mapToGlobal(point))

    def validateLabel(self, label):
        if self._config['validate_label'] is None:
            return True
        for i in range(self.unique_label_list.count()):
            label_i = self.unique_label_list.item(i).data(Qt.UserRole)
            if self._config['validate_label'] in ['exact']:
                if label_i == label:
                    return True
        return False

    def __edit_label(self, value=None):
        if not self.canvas.editing():
            return

        items = self.label_list.selectedItems()
        if not items:
            logger.warning('No label is selected, so cannot edit label.')
            return

        shape = items[0].shape()

        if len(items) == 1:
            edit_text = True
            edit_group_id = True
            edit_description = True
        else:
            edit_text = all(item.shape().label == shape.label for item in items[1:])
            edit_group_id = all(
                item.shape().group_id == shape.group_id for item in items[1:]
            )
            edit_description = all(
                item.shape().description == shape.description for item in items[1:]
            )

        if not edit_text:
            self.label_dialog.edit.setDisabled(True)
            self.label_dialog.label_list.setDisabled(True)
        if not edit_group_id:
            self.label_dialog.edit_group_id.setDisabled(True)
        if not edit_description:
            self.label_dialog.editDescription.setDisabled(True)

        text, _, group_id, description = self.label_dialog.popUp(
            text=shape.label if edit_text else '',
            flags=None,
            group_id=shape.group_id if edit_group_id else None,
            description=shape.description if edit_description else None,
        )

        if not edit_text:
            self.label_dialog.edit.setDisabled(False)
            self.label_dialog.label_list.setDisabled(False)
        if not edit_group_id:
            self.label_dialog.edit_group_id.setDisabled(False)
        if not edit_description:
            self.label_dialog.editDescription.setDisabled(False)

        if text is None:
            assert group_id is None
            assert description is None
            return

        self.canvas.storeShapes()
        for item in items:
            self._update_item(
                item=item,
                text=text if edit_text else None,
                group_id=group_id if edit_group_id else None,
                description=description if edit_description else None,
            )

    def _update_item(self, item, text, group_id, description):
        if not self.validateLabel(text):
            self.__error_message(
                self.tr('Invalid label'),
                self.tr('Invalid label "{}" with validation type "{}"').format(
                    text, self._config['validate_label']
                ),
            )
            return

        shape = item.shape()

        if text is not None:
            shape.label = text
        if group_id is not None:
            shape.group_id = group_id
        if description is not None:
            shape.description = description

        self._update_shape_color(shape)
        if shape.group_id is None:
            item.setText(
                '{} <font color="#{:02x}{:02x}{:02x}">●</font>'.format(
                    html.escape(shape.label), *shape.fill_color.getRgb()[:3]
                )
            )
        else:
            item.setText('{} ({})'.format(shape.label, shape.group_id))
        self.__set_dirty()
        if self.unique_label_list.findItemByLabel(shape.label) is None:
            item = self.unique_label_list.createItemFromLabel(shape.label)
            self.unique_label_list.addItem(item)
            rgb = self._get_rgb_by_label(shape.label)
            self.unique_label_list.setItemLabel(item, shape.label, rgb)

    def __file_search_changed(self) -> None:
        self.__import_dir_images(
            self.image_dir,
            pattern=self.file_search.text(),
            load=False)

    def __file_selection_changed(self) -> None:
        if self.file_list_widget.currentRow() < 0:
            return
        if not self.__may_continue():
            return
        file_path = osp.join(self.image_dir, self.file_list_widget.currentItem().text())
        self.__load_file(file_path)

    def shapeSelectionChanged(self, selected_shapes):
        self._noSelectionSlot = True
        for shape in self.canvas.selectedShapes:
            shape.selected = False
        self.label_list.clearSelection()
        self.canvas.selectedShapes = selected_shapes
        for shape in self.canvas.selectedShapes:
            shape.selected = True
            item = self.label_list.findItemByShape(shape)
            self.label_list.selectItem(item)
            self.label_list.scrollToItem(item)
        self._noSelectionSlot = False
        n_selected = len(selected_shapes)
        self.action_delete.setEnabled(n_selected)
        self.action_copy.setEnabled(n_selected)
        self.action_edit.setEnabled(n_selected)

    def addLabel(self, shape):
        if shape.group_id is None:
            text = shape.label
        else:
            text = '{} ({})'.format(shape.label, shape.group_id)
        label_list_item = LabelListWidgetItem(text, shape)
        self.label_list.addItem(label_list_item)
        if self.unique_label_list.findItemByLabel(shape.label) is None:
            item = self.unique_label_list.createItemFromLabel(shape.label)
            self.unique_label_list.addItem(item)
            rgb = self._get_rgb_by_label(shape.label)
            self.unique_label_list.setItemLabel(item, shape.label, rgb)
        self.label_dialog.addLabelHistory(shape.label)
        for action in self.actions_on_shapes_present:
            action.setEnabled(True)

        self._update_shape_color(shape)
        label_list_item.setText(
            '{} <font color="#{:02x}{:02x}{:02x}">●</font>'.format(
                html.escape(text), *shape.fill_color.getRgb()[:3]
            )
        )

    def _update_shape_color(self, shape):
        r, g, b = self._get_rgb_by_label(shape.label)
        shape.line_color = QColor(r, g, b)
        shape.vertex_fill_color = QColor(r, g, b)
        shape.hvertex_fill_color = QColor(255, 255, 255)
        shape.fill_color = QColor(r, g, b, 128)
        shape.select_line_color = QColor(255, 255, 255)
        shape.select_fill_color = QColor(r, g, b, 155)

    def _get_rgb_by_label(self, label):
        if self._config['shape_color'] == 'auto':
            item = self.unique_label_list.findItemByLabel(label)
            if item is None:
                item = self.unique_label_list.createItemFromLabel(label)
                self.unique_label_list.addItem(item)
                rgb = self._get_rgb_by_label(label)
                self.unique_label_list.setItemLabel(item, label, rgb)
            label_id = self.unique_label_list.indexFromItem(item).row() + 1
            label_id += self._config['shift_auto_shape_color']
            return LABEL_COLORMAP[label_id % len(LABEL_COLORMAP)]
        elif (
            self._config['shape_color'] == 'manual'
            and self._config['label_colors']
            and label in self._config['label_colors']
        ):
            return self._config['label_colors'][label]
        elif self._config['default_shape_color']:
            return self._config['default_shape_color']
        return (0, 255, 0)

    def remLabels(self, shapes):
        for shape in shapes:
            item = self.label_list.findItemByShape(shape)
            self.label_list.removeItem(item)

    def loadShapes(self, shapes, replace=True):
        self._noSelectionSlot = True
        for shape in shapes:
            self.addLabel(shape)
        self.label_list.clearSelection()
        self._noSelectionSlot = False
        self.canvas.loadShapes(shapes, replace=replace)

    def loadLabels(self, shapes):
        s = []
        for shape in shapes:
            label = shape['label']
            points = shape['points']
            shape_type = shape['shape_type']
            description = shape.get('description', '')
            group_id = shape['group_id']
            other_data = shape['other_data']

            if not points:
                # skip point-empty shape
                continue

            shape = Shape(
                label=label,
                shape_type=shape_type,
                group_id=group_id,
                description=description,
                mask=shape['mask'],
            )
            for x, y in points:
                shape.addPoint(QPointF(x, y))
            shape.close()

            shape.other_data = other_data

            s.append(shape)
        self.loadShapes(s)

    def saveLabels(self, filename):
        lf = LabelFile()

        def format_shape(s):
            data = s.other_data.copy()
            data.update(
                dict(
                    label=s.label,
                    points=[(p.x(), p.y()) for p in s.points],
                    group_id=s.group_id,
                    description=s.description,
                    shape_type=s.shape_type,
                    flags=None,
                    mask=None
                    if s.mask is None
                    else utils.img_arr_to_b64(s.mask.astype(np.uint8)),
                )
            )
            return data

        shapes = [format_shape(item.shape()) for item in self.label_list]
        try:
            imagePath = osp.relpath(self.imagePath, osp.dirname(filename))
            imageData = self.imageData if self._config['store_data'] else None
            if osp.dirname(filename) and not osp.exists(osp.dirname(filename)):
                os.makedirs(osp.dirname(filename))
            lf.save(
                filename=filename,
                shapes=shapes,
                imagePath=imagePath,
                imageData=imageData,
                imageHeight=self.image.height(),
                imageWidth=self.image.width(),
                otherData=self.otherData,
            )
            self.labelFile = lf
            items = self.file_list_widget.findItems(self.imagePath, Qt.MatchExactly)
            if len(items) > 0:
                if len(items) != 1:
                    raise RuntimeError('There are duplicate files.')
                items[0].setCheckState(Qt.Checked)
            return True
        except LabelFileError as e:
            self.__error_message(
                self.tr('Error saving label data'), self.tr('<b>%s</b>') % e
            )
            return False

    def pasteSelectedShape(self):
        self.loadShapes(self._copied_shapes, replace=False)
        self.__set_dirty()

    def copySelectedShape(self):
        self._copied_shapes = [s.copy() for s in self.canvas.selectedShapes]
        self.action_paste.setEnabled(len(self._copied_shapes) > 0)

    def __label_selection_changed(self):
        if self._noSelectionSlot:
            return
        if self.canvas.editing():
            selected_shapes = []
            for item in self.label_list.selectedItems():
                selected_shapes.append(item.shape())
            if selected_shapes:
                self.canvas.selectShapes(selected_shapes)
            else:
                self.canvas.deSelectShape()

    def __label_item_changed(self, item):
        shape = item.shape()
        self.canvas.setShapeVisible(shape, item.checkState() == Qt.Checked)

    def __label_order_changed(self):
        self.__set_dirty()
        self.canvas.loadShapes([item.shape() for item in self.label_list])

    def __new_shape(self) -> None:
        items = self.unique_label_list.selectedItems()
        text = None
        if items:
            text = items[0].data(Qt.UserRole)
        group_id = None
        description = ''
        if self._config['display_label_popup'] or not text:
            previous_text = self.label_dialog.edit.text()
            text, _, group_id, description = self.label_dialog.popUp(text)
            if not text:
                self.label_dialog.edit.setText(previous_text)
        if text and not self.validateLabel(text):
            self.__error_message(
                self.tr('Invalid label'),
                self.tr('Invalid label "{}" with validation type "{}"').format(
                    text, self._config['validate_label']))
            text = ''
        if text:
            self.label_list.clearSelection()
            shape = self.canvas.setLastLabel(text, None)
            shape.group_id = group_id
            shape.description = description
            self.addLabel(shape)
            self.action_edit_mode.setEnabled(True)
            self.action_undo_last_point.setEnabled(False)
            self.action_undo.setEnabled(True)
            self.__set_dirty()
        else:
            self.canvas.undoLastLine()
            self.canvas.shapesBackups.pop()

    def __scroll_request(self, delta, orientation):
        units = -delta * 0.1  # natural scroll
        bar = self.scroll_bars[orientation]
        value = bar.value() + bar.singleStep() * units
        self.__set_scroll(orientation, value)

    def __set_scroll(self, orientation, value):
        self.scroll_bars[orientation].setValue(int(value))
        self.scroll_values[orientation][self.filename] = value

    def __set_zoom(self, value):
        self.action_fit_width.setChecked(False)
        self.action_fit_window.setChecked(False)
        self.zoomMode = self.MANUAL_ZOOM
        self.zoom_widget.setValue(value)
        self.zoom_values[self.filename] = (self.zoomMode, value)

    def __add_zoom(self, increment: float = 1.1):
        zoom_value = self.zoom_widget.value() * increment
        if increment > 1:
            zoom_value = math.ceil(zoom_value)
        else:
            zoom_value = math.floor(zoom_value)
        self.__set_zoom(zoom_value)

    def __zoom_request(self, delta, pos):
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
        self.zoomMode = self.FIT_WINDOW
        self.adjustScale()

    def __set_fit_width(self) -> None:
        self.action_fit_window.setChecked(False)
        self.zoomMode = self.FIT_WIDTH
        self.adjustScale()

    def __enable_keep_prev_scale(self, enabled):
        self._config['keep_prev_scale'] = enabled
        self.action_keep_prev_scale.setChecked(enabled)

    def __on_new_brightness_contrast(self, qimage):
        self.canvas.loadPixmap(QPixmap.fromImage(qimage), clear_shapes=False)

    def __brightness_contrast(self, value):
        dialog = BrightnessContrastDialog(
            utils.img_data_to_pil(self.imageData),
            self.__on_new_brightness_contrast,
            parent=self)
        brightness, contrast = self.brightness_contrast_values.get(self.filename, (None, None))
        if brightness is not None:
            dialog.slider_brightness.setValue(brightness)
        if contrast is not None:
            dialog.slider_contrast.setValue(contrast)
        dialog.exec_()
        brightness = dialog.slider_brightness.value()
        contrast = dialog.slider_contrast.value()
        self.brightness_contrast_values[self.filename] = (brightness, contrast)

    def __toggle_polygons(self, value):
        flag = value
        for item in self.label_list:
            if value is None:
                flag = item.checkState() == Qt.Unchecked
            item.setCheckState(Qt.Checked if flag else Qt.Unchecked)

    def __load_file(self, filename: str = None):
        """Load the specified file, or the last opened file if None."""
        # changing file_list_widget loads file
        if (filename in self.imageList) and \
           (self.file_list_widget.currentRow() != self.imageList.index(filename)):
            self.file_list_widget.setCurrentRow(self.imageList.index(filename))
            self.file_list_widget.repaint()
            return

        self.resetState()
        self.canvas.setEnabled(False)
        if filename is None:
            filename = self.settings.value('filename', '')
        filename = str(filename)
        if not QFile.exists(filename):
            self.__error_message(
                self.tr(f'Error opening file'),
                self.tr(f'No such file: <b>{filename}</b>'))
            return False
        self.status(str(self.tr('Loading %s...')) % osp.basename(str(filename)))
        label_file = osp.splitext(filename)[0] + '.json'
        if self.output_dir:
            label_file_without_path = osp.basename(label_file)
            label_file = osp.join(self.output_dir, label_file_without_path)
        if QFile.exists(label_file) and LabelFile.is_label_file(label_file):
            try:
                self.labelFile = LabelFile(label_file)
            except LabelFileError as e:
                self.__error_message(
                    self.tr('Error opening file'),
                    self.tr(
                        '<p><b>%s</b></p>'
                        '<p>Make sure <i>%s</i> is a valid label file.'
                    )
                    % (e, label_file),
                )
                self.status(self.tr('Error reading %s') % label_file)
                return False
            self.imageData = self.labelFile.imageData
            self.imagePath = osp.join(
                osp.dirname(label_file),
                self.labelFile.imagePath,
            )
            self.otherData = self.labelFile.otherData
        else:
            self.imageData = LabelFile.load_image_file(filename)
            if self.imageData:
                self.imagePath = filename
            self.labelFile = None
        image = QImage.fromData(self.imageData)

        if image.isNull():
            formats = [
                '*.{}'.format(fmt.data().decode())
                for fmt in QImageReader.supportedImageFormats()
            ]
            self.__error_message(
                self.tr('Error opening file'),
                self.tr(
                    '<p>Make sure <i>{0}</i> is a valid image file.<br/>'
                    'Supported image formats: {1}</p>'
                ).format(filename, ','.join(formats)))
            self.status(self.tr('Error reading %s') % filename)
            return False
        self.image = image
        self.filename = filename
        if self._config['keep_prev']:
            prev_shapes = self.canvas.shapes
        self.canvas.loadPixmap(QPixmap.fromImage(image))
        if self._config['keep_prev'] and self.noShapes():
            self.loadShapes(prev_shapes, replace=False)
            self.__set_dirty()
        else:
            self.setClean()
        self.canvas.setEnabled(True)
        is_initial_load = not self.zoom_values
        if self.filename in self.zoom_values:
            self.zoomMode = self.zoom_values[self.filename][0]
            self.__set_zoom(self.zoom_values[self.filename][1])
        elif is_initial_load or not self._config['keep_prev_scale']:
            self.adjustScale(initial=True)
        for orientation in self.scroll_values:
            if self.filename in self.scroll_values[orientation]:
                self.__set_scroll(orientation, self.scroll_values[orientation][self.filename])
        dialog = BrightnessContrastDialog(
            utils.img_data_to_pil(self.imageData),
            self.__on_new_brightness_contrast,
            parent=self)
        brightness, contrast = self.brightness_contrast_values.get(
            self.filename, (None, None))
        if self._config['keep_prev_brightness'] and self.recentFiles:
            brightness, _ = self.brightness_contrast_values.get(
                self.recentFiles[0], (None, None))
        if self._config['keep_prev_contrast'] and self.recentFiles:
            _, contrast = self.brightness_contrast_values.get(
                self.recentFiles[0], (None, None))
        if brightness is not None:
            dialog.slider_brightness.setValue(brightness)
        if contrast is not None:
            dialog.slider_contrast.setValue(contrast)
        self.brightness_contrast_values[self.filename] = (brightness, contrast)
        if brightness is not None or contrast is not None:
            dialog.onNewValue(None)
        self.paintCanvas()
        self.addRecentFile(self.filename)
        self.toggleActions(True)
        self.canvas.setFocus()
        self.status(str(self.tr('Loaded %s')) % osp.basename(str(filename)))
        return True

    def resizeEvent(self, event):
        if (
            self.canvas
            and not self.image.isNull()
            and self.zoomMode != self.MANUAL_ZOOM
        ):
            self.adjustScale()
        super(MainWindow, self).resizeEvent(event)

    def paintCanvas(self):
        assert not self.image.isNull(), 'cannot paint null image'
        self.canvas.scale = 0.01 * self.zoom_widget.value()
        self.canvas.adjustSize()
        self.canvas.update()

    def adjustScale(self, initial=False):
        value = self.scalers[self.FIT_WINDOW if initial else self.zoomMode]()
        value = int(100 * value)
        self.zoom_widget.setValue(value)
        self.zoom_values[self.filename] = (self.zoomMode, value)

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

    def closeEvent(self, event):
        if not self.__may_continue():
            event.ignore()
        self.settings.setValue('filename', self.filename if self.filename else '')
        self.settings.setValue('window/size', self.size())
        self.settings.setValue('window/position', self.pos())
        self.settings.setValue('window/state', self.saveState())
        self.settings.setValue('recentFiles', self.recentFiles)

    def dragEnterEvent(self, event):
        extensions = [
            '.%s' % fmt.data().decode().lower()
            for fmt in QImageReader.supportedImageFormats()
        ]
        if event.mimeData().hasUrls():
            items = [i.toLocalFile() for i in event.mimeData().urls()]
            if any([i.lower().endswith(tuple(extensions)) for i in items]):
                event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        if not self.__may_continue():
            event.ignore()
            return
        items = [i.toLocalFile() for i in event.mimeData().urls()]
        self.importDroppedImageFiles(items)

    def __load_recent(self, filename):
        if self.__may_continue():
            self.__load_file(filename)

    def __open_prev(self, _value=False):
        keep_prev = self._config['keep_prev']
        if QApplication.keyboardModifiers() == (Qt.ControlModifier | Qt.ShiftModifier):
            self._config['keep_prev'] = True
        if not self.__may_continue():
            return
        if len(self.imageList) <= 0:
            return
        if self.filename is None:
            return
        currIndex = self.imageList.index(self.filename)
        if currIndex - 1 >= 0:
            filename = self.imageList[currIndex - 1]
            if filename:
                self.__load_file(filename)
        self._config['keep_prev'] = keep_prev

    def __open_next(self, _value=False, load=True):
        keep_prev = self._config['keep_prev']
        if QApplication.keyboardModifiers() == (Qt.ControlModifier | Qt.ShiftModifier):
            self._config['keep_prev'] = True
        if not self.__may_continue():
            return
        if self.file_list_widget.count() < 0:
            return
        size = self.file_list_widget.count()
        row = self.file_list_widget.currentRow()
        if row == -1:
            row = 0
        else:
            if row + 1 < size:
                row = row + 1
        self.file_list_widget.setCurrentRow(row)
        filename = self.file_list_widget.item(row).text()
        if load:
            self.__load_file(osp.join(self.image_dir, filename))
        self._config['keep_prev'] = keep_prev

    def __save_file(self):
        if (self.annot_dir is None) or \
           (self.file_list_widget.currentRow() < 0):
           return
        filename = self.file_list_widget.currentItem().text()
        filename = osp.splitext(filename)[0] + '.json'
        self.saveLabels(osp.join(self.annot_dir, filename))

    def closeFile(self, _value=False):
        if not self.__may_continue():
            return
        self.resetState()
        self.setClean()
        self.toggleActions(False)
        self.canvas.setEnabled(False)

    def __may_continue(self):
        if not self.dirty:
            return True
        msg = self.tr('Save annotations to "{}" before closing?').format(self.filename)
        answer = QMB.question(self, self.tr('Save annotations?'), msg, QMB.Save | QMB.Discard | QMB.Cancel, QMB.Save)
        if answer == QMB.Discard:
            return True
        elif answer == QMB.Save:
            self.__save_file()
            return True
        else:
            return False

    def __error_message(self, title, message):
        return QMessageBox.critical(self, title, '<p><b>%s</b></p>%s' % (title, message))

    def currentPath(self):
        return osp.dirname(str(self.filename)) if self.filename else '.'

    def toggleKeepPrevMode(self):
        self._config['keep_prev'] = not self._config['keep_prev']

    def deleteSelectedShape(self):
        yes, no = QMessageBox.Yes, QMessageBox.No
        msg = self.tr('You are about to permanently delete {} polygons, ' 'proceed anyway?').format(len(self.canvas.selectedShapes))
        if yes == QMessageBox.warning(self, self.tr('Attention'), msg, yes | no, yes):
            self.remLabels(self.canvas.deleteSelected())
            self.__set_dirty()
            if self.noShapes():
                for action in self.actions_on_shapes_present:
                    action.setEnabled(False)

    def copyShape(self):
        self.canvas.endMove(copy=True)
        for shape in self.canvas.selectedShapes:
            self.addLabel(shape)
        self.label_list.clearSelection()
        self.__set_dirty()

    def moveShape(self):
        self.canvas.endMove(copy=False)
        self.__set_dirty()

    @property
    def imageList(self):
        lst = []
        for i in range(self.file_list_widget.count()):
            item = self.file_list_widget.item(i)
            lst.append(item.text())
        return lst

    def importDroppedImageFiles(self, imageFiles):
        extensions = [
            '.%s' % fmt.data().decode().lower()
            for fmt in QImageReader.supportedImageFormats()]
        self.filename = None
        for file in imageFiles:
            if file in self.imageList or not file.lower().endswith(tuple(extensions)):
                continue
            label_file = osp.splitext(file)[0] + '.json'
            if self.output_dir:
                label_file_without_path = osp.basename(label_file)
                label_file = osp.join(self.output_dir, label_file_without_path)
            item = QListWidgetItem(file)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            if QFile.exists(label_file) and LabelFile.is_label_file(label_file):
                item.setCheckState(Qt.Checked)
            else:
                item.setCheckState(Qt.Unchecked)
            self.file_list_widget.addItem(item)
        if len(self.imageList) > 1:
            self.action_open_next.setEnabled(True)
            self.action_open_prev.setEnabled(True)
        self.__open_next()

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

    def __import_dir_images(self, dirpath: str, pattern: Optional[str] = None, load: bool = True) -> None:
        self.action_open_next.setEnabled(True)
        self.action_open_prev.setEnabled(True)
        if not self.__may_continue() or not dirpath:
            return
        self.image_dir = dirpath
        self.filename = None
        self.file_list_widget.clear()
        filenames = self.__scan_all_images(dirpath)
        if pattern:
            try:
                filenames = [f for f in filenames if re.search(pattern, f)]
            except re.error:
                pass
        for filename in filenames:
            label_file = osp.splitext(filename)[0] + '.json'
            if self.output_dir:
                label_file = osp.join(self.output_dir, osp.basename(label_file))
            item = QListWidgetItem(osp.basename(filename))
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            if QFile.exists(label_file) and LabelFile.is_label_file(label_file):
                item.setCheckState(Qt.Checked)
            else:
                item.setCheckState(Qt.Unchecked)
            self.file_list_widget.addItem(item)
        self.__open_next(load=load)

    def __current_file(self) -> str:
        filename = self.file_list_widget.currentItem().text()
        return osp.join(self.image_dir, filename)

    def __scan_all_images(self, dir_path: str) -> list[str]:
        extensions = [
            '.%s' % fmt.data().decode().lower()
            for fmt in QImageReader.supportedImageFormats()]
        images = []
        for root, dirs, files in os.walk(dir_path):
            for file in files:
                if file.lower().endswith(tuple(extensions)):
                    relativePath = os.path.normpath(osp.join(root, file))
                    images.append(relativePath)
        images = natsort.os_sorted(images)
        return images

    def __new_action(
            self,
            text,
            slot=None,
            shortcut=None,
            icon=None,
            tip=None,
            checkable=False,
            enabled=True,
            checked=False,
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

    def __new_icon(self, icon):
        icons_dir = osp.join(osp.dirname(osp.abspath(__file__)), '../labelQuad/icons')
        return QIcon(osp.join(':/', icons_dir, '%s.png' % icon))

from pathlib import Path
import logging
from ruamel.yaml import YAML
import importlib
from instrument_widgets.base_device_widget import BaseDeviceWidget, scan_for_properties, create_widget
from instrument_widgets.acquisition_widgets.scan_plan_widget import ScanPlanWidget
from instrument_widgets.acquisition_widgets.volume_model import VolumeModel
from instrument_widgets.acquisition_widgets.tile_plan_widget import TilePlanWidget
from qtpy.QtCore import Slot
import inflection
from time import sleep
from napari.qt.threading import thread_worker
from qtpy.QtWidgets import QGridLayout, QWidget, QComboBox, QSizePolicy, QScrollArea, QApplication, QDockWidget, \
    QLabel, QVBoxLayout, QCheckBox, QHBoxLayout, QButtonGroup, QRadioButton
from qtpy.QtCore import Qt


class AcquisitionView:
    """"Class to act as a general acquisition view model to voxel instrument"""

    def __init__(self, acquisition, instrument_view, config_path: Path, log_level='INFO'):
        """
        :param acquisition: voxel acquisition object
        :param config_path: path to config specifying UI setup
        :param instrument_view: view object relating to instrument. Needed to lock stage
        :param log_level:
        """
        self.log = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.log.setLevel(log_level)

        # Locks
        self.tiling_stage_locks = instrument_view.tiling_stage_locks
        self.scanning_stage_locks = instrument_view.scanning_stage_locks

        # Eventual widgets
        self.tile_plan_widget = None
        self.volume_model_widget = None
        self.metadata_widget = None

        # Eventual threads
        self.grab_fov_positions_worker = None

        self.acquisition = acquisition
        self.instrument = self.acquisition.instrument
        self.config = YAML(typ='safe', pure=True).load(
            config_path)  # TODO: maybe bulldozing comments but easier

        for device_name, operation_dictionary in self.acquisition.config['acquisition']['operations'].items():
            for operation_name, operation_specs in operation_dictionary.items():
                self.create_operation_widgets(device_name, operation_name, operation_specs)

        # setup additional widgets
        self.metadata_widget = self.create_metadata_widget()
        self.volume_model_widget = self.create_volume_model_widget()
        self.scan_plan_widget = self.create_scan_plan_widget()
        self.tile_plan_widget = self.create_tile_plan_widget()

        # setup stage thread
        self.setup_fov_position()

        # Set up main window
        self.main_window = QWidget()
        self.main_layout = QGridLayout()

        # create scroll wheel for metadata widget
        scroll = QScrollArea()
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self.metadata_widget)
        scroll.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)

        # create dock widget for grid widgets
        for coord, widget in zip([[0, 3]],
                                 [scroll]):
            dock = QDockWidget(widget.windowTitle(), self.main_window)
            dock.setWidget(widget)
            dock.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)
            self.main_layout.addWidget(dock, coord[0], coord[1])

        self.main_layout.addWidget(self.tile_plan_widget, 0, 0)
        self.main_layout.addWidget(self.volume_model_widget, 0, 1, 2, 2)
        self.main_layout.addWidget(QWidget(), 2, 0, 1, 3)  # placeholder for bottom graph

        # create dock widget for operations
        for i, operation in enumerate(['writer', 'transfer', 'process', 'routine']):
            if hasattr(self, f'{operation}_widgets'):
                stack = self.stack_device_widgets(operation)
                stack.setFixedWidth(self.metadata_widget.size().width() - 20)
                scroll = QScrollArea()
                scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                scroll.setWidget(stack)
                scroll.setFixedWidth(self.metadata_widget.size().width())
                dock = QDockWidget(stack.windowTitle())
                dock.setWidget(scroll)
                self.main_layout.addWidget(dock, i + 1, 3)

        self.main_window.setLayout(self.main_layout)
        self.main_window.setWindowTitle('Acquisition View')
        self.main_window.show()

        # Set app events
        app = QApplication.instance()
        app.focusChanged.connect(self.toggle_grab_fov_positions)

    def stack_device_widgets(self, device_type):
        """Stack like device widgets in layout and hide/unhide with combo box
        :param device_type: type of device being stacked"""

        device_widgets = {f'{inflection.pluralize(device_type)} {device_name}': create_widget('V', **widgets)
                          for device_name, widgets in getattr(self, f'{device_type}_widgets').items()}

        overlap_layout = QGridLayout()
        overlap_layout.addWidget(QWidget(), 1, 0)  # spacer widget
        for name, widget in device_widgets.items():
            widget.setVisible(False)
            overlap_layout.addWidget(widget, 2, 0)

        visible = QComboBox()
        visible.currentTextChanged.connect(lambda text: self.hide_devices(text, device_widgets))
        visible.addItems(device_widgets.keys())
        visible.setCurrentIndex(0)
        overlap_layout.addWidget(visible, 0, 0)

        overlap_widget = QWidget()
        overlap_widget.setWindowTitle(device_type)
        overlap_widget.setLayout(overlap_layout)

        return overlap_widget

    def hide_devices(self, text, device_widgets):
        """Hide device widget if not selected in combo box
        :param text: selected text of combo box
        :param device_widgets: dictionary of widget groups"""

        for name, widget in device_widgets.items():
            if name != text:
                widget.setVisible(False)
            else:
                widget.setVisible(True)

    def create_metadata_widget(self):
        """Create custom widget for metadata in config"""

        # TODO: metadata label
        acquisition_properties = dict(self.acquisition.config['acquisition']['metadata'])
        metadata_widget = BaseDeviceWidget(acquisition_properties, acquisition_properties)
        metadata_widget.ValueChangedInside[str].connect(lambda name: self.acquisition.config['acquisition']['metadata'].
                                                        __setitem__(name, getattr(metadata_widget, name)))
        for name, widget in metadata_widget.property_widgets.items():
            widget.setToolTip('')  # reset tooltips
        metadata_widget.setWindowTitle(f'Metadata')
        metadata_widget.show()
        return metadata_widget

    def create_volume_model_widget(self):
        """Create widget to visualize acquisition grid"""

        specs = self.config['operation_widgets'].get('volume_model', {})
        kwds = specs.get('init', {})
        kwds['coordinate_plane'] = kwds.get('coordinate_plane', ['x', 'y', 'z'])
        self.volume_model = VolumeModel(**kwds)
        self.volume_model.fovMoved.connect(self.move_stage)
        self.volume_model.setMinimumHeight(333)
        self.volume_model.setMinimumWidth(333)
        self.volume_model.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # Add extra checkboxes/inputs/buttons to customize model
        volume_model_widget = QWidget()

        layout = QVBoxLayout()
        layout.addWidget(self.volume_model)

        checkboxes = QHBoxLayout()
        path = QCheckBox('Show Path')
        path.setChecked(True)
        path.toggled.connect(self.volume_model.toggle_path_visibility)
        checkboxes.addWidget(path)

        checkboxes.addWidget(QLabel('Plane View: '))
        view_plane = QButtonGroup(volume_model_widget)
        for view in ['(x, z)', '(z, y)', '(x, y)']:
            button = QRadioButton(view)
            button.clicked.connect(lambda clicked, b=button: setattr(self.volume_model, 'grid_plane',
                                                                     tuple(x for x in b.text() if x.isalpha())))
            view_plane.addButton(button)
            button.setChecked(True)
            checkboxes.addWidget(button)
        layout.addLayout(checkboxes)
        volume_model_widget.setLayout(layout)
        return volume_model_widget

    def create_scan_plan_widget(self):
        """Create scanplanwidget"""

        specs = self.config['operation_widgets'].get('scan_plan_widget', {})
        kwds = specs.get('init', {})

        (scan_name, scan_stage), = self.instrument.scanning_stages.items()
        with self.scanning_stage_locks[scan_name]:
            kwds['z_limits'] = scan_stage.limits_mm

        scan_plan_widget = ScanPlanWidget(**kwds)
        #scan_plan_widget.valueChanged
        return scan_plan_widget

    def create_tile_plan_widget(self):
        """Create widget to visualize acquisition grid"""

        specs = self.config['operation_widgets'].get('tile_plan_widget', {})
        kwds = specs.get('init', {})
        coordinate_plane = kwds.get('coordinate_plane', ['x', 'y', 'z'])

        # Populate limits
        limits = {}
        # add tiling stages
        for name, stage in self.instrument.tiling_stages.items():
            if stage.instrument_axis in coordinate_plane:
                with self.tiling_stage_locks[name]:
                    limits.update({f'{stage.instrument_axis}': stage.limits_mm})
        # last axis should be scanning axis
        (scan_name, scan_stage), = self.instrument.scanning_stages.items()
        with self.scanning_stage_locks[scan_name]:
            limits.update({f'{scan_stage.instrument_axis}': scan_stage.limits_mm})
        if len([i for i in limits.keys() if i in coordinate_plane]) != 3:
            raise ValueError('Coordinate plane must match instrument axes in tiling_stages')
        kwds['limits'] = [limits[coordinate_plane[0]], limits[coordinate_plane[1]], limits[coordinate_plane[2]]]

        tile_plan_widget = TilePlanWidget(**kwds)
        tile_plan_widget.fovStop.connect(self.stop_stage)

        # update z widgets when adding rows or columns to grid
        tile_plan_widget.valueChanged.connect(self.scan_plan_widget.z_plan_construction)
        tile_plan_widget.valueChanged.connect(lambda: setattr(self.volume_model, 'grid_coords',
                                                    [(x, y, z) for (x, y), z in zip(tile_plan_widget.tile_positions,
                                                                                self.volume_model.tile_z_dimensions)]))
        return tile_plan_widget

    # def grid_coord_construction(self, value=None):
    #     """Create current list of x,y,z of planned grid"""
    #
    #     if len(self.z_plan_widgets[0]) != 0:
    #         if self.apply_all.isChecked():
    #             z = self.z_plan_widgets[0][0].value()
    #             # TODO: update other tiles
    #             # set tile_z_dimension first so grid can render properly
    #             self.grid_view.tile_z_dimensions = [z[-1] - z[0]] * len(self.grid_plan.tile_positions)
    #             self.grid_view.tile_visibility = [True] * len(self.grid_plan.tile_positions)
    #             self.grid_view.grid_coords = [(x, y, z[0]) for x, y in self.grid_plan.tile_positions]
    #         else:
    #             tile_z_dimensions = []
    #             tile_xyz = []
    #             tile_visibility = []
    #             tile_xy = self.grid_plan.tile_positions
    #             for i, tile in enumerate(self.grid_plan.value().iter_grid_positions()):  # need to match row, col
    #                 x, y = tile_xy[i]
    #                 z = self.z_plan_widgets[tile.row][tile.col].value()
    #                 tile_xyz.append((x, y, z[0]))
    #                 tile_z_dimensions.append(z[-1] - z[0])
    #                 if not self.z_plan_widgets[tile.row][tile.col].hidden:
    #                     tile_visibility.append(True)
    #                 else:
    #                     tile_visibility.append(False)
    #             self.grid_view.tile_z_dimensions = tile_z_dimensions
    #             self.grid_view.grid_coords = tile_xyz
    #             self.grid_view.tile_visibility = tile_visibility

    def move_stage(self, fov_position):
        """Slot for moving stage when fov_position is changed internally by grid_widget"""

        stage_names = {stage.instrument_axis: name for name, stage in self.instrument.tiling_stages.items()}
        # Move stages
        for axis, position in zip(self.volume_model.coordinate_plane[:2], fov_position[:2]):
            with self.tiling_stage_locks[stage_names[axis]]:
                self.instrument.tiling_stages[stage_names[axis]].move_absolute_mm(position, wait=False)
        (scan_name, scan_stage), = self.instrument.scanning_stages.items()
        with self.scanning_stage_locks[scan_name]:
            scan_stage.move_absolute_mm(fov_position[2], wait=False)

    def stop_stage(self):
        """Slot for stop stage"""

        # TODO: Should we do this? I'm worried that halting is pretty time sensitive but pausing
        #  grab_fov_positions_worker shouldn't take too long
        self.grab_fov_positions_worker.pause()
        while not self.grab_fov_positions_worker.is_paused:
            sleep(.0001)
        for name, stage in {**getattr(self.instrument, 'scanning_stages', {}),
                            **getattr(self.instrument, 'tiling_stages', {})}.items():  # combine stage
            stage.halt()
        self.grab_fov_positions_worker.resume()

    def setup_fov_position(self):
        """Set up live position thread"""

        self.grab_fov_positions_worker = self.grab_fov_positions()
        self.grab_fov_positions_worker.yielded.connect(self.yield_fov_positions)
        self.grab_fov_positions_worker.start()

    @thread_worker
    def grab_fov_positions(self):
        """Grab stage position from all stage objects and yield positions"""

        while True:  # best way to do this or have some sort of break?
            sleep(.1)
            fov_pos = [None] * 2
            for name, stage in self.instrument.tiling_stages.items():
                with self.tiling_stage_locks[name]:
                    if stage.instrument_axis in self.tile_plan_widget.coordinate_plane:
                        fov_index = self.tile_plan_widget.coordinate_plane.index(stage.instrument_axis)
                        position = stage.position_mm
                        # FIXME: Sometimes tigerbox yields empty stage position so just give last position
                        fov_pos[fov_index] = position.get(stage.instrument_axis,
                                                          self.tile_plan_widget.fov_position[fov_index])
                (scan_name, scan_stage), = self.instrument.scanning_stages.items()
                with self.scanning_stage_locks[scan_name]:
                    position = scan_stage.position_mm
                    fov_pos.append(position.get(scan_stage.instrument_axis,
                                                self.tile_plan_widget.fov_position[-1]))
            yield fov_pos  # don't yield while locked

    def toggle_grab_fov_positions(self):
        """When focus on view has changed, resume or pause grabbing stage positions"""
        # TODO: Think about locking all device locks to make sure devices aren't being communicated with?
        # TODO: Update widgets with values from hardware? Things could've changed when using the acquisition widget
        try:
            if self.main_window.isActiveWindow() and self.grab_fov_positions_worker.is_paused:
                self.grab_fov_positions_worker.resume()
            elif not self.main_window.isActiveWindow() and self.grab_fov_positions_worker.is_running:
                self.grab_fov_positions_worker.pause()
        except RuntimeError:  # Pass error when window has been closed
            pass

    def yield_fov_positions(self, pos: list):
        """Correctly yield fov position to tiling widget, scan widget, and volume model """

        self.tile_plan_widget.fov_position = pos
        if not self.tile_plan_widget.anchor_widgets[2].isChecked():  # if not anchored in scan dir, update start pos
            self.scan_plan_widget.grid_position = pos[2]
        self.volume_model.fov_position = pos

    def create_operation_widgets(self, device_name: str, operation_name: str, operation_specs: dict):
        """Create widgets based on operation dictionary attributes from instrument or acquisition
         :param device_name: name of device correlating to operation
         :param operation_specs: dictionary describing set up of operation
         """

        operation_type = operation_specs['type']
        operation = getattr(self.acquisition, inflection.pluralize(operation_type))[device_name][operation_name]

        specs = self.config['operation_widgets'].get(device_name, {}).get(operation_name, {})
        if specs != {} and specs.get('type', '') == operation_type:
            gui_class = getattr(importlib.import_module(specs['driver']), specs['module'])
            gui = gui_class(operation, **specs.get('init', {}))  # device gets passed into widget
        else:
            properties = scan_for_properties(operation)
            gui = BaseDeviceWidget(type(operation), properties)  # create label

        # if gui is BaseDeviceWidget or inherits from it
        if type(gui) == BaseDeviceWidget or BaseDeviceWidget in type(gui).__bases__:
            # Hook up widgets to device_property_changed
            gui.ValueChangedInside[str].connect(
                lambda value, op=operation, widget=gui:
                self.operation_property_changed(value, op, widget))
        # Add label to gui
        labeled = create_widget('V', QLabel(operation_name), gui)

        # add ui to widget dictionary
        if not hasattr(self, f'{operation_type}_widgets'):
            setattr(self, f'{operation_type}_widgets', {device_name: {}})
        elif not getattr(self, f'{operation_type}_widgets').get(device_name, False):
            getattr(self, f'{operation_type}_widgets')[device_name] = {}
        getattr(self, f'{operation_type}_widgets')[device_name][operation_name] = labeled

        # TODO: Do we need this?
        for subdevice_name, suboperation_dictionary in operation_specs.get('subdevices', {}).items():
            for suboperation_name, suboperation_specs in suboperation_dictionary.items():
                self.create_operation_widgets(subdevice_name, suboperation_name, suboperation_specs)

        labeled.setWindowTitle(f'{device_name} {operation_type} {operation_name}')
        labeled.show()

    @Slot(str)
    def operation_property_changed(self, attr_name: str, operation, widget):
        """Slot to signal when operation widget has been changed
        :param widget: widget object relating to operation
        :param operation: operation object
        :param attr_name: name of attribute"""

        name_lst = attr_name.split('.')
        self.log.debug(f'widget {attr_name} changed to {getattr(widget, name_lst[0])}')
        value = getattr(widget, name_lst[0])
        try:  # Make sure name is referring to same thing in UI and operation
            dictionary = getattr(operation, name_lst[0])
            for k in name_lst[1:]:
                dictionary = dictionary[k]
            setattr(operation, name_lst[0], value)
            self.log.info(f'Device changed to {getattr(operation, name_lst[0])}')
            # Update ui with new operation values that might have changed
            # WARNING: Infinite recursion might occur if operation property not set correctly
            for k, v in widget.property_widgets.items():
                if getattr(widget, k, False):
                    operation_value = getattr(operation, k)
                    setattr(widget, k, operation_value)

        except (KeyError, TypeError) as e:
            self.log.warning(f"{attr_name} can't be mapped into operation properties due to {e}")
            pass

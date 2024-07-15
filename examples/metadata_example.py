from voxel.metadata.metadata_class import MetadataClass
from view.widgets.acquisition_widgets.metadata_widget import MetadataWidget
from view.widgets.base_device_widget import scan_for_properties
from qtpy.QtWidgets import QApplication
import sys
from qtpy.QtCore import Slot

@Slot(str)
def widget_property_changed(name, device, widget):
    """Slot to signal when widget has been changed
    :param name: name of attribute and widget"""

    name_lst = name.split('.')
    print('widget', name, ' changed to ', getattr(widget, name_lst[0]))
    value = getattr(widget, name_lst[0])
    setattr(device, name_lst[0], value)
    print('Device', name, ' changed to ', getattr(device, name_lst[0]))
    for k, v in widget.property_widgets.items():
        instrument_value = getattr(device, k)
        #print(k, instrument_value)
        #setattr(widget, k, instrument_value)


if __name__ == "__main__":
    app = QApplication(sys.argv)

    metadata_dictionary = {
        'instrument_type': 'simulated',
        'subject_id': 123456,
        'experimenter_name': 'Chris P. Bacon',
        'immersion_medium': '0.05XSSC',
        'immersion_medium_refractive_index': 1.33,
        'x_anatomical_direction': 'Anterior_to_posterior',
        'y_anatomical_direction': 'Inferior_to_superior',
        'z_anatomical_direction': 'Left_to_right'}

    datetime_format = 'year/month/day/hour/minute/second'

    name_specs = {
        'deliminator': '_',
        'format': ['instrument_type', 'subject_id']}

    metadata_class = MetadataClass(metadata_dictionary, datetime_format, name_specs)
    metadata_widget = MetadataWidget(metadata_class)

    metadata_widget.show()

    metadata_widget.ValueChangedInside[str].connect(
        lambda value, dev=metadata_class, widget=metadata_widget,: widget_property_changed(value, dev, widget))
    sys.exit(app.exec_())
"""OSP Connectivity QGIS plugin.

classFactory is the only QGIS entry point. Core modules do not import QGIS,
so they can be tested with the system Python.
"""


def classFactory(iface):
    from .plugin import OspConnectivityPlugin

    return OspConnectivityPlugin(iface)

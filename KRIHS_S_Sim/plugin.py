# -*- coding: utf-8 -*-
import os
from PyQt5.QtWidgets import QAction
from PyQt5.QtGui import QIcon
from qgis.core import QgsApplication

PLUGIN_NAME = 'KRIHS S-Sim'
_ICON = os.path.join(os.path.dirname(__file__), 'icon.png')


class PopAllocPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.action = None
        self.toolbar = None
        self.dialog = None

    def initGui(self):
        from .provider import PopAllocProvider
        self.provider = PopAllocProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

        self.action = QAction(
            QIcon(_ICON), PLUGIN_NAME, self.iface.mainWindow())
        self.action.triggered.connect(self._open_dialog)

        self.toolbar = self.iface.addToolBar(PLUGIN_NAME)
        self.toolbar.setObjectName('SimPopAllocKpiCarbonToolBar')
        self.toolbar.addAction(self.action)
        self.iface.addPluginToMenu(PLUGIN_NAME, self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginMenu(PLUGIN_NAME, self.action)
        if self.toolbar:
            self.toolbar.deleteLater()
        if self.dialog:
            self.dialog.deleteLater()
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)

    def _open_dialog(self):
        if self.dialog is None:
            from .dialog import PopAllocDialog
            self.dialog = PopAllocDialog(self.iface.mainWindow())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

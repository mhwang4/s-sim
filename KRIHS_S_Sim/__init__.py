# -*- coding: utf-8 -*-
def classFactory(iface):
    from .plugin import PopAllocPlugin
    return PopAllocPlugin(iface)

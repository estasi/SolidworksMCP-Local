"""
SolidworksMCP Package
----------------------
Model Context Protocol server for SolidWorks automation.

Version: 2026.10.18.6 (pre-release; native SW2025 document-target acceptance pending)
Author: Samsaam Ali Baig
"""

__version__ = "2026.10.18.6"
__author__ = "Samsaam Ali Baig"

def __getattr__(name):
    # Queue clients/journal tools must not import pywin32, load COM helpers or
    # instantiate automation. Preserve the original package's public imports.
    import importlib
    groups = {
        "automation": {"SolidWorksAutomation"},
        "config": {"get_config", "reload_config", "save_config", "SolidWorksConfig"},
        "constants": {"SwErrors", "SwPlanes", "SwDocumentTypes", "SwViews"},
        "utils": {"UnitConverter", "mm", "cm", "inch", "ft", "find_solidworks",
                  "find_template", "get_solidworks_info"},
    }
    for module, names in groups.items():
        if name in names:
            value = getattr(importlib.import_module(f".{module}", __name__), name)
            globals()[name] = value
            return value
    raise AttributeError(name)

__all__ = [
    # Version
    "__version__",
    "__author__",

    # Main class
    "SolidWorksAutomation",

    # Config
    "get_config",
    "reload_config",
    "save_config",
    "SolidWorksConfig",

    # Constants
    "SwErrors",
    "SwPlanes",
    "SwDocumentTypes",
    "SwViews",

    # Utils
    "UnitConverter",
    "mm", "cm", "inch", "ft",
    "find_solidworks",
    "find_template",
    "get_solidworks_info",
]

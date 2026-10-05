"""Local web dashboard (``papermap ui``): model settings, a library of generated
pages, and a background queue of runs. See :mod:`papermap.ui.server`."""

from .server import print_url, run_ui

__all__ = ["print_url", "run_ui"]

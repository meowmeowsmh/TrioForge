# paths.py – shared project-root helper.
import os

# PROJECT_ROOT is the folder that contains this "py/" directory.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def root_path(*parts):
    """Join PROJECT_ROOT with the given relative parts."""
    return os.path.join(PROJECT_ROOT, *parts)

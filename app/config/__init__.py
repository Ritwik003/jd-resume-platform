"""Application configuration.

``config.py`` holds the settings themselves and ``database.py`` the SQLAlchemy
engine and session factory. Both are re-exported here so the rest of the
codebase keeps importing ``from app.config import SETTING`` rather than caring
which file inside the package a given name lives in.
"""

from app.config.config import *  # noqa: F401,F403
from app.config.config import BASE_DIR  # noqa: F401  - re-export, not a star name

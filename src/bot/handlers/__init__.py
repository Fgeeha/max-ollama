"""Handler registration.

Importing a submodule registers its handlers on the shared ``Dispatcher``.
MAX dispatches an update to the FIRST matching handler, so IMPORT ORDER IS
HANDLER PRIORITY and must stay as written below: commands first, then image
attachments, then the plain-text catch-all in ``chat``. Do not let an import
sorter reorder these lines.
"""
# isort: skip_file
# ruff: noqa: E402, F401, I001
from bot.handlers import common
from bot.handlers import admin
from bot.handlers import models
from bot.handlers import chat
from bot.handlers import errors

__all__ = ["admin", "chat", "common", "errors", "models"]

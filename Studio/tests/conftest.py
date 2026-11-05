"""Keep the test process away from the user's managed DocuRandom data."""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import tempfile


_ORIGINAL_DOCURANDOM_HOME = os.environ.get("DOCURANDOM_HOME")
_TEST_HOME = tempfile.mkdtemp(prefix="docurandom-pytest-home-")
os.environ["DOCURANDOM_HOME"] = _TEST_HOME


def _close_test_file_handlers() -> None:
    test_home = os.path.normcase(os.path.abspath(_TEST_HOME))
    root_logger = logging.getLogger()
    for handler in tuple(root_logger.handlers):
        filename = getattr(handler, "baseFilename", None)
        if not filename:
            continue
        log_path = os.path.normcase(os.path.abspath(filename))
        if log_path.startswith(test_home + os.sep):
            root_logger.removeHandler(handler)
            handler.close()


def _restore_test_environment() -> None:
    _close_test_file_handlers()
    if _ORIGINAL_DOCURANDOM_HOME is None:
        os.environ.pop("DOCURANDOM_HOME", None)
    else:
        os.environ["DOCURANDOM_HOME"] = _ORIGINAL_DOCURANDOM_HOME
    shutil.rmtree(_TEST_HOME, ignore_errors=True)


def pytest_unconfigure(config: object) -> None:
    del config
    _restore_test_environment()


atexit.register(_restore_test_environment)

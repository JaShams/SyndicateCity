import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import PrivateBus  # noqa: E402


@pytest.fixture
def private_bus():
    if shutil.which("dbus-daemon") is None:
        pytest.skip("dbus-daemon not installed")
    d = tempfile.mkdtemp(prefix="syndicate-test-")
    bus = PrivateBus(d)
    yield bus
    bus.stop()
    shutil.rmtree(d, ignore_errors=True)

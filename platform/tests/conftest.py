import os

import pytest
from indic_platform.config import settings as _settings  # noqa: F401


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.getenv("LIVE_API_TESTS") != "1":
        for item in items:
            if "slow" in item.keywords:
                item.add_marker(
                    pytest.mark.skip(reason="Set LIVE_API_TESTS=1 for live vendor smoke")
                )

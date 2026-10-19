"""Real Home Assistant compatibility tests run separately from standalone stubs."""

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    return None

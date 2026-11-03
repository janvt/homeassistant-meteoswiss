"""The helpers of the test harness: pytest-home-assistant from Home Assistant
2026.11 on, pytest-homeassistant-custom-component before it."""

try:
    from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed
    from pytest_homeassistant.components.recorder.common import (
        async_wait_recording_done,
        do_adhoc_statistics,
    )
except ImportError:
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        async_fire_time_changed,
    )
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
        do_adhoc_statistics,
    )

__all__ = [
    "MockConfigEntry",
    "async_fire_time_changed",
    "async_wait_recording_done",
    "do_adhoc_statistics",
]

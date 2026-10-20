"""The Föhn outlook from the pressure difference, and MeteoSwiss's index."""

import pytest

from custom_components.meteoswiss import foehn


@pytest.mark.parametrize(
    ("difference", "expected"),
    [
        (0.0, "none"),
        (3.9, "none"),
        (4.0, "south"),  # from the threshold on
        (7.9, "south"),
        (8.0, "strong_south"),
        (12.5, "strong_south"),
        (-3.9, "none"),
        (-4.0, "north"),
        (-8.0, "strong_north"),
        (None, None),
    ],
)
def test_outlook(difference, expected):
    assert foehn.outlook(difference, 4.0, 8.0) == expected


def test_outlook_with_other_thresholds():
    assert foehn.outlook(6.0, 6.5, 10.0) == "none"
    assert foehn.outlook(6.0, 3.0, 5.5) == "strong_south"


@pytest.mark.parametrize(
    ("south", "north", "expected"),
    [
        (1016.0, 1010.0, 6.0),
        (1013.9, 1010.1, 3.8),
        (None, 1010.0, None),
        (1010.0, None, None),
    ],
)
def test_difference(south, north, expected):
    assert foehn.difference(south, north) == expected


@pytest.mark.parametrize(
    ("code", "expected"), [(0.0, "none"), (1.0, "mixed"), (2.0, "foehn"), (None, None)]
)
def test_station_index(code, expected):
    assert foehn.station_index(code) == expected

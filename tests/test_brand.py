"""The brand images Home Assistant shows for the integration."""

import struct
from pathlib import Path

BRAND = Path(__file__).parent.parent / "custom_components" / "meteoswiss" / "brand"
# Home Assistant serves only these names, and falls back to the icon for the
# others.
SIZES = {"icon.png": 256, "icon@2x.png": 512}


def test_icons_of_their_sizes():
    assert {p.name for p in BRAND.iterdir()} == set(SIZES)
    for name, size in SIZES.items():
        data = (BRAND / name).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", name
        # The width and height of the IHDR chunk.
        assert struct.unpack(">II", data[16:24]) == (size, size), name

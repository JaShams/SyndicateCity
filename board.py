"""Syndicate City board constants (PRD section 4.2).

Pure data shared by the coordinator and the client. Nothing in this module is
mutable game state.
"""

from dataclasses import dataclass
from typing import Optional

BOARD_SIZE = 12
START_TILE = 0
START_BONUS = 50
DISCOVERER_DISCOUNT = 15
DISTRICT_BONUS = 80          # net-worth bonus per completed district
MIN_BID_RATIO = (4, 5)       # minimum bid = ceil(0.8 * face_value)


@dataclass(frozen=True)
class Tile:
    index: int
    name: str
    category: str                  # "start" | "property" | "special"
    district: Optional[str] = None
    face_value: int = 0
    base_rent: int = 0
    cash_effect: int = 0           # special tiles only

    @property
    def is_property(self) -> bool:
        return self.category == "property"

    @property
    def min_bid(self) -> int:
        num, den = MIN_BID_RATIO
        return -(-self.face_value * num // den)  # integer ceil


TILES = (
    Tile(0, "START", "start"),
    Tile(1, "Dockyard", "property", "Blue", 160, 25),
    Tile(2, "Old Town", "property", "Blue", 160, 25),
    Tile(3, "Safehouse", "special", cash_effect=30),
    Tile(4, "Midtown", "property", "Green", 180, 30),
    Tile(5, "Financial", "property", "Green", 180, 30),
    Tile(6, "Checkpoint", "special", cash_effect=-40),
    Tile(7, "Industrial", "property", "Purple", 200, 35),
    Tile(8, "Warehouse", "property", "Purple", 200, 35),
    Tile(9, "Supply Drop", "special", cash_effect=30),
    Tile(10, "Tech Park", "property", "Red", 220, 40),
    Tile(11, "Uptown", "property", "Red", 220, 40),
)

assert len(TILES) == BOARD_SIZE and all(t.index == i for i, t in enumerate(TILES))

PROPERTY_IDS = tuple(t.index for t in TILES if t.is_property)
DISTRICTS = {}
for _t in TILES:
    if _t.is_property:
        DISTRICTS.setdefault(_t.district, []).append(_t.index)
DISTRICTS = {name: tuple(ids) for name, ids in DISTRICTS.items()}
del _t


def tile(index: int) -> Tile:
    return TILES[index]


def find_property(ref) -> Optional[int]:
    """Resolve a tile index or (case/space-insensitive) property name to an index."""
    if isinstance(ref, int):
        return ref if ref in PROPERTY_IDS else None
    text = str(ref).strip().lower()
    if text.isdigit():
        return find_property(int(text))
    key = text.replace(" ", "").replace("-", "").replace("_", "")
    for idx in PROPERTY_IDS:
        if TILES[idx].name.lower().replace(" ", "") == key:
            return idx
    return None

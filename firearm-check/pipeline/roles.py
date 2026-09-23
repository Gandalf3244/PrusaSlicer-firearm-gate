"""Functional role of a reference part, from its name. Used to report what
the shipped library covers (a frame is worth more than a rail riser) and by
`scripts/reliability.py`."""
from __future__ import annotations

import re

ROLES: list[tuple[str, str]] = [
    ("frame/receiver", r"receiver|frame|lower|\brec\b|\bupper\b|magwell|mag_?well|body|housing_mainspring|grip_?frame|shell|chassis|module|fcg|trigger_?group|trigger_?housing|pocket|buffer_?tower|assy|assembly"),
    ("slide", r"slide"),
    ("barrel", r"barrel|bbl|liner|chamber|insert"),
    ("bolt/carrier", r"\bbolt|carrier|breech|striker|firing_?pin|extractor|ejector"),
    ("cylinder", r"cylinder|pepperbox|index_?ring"),
    ("fire control", r"trigger|hammer|sear|disconnect|selector|safety|catch|release|lever|latch|plunger"),
    ("muzzle device", r"suppressor|silencer|moderator|baffle|flash|compensator|comp\b|muzzle|adapter"),
    ("stock/grip/handguard", r"stock|grip|handguard|hand_?guard|foregrip|fore_?grip|butt|cheek|brace|hand_?stop"),
    ("sight/rail", r"sight|rail|riser|picatinny|keymod|mount"),
    ("magazine", r"magazine|\bmag\b|follower|baseplate|floorplate|drum|feed"),
    ("spring/pin/screw", r"spring|\bpin\b|pins|screw|bolt_?head|detent|washer|spacer|peg|nut|roller|rod"),
]
_C = [(r, re.compile(p, re.I)) for r, p in ROLES]
CRITICAL = {"frame/receiver", "slide", "barrel", "bolt/carrier", "cylinder", "fire control"}


def role_of(part: str, model: str = "") -> str:
    text = part.replace("-", "_").replace(" ", "_")
    for r, rx in _C:
        if rx.search(text):
            return r
    text = model.replace("-", "_")
    for r, rx in _C:
        if rx.search(text):
            return r
    return "other"


_GRIP = re.compile(r"grip", re.I)
_GRIP_FRAME = re.compile(r"grip_?frame", re.I)


def is_grip(part: str, model: str = "") -> bool:
    """A grip (pistol grip, fore grip, grip panel): blocked only on an exact match."""
    text = part.replace("-", "_").replace(" ", "_")
    return role_of(part, model) == "stock/grip/handguard" and bool(_GRIP.search(text)) and not _GRIP_FRAME.search(text)


def same_part_type(path_a: str, path_b: str) -> bool:
    """Two barrels: a bore or rifling constellation shared by barrels of
    different families is part-type evidence (a barrel is a barrel), not a
    foreign-family label (the rifled Liberator barrel's grooves fire on the
    rifled AR-10 barrel)."""
    def role(p):
        parts = p.split("/")
        return role_of(parts[-1].rsplit(".", 1)[0], parts[1] if len(parts) > 2 else "")
    return role(path_a) == role(path_b) == "barrel"


_TOKEN = re.compile(r"[a-z]{5,}")


def same_kind(part_a: str, model_a: str, part_b: str, model_b: str) -> bool:
    """The same kind of part: same role (not the catch-all "other"), or a
    shared name word of 5+ letters ("charging" handle, "slide" stop)."""
    ra, rb = role_of(part_a, model_a), role_of(part_b, model_b)
    if ra == rb and ra != "other":
        return True
    ta = set(_TOKEN.findall(part_a.lower().replace("_", " ")))
    tb = set(_TOKEN.findall(part_b.lower().replace("_", " ")))
    return bool(ta & tb)

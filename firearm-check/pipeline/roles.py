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

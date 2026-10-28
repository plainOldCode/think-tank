"""Card state machine.

States match the design diagram. ``open`` is the unclaimed card, which the
diagram starts at the claim action. ``merged`` is the terminal merge node.
"""

from .errors import TurboError

STATES = (
    "open",
    "working",
    "question",
    "awaiting-human",
    "submit",
    "review",
    "merged",
)

# A runner that holds a card in any of these may not claim another.
BUSY = {"working", "question", "awaiting-human", "submit", "review"}

TRANSITIONS = {
    "open": {"working"},
    "working": {"question", "submit"},
    "question": {"awaiting-human"},
    "awaiting-human": {"working"},
    "submit": {"review"},
    "review": {"working", "merged"},
    "merged": set(),
}


def require(src: str, dst: str) -> None:
    if dst not in TRANSITIONS.get(src, ()):
        raise TurboError(409, f"cannot move {src} → {dst}")

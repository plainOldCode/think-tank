"""Picked defaults for decisions the design left open.

Question grace: if the turn is still alive 120 seconds after a question is
posted, the runner interrupts it and moves the card to awaiting-human.
Human default: if nobody answers for 24 hours, the card's blocked_default
is applied and a resume is queued.
"""

QUESTION_GRACE_S = 120
DEFAULT_ANSWER_S = 24 * 3600

"""Picked defaults for decisions the design left open.

Question grace: if the turn is still alive 120 seconds after a question is
posted, the runner sends SIGINT and moves the card to awaiting-human
immediately. The card does not wait for the child to exit.
Kill wait: if that child is still alive after KILL_WAIT_S, the next tick
sends SIGKILL. TURBO_KILL_WAIT_S overrides this and must be a number of
seconds.
Human default: if nobody answers for 24 hours, a question that is not
confirm applies blocked_default and queues a resume. confirm questions
never auto-apply.
"""

QUESTION_GRACE_S = 120
KILL_WAIT_S = 5
DEFAULT_ANSWER_S = 24 * 3600

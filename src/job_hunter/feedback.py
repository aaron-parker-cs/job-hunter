"""Feedback actions shared by the Telegram and Discord notifiers."""

from __future__ import annotations

import logging

from job_hunter.store import Store

log = logging.getLogger(__name__)

# action -> (button label, reaction emoji)
ACTIONS: dict[str, tuple[str, str]] = {
    "interested": ("Interested", "\N{THUMBS UP SIGN}"),
    "not_fit": ("Not a fit", "\N{THUMBS DOWN SIGN}"),
    "hide_company": ("Hide company", "\N{NO ENTRY}"),
    "applied": ("Applied", "\N{WHITE HEAVY CHECK MARK}"),
}
EMOJI_TO_ACTION = {emoji: action for action, (_, emoji) in ACTIONS.items()}


def apply_feedback(store: Store, job_id: str, action: str, source: str) -> bool:
    """Record feedback (and hide the company if asked). False if unknown job/action."""
    job = store.get_job(job_id)
    if job is None or action not in ACTIONS:
        log.warning("ignoring feedback for unknown job/action (%s)", action)
        return False
    store.add_feedback(job_id, action, source)
    if action == "hide_company":
        store.hide_company(job.company)
    return True


def revert_feedback(store: Store, job_id: str, action: str, source: str) -> bool:
    """Undo feedback, e.g. when a reaction is removed."""
    job = store.get_job(job_id)
    if job is None or action not in ACTIONS:
        return False
    store.remove_feedback(job_id, action, source)
    if action == "hide_company":
        store.unhide_company(job.company)
    return True

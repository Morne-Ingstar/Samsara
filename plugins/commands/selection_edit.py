"""Discovery seam for the Ava selection editor; implementation stays in ava_edit."""
from samsara.ava_edit.selection import register_commands
from samsara.ava_edit import draft_review

register_commands(__name__)
draft_review.register_commands(__name__)

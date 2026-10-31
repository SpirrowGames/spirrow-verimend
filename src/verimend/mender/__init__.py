"""mender (docs/design.md 5.4).

M3a: ``mend_stale`` drafts ``verdict.patch_draft`` for the stale verdicts of a
run, through Lexora's ``mender_tier``.
M3b: ``publish_prs`` turns the drafts into one branch and pull request per repository.
Escalations are not here yet.
"""

from verimend.mender.draft import (
    MendPass,
    MenderAborted,
    MenderConfigError,
    MenderSession,
    build_patch,
    mend_stage_failed,
    mend_stale,
    mender_from_settings,
)
from verimend.mender.publish import PrItem, PublishConfig, build_pr_body, publish_prs

__all__ = [
    "MendPass",
    "MenderAborted",
    "MenderConfigError",
    "MenderSession",
    "PrItem",
    "PublishConfig",
    "build_patch",
    "build_pr_body",
    "mend_stage_failed",
    "mend_stale",
    "mender_from_settings",
    "publish_prs",
]

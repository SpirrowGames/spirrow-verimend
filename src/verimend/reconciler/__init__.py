"""reconciler: claims against facts (docs/design.md section 5.3).

Two stages: ``reconcile`` (M2a) runs the deterministic ``port`` /
``parameter`` / ``command`` checkers over the structured claim subject;
``judge_deferred`` (M2c) hands every claim they left undecided to the LLM
judge, with BM25-retrieved facts.
"""

from verimend.reconciler.checkers import Decision, FactRow
from verimend.reconciler.llm import (
    JudgeAborted,
    JudgeConfigError,
    JudgeSession,
    judge_deferred,
    judge_from_settings,
    llm_stage_failed,
)
from verimend.reconciler.run import ReconcileResult, reconcile

__all__ = [
    "Decision",
    "FactRow",
    "JudgeAborted",
    "JudgeConfigError",
    "JudgeSession",
    "ReconcileResult",
    "judge_deferred",
    "judge_from_settings",
    "llm_stage_failed",
    "reconcile",
]

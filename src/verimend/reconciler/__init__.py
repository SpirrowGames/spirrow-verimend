"""reconciler: claims against facts (docs/design.md section 5.3).

M2a holds the deterministic stage only: ``port`` / ``parameter`` / ``command``
checkers over the structured claim subject. The LLM stage arrives later in M2.
"""

from verimend.reconciler.checkers import Decision, FactRow
from verimend.reconciler.run import ReconcileResult, reconcile

__all__ = ["Decision", "FactRow", "ReconcileResult", "reconcile"]

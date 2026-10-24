"""``python -m verimend.eval``: score the pipeline end to end on the golden set, through Lexora.

Scores the ``deterministic`` layer (extractor + checkers) and the ``final``
layer (+ the LLM judge). Needs ``VERIMEND_LEXORA_URL``. A configuration error
or any transient failure raises (no score is printed): see
``verimend.eval.e2e``. An empty golden set is an error, not an empty score.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence

from verimend.eval.e2e import run_e2e
from verimend.eval.golden import GOLDEN_DIR, Score, load_cases
from verimend.extractor import session_from_settings
from verimend.reconciler import judge_from_settings
from verimend.settings import get_settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m verimend.eval", description=__doc__.splitlines()[0])
    parser.add_argument("--case", action="append", metavar="ID", help="only this case (repeatable)")
    parser.add_argument("--root", default=str(GOLDEN_DIR), help="golden set directory")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    cases = load_cases(args.root)
    if not cases:
        print(f"no golden cases under {args.root}", file=sys.stderr)
        return 1
    if args.case:
        unknown = set(args.case) - {c.id for c in cases}
        if unknown:
            parser.error(f"unknown case(s): {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c.id in args.case]

    settings = get_settings()
    with session_from_settings(settings, strict=True) as session, judge_from_settings(settings, strict=True) as judge:
        results = [run_e2e(case, session, judge) for case in cases]
    for r in results:
        downgraded = (r.reconcile or {}).get("llm_downgraded", 0)
        print(f"{r.case_id}: failed_chunks={len(r.extract['failed_chunks'])} llm_downgraded={downgraded}")
        print(f"  deterministic {_summary(r.score)}")
        print(f"  final         {_summary(r.final_score)}")
    for layer, scores in (("deterministic", [r.score for r in results]), ("final", [r.final_score for r in results])):
        total = Score.combine(scores)
        print(f"TOTAL {layer} " + _summary(total))
        print(json.dumps({"by_type": {t: vars(c) for t, c in sorted(total.by_type.items())}}, indent=2))
    print(f"TOTAL llm_downgraded={sum((r.reconcile or {}).get('llm_downgraded', 0) for r in results)}")
    return 0


def _summary(s: Score) -> str:
    def pct(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.1%}"

    t = s.total
    return (
        f"stale P={pct(s.stale_precision)} R={pct(s.stale_recall)} "
        f"(tp={t.stale_tp} fp={t.stale_fp} fn={t.stale_fn})  "
        f"extraction R={pct(s.extraction_recall)} ({t.matched}/{t.gold})  agree={t.agree}  "
        f"extra={s.extra} non_claim_hits={s.non_claim_hits}"
    )


if __name__ == "__main__":
    raise SystemExit(main())

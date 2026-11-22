"""Lexora /v1/decide judgements around the mender (T06, thread T-decide-drift-score).

``routing`` holds the decision rules as pure functions; ``lexora`` is the
``/v1/decide`` adapter; ``stage`` runs the judgements inside the mender and
writes the ``decision`` rows; ``evaluate`` joins them to Lexora's decisions.db.
"""

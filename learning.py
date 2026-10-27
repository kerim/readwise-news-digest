"""Learn from grades which topics, sites, forms and angles you like.

A centered click-rate model: for each value (a topic, a site, a form, an angle)
compare how well its articles did with the average for that kind of value, and
emit a small score nudge. Because every nudge is measured against the average,
the whole field can never sink together.

How it works here:

* the signal is the grade, not a click: each counted item is one impression
  with a positive weight of (grade - 1) / 4, so 0 to 1;
* an item counts only once it is 30 to 120 days old and currently has a grade.
  "Left alone" verdicts only arrive at day 30 while positives arrive within
  days; counting positives early would make recent data look mostly positive;
* items are decayed by their tagged date with a true 30-day half-life;
* below GATE decayed counted items, no nudges: pure Haiku scores.
"""

from collections import Counter
from datetime import datetime, timezone

from common import atomic_write, days_between

MIN_AGE_DAYS = 30
MAX_AGE_DAYS = 120
HALF_LIFE_DAYS = 30
GATE = 50.0             # decayed counted items needed before any nudge
K = 30.0                # smoothing: pseudo-impressions pulling thin values to the mean
C = 0.1                 # fixed floor in the denominator; never use max(m, eps)
SCALE = 1.0
MAX_NUDGE = 2.0         # hard clamp on |delta| out of a 1-10 score axis

# Dimension name -> the tracked-entry field carrying its value.
DIMENSIONS = {
    "topics": "primary_topic",
    "sources": "host",
    "forms": "form",
    "frames": "frame",
}


def _empty(now):
    return {"generated": now.isoformat(), "topics": {}, "sources": {},
            "forms": {}, "frames": {}}


def counted_items(tracked, today):
    """[(entry, decay weight)] for graded items that are 30-120 days old."""
    counted = []
    for entry in tracked.values():
        if entry.get("grade") is None:
            continue
        age = days_between(entry["tagged_date"], today)
        if MIN_AGE_DAYS <= age <= MAX_AGE_DAYS:
            counted.append((entry, 0.5 ** (age / HALF_LIFE_DAYS)))
    return counted


def decayed_total(tracked, today):
    """Decayed counted items that carry at least one dimension: the number the
    gate looks at."""
    return sum(decay for entry, decay in counted_items(tracked, today)
               if any(entry.get(field) for field in DIMENSIONS.values()))


def compute_learned_weights(tracked, today, now=None):
    now = now or datetime.now(timezone.utc)
    weights = _empty(now)
    counted = counted_items(tracked, today)
    total = sum(decay for entry, decay in counted
                if any(entry.get(f) for f in DIMENSIONS.values()))
    weights["counted"] = len(counted)
    weights["decayed_total"] = round(total, 2)
    if total < GATE:
        return weights

    impressions = {dim: Counter() for dim in DIMENSIONS}
    positives = {dim: Counter() for dim in DIMENSIONS}
    for entry, decay in counted:
        positive = (entry["grade"] - 1) / 4
        for dim, field in DIMENSIONS.items():
            value = entry.get(field)
            if value:
                impressions[dim][value] += decay
                positives[dim][value] += decay * positive

    for dim in DIMENSIONS:
        total_imp = sum(impressions[dim].values())
        total_pos = sum(positives[dim].values())
        # The global gate does not protect an individually empty dimension
        # (say, every article had a blank frame).
        mean = (total_pos / total_imp) if total_imp > 0 else 0.0
        for value, imp in impressions[dim].items():
            rate = (positives[dim][value] + K * mean) / (imp + K)
            delta = SCALE * (rate - mean) / (mean + C)
            delta = max(-MAX_NUDGE, min(MAX_NUDGE, delta))
            if abs(delta) > 1e-9:
                weights[dim][value] = delta
    return weights


def update_learned_weights(paths, tracked, today):
    """Recompute and save the nudges the next tagging run will use."""
    weights = compute_learned_weights(tracked, today)
    atomic_write(paths.learned_weights, weights)
    return weights


def biggest_nudges(weights, n=5):
    """The n largest nudges in either direction, as (kind, value, delta)."""
    rows = [(dim, value, delta) for dim in DIMENSIONS
            for value, delta in weights.get(dim, {}).items()]
    return sorted(rows, key=lambda row: abs(row[2]), reverse=True)[:n]

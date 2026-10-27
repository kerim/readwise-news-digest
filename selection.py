"""Pick the day's digest: the best items under diversity caps, plus a few
random near-misses.

The near-misses matter for learning: if only the top 25 are ever tagged, the
script never finds out whether something it ranked just below the cut would
have been liked.
"""

import random

DIGEST_SIZE = 25
NEAR_MISS_COUNT = 3
NEAR_MISS_FIRST_RANK = 26   # near-misses come from ranks 26..60 of the
NEAR_MISS_LAST_RANK = 60    # above-junk pool, never from the 25 picked
MAX_PER_TOPIC = 3           # base diversity caps, widened when the pool is thin
MAX_PER_SOURCE = 3


def _ceil_div(a, b):
    return -(-a // b)


def select_digest(pool, rng=None):
    """Choose picks and near-misses from items already above the junk floor.

    Returns (chosen, stats). `chosen` is the picks in rank order followed by the
    near-misses; each item gets `rank` (1-based position in the pool) and
    `near_miss`. Fewer candidates than 28 simply gives fewer picks.
    """
    rng = rng or random.Random()
    ranked = sorted(pool, key=lambda item: item.get("score", 0), reverse=True)

    # Caps are adaptive so low topic/source diversity can't ceiling the
    # digest below DIGEST_SIZE.
    distinct_topics = len({it.get("primary_topic", "other") for it in ranked})
    distinct_sources = len({it.get("source", "") for it in ranked})
    per_topic_cap = max(MAX_PER_TOPIC,
                        _ceil_div(DIGEST_SIZE, max(1, distinct_topics)))
    per_source_cap = max(MAX_PER_SOURCE,
                         _ceil_div(DIGEST_SIZE, max(1, distinct_sources)))

    picks = []
    source_counts, topic_counts = {}, {}
    for item in ranked:
        source = item.get("source", "")
        topic = item.get("primary_topic", "other")
        if source_counts.get(source, 0) >= per_source_cap:
            continue
        if topic_counts.get(topic, 0) >= per_topic_cap:
            continue
        source_counts[source] = source_counts.get(source, 0) + 1
        topic_counts[topic] = topic_counts.get(topic, 0) + 1
        picks.append(item)
        if len(picks) >= DIGEST_SIZE:
            break

    picked_ids = {it["id"] for it in picks}
    near_pool = [it for it in ranked[NEAR_MISS_FIRST_RANK - 1:NEAR_MISS_LAST_RANK]
                 if it["id"] not in picked_ids]
    near_misses = rng.sample(near_pool, min(NEAR_MISS_COUNT, len(near_pool)))

    rank_of = {it["id"]: n for n, it in enumerate(ranked, start=1)}
    for item in picks:
        item["rank"] = rank_of[item["id"]]
        item["near_miss"] = False
    for item in near_misses:
        item["rank"] = rank_of[item["id"]]
        item["near_miss"] = True

    stats = {
        "pool": len(ranked),
        "picks": len(picks),
        "near_misses": len(near_misses),
        "per_topic_cap": per_topic_cap,
        "per_source_cap": per_source_cap,
    }
    return picks + near_misses, stats

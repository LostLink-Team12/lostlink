"""Low-cardinality search measurements; no query text, IDs, photos or tokens."""

from prometheus_client import Counter, Histogram


class SearchMetrics:
    def __init__(self, registry):
        self.phase_seconds = Histogram(
            "lostlink_search_phase_duration_seconds",
            "Search phase duration, including failures: encode, candidates, item_context.",
            ["phase"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 120),
            registry=registry,
        )
        self.item_context_calls = Counter(
            "lostlink_search_item_context_calls",
            "Item context calls attempted by interactive search.",
            registry=registry,
        )
        self.candidates = Histogram(
            "lostlink_search_candidates",
            "Candidates retrieved per search.",
            buckets=(0, 1, 5, 20, 50, 100),
            registry=registry,
        )
        self.results = Histogram(
            "lostlink_search_results",
            "Results returned by successful searches.",
            buckets=(0, 1, 5, 20, 50, 100),
            registry=registry,
        )

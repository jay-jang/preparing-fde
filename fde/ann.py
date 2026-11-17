"""Choose how much of a vector index to scan from the ACL selectivity, never the ACL itself.

Selectivity is allowed / rows of the index searched. Expected counts assume allowed
rows are spread independently of vector distance; real ACLs correlate with topic
(factory, equipment group), so recall is re-measured on labelled queries after every
change. The plan only changes how much of the index is visited. It never widens the
allowed set to recover recall. Defaults follow pgvector (hnsw.ef_search 40,
hnsw.iterative_scan off, hnsw.max_scan_tuples 20,000); they are inputs, not
performance guarantees.

pgvector counts hnsw.max_scan_tuples as graph elements visited, which is more than
the rows the scan returns. k / selectivity is therefore a lower bound on the visits
needed: above the budget, k is unreachable even on average; below it, k is still
not guaranteed (the scan can also stop at its memory limit).
"""
from dataclasses import dataclass
from fractions import Fraction
import math

EF_SEARCH_DEFAULT = 40            # pgvector: hnsw.ef_search, valid range 1..1000
MAX_SCAN_TUPLES_DEFAULT = 20_000  # pgvector: hnsw.max_scan_tuples (approximate)
EXACT_CAP = 5_000                 # fde.retrieval.retrieve(cap=5000)
MAX_SHORTFALL_DEFAULT = Fraction(1, 100)  # proposal: tolerated P(first scan < k)
STRATEGIES = ('exact', 'post_filter', 'iterative', 'partition')


def _integers(*values, minimum=1):
    if any(type(value) is not int or value < minimum for value in values):
        raise ValueError('invalid integer')


def shortfall(ef_search: int, allowed: int, rows: int, k: int) -> Fraction:
    """P(fewer than k allowed rows among ef_search candidates), X ~ Binomial(ef_search,
    allowed / rows). Exact integer arithmetic; same independence assumption as above."""
    _integers(ef_search, rows, k)
    _integers(allowed, minimum=0)
    if allowed > rows:
        raise ValueError('allowed rows exceed the index')
    rest = rows - allowed
    favourable = sum(math.comb(ef_search, i) * allowed**i * rest**(ef_search - i)
                     for i in range(min(k, ef_search + 1)))
    return Fraction(favourable, rows**ef_search)


@dataclass(frozen=True)
class SearchPlan:
    strategy: str
    allowed: int
    index_rows: int                   # rows of the index scanned: whole table or one partition
    selectivity: Fraction
    expected_matches: Fraction        # ef_search x selectivity: allowed rows in the first scan
    shortfall: Fraction | None        # P(first scan returns fewer than k allowed rows)
    expected_tuples: Fraction | None  # k / selectivity: rows the scan must return for k
    #                                   allowed rows; a lower bound on tuples visited
    iterative_scan: str               # value for SET LOCAL hnsw.iterative_scan
    ef_search: int
    max_scan_tuples: int

    def settings(self) -> dict:
        """SET LOCAL values for the transaction; the exact path uses no vector index."""
        if self.strategy == 'exact':
            return {}
        settings = {'hnsw.ef_search': self.ef_search, 'hnsw.iterative_scan': self.iterative_scan}
        if self.iterative_scan != 'off':
            settings['hnsw.max_scan_tuples'] = self.max_scan_tuples
        return settings


def plan_search(*, total: int, allowed: int, k: int, partition_rows: int | None = None,
                ef_search: int = EF_SEARCH_DEFAULT,
                max_scan_tuples: int = MAX_SCAN_TUPLES_DEFAULT,
                exact_cap: int = EXACT_CAP,
                max_shortfall: Fraction = MAX_SHORTFALL_DEFAULT) -> SearchPlan:
    """exact if allowed <= exact_cap; else scan the partition named by the filter, if any.
    Turn iterative scan off only when ef_search x selectivity >= k AND the first scan
    misses k with probability <= max_shortfall; otherwise use relaxed_order iterative
    scan while k / selectivity fits max_scan_tuples. Beyond that raise: partition or
    build a separate index.
    """
    _integers(total, k, ef_search, max_scan_tuples, exact_cap)
    _integers(allowed, minimum=0)
    if allowed > total or exact_cap > EXACT_CAP or ef_search > 1_000:
        raise ValueError('invalid search budget')
    if type(max_shortfall) is not Fraction or not 0 < max_shortfall < 1:
        raise ValueError('max_shortfall must be a Fraction in (0, 1)')
    if k > ef_search:
        # pgvector: results are limited by the dynamic candidate list (hnsw.ef_search).
        raise ValueError('k exceeds hnsw.ef_search')
    if partition_rows is not None:
        _integers(partition_rows)
        if not allowed <= partition_rows <= total:
            raise ValueError('allowed rows must lie inside the partition')
    rows = total if partition_rows is None else partition_rows
    selectivity = Fraction(allowed, rows)
    if allowed <= exact_cap:
        # fde.retrieval compares every allowed row: no candidate list, nothing to miss.
        return SearchPlan('exact', allowed, rows, selectivity, Fraction(allowed), None, None,
                          'off', ef_search, max_scan_tuples)
    expected = ef_search * selectivity
    miss = shortfall(ef_search, allowed, rows, k)
    tuples = k / selectivity
    if expected >= k and miss <= max_shortfall:
        mode = 'off'
    elif tuples <= max_scan_tuples:
        # pgvector: relaxed order gives better recall; re-sort candidates by distance before fusion.
        mode = 'relaxed_order'
    else:
        raise ValueError('k allowed rows not reachable within hnsw.max_scan_tuples: '
                         'partition by the filter or build a separate index; never widen the ACL')
    if partition_rows is not None:
        strategy = 'partition'
    else:
        strategy = 'post_filter' if mode == 'off' else 'iterative'
    return SearchPlan(strategy, allowed, rows, selectivity, expected, miss, tuples, mode,
                      ef_search, max_scan_tuples)

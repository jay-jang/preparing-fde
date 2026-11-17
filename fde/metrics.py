"""Retrieval metrics against span labels, NOT an evaluator of answers or a model.

Relevance labels name a span of one document version: (document, version,
section path prefix, page range) plus an integer grade. A retrieved chunk
matches a label when document and version are equal, the page ranges overlap
and the chunk's heading path equals the label section or lies under it. Labels
therefore survive a change of chunking strategy; chunk IDs never appear in them.
The label resolution is the metric resolution: a page-level label also accepts
a neighbouring paragraph on the same page of the same section. A chunk that
crosses sections matches only through the one heading path it records, and a
chunk tagged with a parent section never matches a child-section label, so
strategies that mix sections are scored conservatively.

- hit: 1 if any label is matched in the top k, else 0 (Hit@k).
- recall: labels matched by any of the top k chunks / all labels (Recall@k).
- rr: 1 / rank of the first matching chunk within k, else 0 (MRR per query).
- ndcg: DCG / ideal DCG with linear gain = grade and discount log2(rank + 1),
  the ideal DCG taken from the labels' own grades (as scikit-learn ndcg_score).
  Each rank earns the grade of at most one not-yet-credited label (highest
  grade first) and each label is credited once, at its first matching rank.
  Redundant chunks for an already found label earn nothing, so nDCG stays in
  [0, 1] whatever the chunk size; recall still counts every label a chunk covers.

Slices follow fde/release_gate.py ('factoid' = code-type, 'semantic'). Queries
without labels (unanswerable, forbidden) are scored by abstention gates instead.
paired_bootstrap() resamples query IDs with a seeded random.Random and returns a
percentile interval; it is a decision aid, not a significance test.
"""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
import random
import re

SLICES = ('factoid', 'semantic')
METRICS = ('hit', 'recall', 'rr', 'ndcg')
_DOC_ID = re.compile(r'[A-Za-z0-9._-]{1,120}')
_QUERY_ID = re.compile(r'[A-Za-z0-9._:-]{1,120}')


def _span(document, version, section, pages, *, section_required):
    if type(document) is not str or not _DOC_ID.fullmatch(document):
        raise ValueError('invalid document id')
    if type(version) is not int or version < 1:
        raise ValueError('version must be a positive int')
    if section is None and section_required:
        raise ValueError('a retrieved chunk must carry its heading path')
    if section is not None and (type(section) is not str or len(section) > 300):
        raise ValueError('section must be a bounded string')
    if (type(pages) is not tuple or len(pages) != 2 or any(type(p) is not int for p in pages)
            or not 1 <= pages[0] <= pages[1]):
        raise ValueError('pages must be (first, last) with 1 <= first <= last')


@dataclass(frozen=True)
class Label:
    document: str
    version: int
    section: str | None
    pages: tuple[int, int]
    grade: int = 1

    def __post_init__(self):
        _span(self.document, self.version, self.section, self.pages, section_required=False)
        if type(self.grade) is not int or not 1 <= self.grade <= 10:
            raise ValueError('grade must be an int in 1..10')


@dataclass(frozen=True)
class Retrieved:
    chunk_id: str
    document: str
    version: int
    section: str
    pages: tuple[int, int]

    def __post_init__(self):
        if type(self.chunk_id) is not str or not 1 <= len(self.chunk_id) <= 200:
            raise ValueError('invalid chunk id')
        _span(self.document, self.version, self.section, self.pages, section_required=True)

    def matches(self, label: Label) -> bool:
        if (self.document, self.version) != (label.document, label.version):
            return False
        if self.pages[1] < label.pages[0] or label.pages[1] < self.pages[0]:
            return False
        return (label.section is None or self.section == label.section
                or self.section.startswith(label.section + ' > '))


@dataclass(frozen=True)
class Scores:
    hit: int
    recall: float
    rr: float
    ndcg: float

    def __str__(self):
        return f'hit={self.hit} recall={self.recall:.4f} rr={self.rr:.4f} ndcg={self.ndcg:.4f}'


def _k(k):
    if type(k) is not int or not 1 <= k <= 100:
        raise ValueError('k must be an int in 1..100')


def score_query(ranked: Sequence[Retrieved], labels: Sequence[Label], k: int = 5) -> Scores:
    _k(k)
    if not isinstance(ranked, Sequence) or not isinstance(labels, Sequence):
        raise TypeError('ranked results and labels must be sequences')
    if any(type(r) is not Retrieved for r in ranked) or any(type(lb) is not Label for lb in labels):
        raise TypeError('expected Retrieved results and Label labels')
    if not labels:
        raise ValueError('a query without labels has no retrieval metric; use the abstention gate')
    if len(set(labels)) != len(labels):
        raise ValueError('duplicate label')
    if len({r.chunk_id for r in ranked}) != len(ranked):
        raise ValueError('duplicate chunk in one ranking')
    found, credited, gains, first = set(), set(), [], None
    for rank, result in enumerate(ranked[:k], 1):
        matched = [i for i, label in enumerate(labels) if result.matches(label)]
        if matched and first is None:
            first = rank
        found.update(matched)
        fresh = [i for i in matched if i not in credited]
        if fresh:
            best = max(fresh, key=lambda i: (labels[i].grade, -i))
            credited.add(best)
            gains.append(labels[best].grade / math.log2(rank + 1))
    ideal = sorted((label.grade for label in labels), reverse=True)[:k]
    idcg = math.fsum(grade / math.log2(rank + 1) for rank, grade in enumerate(ideal, 1))
    return Scores(hit=int(first is not None), recall=len(found) / len(labels),
                  rr=0.0 if first is None else 1 / first, ndcg=math.fsum(gains) / idcg)


@dataclass(frozen=True)
class Query:
    id: str
    kind: str
    labels: tuple[Label, ...]

    def __post_init__(self):
        if type(self.id) is not str or not _QUERY_ID.fullmatch(self.id):
            raise ValueError('invalid query id')
        if self.kind not in SLICES:
            raise ValueError(f'unknown slice: {self.kind!r}')
        if type(self.labels) is not tuple or not self.labels:
            raise ValueError('labels must be a non-empty tuple')


def score_run(queries: Sequence[Query], run: Mapping[str, Sequence[Retrieved]], k: int = 5):
    """Score every query; a missing or unknown query ID in the run fails closed."""
    if any(type(q) is not Query for q in queries) or not isinstance(run, Mapping):
        raise TypeError('expected Query objects and a run mapping query IDs to rankings')
    ids = [q.id for q in queries]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError('queries must be non-empty with unique IDs')
    if set(run) != set(ids):
        raise ValueError('run must contain exactly the evaluated query IDs')
    return {q.id: score_query(run[q.id], q.labels, k) for q in queries}


@dataclass(frozen=True)
class Summary:
    n: int
    hit_rate: float
    recall: float
    mrr: float
    ndcg: float


def summarize(queries: Sequence[Query], scores: Mapping[str, Scores]) -> dict[str, Summary]:
    """Per-slice means plus 'all'. Gate on slices; 'all' is reported, never gated alone."""
    if (any(type(q) is not Query for q in queries) or not isinstance(scores, Mapping)
            or any(type(s) is not Scores for s in scores.values())):
        raise TypeError('expected Query objects and a mapping of Scores')
    if {q.id for q in queries} != set(scores) or len(queries) != len(scores):
        raise ValueError('scores must cover exactly the queries')
    groups = {'all': list(queries)}
    for kind in SLICES:
        groups[kind] = [q for q in queries if q.kind == kind]
    result = {}
    for name, group in groups.items():
        if not group:
            continue
        rows = [scores[q.id] for q in group]
        result[name] = Summary(
            n=len(rows), hit_rate=sum(s.hit for s in rows) / len(rows),
            recall=math.fsum(s.recall for s in rows) / len(rows),
            mrr=math.fsum(s.rr for s in rows) / len(rows),
            ndcg=math.fsum(s.ndcg for s in rows) / len(rows))
    return result


@dataclass(frozen=True)
class Comparison:
    n: int
    confidence: float
    mean_difference: float
    low: float
    high: float

    @property
    def verdict(self) -> str:
        if self.low > 0:
            return 'better'
        if self.high < 0:
            return 'worse'
        return 'inconclusive'

    def __str__(self):
        return (f'n={self.n} diff={self.mean_difference:+.4f} {self.confidence:.0%} '
                f'[{self.low:+.4f}, {self.high:+.4f}] {self.verdict}')


def paired_bootstrap(baseline: Mapping[str, float], candidate: Mapping[str, float], *,
                     resamples: int = 9999, confidence: float = 0.95, seed: int = 0) -> Comparison:
    """Candidate minus baseline over the SAME query IDs, resampling query IDs jointly.

    The interval is the percentile interval: sort the resampled means and drop
    tail = round(resamples * (1 - confidence) / 2) values at each end.
    """
    if not isinstance(baseline, Mapping) or not isinstance(candidate, Mapping):
        raise TypeError('per-query values must be mappings keyed by query ID')
    if set(baseline) != set(candidate) or len(baseline) < 2:
        raise ValueError('both runs must cover the same two or more query IDs')
    if type(resamples) is not int or not 1000 <= resamples <= 100_000:
        raise ValueError('resamples must be an int in 1000..100000')
    if type(confidence) is not float or not 0.5 <= confidence < 1:
        raise ValueError('confidence must be a float in [0.5, 1)')
    if type(seed) is not int:
        raise ValueError('seed must be an int')
    ids = sorted(baseline)
    values = [(baseline[i], candidate[i]) for i in ids]
    if any(type(v) not in (int, float) or not math.isfinite(v) for pair in values for v in pair):
        raise ValueError('per-query values must be finite numbers')
    differences = [c - b for b, c in values]
    n = len(differences)
    rng = random.Random(seed)
    means = sorted(math.fsum(differences[rng.randrange(n)] for _ in range(n)) / n
                   for _ in range(resamples))
    tail = round(resamples * (1 - confidence) / 2)
    return Comparison(n=n, confidence=confidence, mean_difference=math.fsum(differences) / n,
                      low=means[tail], high=means[resamples - 1 - tail])

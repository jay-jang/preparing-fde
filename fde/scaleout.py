"""Project per-factory pilot numbers to a scale-out and rank resources by headroom.

Arithmetic over stated assumptions, NOT a capacity guarantee. Every limit carries
its basis: 'design' (a limit the code enforces), 'derived' (computed by
fde.capacity from assumptions) or 'assumption' (a budget to replace with a
per-component load test or a customer number). Components break at different
points, so the ranking is trusted only after the assumption limits are measured.
"""
from dataclasses import dataclass
from fractions import Fraction
import math

from fde.capacity import gpu_plan, in_flight_requests, vector_bytes

# fde.retrieval.retrieve(cap=5000): the exact pilot path; a unit test keeps them equal.
EXACT_SEARCH_CAP = 5_000
BASES = ('design', 'derived', 'assumption')
MINUTES_PER_DAY = 1_440


def _integers(*values, minimum=1):
    if any(type(value) is not int or value < minimum for value in values):
        raise ValueError('invalid integer')


@dataclass(frozen=True)
class Pilot:
    """One factory's pilot numbers, measured or assumed."""
    chunks: int
    dims: int
    bytes_per_value: int
    widest_allowed_chunks: int      # allowed chunks of the broadest role in one request
    burst_changes_per_second: int
    users: int
    questions_per_user_per_minute: int
    latency_seconds: float          # mean time in system W for Little's law
    audit_bytes_per_request: int

    def __post_init__(self):
        _integers(self.chunks, self.dims, self.bytes_per_value, self.widest_allowed_chunks,
                  self.burst_changes_per_second, self.users,
                  self.questions_per_user_per_minute, self.audit_bytes_per_request)
        if self.widest_allowed_chunks > self.chunks:
            raise ValueError('allowed chunks exceed the corpus')
        # Validates latency_seconds with the same rules as fde.capacity.
        in_flight_requests(1, self.latency_seconds)


@dataclass(frozen=True)
class Limits:
    gpu_sequences: int              # derived: gpu_sequence_limit(...)
    vector_bytes: int               # assumption: memory budget for raw vectors
    ingest_per_second: int          # assumption: measured pipeline throughput
    audit_bytes_per_day: int        # assumption: agreed audit storage per day
    exact_search_chunks: int = EXACT_SEARCH_CAP

    def __post_init__(self):
        _integers(self.gpu_sequences, self.vector_bytes, self.ingest_per_second,
                  self.audit_bytes_per_day, self.exact_search_chunks)
        if self.exact_search_chunks > EXACT_SEARCH_CAP:
            raise ValueError('the exact path enforces at most 5,000 allowed chunks')


@dataclass(frozen=True)
class Projection:
    resource: str
    unit: str
    pilot: int
    projected: int
    limit: int
    basis: str

    def __post_init__(self):
        _integers(self.pilot, self.projected, self.limit)
        if self.basis not in BASES:
            raise ValueError('unknown limit basis')

    @property
    def utilization(self) -> Fraction:
        return Fraction(self.projected, self.limit)

    @property
    def headroom(self) -> int:
        return self.limit - self.projected


def gpu_sequence_limit(*, gpus, gpu_bytes, utilization, weights_bytes, overhead_bytes,
                       kv_bytes_per_token, context_tokens) -> int:
    """Sequences at full context that fit in memory, per fde.capacity.gpu_plan, times GPUs."""
    _integers(gpus)
    plan = gpu_plan(gpu_bytes=gpu_bytes, utilization=utilization, weights_bytes=weights_bytes,
                    overhead_bytes=overhead_bytes, kv_bytes_per_token=kv_bytes_per_token,
                    context_tokens=context_tokens, sequences=1)
    return plan.max_sequences * gpus


def _sequences(pilot, factories):
    per_minute = pilot.users * factories * pilot.questions_per_user_per_minute
    in_flight_requests(per_minute / 60, pilot.latency_seconds)  # same validation as 5-3
    # Ceil of the mean L, exact: a float product such as 1,000/60 x 7.5 lands just above
    # 125 and would round up to 126. latency is read as the decimal it was written as.
    # L includes retrieval-stage requests (overstates GPU decodes) but is a mean, not a peak.
    return math.ceil(Fraction(per_minute, 60) * Fraction(str(pilot.latency_seconds)))


def _audit_bytes(pilot, factories):
    # Upper bound: the assumed per-user rate held for 24 hours.
    requests = pilot.users * factories * pilot.questions_per_user_per_minute * MINUTES_PER_DAY
    return requests * pilot.audit_bytes_per_request


def project(pilot: Pilot, limits: Limits, *, factories: int,
            allowed_growth: int) -> tuple[Projection, ...]:
    """Scale corpus, change bursts and users by factories (linear, coincident bursts).

    allowed_growth multiplies the broadest role's allowed set: 1 keeps factory-local
    roles, factories means one role (e.g. headquarters) reads every factory.
    Sorted by utilization, highest first: the first row is the first bottleneck.
    """
    if not isinstance(pilot, Pilot) or not isinstance(limits, Limits):
        raise TypeError('pilot and limits must be Pilot and Limits')
    _integers(factories, allowed_growth)
    if allowed_growth > factories:
        raise ValueError('allowed set cannot grow faster than the factories it spans')
    rows = (
        Projection('exact_search', 'chunks', pilot.widest_allowed_chunks,
                   pilot.widest_allowed_chunks * allowed_growth,
                   limits.exact_search_chunks, 'design'),
        Projection('raw_vectors', 'B',
                   vector_bytes(pilot.chunks, pilot.dims, pilot.bytes_per_value),
                   vector_bytes(pilot.chunks * factories, pilot.dims, pilot.bytes_per_value),
                   limits.vector_bytes, 'assumption'),
        Projection('ingest_burst', 'changes/s', pilot.burst_changes_per_second,
                   pilot.burst_changes_per_second * factories,
                   limits.ingest_per_second, 'assumption'),
        Projection('gpu_sequences', 'sequences', _sequences(pilot, 1),
                   _sequences(pilot, factories), limits.gpu_sequences, 'derived'),
        Projection('audit_log', 'B/day', _audit_bytes(pilot, 1), _audit_bytes(pilot, factories),
                   limits.audit_bytes_per_day, 'assumption'),
    )
    return tuple(sorted(rows, key=lambda row: (-row.utilization, row.resource)))

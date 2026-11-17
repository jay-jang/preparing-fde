"""Canary verdicts from counted request outcomes, using Wilson score intervals.

Each watched rate (errors, abstentions) is a binomial proportion events/total.
Its Wilson interval (NIST/SEMATECH e-Handbook 7.2.4.1) stays inside [0, 1],
unlike the Wald interval p +- z*sqrt(p(1-p)/n). The baseline is the control
population observed over the SAME window, never an earlier period.

The rules compare intervals conservatively; they are not a p-value and not a
sequential test. Looking after every request inflates false verdicts, so the
caller evaluates only at pre-agreed observation checkpoints. All thresholds are
proposals agreed with the customer. A security failure is not a statistic: one
ACL leak rolls back at once, whatever the counts.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
import math
from statistics import NormalDist
from types import MappingProxyType

Z95 = NormalDist().inv_cdf(0.975)      # two-sided 95%: 1.959963984540054
ACTIONS = ('continue', 'promote', 'rollback')


def _count(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError('counts must be integers')


def _z(value):
    if type(value) is not float or not math.isfinite(value) or value <= 0:
        raise ValueError('z must be a finite positive float')


def _rate(value):
    if type(value) is not float or not 0 < value < 1:
        raise ValueError('rates must be floats in (0, 1)')


def z_value(confidence, *, two_sided=True) -> float:
    """Standard normal quantile for a confidence level such as 0.95."""
    _rate(confidence)
    tail = (1 - confidence) / 2 if two_sided else 1 - confidence
    return NormalDist().inv_cdf(1 - tail)


@dataclass(frozen=True)
class Interval:
    events: int
    total: int
    low: float
    high: float

    @property
    def rate(self) -> float:
        return self.events / self.total


def wilson(events, total, *, z=Z95) -> Interval:
    """Wilson score interval; 0 events gives low 0.0, all events gives high 1.0."""
    _count(events)
    _count(total, minimum=1)
    _z(z)
    if events > total:
        raise ValueError('events exceed total')
    p, z2 = events / total, z * z
    denominator = 1 + z2 / total
    center = (p + z2 / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total)) / denominator
    low = 0.0 if events == 0 else max(0.0, center - half)
    high = 1.0 if events == total else min(1.0, center + half)
    return Interval(events, total, low, high)


@dataclass(frozen=True)
class Limit:
    """ceiling: roll back when the canary LOWER bound exceeds it (absolute measure).
    promote_below: promote only when the canary UPPER bound is below it."""
    ceiling: float
    promote_below: float

    def __post_init__(self):
        _rate(self.ceiling)
        _rate(self.promote_below)
        if self.promote_below > self.ceiling:
            raise ValueError('promote_below must not exceed the ceiling')


@dataclass(frozen=True)
class Policy:
    limits: Mapping            # metric name -> Limit
    min_observations: int      # per population, before any statistical verdict
    max_observations: int      # canary still inconclusive here -> roll back
    z: float = Z95

    def __post_init__(self):
        if not isinstance(self.limits, Mapping) or not self.limits:
            raise ValueError('at least one metric limit is required')
        if any(type(name) is not str or not name or type(limit) is not Limit
               for name, limit in self.limits.items()):
            raise ValueError('limits map metric names to Limit')
        _count(self.min_observations, minimum=1)
        _count(self.max_observations, minimum=1)
        if self.max_observations < self.min_observations:
            raise ValueError('max_observations below min_observations')
        _z(self.z)
        object.__setattr__(self, 'limits', MappingProxyType(dict(self.limits)))


@dataclass(frozen=True)
class Verdict:
    action: str
    reasons: tuple
    intervals: Mapping         # metric -> (canary Interval, baseline Interval)


def _counts(observed, names, label):
    if not isinstance(observed, Mapping) or set(observed) != names:
        raise ValueError(f'{label} metrics must match the policy exactly')
    pairs = {}
    for name in sorted(names):
        pair = observed[name]
        if type(pair) is not tuple or len(pair) != 2:
            raise ValueError(f'{label} {name} must be (events, total)')
        _count(pair[0])
        _count(pair[1])
        if pair[0] > pair[1]:
            raise ValueError(f'{label} {name}: events exceed total')
        pairs[name] = pair
    return pairs


def decide(canary, baseline, policy, *, leaks=0) -> Verdict:
    """canary/baseline: {metric: (events, total)} counted over the same window."""
    if type(policy) is not Policy:
        raise TypeError('policy must be a Policy')
    _count(leaks)
    names = set(policy.limits)
    canary, baseline = _counts(canary, names, 'canary'), _counts(baseline, names, 'baseline')
    if leaks:
        return Verdict('rollback', (f'acl leak: {leaks}',), {})
    short = sorted(name for name in names
                   if min(canary[name][1], baseline[name][1]) < policy.min_observations)
    if short:
        reasons = tuple(f'{name}: insufficient observations' for name in short)
        return Verdict('continue', reasons, {})
    intervals, rollback, pending = {}, [], []
    for name in sorted(names):
        limit = policy.limits[name]
        test = wilson(*canary[name], z=policy.z)
        control = wilson(*baseline[name], z=policy.z)
        intervals[name] = (test, control)
        if test.low > limit.ceiling:
            rollback.append(f'{name}: lower bound above ceiling')
        if test.low > control.high:
            rollback.append(f'{name}: worse than baseline')
        if test.high >= limit.promote_below:
            pending.append(f'{name}: upper bound not below promote threshold')
    if rollback:
        return Verdict('rollback', tuple(rollback), intervals)
    if not pending:
        return Verdict('promote', (), intervals)
    if min(pair[1] for pair in canary.values()) >= policy.max_observations:
        reasons = ('inconclusive at max observations',) + tuple(pending)
        return Verdict('rollback', reasons, intervals)
    return Verdict('continue', tuple(pending), intervals)


def observations_needed(threshold, *, events=0, z=Z95, cap=100_000_000) -> int:
    """Smallest total whose Wilson upper bound with `events` is below threshold.

    The upper bound falls monotonically as total grows with events fixed, so a
    doubling-then-bisection search is exact. Raises when even cap is not enough.
    """
    _rate(threshold)
    _count(events)
    _z(z)
    _count(cap, minimum=1)

    def passes(total):
        return wilson(events, total, z=z).high < threshold

    low, high = max(events, 1), max(events, 1)
    while not passes(high):
        if high >= cap:
            raise ValueError('threshold not reachable within cap observations')
        low, high = high + 1, min(cap, high * 2)
    while low < high:
        middle = (low + high) // 2
        if passes(middle):
            high = middle
        else:
            low = middle + 1
    return high


def window_hours(observations, requests_per_hour, canary_percent) -> Fraction:
    """Hours of traffic until the canary share has seen `observations` requests."""
    _count(observations, minimum=1)
    _count(requests_per_hour, minimum=1)
    _count(canary_percent, minimum=1)
    if canary_percent > 100:
        raise ValueError('canary_percent must be 1..100')
    return Fraction(observations * 100, requests_per_hour * canary_percent)

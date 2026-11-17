"""Operational handover readiness from dated evidence, NOT an operations review.

The FDE team hands the service to the customer's operations owner (4-1 D10)
only when every blocking requirement has evidence with a named owner, a date
and a reference to the artifact (review ticket, drill report, roster, decision
record). A claim without evidence counts as missing. A requirement with a
limit also needs a measured value at or below it, e.g. the restore drill's
recovery time in minutes against the proposed RTO of 60 minutes (4-3).

Freshness: age = assessment date - evidence date in days. Age above
max_age_days is stale; evidence dated after the assessment is future-dated.
Gaps of non-blocking requirements are warnings. Whether a drill ran on the
right environment and release is a reviewer's judgement that code cannot make.
Windows, limits and the blocking split are proposals to agree with the
customer's operations owner and record as decisions (5-4); accepting a
blocking gap because a mitigation exists is such a decision, not a flag here.
"""
from dataclasses import dataclass
from datetime import date
import re

REASONS = ('missing', 'no_owner', 'undated', 'no_reference', 'future_dated', 'stale',
           'unmeasured', 'over_limit')
_KEY = re.compile(r'[a-z][a-z0-9_]{0,39}')
_OWNER = re.compile(r'[a-z][a-z0-9_-]{0,39}')            # role names as in fde/decisions.py
_REFERENCE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}')


def _key(value):
    if type(value) is not str or not _KEY.fullmatch(value):
        raise ValueError('invalid requirement key')


def _count(value, minimum, what):
    if type(value) is not int or value < minimum:
        raise ValueError(f'{what} must be an integer >= {minimum}')


@dataclass(frozen=True)
class Requirement:
    key: str
    blocking: bool
    max_age_days: int
    limit: int | None = None          # measured value must be <= limit

    def __post_init__(self):
        _key(self.key)
        if type(self.blocking) is not bool:
            raise ValueError('blocking must be a bool')
        _count(self.max_age_days, 1, 'max_age_days')
        if self.limit is not None:
            _count(self.limit, 0, 'limit')


@dataclass(frozen=True)
class Evidence:
    key: str
    owner: str | None
    collected_on: date | None
    reference: str | None
    measured: int | None = None

    def __post_init__(self):
        _key(self.key)
        if self.owner is not None and (type(self.owner) is not str
                                       or not _OWNER.fullmatch(self.owner)):
            raise ValueError('invalid owner')
        if self.collected_on is not None and type(self.collected_on) is not date:
            raise ValueError('collected_on must be a date')
        if self.reference is not None and (type(self.reference) is not str
                                           or not _REFERENCE.fullmatch(self.reference)):
            raise ValueError('invalid reference')
        if self.measured is not None:
            _count(self.measured, 0, 'measured')


# Proposals (제안값): windows in days; restore_drill limit is RTO minutes (4-3).
DEFAULT_REQUIREMENTS = (
    Requirement('runbook_review', True, 180),
    Requirement('alert_drill', True, 90),          # page reached on-call in a drill (4-3)
    Requirement('restore_drill', True, 90, limit=60),
    Requirement('oncall_roster', True, 30),
    Requirement('access_review', True, 90),
    Requirement('error_budget_policy', True, 365),  # agreed policy (4-5)
    Requirement('known_issues', True, 30),
    Requirement('dashboards', False, 180),
)


@dataclass(frozen=True)
class Readiness:
    ready: bool
    blocking: tuple[tuple[str, str], ...]
    warnings: tuple[tuple[str, str], ...]


def _reasons(requirement, item, on):
    if item is None:
        return ['missing']
    reasons = []
    if item.owner is None:
        reasons.append('no_owner')
    if item.reference is None:
        reasons.append('no_reference')
    if item.collected_on is None:
        reasons.append('undated')
    elif item.collected_on > on:
        reasons.append('future_dated')
    elif (on - item.collected_on).days > requirement.max_age_days:
        reasons.append('stale')
    if requirement.limit is not None:
        if item.measured is None:
            reasons.append('unmeasured')
        elif item.measured > requirement.limit:
            reasons.append('over_limit')
    return reasons


def assess(requirements, evidence, on) -> Readiness:
    """Ready only when no blocking requirement has a gap; every gap is listed."""
    if type(on) is not date:
        raise ValueError('on must be a date')
    if (type(requirements) is not tuple or not requirements
            or any(type(r) is not Requirement for r in requirements)):
        raise ValueError('requirements must be a non-empty tuple of Requirement')
    if type(evidence) is not tuple or any(type(e) is not Evidence for e in evidence):
        raise ValueError('evidence must be a tuple of Evidence')
    keys = [r.key for r in requirements]
    if len(set(keys)) != len(keys):
        raise ValueError('duplicate requirement')
    by_key = {}
    for item in evidence:
        if item.key not in keys:
            raise ValueError(f'evidence for unknown requirement: {item.key}')
        if item.key in by_key:
            raise ValueError(f'duplicate evidence: {item.key}')
        by_key[item.key] = item
    blocking, warnings = [], []
    for requirement in requirements:
        target = blocking if requirement.blocking else warnings
        for reason in _reasons(requirement, by_key.get(requirement.key), on):
            target.append((requirement.key, reason))
    return Readiness(not blocking, tuple(sorted(blocking)), tuple(sorted(warnings)))

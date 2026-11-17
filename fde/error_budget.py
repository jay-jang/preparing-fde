"""Error budget arithmetic and an agreed release policy, in exact fractions.

SLO targets are decimal strings such as '0.999', never floats, so a 30-day
budget is exactly 43.2 minutes. The burn-rate rules are the multiwindow,
multi-burn-rate starting point of the Google SRE workbook (Alerting on SLOs,
Table 5-8): 2% of the budget in 1 h, 5% in 6 h, 10% in 3 days, each with a
short window of 1/12. The 14.4 / 6 / 1 thresholds are DERIVED from those
fractions and the SLO window, so another window gives other thresholds.
deploy/alerts.yaml implements the first rule; a unit test keeps them equal.

Policy states: normal; slow (proposal: half the budget spent, one change per
release and twice the canary observations of fde.canary); freeze (budget
exceeded: only P0 fixes and security fixes, as in the workbook's example
error budget policy; an ordinary 'fix' waits). Budget spent is counted either
in bad minutes or, for a request-based SLI such as 4-3 availability, in bad
events over the window's valid events. The thresholds are agreed with the
customer, not facts.
"""
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
import re

MINUTES_PER_DAY = 1_440
MIN_REQUESTS = 100        # deploy/alerts.yaml: sum(increase(rag_requests_total[1h])) >= 100
CHANGE_KINDS = ('feature', 'config', 'fix', 'p0_fix', 'security')
STATES = {                # state -> (allowed change kinds, canary scale, changes per release)
    'normal': (CHANGE_KINDS, 1, None),
    'slow': (CHANGE_KINDS, 2, 1),
    'freeze': (('p0_fix', 'security'), 1, None),
}
_DECIMAL = re.compile(r'\d+(?:\.\d+)?')


def _exact(value, name):
    """Accept int, Fraction or a plain decimal string; floats are rejected, not rounded."""
    if type(value) is int or type(value) is Fraction:
        return Fraction(value)
    if type(value) is str:
        if not _DECIMAL.fullmatch(value):
            raise ValueError(f'{name} is not a plain decimal number')
        return Fraction(value)
    raise TypeError(f'{name} must be int, Fraction or a decimal string')


def _non_negative(value, name):
    exact = _exact(value, name)
    if exact < 0:
        raise ValueError(f'{name} must not be negative')
    return exact


@dataclass(frozen=True)
class Slo:
    target: Fraction
    days: int

    def __post_init__(self):
        target = _exact(self.target, 'target')
        if not 0 < target < 1:
            raise ValueError('target must be between 0 and 1')
        if type(self.days) is not int or not 1 <= self.days <= 366:
            raise ValueError('days must be an integer in 1..366')
        object.__setattr__(self, 'target', target)

    @property
    def budget_ratio(self) -> Fraction:
        return 1 - self.target

    @property
    def window_minutes(self) -> int:
        return self.days * MINUTES_PER_DAY

    @property
    def budget_minutes(self) -> Fraction:
        """Minutes of total outage the window allows."""
        return self.window_minutes * self.budget_ratio

    def budget_events(self, total) -> Fraction:
        """Bad events the window allows for `total` valid events."""
        return _non_negative(total, 'total') * self.budget_ratio

    def consumed_minutes(self, bad_minutes) -> Fraction:
        bad_minutes = _non_negative(bad_minutes, 'bad_minutes')
        if bad_minutes > self.window_minutes:
            raise ValueError('bad_minutes exceed the SLO window')
        return bad_minutes / self.budget_minutes

    def burn_rate(self, bad, total) -> Fraction:
        """Observed error ratio over the error ratio the SLO allows."""
        bad, total = _non_negative(bad, 'bad'), _non_negative(total, 'total')
        if total == 0 or bad > total:
            raise ValueError('need 0 <= bad <= total and total > 0')
        return bad / total / self.budget_ratio

    def consumed_events(self, bad, window_total) -> Fraction:
        """Budget share spent; the burn rate of the WHOLE window is the same number."""
        return self.burn_rate(bad, window_total)

    def consumed_by_burn(self, burn, minutes) -> Fraction:
        return _non_negative(burn, 'burn') * _non_negative(minutes, 'minutes') / self.window_minutes

    def exhaustion_minutes(self, burn) -> Fraction:
        burn = _non_negative(burn, 'burn')
        if burn == 0:
            raise ValueError('a zero burn rate never exhausts the budget')
        return self.window_minutes / burn


@dataclass(frozen=True)
class BurnRule:
    severity: str
    long_minutes: int
    short_minutes: int
    budget_fraction: Fraction

    def __post_init__(self):
        if self.severity not in ('page', 'ticket'):
            raise ValueError('severity must be page or ticket')
        if (type(self.long_minutes) is not int or type(self.short_minutes) is not int
                or not 1 <= self.short_minutes < self.long_minutes):
            raise ValueError('need integer minutes with 1 <= short < long')
        fraction = _exact(self.budget_fraction, 'budget_fraction')
        if not 0 < fraction <= 1:
            raise ValueError('budget_fraction must be in (0, 1]')
        object.__setattr__(self, 'budget_fraction', fraction)

    def threshold(self, slo) -> Fraction:
        """Burn rate that spends budget_fraction of the budget in long_minutes."""
        if self.long_minutes > slo.window_minutes:
            raise ValueError('long window exceeds the SLO window')
        return self.budget_fraction * slo.window_minutes / self.long_minutes

    def error_ratio(self, slo) -> Fraction:
        return self.threshold(slo) * slo.budget_ratio

    def fires(self, slo, long_window, short_window, *, min_requests=MIN_REQUESTS) -> bool:
        """(bad, total) per window; both must exceed the ratio, as in alerts.yaml."""
        limit = self.error_ratio(slo)
        for bad, total in (long_window, short_window):
            if type(bad) is not int or type(total) is not int or not 0 <= bad <= total:
                raise ValueError('windows are (bad, total) integer counts')
        if long_window[1] < min_requests or short_window[1] == 0:
            return False
        return all(Fraction(bad, total) > limit for bad, total in (long_window, short_window))


RULES = (
    BurnRule('page', 60, 5, Fraction('0.02')),
    BurnRule('page', 360, 30, Fraction('0.05')),
    BurnRule('ticket', 4_320, 360, Fraction('0.10')),
)


@dataclass(frozen=True)
class Policy:
    slo: Slo
    approvers: tuple                         # customer and operator roles who agreed
    revisit: date
    slow_at: Fraction = Fraction(1, 2)       # proposal
    postmortem_over: Fraction = Fraction(1, 5)

    def __post_init__(self):
        if type(self.slo) is not Slo:
            raise TypeError('slo must be an Slo')
        if (type(self.approvers) is not tuple or len(set(self.approvers)) < 2
                or len(set(self.approvers)) != len(self.approvers)
                or any(type(name) is not str or not name.strip() for name in self.approvers)):
            raise ValueError('at least two distinct approvers are required')
        if type(self.revisit) is not date:
            raise TypeError('revisit must be a date')
        for name in ('slow_at', 'postmortem_over'):
            value = _exact(getattr(self, name), name)
            if not 0 < value <= 1:
                raise ValueError(f'{name} must be in (0, 1]')
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class Decision:
    state: str
    consumed: Fraction        # share of the window's budget spent; above 1 means exceeded
    remaining: Fraction       # 1 - consumed (negative once the budget is exceeded)
    allowed: tuple
    canary_scale: int
    max_changes: int | None   # changes per release; None means the release policy decides


def _consumed(slo, bad_minutes, bad_events) -> Fraction:
    """Exactly one unit: bad minutes, or (bad, total) valid events over the window."""
    if (bad_minutes is None) == (bad_events is None):
        raise ValueError('give exactly one of bad_minutes or bad_events')
    if bad_minutes is not None:
        return slo.consumed_minutes(bad_minutes)
    if type(bad_events) is not tuple or len(bad_events) != 2:
        raise ValueError('bad_events must be (bad, total)')
    return slo.consumed_events(*bad_events)


def decide(policy, *, today, bad_minutes=None, bad_events=None) -> Decision:
    """State for the trailing SLO window; an expired policy is not used."""
    if type(policy) is not Policy:
        raise TypeError('policy must be a Policy')
    if type(today) is not date:
        raise TypeError('today must be a date')
    if today > policy.revisit:
        raise ValueError('policy is past its revisit date; agree it again')
    consumed = _consumed(policy.slo, bad_minutes, bad_events)
    if consumed > 1:
        state = 'freeze'
    elif consumed >= policy.slow_at:
        state = 'slow'
    else:
        state = 'normal'
    allowed, scale, max_changes = STATES[state]
    return Decision(state, consumed, 1 - consumed, allowed, scale, max_changes)


def permit(decision, kind, *, changes=1) -> None:
    """Raise PermissionError when the current state does not allow this release."""
    if type(decision) is not Decision:
        raise TypeError('decision must be a Decision')
    if kind not in CHANGE_KINDS:
        raise ValueError('unknown change kind')
    if type(changes) is not int or changes < 1:
        raise ValueError('changes must be a positive integer')
    if kind not in decision.allowed:
        raise PermissionError(f'{kind} change not allowed while {decision.state}')
    if decision.max_changes is not None and changes > decision.max_changes:
        raise PermissionError(f'{changes} changes in one release not allowed while '
                              f'{decision.state}')


def requires_postmortem(policy, incident_bad_minutes=None, *, incident_bad_events=None) -> bool:
    """One incident spending more than postmortem_over of the window's budget."""
    if type(policy) is not Policy:
        raise TypeError('policy must be a Policy')
    return _consumed(policy.slo, incident_bad_minutes, incident_bad_events) > policy.postmortem_over

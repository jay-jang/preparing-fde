"""Incident timeline, severity and postmortem completeness, NOT incident response.

Timeline phases, in order: impact_start, detected, acknowledged, mitigated,
resolved. Recorded phases form a prefix of that order (an open incident has no
resolved entry yet) and times never decrease. Each entry names the data source
its time came from (alert, page, ticket, log query) so the timeline can be
re-derived; impact_start is usually estimated afterwards from metrics. Times
are timezone-aware whole seconds from trusted internal time (3-1).

Durations in integer seconds, each from the phase that starts its clock:
detect = detected - impact_start, acknowledge = acknowledged - detected,
mitigate = mitigated - impact_start, resolve = resolved - impact_start.

classify() is a proposed severity matrix, not a standard: data exposure or
safety-relevant wrong guidance is SEV1 at any scale, and so is impact on all
pilot users; an unknown input counts as the worst case. review_due() borrows
the postmortem meeting windows of the PagerDuty response guide (SEV-1 within
3 calendar days, SEV-2 within 5 business days) and counts them from the
resolved time's local date, a choice of this module; holidays are not
modelled. Blameless wording is a review rule for people that code cannot check.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re

PHASES = ('impact_start', 'detected', 'acknowledged', 'mitigated', 'resolved')
SEVERITIES = ('SEV1', 'SEV2', 'SEV3')
_SOURCE = re.compile(r'[a-z]+:[A-Za-z0-9._/-]{1,120}')     # e.g. alert:RagFastBudgetBurn
_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
_OWNER = re.compile(r'[a-z][a-z0-9_-]{0,39}')              # role names as in fde/decisions.py
_SECOND = timedelta(seconds=1)


def _key(value, what):
    if type(value) is not str or not _KEY.fullmatch(value):
        raise ValueError(f'invalid {what}')


@dataclass(frozen=True)
class Entry:
    phase: str
    at: datetime
    source: str

    def __post_init__(self):
        if type(self.phase) is not str or self.phase not in PHASES:
            raise ValueError('unknown phase')
        if type(self.at) is not datetime or self.at.utcoffset() is None:
            raise ValueError('use timezone-aware datetimes from trusted time')
        if self.at.microsecond:
            raise ValueError('record whole seconds')
        if type(self.source) is not str or not _SOURCE.fullmatch(self.source):
            raise ValueError('every entry names its data source, e.g. alert:<name>')


@dataclass(frozen=True)
class Timeline:
    entries: tuple[Entry, ...]

    def __post_init__(self):
        if (type(self.entries) is not tuple or not self.entries
                or any(type(entry) is not Entry for entry in self.entries)):
            raise ValueError('timeline needs a non-empty tuple of Entry')
        if tuple(entry.phase for entry in self.entries) != PHASES[:len(self.entries)]:
            raise ValueError('phases must follow impact_start..resolved without gaps')
        for earlier, later in zip(self.entries, self.entries[1:]):
            if later.at < earlier.at:
                raise ValueError('timeline is not monotonic')

    def time(self, phase):
        if phase not in PHASES:
            raise ValueError('unknown phase')
        return next((entry.at for entry in self.entries if entry.phase == phase), None)

    def durations(self) -> dict[str, int | None]:
        def span(start, end):
            first, last = self.time(start), self.time(end)
            return None if last is None else (last - first) // _SECOND
        return {'detect': span('impact_start', 'detected'),
                'acknowledge': span('detected', 'acknowledged'),
                'mitigate': span('impact_start', 'mitigated'),
                'resolve': span('impact_start', 'resolved')}


def classify(users_affected, safety_relevant, data_exposure, *, all_users=50, major_users=10):
    """Proposed matrix; None means unknown and is treated as the worse case."""
    for threshold in (all_users, major_users):
        if type(threshold) is not int or threshold < 1:
            raise ValueError('thresholds must be positive integers')
    if major_users > all_users:
        raise ValueError('major_users cannot exceed all_users')
    if users_affected is not None and (type(users_affected) is not int or users_affected < 0):
        raise ValueError('users_affected must be a non-negative integer or None')
    for flag in (safety_relevant, data_exposure):
        if flag is not None and type(flag) is not bool:
            raise ValueError('flags must be bool or None')
    if data_exposure is not False or safety_relevant is not False:
        return 'SEV1'
    if users_affected is None or users_affected >= all_users:
        return 'SEV1'
    return 'SEV2' if users_affected >= major_users else 'SEV3'


def review_due(severity, resolved_at):
    """Latest postmortem meeting date; None for SEV3 (still allowed on request)."""
    if severity not in SEVERITIES:
        raise ValueError('unknown severity')
    if type(resolved_at) is not datetime or resolved_at.utcoffset() is None:
        raise ValueError('use a timezone-aware resolved time')
    day = resolved_at.date()
    if severity == 'SEV1':
        return day + timedelta(days=3)
    if severity == 'SEV3':
        return None
    remaining = 5
    while remaining:
        day += timedelta(days=1)
        if day.weekday() < 5:
            remaining -= 1
    return day


@dataclass(frozen=True)
class ActionItem:
    ticket: str                # tracking number in the customer's system
    owner: str | None          # role, as in the 5-4 RACI
    due: date | None
    trigger: str | None        # key in Postmortem.causes that this item addresses

    def __post_init__(self):
        _key(self.ticket, 'ticket')
        if self.owner is not None and (type(self.owner) is not str
                                       or not _OWNER.fullmatch(self.owner)):
            raise ValueError('invalid owner')
        if self.due is not None and type(self.due) is not date:
            raise ValueError('due must be a date')
        if self.trigger is not None:
            _key(self.trigger, 'trigger')


@dataclass(frozen=True)
class Postmortem:
    incident_id: str
    severity: str
    timeline: Timeline
    causes: tuple[str, ...]    # contributing causes and detection gaps, never people
    actions: tuple[ActionItem, ...]

    def __post_init__(self):
        _key(self.incident_id, 'incident id')
        if self.severity not in SEVERITIES:
            raise ValueError('unknown severity')
        if type(self.timeline) is not Timeline:
            raise ValueError('expected Timeline')
        if type(self.causes) is not tuple or type(self.actions) is not tuple:
            raise ValueError('causes and actions must be tuples')
        for cause in self.causes:
            _key(cause, 'cause')
        if any(type(item) is not ActionItem for item in self.actions):
            raise ValueError('expected ActionItem')
        if len(set(self.causes)) != len(self.causes):
            raise ValueError('duplicate cause')
        if len({item.ticket for item in self.actions}) != len(self.actions):
            raise ValueError('duplicate ticket')


def gaps(postmortem) -> list[tuple[str, str]]:
    """Sorted (gap, subject) pairs; the postmortem is complete only when empty."""
    if type(postmortem) is not Postmortem:
        raise ValueError('expected Postmortem')
    found = set()
    subject = postmortem.incident_id
    if postmortem.timeline.time('resolved') is None:
        found.add(('unresolved', subject))
    if not postmortem.causes:
        found.add(('no_causes', subject))
    if not postmortem.actions:
        found.add(('no_action_items', subject))
    for item in postmortem.actions:
        if item.owner is None:
            found.add(('missing_owner', item.ticket))
        if item.due is None:
            found.add(('missing_due', item.ticket))
        if item.trigger not in postmortem.causes:
            found.add(('unlinked_trigger', item.ticket))
    covered = {item.trigger for item in postmortem.actions}
    for cause in postmortem.causes:
        if cause not in covered:
            found.add(('cause_without_action', cause))
    return sorted(found)

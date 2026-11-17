"""Stakeholder map, RACI matrix and decision log checks, NOT the agreement itself.

Who decides what is agreed with the customer. This module only reports the
gaps that make an agreement unenforceable, so a review can list them at once:

- RACI: a workstream with zero or several Accountables (this deck's rule is
  exactly one), no Responsible, an Accountable role nobody fills (vacant), a
  role missing from the stakeholder map;
- decision log: a decision without an owner, an unknown workstream, an
  accepted (or later superseded) decision that its workstream's Accountable
  did not approve, a rejection without a reason, and a supersedes chain that
  dangles, loops, leaves a superseded record without a successor, leaves a
  replaced record unmarked, or forks into competing successors. A successor
  is a record that was accepted: it is 'accepted' now, or 'superseded' itself
  later in a longer chain (DR-001 <- DR-002 <- DR-003). Proposed and rejected
  records replace nothing.

Record shapes (ids, statuses, dates, letters) fail closed with ValueError at
construction. Ids follow 5-1 (DR-001) and are never reused. Accepted records
are treated as immutable: a change is a new record that supersedes the old
one. Role and workstream names are proposals for the pilot.
"""
from dataclasses import dataclass
from datetime import date
import re

STATUSES = ('proposed', 'accepted', 'rejected', 'superseded')
LETTERS = ('R', 'A', 'C', 'I')
FINDINGS = ('no_accountable', 'multiple_accountable', 'no_responsible', 'vacant_accountable',
            'unknown_role', 'missing_owner', 'unknown_workstream', 'unsigned_decision',
            'rejected_without_reason', 'dangling_supersedes', 'supersedes_cycle',
            'superseded_without_successor', 'not_marked_superseded', 'competing_successors')
_NAME = re.compile(r'[a-z][a-z0-9_-]{0,39}')
_ASSIGNEE = re.compile(r'[A-Za-z0-9._@-]{1,80}')
_ID = re.compile(r'DR-\d{3}')


def _name(value, what):
    if type(value) is not str or not _NAME.fullmatch(value):
        raise ValueError(f'invalid {what}')


@dataclass(frozen=True)
class Stakeholder:
    role: str
    assignee: str | None      # person or team alias from the customer; None = vacant

    def __post_init__(self):
        _name(self.role, 'role')
        if self.assignee is not None and (type(self.assignee) is not str
                                          or not _ASSIGNEE.fullmatch(self.assignee)):
            raise ValueError('invalid assignee')


@dataclass(frozen=True)
class Raci:
    workstream: str
    assignments: tuple[tuple[str, str], ...]   # (role, letter), one letter per role

    def __post_init__(self):
        _name(self.workstream, 'workstream')
        if type(self.assignments) is not tuple or not self.assignments:
            raise ValueError('assignments must be a non-empty tuple')
        roles = []
        for item in self.assignments:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError('assignment must be a (role, letter) pair')
            _name(item[0], 'role')
            if type(item[1]) is not str or item[1] not in LETTERS:
                raise ValueError('letter must be R, A, C or I')
            roles.append(item[0])
        if len(set(roles)) != len(roles):
            raise ValueError('a role holds one letter per workstream')


@dataclass(frozen=True)
class Decision:
    id: str
    status: str
    decided_on: date           # date of the current status
    workstream: str
    owner: str | None          # None is reported as missing_owner, not refused
    approved_by: tuple[str, ...] = ()
    supersedes: str | None = None
    reason: str = ''

    def __post_init__(self):
        if type(self.id) is not str or not _ID.fullmatch(self.id):
            raise ValueError('decision id must look like DR-001')
        if type(self.status) is not str or self.status not in STATUSES:
            raise ValueError('unknown status')
        if type(self.decided_on) is not date:
            raise ValueError('decided_on must be a date')
        _name(self.workstream, 'workstream')
        if self.owner is not None:
            _name(self.owner, 'owner')
        if type(self.approved_by) is not tuple:
            raise ValueError('approved_by must be a tuple')
        for role in self.approved_by:
            _name(role, 'approver')
        if len(set(self.approved_by)) != len(self.approved_by):
            raise ValueError('duplicate approver')
        if self.supersedes is not None and (type(self.supersedes) is not str
                                            or not _ID.fullmatch(self.supersedes)):
            raise ValueError('supersedes must be a decision id')
        if type(self.reason) is not str or len(self.reason) > 500:
            raise ValueError('reason must be a string of at most 500 characters')


def _index(items, kind, key, allow_empty=False):
    if type(items) is not tuple or (not items and not allow_empty):
        raise ValueError(f'{kind.__name__} records must be a non-empty tuple')
    index = {}
    for item in items:
        if type(item) is not kind:
            raise ValueError(f'expected {kind.__name__}')
        if getattr(item, key) in index:
            raise ValueError(f'duplicate {key}: ids and names are never reused')
        index[getattr(item, key)] = item
    return index


def review(stakeholders, matrix, decisions) -> list[tuple[str, str]]:
    """Return sorted (finding, subject) pairs; an empty list means no gap was found."""
    people = _index(stakeholders, Stakeholder, 'role')
    streams = _index(matrix, Raci, 'workstream')
    records = _index(decisions, Decision, 'id', allow_empty=True)
    found = set()
    accountable = {}
    for name, raci in streams.items():
        owners = [role for role, letter in raci.assignments if letter == 'A']
        if not owners:
            found.add(('no_accountable', name))
        elif len(owners) > 1:
            found.add(('multiple_accountable', name))
        else:
            accountable[name] = owners[0]
        if not any(letter == 'R' for _, letter in raci.assignments):
            found.add(('no_responsible', name))
        for role, letter in raci.assignments:
            if role not in people:
                found.add(('unknown_role', role))
            elif letter == 'A' and people[role].assignee is None:
                found.add(('vacant_accountable', name))
    successors = {}
    for record in records.values():
        if record.owner is None:
            found.add(('missing_owner', record.id))
        for role in (record.owner,) + record.approved_by:
            if role is not None and role not in people:
                found.add(('unknown_role', role))
        if record.workstream not in streams:
            found.add(('unknown_workstream', record.id))
        elif (record.status in ('accepted', 'superseded') and record.workstream in accountable
              and accountable[record.workstream] not in record.approved_by):
            found.add(('unsigned_decision', record.id))
        if record.status == 'rejected' and not record.reason.strip():
            found.add(('rejected_without_reason', record.id))
        if record.supersedes is not None:
            if record.supersedes not in records:
                found.add(('dangling_supersedes', record.id))
            elif record.status in ('accepted', 'superseded'):   # was accepted at some point
                successors.setdefault(record.supersedes, []).append(record.id)
    for record in records.values():
        current, steps = record.supersedes, 0
        while current in records and steps <= len(records):
            if current == record.id:
                found.add(('supersedes_cycle', record.id))
                break
            current, steps = records[current].supersedes, steps + 1
        replaced_by = successors.get(record.id, [])
        if record.status == 'superseded' and not replaced_by:
            found.add(('superseded_without_successor', record.id))
        if replaced_by and record.status != 'superseded':
            found.add(('not_marked_superseded', record.id))
        if len(replaced_by) > 1:
            found.add(('competing_successors', record.id))
    return sorted(found)

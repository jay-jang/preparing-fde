"""Blue/green reindex for an embedding-model or index change, as a state machine.

Green is built from a snapshot at a recorded source position, then replays every
later change from one ordered change log (upserts, deletions, ACL changes) with
contiguous positions. Green may serve only after it has applied the same position
as blue, matches blue chunk by chunk (version, tombstone, ACL digest) and passed
the release gate (fde/release_gate.py) on an evaluation report whose
artifact_digest is the green release digest (4-2), so a pass recorded for
another release cannot be reused. The switch is one atomic alias change. Blue
keeps applying changes during the rollback window, so a rollback is refused when
blue is behind. Vectors are not compared: a new embedding model changes them by
design. Illegal transitions raise ValueError. Building a Reindex directly in a
later state (e.g. restoring a persisted job) is allowed only with fields that
are consistent with that state.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import math
import re

from fde.release_gate import evaluate

DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
CHUNK_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}')
_NEXT = {
    'planned': ('built', 'aborted'),
    'built': ('caught_up', 'aborted'),
    'caught_up': ('caught_up', 'verified', 'aborted'),
    'verified': ('switched', 'aborted'),
    'switched': ('rolled_back', 'retired'),
    'rolled_back': (),
    'retired': (),
    'aborted': (),
}


def _position(value, name='position'):
    if type(value) is not int or value < 0:
        raise ValueError(f'{name} must be a non-negative integer')
    return value


def _rate(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be a finite positive number')


def _instant(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('timestamps must be timezone-aware datetimes')


@dataclass(frozen=True)
class ChunkRow:
    version: int
    deleted: bool
    acl_digest: str

    def __post_init__(self):
        if type(self.version) is not int or self.version < 1:
            raise ValueError('version must be a positive integer')
        if type(self.deleted) is not bool:
            raise ValueError('deleted must be a boolean')
        if not isinstance(self.acl_digest, str) or not DIGEST.fullmatch(self.acl_digest):
            raise ValueError('acl_digest must be sha256:<64 hex>')


@dataclass(frozen=True)
class Change:
    position: int
    chunk_id: str
    row: ChunkRow

    def __post_init__(self):
        _position(self.position)
        if not isinstance(self.chunk_id, str) or not CHUNK_ID.fullmatch(self.chunk_id):
            raise ValueError('invalid chunk id')
        if not isinstance(self.row, ChunkRow):
            raise TypeError('row must be a ChunkRow')


def _rows(rows):
    if not isinstance(rows, dict) or not all(
            isinstance(key, str) and CHUNK_ID.fullmatch(key) and isinstance(row, ChunkRow)
            for key, row in rows.items()):
        raise TypeError('rows must map chunk ids to ChunkRow values')
    return rows


def apply_changes(rows, changes, *, after):
    """Apply changes at positions after+1, after+2, ...; gaps, reordering and regressions raise."""
    result, position = dict(_rows(rows)), _position(after, 'after')
    for change in changes:
        if not isinstance(change, Change):
            raise TypeError('changes must be Change values')
        if change.position != position + 1:
            raise ValueError(f'expected position {position + 1}, got {change.position}')
        current = result.get(change.chunk_id)
        if current is not None and (change.row.version < current.version or (
                current.deleted and not change.row.deleted
                and change.row.version == current.version)):
            raise ValueError(f'version regression or resurrection for {change.chunk_id}')
        result[change.chunk_id] = change.row
        position = change.position
    return result, position


def counts(rows):
    """(live, tombstones): a quick first check; parity() is the verdict."""
    _rows(rows)
    tombstones = sum(row.deleted for row in rows.values())
    return len(rows) - tombstones, tombstones


def parity(blue, green):
    """Sorted problem codes; an empty tuple means chunk-by-chunk parity."""
    _rows(blue)
    _rows(green)
    problems = []
    for chunk_id in sorted(set(blue) | set(green)):
        old, new = blue.get(chunk_id), green.get(chunk_id)
        if new is None:
            problems.append(f'missing:{chunk_id}')
        elif old is None:
            problems.append(f'extra:{chunk_id}')
        else:
            problems += [f'{name}:{chunk_id}' for name, same in (
                ('version', old.version == new.version), ('tombstone', old.deleted == new.deleted),
                ('acl', old.acl_digest == new.acl_digest)) if not same]
    return tuple(sorted(problems))


def catch_up_seconds(backlog, *, replay_per_second, change_per_second) -> float:
    """Seconds for green to drain a backlog while changes keep arriving."""
    if type(backlog) not in (int, float) or not math.isfinite(backlog) or backlog < 0:
        raise ValueError('backlog must be a finite non-negative number')
    _rate(replay_per_second, 'replay_per_second')
    _rate(change_per_second, 'change_per_second')
    if replay_per_second <= change_per_second:
        raise ValueError('replay never catches up: replay rate must exceed change rate')
    return backlog / (replay_per_second - change_per_second)


@dataclass(frozen=True)
class Reindex:
    blue: str
    green: str
    state: str = 'planned'
    snapshot_position: int | None = None
    green_position: int | None = None
    rollback_until: datetime | None = None

    def __post_init__(self):
        for digest in (self.blue, self.green):
            if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
                raise ValueError('releases must be sha256:<64 hex> manifest digests')
        if self.blue == self.green:
            raise ValueError('green must be a different release')
        if self.state not in _NEXT:
            raise ValueError('unknown state')
        positions = (self.snapshot_position, self.green_position)
        if self.state == 'planned' or (self.state == 'aborted' and positions == (None, None)):
            if positions != (None, None):
                raise ValueError('a planned job has no positions yet')
        else:
            _position(self.snapshot_position, 'snapshot_position')
            if _position(self.green_position, 'green_position') < self.snapshot_position:
                raise ValueError('green position cannot be before the snapshot')
        if self.state in ('switched', 'rolled_back', 'retired'):
            _instant(self.rollback_until)
        elif self.rollback_until is not None:
            raise ValueError('rollback_until is set only by switch()')

    @property
    def serving(self):
        return self.green if self.state in ('switched', 'retired') else self.blue

    def _check(self, state):
        if state not in _NEXT[self.state]:
            raise ValueError(f'illegal transition: {self.state} -> {state}')

    def _to(self, state, **fields):
        self._check(state)
        return replace(self, state=state, **fields)

    def built(self, *, snapshot_position):
        self._check('built')
        position = _position(snapshot_position, 'snapshot_position')
        return self._to('built', snapshot_position=position, green_position=position)

    def caught_up(self, green_position):
        self._check('caught_up')
        if _position(green_position, 'green_position') < self.green_position:
            raise ValueError('green position cannot move backwards')
        return self._to('caught_up', green_position=green_position)

    def verify(self, *, blue_rows, green_rows, blue_position, report):
        """Parity at one position, then the release gate on the green release's own report."""
        self._check('verified')
        if _position(blue_position, 'blue_position') != self.green_position:
            raise ValueError('compare blue and green at the same source position')
        problems = parity(blue_rows, green_rows)
        if problems:
            raise ValueError('parity failed: ' + ', '.join(problems[:5]))
        if not isinstance(report, dict) or report.get('artifact_digest') != self.green:
            raise ValueError('evaluation report is not bound to the green release')
        if evaluate(report)['pass'] is not True:      # malformed reports raise ValueError
            raise ValueError('evaluation gate did not pass')
        return self._to('verified')

    def switch(self, *, now, rollback_window, blue_position, green_position):
        """One atomic alias change at a barrier where both sides applied the same position."""
        self._check('switched')
        _instant(now)
        if not isinstance(rollback_window, timedelta) or rollback_window <= timedelta(0):
            raise ValueError('rollback window must be a positive timedelta')
        if _position(blue_position, 'blue_position') != _position(green_position, 'green_position'):
            raise ValueError('green is not at the blue position')
        if green_position < self.green_position:
            raise ValueError('green position cannot move backwards')
        return self._to('switched', green_position=green_position,
                        rollback_until=now + rollback_window)

    def rollback(self, *, now, blue_position, green_position):
        self._check('rolled_back')
        _instant(now)
        if now >= self.rollback_until:
            raise ValueError('rollback window closed')
        if _position(blue_position, 'blue_position') != _position(green_position, 'green_position'):
            raise ValueError('blue is behind: rollback would serve stale deletions or ACLs')
        return self._to('rolled_back')

    def retire(self, *, now):
        self._check('retired')
        _instant(now)
        if now < self.rollback_until:
            raise ValueError('rollback window still open')
        return self._to('retired')

    def abort(self):
        return self._to('aborted')

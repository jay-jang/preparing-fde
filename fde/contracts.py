"""Per-source data contracts with quarantine, NOT a schema registry or a dbt replacement.

validate_batch() never coerces a value: "92" is not a number, 3.0 is not an int and
a Fahrenheit reading is not converted to Celsius. Each record is either accepted
unchanged or quarantined with explicit reason codes ('code:field'), the raw record,
its source position, the contract source and revision and the ingest time, so it
can be replayed with the same clock after the contract owner revises the contract.
ID patterns match ASCII only (re.ASCII), so \d does not accept other digit scripts.

A schema-level change (missing or unknown schema_version, unknown field, record
that is not an object) raises SchemaBreak: the batch is not split, no field is
dropped and the consumer does not ACK until the contract is revised (1-3).

Freshness is a warning, not a rejection: a late but valid event is accepted with
'late_event' because dropping it would make state more wrong; ordering is still
decided by source position (1-2). An event time ahead of the ingest clock beyond
max_skew_seconds is quarantined as a clock or mapping error.
"""
from dataclasses import dataclass
from datetime import datetime
import math
import re

KINDS = ('id', 'enum', 'int', 'number', 'quantity', 'timestamp', 'text')
REASONS = ('missing_field', 'null_value', 'wrong_type', 'bad_id_format', 'not_in_enum',
           'unit_mismatch', 'out_of_range', 'bad_timestamp', 'future_event_time')
WARNINGS = ('late_event',)


class SchemaBreak(ValueError):
    """The producer changed shape; stop this source instead of dropping fields."""

    def __init__(self, position, reason):
        super().__init__(f'position {position}: {reason}; revise the contract before ACK')
        self.position = position
        self.reason = reason


@dataclass(frozen=True)
class Field:
    name: str
    kind: str
    required: bool = True
    pattern: str | None = None
    choices: tuple[str, ...] = ()
    unit: str | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None

    def __post_init__(self):
        if type(self.name) is not str or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', self.name):
            raise ValueError('invalid field name')
        if self.kind not in KINDS or type(self.required) is not bool:
            raise ValueError(f'invalid field kind for {self.name}')
        if (self.kind == 'id') != (type(self.pattern) is str):
            raise ValueError('id fields, and only id fields, need a pattern')
        if self.kind == 'id':
            try:
                re.compile(self.pattern, re.ASCII)
            except re.error as error:
                raise ValueError(f'invalid pattern for {self.name}: {error}') from None
        if (self.kind == 'enum') != bool(self.choices) or type(self.choices) is not tuple:
            raise ValueError('enum fields, and only enum fields, need choices')
        if (self.kind == 'quantity') != (type(self.unit) is str and bool(self.unit)):
            raise ValueError('quantity fields, and only quantity fields, need a unit')
        bounds = [b for b in (self.minimum, self.maximum) if b is not None]
        if bounds and self.kind not in ('int', 'number', 'quantity'):
            raise ValueError('only numeric fields take a range')
        if any(type(b) not in (int, float) or not math.isfinite(b) for b in bounds):
            raise ValueError('invalid range bound')
        if len(bounds) == 2 and self.minimum > self.maximum:
            raise ValueError('minimum above maximum')


@dataclass(frozen=True)
class Contract:
    source: str
    revision: int
    schema_versions: tuple[int, ...]
    owner: str
    fields: tuple[Field, ...]
    max_lag_seconds: int
    max_skew_seconds: int
    event_time_field: str = 'event_time'

    def __post_init__(self):
        if type(self.source) is not str or not re.fullmatch(r'[a-z0-9._-]{1,80}', self.source):
            raise ValueError('invalid source name')
        if type(self.owner) is not str or not self.owner.strip():
            raise ValueError('a contract needs an owner who can fix quarantined data')
        if type(self.schema_versions) is not tuple or not self.schema_versions:
            raise ValueError('declare the accepted schema versions')
        for value in (self.revision, *self.schema_versions):
            if type(value) is not int or value < 1:
                raise ValueError('revision and schema versions must be positive ints')
        if type(self.fields) is not tuple or not all(type(f) is Field for f in self.fields):
            raise ValueError('fields must be a tuple of Field')
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names) or 'schema_version' in names:
            raise ValueError('duplicate or reserved field name')
        event = [f for f in self.fields if f.name == self.event_time_field]
        if not event or event[0].kind != 'timestamp' or not event[0].required:
            raise ValueError('event time must be a required timestamp field')
        for value in (self.max_lag_seconds, self.max_skew_seconds):
            if type(value) is not int or value < 0:
                raise ValueError('freshness bounds must be non-negative int seconds')
        if self.max_lag_seconds == 0:
            raise ValueError('max_lag_seconds must be positive')


@dataclass(frozen=True)
class Accepted:
    position: int
    record: dict
    lag_seconds: float
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class Quarantined:
    position: int
    raw: dict
    reasons: tuple[str, ...]
    source: str
    contract_revision: int
    ingest_time: datetime


@dataclass(frozen=True)
class BatchResult:
    accepted: tuple[Accepted, ...]
    quarantined: tuple[Quarantined, ...]
    counts: dict
    lag_p95_seconds: float | None


def _timestamp(value):
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _check(field, value):
    """Return the reason code for one present, non-null value, or None."""
    if field.kind in ('id', 'enum', 'text') and type(value) is not str:
        return 'wrong_type'
    if field.kind == 'id':
        return None if re.fullmatch(field.pattern, value, re.ASCII) else 'bad_id_format'
    if field.kind == 'enum':
        return None if value in field.choices else 'not_in_enum'
    if field.kind == 'timestamp':
        return None if _timestamp(value) else 'bad_timestamp'
    if field.kind == 'quantity':
        if type(value) is not dict or value.keys() != {'value', 'unit'}:
            return 'wrong_type'
        if value['unit'] != field.unit:
            return 'unit_mismatch'
        value = value['value']
    if field.kind == 'int' and type(value) is not int:
        return 'wrong_type'
    if field.kind in ('number', 'quantity') and not _number(value):
        return 'wrong_type'
    if field.kind == 'text':
        return None
    if ((field.minimum is not None and value < field.minimum)
            or (field.maximum is not None and value > field.maximum)):
        return 'out_of_range'
    return None


def _validate(contract, position, record, ingest_time):
    if type(record) is not dict:
        raise SchemaBreak(position, 'record is not an object')
    if 'schema_version' not in record:
        raise SchemaBreak(position, 'missing schema_version')
    version = record['schema_version']
    if type(version) is not int or version not in contract.schema_versions:
        raise SchemaBreak(position, f'unknown schema_version {version!r}')
    unknown = sorted(map(str, record.keys() - {f.name for f in contract.fields} - {'schema_version'}))
    if unknown:
        raise SchemaBreak(position, f'unknown field {unknown[0]}')
    reasons = []
    for field in contract.fields:
        if field.name not in record:
            if field.required:
                reasons.append(f'missing_field:{field.name}')
            continue
        value = record[field.name]
        code = 'null_value' if value is None else _check(field, value)
        if code:
            reasons.append(f'{code}:{field.name}')
    event_time = _timestamp(record.get(contract.event_time_field))
    lag = None
    if event_time is not None:
        lag = (ingest_time - event_time).total_seconds()
        if -lag > contract.max_skew_seconds:
            reasons.append(f'future_event_time:{contract.event_time_field}')
    if reasons:
        return Quarantined(position, dict(record), tuple(reasons), contract.source, contract.revision,
                           ingest_time)
    warnings = ('late_event',) if lag > contract.max_lag_seconds else ()
    return Accepted(position, dict(record), lag, warnings)


def _summarize(results):
    accepted = tuple(r for r in results if type(r) is Accepted)
    quarantined = tuple(r for r in results if type(r) is Quarantined)
    counts = {'accepted': len(accepted), 'quarantined': len(quarantined)}
    for code in REASONS:
        n = sum(1 for q in quarantined for reason in q.reasons if reason.split(':')[0] == code)
        if n:
            counts[code] = n
    late = sum(1 for a in accepted if 'late_event' in a.warnings)
    if late:
        counts['late_event'] = late
    lags = sorted(a.lag_seconds for a in accepted)
    p95 = lags[math.ceil(.95 * len(lags)) - 1] if lags else None
    return BatchResult(accepted, quarantined, counts, p95)


def validate_batch(contract, records, *, ingest_time):
    """records: (source_position, record) pairs in increasing position order.

    Raises SchemaBreak for a shape change, so no partial batch is released.
    """
    if type(contract) is not Contract:
        raise TypeError('contract must be a Contract')
    if type(ingest_time) is not datetime or ingest_time.tzinfo is None:
        raise ValueError('ingest_time must be timezone-aware')
    results, last = [], -1
    for item in records:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError('records must be (position, record) pairs')
        position, record = item
        if type(position) is not int or position <= last:
            raise ValueError('positions must be increasing ints from one partition')
        last = position
        results.append(_validate(contract, position, record, ingest_time))
    return _summarize(results)


def replay(contract, quarantined):
    """Re-judge quarantined records with a revised contract and their original clock.

    Each entry keeps its own ingest_time, so freshness and skew decisions are
    reproducible. Accepted records still go through position-ordered upserts (1-2).
    """
    if type(contract) is not Contract:
        raise TypeError('contract must be a Contract')
    entries = list(quarantined)
    if not entries or any(type(q) is not Quarantined for q in entries):
        raise TypeError('replay takes a non-empty list of Quarantined entries')
    if any(q.source != contract.source for q in entries):
        raise ValueError('replay needs the contract of the source that quarantined the record')
    if any(q.contract_revision >= contract.revision for q in entries):
        raise ValueError('replay needs a newer contract revision; the same one repeats the verdict')
    entries.sort(key=lambda q: q.position)
    return _summarize([_validate(contract, q.position, q.raw, q.ingest_time) for q in entries])

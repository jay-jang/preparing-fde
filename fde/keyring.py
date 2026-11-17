"""Key versions over time for HMAC pseudonyms and release signatures, NOT a KMS.

Per purpose, active intervals [activates_at, retires_at) are contiguous and
never overlap, so exactly one key applies new protection at any instant of the
chain. A key processes an old record (re-derives a pseudonym, verifies a
signature) only if the record's event time lies in that key's active interval,
and only until verify_until. This mirrors the originator- and recipient-usage
periods of NIST SP 800-57 Part 1 Rev. 5. The event time is the record's own
claim; compare it with a trusted receive time such as the audit sink's (3-3).

Times are timezone-aware datetimes from trusted internal time (3-1). `material`
holds lab-only raw bytes (kept out of repr); production pseudonym keys carry a
KMS/HSM `reference`.
Private signing keys never enter the closed network: a signing entry holds only
the reference of the provisioned public verification key. Destroying a
pseudonym key makes its pseudonyms unlinkable for everyone, auditors included,
so destroy() refuses before verify_until. Cryptoperiod, retention and lead time
are proposals to agree with the customer's security officer.
"""
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import re

PURPOSES = ('pseudonym', 'signing')
_KEY_ID = re.compile(r'[A-Za-z0-9_-]{1,40}')      # same format as fde/privacy.py
_REFERENCE = re.compile(r'[A-Za-z0-9._:/@+-]{1,200}')


def _aware(value):
    if type(value) is not datetime or value.utcoffset() is None:
        raise ValueError('use timezone-aware datetimes from trusted time')
    return value


@dataclass(frozen=True)
class KeyVersion:
    purpose: str
    key_id: str
    activates_at: datetime
    retires_at: datetime
    verify_until: datetime
    material: bytes | None = field(default=None, repr=False)   # never in logs or tracebacks
    reference: str | None = None
    destroyed: bool = False

    def __post_init__(self):
        if self.purpose not in PURPOSES:
            raise ValueError('unknown key purpose')
        if type(self.key_id) is not str or not _KEY_ID.fullmatch(self.key_id):
            raise ValueError('invalid key id')
        for moment in (self.activates_at, self.retires_at, self.verify_until):
            _aware(moment)
        if not self.activates_at < self.retires_at <= self.verify_until:
            raise ValueError('need activates_at < retires_at <= verify_until')
        if type(self.destroyed) is not bool:
            raise ValueError('destroyed must be a bool')
        if self.material is not None:
            if self.purpose == 'signing':
                raise ValueError('private signing keys never enter the closed network')
            if type(self.material) is not bytes or len(self.material) < 32:
                raise ValueError('pseudonym key material needs at least 32 bytes')
        if self.reference is not None and (type(self.reference) is not str
                                           or not _REFERENCE.fullmatch(self.reference)):
            raise ValueError('invalid key reference')
        present = (self.material is not None) + (self.reference is not None)
        if present != (0 if self.destroyed else 1):
            raise ValueError('a live key has exactly one of material or reference; destroyed has none')

    def state(self, at):
        _aware(at)
        if self.destroyed:
            return 'destroyed'
        if at < self.activates_at:
            return 'pending'
        if at < self.retires_at:
            return 'active'
        return 'verify-only' if at < self.verify_until else 'retired'


@dataclass(frozen=True)
class Keyring:
    versions: tuple[KeyVersion, ...]

    def __post_init__(self):
        if (type(self.versions) is not tuple or not self.versions
                or any(type(v) is not KeyVersion for v in self.versions)):
            raise ValueError('keyring needs a non-empty tuple of KeyVersion')
        if len({v.key_id for v in self.versions}) != len(self.versions):
            raise ValueError('duplicate key id')
        for purpose in PURPOSES:
            chain = sorted((v for v in self.versions if v.purpose == purpose),
                           key=lambda v: v.activates_at)
            for previous, current in zip(chain, chain[1:]):
                if current.activates_at != previous.retires_at:
                    raise ValueError('active intervals must be contiguous and must not overlap')

    def _get(self, purpose, key_id):
        if purpose not in PURPOSES:
            raise ValueError('unknown key purpose')
        for version in self.versions:
            if version.key_id == key_id:
                if version.purpose != purpose:
                    raise ValueError('key id belongs to another purpose')
                return version
        raise ValueError('unknown key id')

    def active_key(self, purpose, at):
        """The single key that protects new data at `at`; none means stop, not guess."""
        if purpose not in PURPOSES:
            raise ValueError('unknown key purpose')
        for version in self.versions:
            if version.purpose == purpose and version.state(at) == 'active':
                return version
        raise PermissionError('no active key: rotation overdue or key destroyed')

    def require_active(self, purpose, key_id, at):
        """Refuse a cached key after rotation, e.g. a replica that kept the old one."""
        version = self._get(purpose, key_id)
        state = version.state(at)
        if state != 'active':
            raise PermissionError(f'key {key_id} is {state}; it cannot protect new data')
        return version

    def key_for_record(self, purpose, key_id, event_time, now):
        """The key that was active at event_time, while its verify window lasts."""
        _aware(event_time)
        _aware(now)
        if event_time > now:
            raise ValueError('event time is in the future')
        version = self._get(purpose, key_id)
        if not version.activates_at <= event_time < version.retires_at:
            raise ValueError('record names a key that was not active at its event time')
        if version.destroyed:
            raise PermissionError('key destroyed: records under it can no longer be linked')
        if now >= version.verify_until:
            raise PermissionError('verification window ended')
        return version

    def due_for_destruction(self, now):
        return tuple(v.key_id for v in self.versions if v.state(now) == 'retired')

    def destroy(self, key_id, now):
        """Destroy only after verify_until; earlier would orphan records in retention."""
        version = next((v for v in self.versions if v.key_id == key_id), None)
        if version is None:
            raise ValueError('unknown key id')
        state = version.state(now)
        if state != 'retired':
            raise PermissionError(f'key {key_id} is {state}; retention is not over')
        gone = replace(version, material=None, reference=None, destroyed=True)
        return Keyring(tuple(gone if v is version else v for v in self.versions))


@dataclass(frozen=True)
class RotationStep:
    key_id: str
    distribute_by: datetime
    activates_at: datetime
    retires_at: datetime
    verify_until: datetime


def plan_rotation(prefix, *, start, count, cryptoperiod, retention, lead):
    """Contiguous schedule with verify_until = retires_at + retention.

    The last record under a key is written just before retires_at and must stay
    verifiable for the retention period. lead is the overlap window: the next
    key reaches every replica and verifier before it activates.
    """
    _aware(start)
    if type(count) is not int or not 1 <= count <= 100:
        raise ValueError('count must be 1..100')
    if any(type(value) is not timedelta for value in (cryptoperiod, retention, lead)):
        raise ValueError('durations must be timedelta')
    if retention < timedelta(0) or not timedelta(0) < lead < cryptoperiod:
        raise ValueError('need retention >= 0 and 0 < lead < cryptoperiod')
    if type(prefix) is not str or not _KEY_ID.fullmatch(f'{prefix}-v{count}'):
        raise ValueError('invalid key id prefix')
    steps = []
    for index in range(count):
        activates = start + index * cryptoperiod
        retires = activates + cryptoperiod
        steps.append(RotationStep(f'{prefix}-v{index + 1}', activates - lead, activates,
                                  retires, retires + retention))
    return tuple(steps)

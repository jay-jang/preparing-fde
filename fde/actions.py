"""Agent writes as draft -> approval of a digest -> idempotent, versioned execution.

NOT a workflow engine and NOT the maintenance system. The model-facing tool can
only build a WorkOrderDraft whose citations were retrieved (as in 2-3). Its
tenant and requester come from the gateway's verified Principal, never from
model arguments: a model-chosen requester would defeat the requester != approver
rule. The gateway also fills asset_version from the asset record it read.
approve() runs in the approval UI with the approver identity and roles taken
from the IdP or membership store, never from model output.
WorkOrderStore.execute() is an in-memory stand-in for the maintenance system's
write API. Under one lock (concurrent requests with one key are serialized, so
the second one replays instead of seeing an in-flight state) it checks, in this
order:

1. idempotency: the same (store, key) with the same fingerprint returns the
   stored result without a second write; a different fingerprint raises
   IdempotencyConflict;
2. approval: digest of this exact draft, approver role, approver != requester,
   approved_at <= now < expires_at (PermissionError otherwise);
3. the draft was not executed under another key (an approval is single use);
4. expected_version equals the asset version the approved draft was based on
   (ValueError otherwise) and the asset's current version (StaleVersion
   otherwise); a changed asset needs a new draft and a new approval.

Only then does it write once and store the result under the key. Rejected
requests are not stored, so they can be corrected and retried. Keys live for
the store's lifetime here; a real store keeps them longer than the approval TTL
plus the retry window, and checks 3-4 still block a duplicate after a purge.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import hashlib
import json
import re
import threading

from fde.retrieval import Chunk, Principal

APPROVER_ROLE = 'maintenance_approver'
_NAME = re.compile(r'[A-Za-z0-9._@-]{1,120}')
_ASSET = re.compile(r'[A-Z]{1,4}-[0-9]{1,6}')
_KEY = re.compile(r'[A-Za-z0-9_-]{16,255}')
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}')


class IdempotencyConflict(ValueError):
    """The key was already used with a different request payload."""


class StaleVersion(ValueError):
    """The asset changed after the draft was approved."""


def _name(value, what):
    if type(value) is not str or not _NAME.fullmatch(value):
        raise ValueError(f'invalid {what}')


def _text(value, what, limit):
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise ValueError(f'invalid {what}')


def _aware(value, what):
    if type(value) is not datetime or value.utcoffset() is None:
        raise ValueError(f'{what} must be a timezone-aware datetime')


def _sha256(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return 'sha256:' + hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class WorkOrderDraft:
    tenant: str
    requested_by: str
    asset_id: str
    asset_version: int
    summary: str
    steps: tuple[str, ...]
    citations: tuple[tuple[str, int], ...]

    def __post_init__(self):
        _name(self.tenant, 'tenant')
        _name(self.requested_by, 'requester')
        if type(self.asset_id) is not str or not _ASSET.fullmatch(self.asset_id):
            raise ValueError('invalid asset id')
        if type(self.asset_version) is not int or self.asset_version < 1:
            raise ValueError('asset_version must be a positive int')
        _text(self.summary, 'summary', 200)
        if type(self.steps) is not tuple or not 1 <= len(self.steps) <= 10:
            raise ValueError('a draft needs 1..10 steps')
        for step in self.steps:
            _text(step, 'step', 200)
        if (type(self.citations) is not tuple or not 1 <= len(self.citations) <= 5
                or len(set(self.citations)) != len(self.citations)):
            raise ValueError('a draft needs 1..5 distinct citations')
        for item in self.citations:
            if (type(item) is not tuple or len(item) != 2 or type(item[0]) is not str
                    or not item[0] or type(item[1]) is not int or item[1] < 1):
                raise ValueError('citation must be (chunk_id, version)')

    @property
    def digest(self) -> str:
        return _sha256({'tenant': self.tenant, 'requested_by': self.requested_by,
                        'asset_id': self.asset_id, 'asset_version': self.asset_version,
                        'summary': self.summary,
                        'steps': list(self.steps), 'citations': [list(c) for c in self.citations]})


def draft_work_order(principal: Principal, *, asset_id, asset_version, summary, steps,
                     citations, evidence) -> WorkOrderDraft:
    """The only write-shaped tool a model may call: it returns a draft, never a write.

    The gateway binds principal (verified token) and asset_version (asset record);
    the model supplies only asset_id, summary, steps and citations.
    """
    if type(principal) is not Principal:
        raise TypeError('principal must be the gateway-verified Principal')
    if not isinstance(evidence, list) or any(type(c) is not Chunk for c in evidence):
        raise TypeError('evidence must be the retrieved Chunk list')
    draft = WorkOrderDraft(principal.tenant, principal.subject, asset_id, asset_version,
                           summary, steps, citations)
    retrieved = {(c.id, c.version) for c in evidence}
    if any(citation not in retrieved for citation in draft.citations):
        raise ValueError('unretrieved or stale citation')
    return draft


@dataclass(frozen=True)
class Approval:
    tenant: str
    draft_digest: str
    approver: str
    roles: frozenset[str]
    approved_at: datetime
    expires_at: datetime

    def __post_init__(self):
        _name(self.tenant, 'tenant')
        _name(self.approver, 'approver')
        if type(self.draft_digest) is not str or not _DIGEST.fullmatch(self.draft_digest):
            raise ValueError('invalid draft digest')
        if type(self.roles) is not frozenset or any(type(r) is not str for r in self.roles):
            raise ValueError('roles must be a frozenset of str')
        _aware(self.approved_at, 'approved_at')
        _aware(self.expires_at, 'expires_at')
        if not self.approved_at < self.expires_at:
            raise ValueError('approval must expire after it is granted')


def _check_approver(draft, approver, roles):
    if APPROVER_ROLE not in roles:
        raise PermissionError('approver lacks the maintenance_approver role')
    if approver == draft.requested_by:
        raise PermissionError('requester cannot approve their own draft')


def approve(draft: WorkOrderDraft, *, approver, roles, now,
            ttl=timedelta(minutes=30)) -> Approval:
    """Called by the approval UI with IdP-verified identity; ttl is a proposal value."""
    if type(draft) is not WorkOrderDraft:
        raise TypeError('expected a WorkOrderDraft')
    if type(ttl) is not timedelta or not timedelta(minutes=1) <= ttl <= timedelta(hours=8):
        raise ValueError('ttl must be between 1 minute and 8 hours')
    _aware(now, 'now')
    roles = frozenset(roles)
    _check_approver(draft, approver, roles)
    return Approval(draft.tenant, draft.digest, approver, roles, now, now + ttl)


@dataclass(frozen=True)
class Result:
    work_order_id: str
    asset_id: str
    version: int
    replayed: bool = False


class WorkOrderStore:
    """In-memory stand-in for one tenant's maintenance write API (thread-safe)."""

    def __init__(self, tenant, versions):
        _name(tenant, 'tenant')
        if not isinstance(versions, dict) or not versions:
            raise ValueError('versions must map asset IDs to current versions')
        for asset, version in versions.items():
            if type(asset) is not str or not _ASSET.fullmatch(asset):
                raise ValueError('invalid asset id')
            if type(version) is not int or version < 1:
                raise ValueError('version must be a positive int')
        self.tenant = tenant
        self._lock = threading.Lock()
        self._versions = dict(versions)
        self._keys = {}          # key -> (fingerprint, Result)
        self._executed = set()   # draft digests already written
        self._orders = []        # work orders actually written

    def version(self, asset_id) -> int:
        with self._lock:
            return self._versions[asset_id]

    @property
    def writes(self) -> int:
        with self._lock:
            return len(self._orders)

    def execute(self, draft, approval, idempotency_key, expected_version, now) -> Result:
        if type(draft) is not WorkOrderDraft or type(approval) is not Approval:
            raise TypeError('expected a WorkOrderDraft and an Approval')
        if type(idempotency_key) is not str or not _KEY.fullmatch(idempotency_key):
            raise ValueError('idempotency key must be 16..255 of [A-Za-z0-9_-]')
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError('expected_version must be a positive int')
        _aware(now, 'now')
        if draft.tenant != self.tenant or approval.tenant != self.tenant:
            raise PermissionError('draft or approval belongs to another tenant')
        fingerprint = _sha256([draft.digest, expected_version])
        with self._lock:
            stored = self._keys.get(idempotency_key)
            if stored is not None:
                if stored[0] != fingerprint:
                    raise IdempotencyConflict('idempotency key reused with a different payload')
                return replace(stored[1], replayed=True)
            if approval.draft_digest != draft.digest:
                raise PermissionError('approval does not cover this draft')
            _check_approver(draft, approval.approver, approval.roles)
            if not approval.approved_at <= now < approval.expires_at:
                raise PermissionError('approval is not valid at this time')
            if draft.digest in self._executed:
                raise ValueError('draft already executed under another key')
            if expected_version != draft.asset_version:
                raise ValueError('expected_version differs from the approved draft')
            current = self._versions.get(draft.asset_id)
            if current is None:
                raise ValueError('unknown asset')
            if expected_version != current:
                raise StaleVersion(f'expected version {expected_version}, current {current}')
            result = self._write(draft, current)
            self._executed.add(draft.digest)
            self._keys[idempotency_key] = (fingerprint, result)
            return result

    def _write(self, draft, current) -> Result:
        """The single side effect, called with the lock held (a real adapter calls the API)."""
        self._versions[draft.asset_id] = current + 1
        self._orders.append(draft.digest)
        return Result(f'WO-{len(self._orders):06d}', draft.asset_id, current + 1)

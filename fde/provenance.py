"""Record which versions and positions were used, NOT answer correctness.

Store identifiers and versions only, never question, answer or chunk text, or
raw identities. The earliest differing boundary starts investigation, not a
proven root cause; later boundaries can differ as a consequence.

snapshot_id names the serving DB recovery target (backup ID or PITR target).
source_positions holds one monotonic integer committed offset per upstream
source key; encode an epoch in the key (e.g. 'erp:epoch-3'), not a global LSN.
Version strings must name immutable artifacts from the release manifest; this
module checks their format only. The 20-evidence cap is a proposed bound.
"""
from dataclasses import dataclass
import re
from uuid import UUID


BOUNDARIES = ('data', 'policy', 'retrieval', 'generation')
_FIELDS = {
    'data': ('snapshot_id', 'source_positions'),
    'policy': ('policy_version',),
    'retrieval': ('index_version', 'embedding_model', 'reranker', 'evidence'),
    'generation': ('prompt_version', 'model_digest'),
}


def _identifier(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9._:/@+-]{1,200}', value):
        raise ValueError('invalid identifier')


def _pairs(items, minimum):
    if type(items) is not tuple:
        raise ValueError('expected tuple')
    names = []
    for item in items:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError('invalid pair')
        name, position = item
        _identifier(name)
        if type(position) is not int or position < minimum:
            raise ValueError('invalid position or version')
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError('duplicate identifier')
    return names


@dataclass(frozen=True)
class AnswerRecord:
    request_id: str
    tenant: str
    subject_pseudonym: str
    key_id: str
    snapshot_id: str
    source_positions: tuple[tuple[str, int], ...]
    policy_version: str
    index_version: str
    embedding_model: str
    reranker: str | None
    prompt_version: str
    model_digest: str
    evidence: tuple[tuple[str, int], ...]
    abstained: bool

    def __post_init__(self):
        try:
            canonical = str(UUID(self.request_id)) if type(self.request_id) is str else None
        except ValueError:
            canonical = None
        if canonical is None or canonical != self.request_id:
            raise ValueError('invalid request id')
        for name in ('tenant', 'snapshot_id', 'policy_version', 'index_version',
                     'embedding_model', 'prompt_version'):
            _identifier(getattr(self, name))
        if self.reranker is not None:
            _identifier(self.reranker)
        if (type(self.subject_pseudonym) is not str
                or not re.fullmatch(r'[0-9a-f]{64}', self.subject_pseudonym)):
            raise ValueError('invalid subject pseudonym')
        # Same key ID format as fde/privacy.py; needed to re-derive after rotation.
        if type(self.key_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', self.key_id):
            raise ValueError('invalid key id')
        if (type(self.model_digest) is not str
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', self.model_digest)):
            raise ValueError('invalid model digest')
        sources = _pairs(self.source_positions, 0)
        if not sources or sources != sorted(sources):
            raise ValueError('empty or unsorted sources')
        _pairs(self.evidence, 1)
        if len(self.evidence) > 20:
            raise ValueError('too much evidence')
        if type(self.abstained) is not bool:
            raise ValueError('invalid abstention')
        if not self.abstained and not self.evidence:
            raise ValueError('answer requires evidence')


def localize(reported: AnswerRecord, replayed: AnswerRecord) -> list[str]:
    """List changed input boundaries in pipeline order, not proven causes.

    Empty differences with a changed answer point to an unrecorded input, such
    as serving engine or batch config, or to serving nondeterminism; abstention
    is an outcome, not an input. Pseudonyms compare only under the same key ID.
    """
    if type(reported) is not AnswerRecord or type(replayed) is not AnswerRecord:
        raise ValueError('invalid answer record')
    if reported.tenant != replayed.tenant:
        raise PermissionError('cross-tenant comparison refused')
    if (reported.request_id != replayed.request_id
            or reported.key_id != replayed.key_id
            or reported.subject_pseudonym != replayed.subject_pseudonym):
        raise ValueError('request, key or principal differs')
    return [boundary for boundary in BOUNDARIES
            if any(getattr(reported, field) != getattr(replayed, field)
                   for field in _FIELDS[boundary])]

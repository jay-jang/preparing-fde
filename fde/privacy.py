"""Demonstrate minimization and keyed pseudonyms, not universal PII detection."""
import hashlib
import hmac
import json
import re
from uuid import UUID


def mask_contact_fields(record):
    # Never forward unspecified source fields into the searchable projection.
    return {'asset_id': str(UUID(record['asset_id'])),
            'contact_email': '[REDACTED]' if record.get('contact_email') else None,
            'contact_phone': '[REDACTED]' if record.get('contact_phone') else None}


def mask_known_patterns(text):
    # Secondary defense only: misses names, OCR corruption and unknown formats.
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[EMAIL]', text)
    return re.sub(r'(?<!\d)01[016789][- .]?\d{3,4}[- .]?\d{4}(?!\d)', '[PHONE]', text)


def audit_record(*, key, key_id, tenant, subject, request_id, decision, artifact_digest):
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError('use a KMS-managed key of at least 32 random bytes')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', key_id):
        raise ValueError('invalid key id')
    if decision not in ('allow', 'deny', 'abstain', 'error'):
        raise ValueError('invalid decision')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', artifact_digest):
        raise ValueError('invalid artifact digest')
    def pseudonym(kind, value):
        if not isinstance(value, str) or not 1 <= len(value) <= 200:
            raise ValueError('invalid identity')
        message = json.dumps([kind, tenant, value], ensure_ascii=False).encode()
        return hmac.new(key, message, hashlib.sha256).hexdigest()
    return {'key_id': key_id, 'tenant_hash': pseudonym('tenant', tenant),
            'actor_hash': pseudonym('subject', subject),
            'request_id': str(UUID(request_id)), 'decision': decision,
            'artifact_digest': artifact_digest}

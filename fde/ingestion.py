"""At-least-once input -> idempotent, monotonic materialized asset state.

The adapter must translate CDC positions into a comparable (epoch, offset, seq)
per authoritative source. Kafka partition reassignment/repartition needs a new
epoch and a coordinated bootstrap. Timestamps are NOT used as ordering keys.
"""
from dataclasses import dataclass
import hashlib
import json

from psycopg.types.json import Jsonb
from psycopg.pq import TransactionStatus


@dataclass(frozen=True)
class Event:
    tenant: str
    source: str
    source_key: str
    epoch: int
    offset: int
    sequence: int
    schema_version: int
    op: str
    payload: dict

    def normalize(self) -> dict:
        for value in (self.tenant, self.source, self.source_key):
            if not isinstance(value, str) or not 0 < len(value) <= 200:
                raise ValueError('invalid identity')
        for value in (self.epoch, self.offset, self.sequence):
            if type(value) is not int or not 0 <= value < 2**63:
                raise ValueError('invalid source position')
        if type(self.schema_version) is not int or self.schema_version not in (1, 2):
            raise ValueError('unknown schema: quarantine before acknowledgement')
        if self.op not in ('upsert', 'delete') or not isinstance(self.payload, dict):
            raise ValueError('invalid operation')
        if self.op == 'delete':
            if self.payload:
                raise ValueError('delete envelope must have an empty payload')
            return {'name': '', 'status': 'unknown', 'criticality': 3}
        keys = {'asset_name', 'state'} if self.schema_version == 1 else {'name', 'status', 'criticality'}
        required = {'asset_name', 'state'} if self.schema_version == 1 else {'name', 'status'}
        if not required <= self.payload.keys() or self.payload.keys() - keys:
            raise ValueError('schema mismatch')
        name = self.payload['asset_name' if self.schema_version == 1 else 'name']
        status = self.payload['state' if self.schema_version == 1 else 'status']
        criticality = self.payload.get('criticality', 3)
        if not isinstance(name, str) or not 0 < len(name) <= 200:
            raise ValueError('invalid name')
        if status not in ('running', 'stopped', 'unknown'):
            raise ValueError('invalid status')
        if type(criticality) is not int or not 1 <= criticality <= 5:
            raise ValueError('invalid criticality')
        return {'name': name, 'status': status, 'criticality': criticality}

    @property
    def event_id(self) -> str:
        identity = [self.tenant, self.source, self.source_key,
                    self.epoch, self.offset, self.sequence]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def apply_event(conn, event: Event) -> str:
    """Return applied/stale/duplicate. Caller ACKs only AFTER this call commits.

    conn must be idle on entry. A nested transaction in a caller would delay the
    commit, so consumers must use the documented autocommit connection pattern.
    """
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError('use a dedicated autocommit=True connection')
    normalized = event.normalize()
    payload_hash = hashlib.sha256(json.dumps(
        [event.schema_version, event.op, event.payload],
        sort_keys=True, ensure_ascii=False, allow_nan=False,
    ).encode()).hexdigest()
    with conn.transaction():
        conn.execute("SET LOCAL lock_timeout = '2s'")
        conn.execute("SET LOCAL statement_timeout = '3s'")
        identity = conn.execute(
            'SELECT asset_id FROM fde.identity_map '
            'WHERE tenant_id=%s AND source=%s AND source_key=%s AND authoritative',
            (event.tenant, event.source, event.source_key),
        ).fetchone()
        if identity is None:
            raise ValueError('unmapped or non-authoritative identity')
        receipt = conn.execute(
            'INSERT INTO fde.receipts(event_id,payload_hash) VALUES (%s,%s) '
            'ON CONFLICT DO NOTHING RETURNING event_id',
            (event.event_id, payload_hash),
        ).fetchone()
        if receipt is None:
            previous = conn.execute(
                'SELECT payload_hash FROM fde.receipts WHERE event_id=%s',
                (event.event_id,),
            ).fetchone()[0]
            if previous != payload_hash:
                raise ValueError('same event identity with different payload')
            return 'duplicate'
        result = conn.execute('''
            INSERT INTO fde.assets AS a
                (tenant_id,asset_id,source,name,status,criticality,
                 epoch,source_offset,event_sequence,deleted)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (tenant_id,asset_id) DO UPDATE SET
                source=EXCLUDED.source, name=EXCLUDED.name,
                status=EXCLUDED.status, criticality=EXCLUDED.criticality,
                epoch=EXCLUDED.epoch, source_offset=EXCLUDED.source_offset,
                event_sequence=EXCLUDED.event_sequence, deleted=EXCLUDED.deleted
            WHERE a.source=EXCLUDED.source
              AND (a.epoch,a.source_offset,a.event_sequence)
                < (EXCLUDED.epoch,EXCLUDED.source_offset,EXCLUDED.event_sequence)
            RETURNING asset_id
        ''', (event.tenant, identity[0], event.source, normalized['name'],
              normalized['status'], normalized['criticality'], event.epoch,
              event.offset, event.sequence, event.op == 'delete')).fetchone()
        outcome = 'applied' if result else 'stale'
        conn.execute(
            'INSERT INTO fde.audit_outbox(event_id,record) VALUES (%s,%s)',
            (event.event_id, Jsonb({'event_id': event.event_id,
                                  'schema_version': event.schema_version,
                                  'outcome': outcome, 'operation': event.op})),
        )
    return outcome

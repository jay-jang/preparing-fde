"""RLS first, exact BM25 + cosine + RRF for a bounded pilot corpus.

No tokenizer/embedding is presented as a production Korean language model.
Use the same versioned tokenizer in queries and documents; replace the simple
tokenizer with a validated Korean analyzer when the evaluation requires it.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass
import math
import re
from psycopg.pq import TransactionStatus


@dataclass(frozen=True)
class Principal:
    # Only the gateway constructs this from a verified token and server mapping.
    tenant: str
    subject: str


@dataclass(frozen=True)
class Chunk:
    id: str
    version: int
    text: str
    embedding: tuple[float, ...]


def tokens(text):
    return re.findall(r'[\w-]+', text.casefold())


def cosine(left, right):
    if not left or len(left) != len(right):
        raise ValueError('embedding dimensions differ')
    if not all(type(x) in (float, int) and math.isfinite(x) for x in (*left, *right)):
        raise ValueError('non-finite embedding')
    a, b = math.hypot(*left), math.hypot(*right)
    if not a or not b:
        raise ValueError('zero embedding')
    return math.fsum((x / a) * (y / b) for x, y in zip(left, right))


def bm25(query, documents, k1=1.2, b=0.75):
    """BM25 over authorized documents only; global IDF is not leaked."""
    query_terms = set(tokens(query))
    terms = [Counter(tokens(doc.text)) for doc in documents]
    lengths = [sum(t.values()) for t in terms]
    avg = sum(lengths) / len(lengths) if lengths else 0
    if not avg:
        return {}
    df = Counter(term for doc in terms for term in doc)
    result = {}
    for doc, counts, length in zip(documents, terms, lengths):
        score = 0.0
        for term in query_terms:
            tf = counts[term]
            idf = math.log1p((len(documents) - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / avg))
        if score > 0:
            result[doc.id] = score
    return result


def rank(query, vector, chunks, k=5, window=50):
    if type(k) is not int or not 1 <= k <= window <= 100:
        raise ValueError('invalid retrieval budget')
    # Validate the query even when the authorized corpus is empty.
    cosine(vector, vector)
    sparse = bm25(query, chunks)
    dense = {c.id: cosine(vector, c.embedding) for c in chunks}
    fused = defaultdict(float)
    for scores in (sparse, dense):
        ordered = sorted(scores, key=lambda key: (-scores[key], key))[:window]
        for position, key in enumerate(ordered, 1):
            fused[key] += 1 / (60 + position)
    # RRF is a ranking score, never an answer-confidence probability.
    ids = sorted(fused, key=lambda key: (-fused[key], key))[:k]
    by_id = {c.id: c for c in chunks}
    return [by_id[key] for key in ids]


def retrieve(conn, principal: Principal, query: str, vector, model: str, cap=5000):
    """Non-owner fde_reader connection, no browser-supplied roles or tenant.

    conn.transaction() also scopes set_config(..., true) to the transaction;
    pooled sessions therefore do not retain identity settings after commit.
    """
    if (not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE
            or type(cap) is not int or not 1 <= cap <= 5000):
        raise ValueError('use an idle autocommit connection and a bounded cap')
    if not principal.tenant or not principal.subject or not query.strip() or len(query) > 2000:
        raise ValueError('invalid request')
    with conn.transaction():
        conn.execute("SET LOCAL statement_timeout = '2s'")
        conn.execute("SELECT set_config('app.tenant',%s,true)", (principal.tenant,))
        conn.execute("SELECT set_config('app.subject',%s,true)", (principal.subject,))
        rows = conn.execute(
            'SELECT chunk_id,document_version,content,embedding '
            'FROM fde.chunks WHERE embedding_model=%s ORDER BY chunk_id LIMIT %s',
            (model, cap + 1),
        ).fetchall()
    if len(rows) > cap:
        raise ValueError('pilot corpus limit: use indexed retrieval, do not truncate silently')
    chunks = [Chunk(row[0], row[1], row[2], tuple(row[3])) for row in rows]
    return rank(query, vector, chunks)


def revalidate_evidence(conn, principal: Principal, evidence: list[Chunk], model: str):
    """Fresh READ COMMITTED statement before emitting an answer; no response cache.

    This bounds the revocation window but cannot make a network response atomic
    with a concurrent policy change. The caller also bounds request lifetime.
    """
    if (not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE
            or not 1 <= len(evidence) <= 5 or len({c.id for c in evidence}) != len(evidence)):
        raise ValueError('invalid evidence revalidation request')
    with conn.transaction():
        conn.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        conn.execute("SET LOCAL statement_timeout = '2s'")
        conn.execute("SELECT set_config('app.tenant',%s,true)", (principal.tenant,))
        conn.execute("SELECT set_config('app.subject',%s,true)", (principal.subject,))
        rows = conn.execute(
            'SELECT chunk_id,document_version,content FROM fde.chunks '
            'WHERE chunk_id=ANY(%s) AND embedding_model=%s',
            ([chunk.id for chunk in evidence], model),
        ).fetchall()
    current = {row[0]: (row[1], row[2]) for row in rows}
    if any(current.get(c.id) != (c.version, c.text) for c in evidence):
        raise PermissionError('evidence is no longer accessible or current')

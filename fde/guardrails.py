"""Validate model output structurally and bind quotations to retrieved evidence.

This proves quote provenance, NOT that a claim is semantically entailed.
Independent evaluation and, for consequential actions, human review remain.
"""
import json
import time

from fde.retrieval import Chunk


def validate_answer(raw: str, evidence: list[Chunk]) -> dict:
    if not isinstance(raw, str) or len(raw.encode()) > 16000:
        raise ValueError('output too large')
    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError('duplicate JSON key')
            obj[key] = value
        return obj
    answer = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(answer, dict) or set(answer) != {'abstain', 'claims'}:
        raise ValueError('invalid response schema')
    if type(answer['abstain']) is not bool or not isinstance(answer['claims'], list):
        raise ValueError('invalid response types')
    if answer['abstain']:
        if answer['claims']:
            raise ValueError('abstention cannot contain claims')
        return answer
    if not 1 <= len(answer['claims']) <= 5:
        raise ValueError('missing or excessive claims')
    allowed = {c.id: c for c in evidence}
    for claim in answer['claims']:
        if not isinstance(claim, dict) or set(claim) != {'text', 'chunk_id', 'version', 'quote'}:
            raise ValueError('invalid claim schema')
        if any(not isinstance(claim[key], str) for key in ('text', 'chunk_id', 'quote')):
            raise ValueError('invalid claim types')
        chunk = allowed.get(claim['chunk_id'])
        if chunk is None or type(claim['version']) is not int or claim['version'] != chunk.version:
            raise ValueError('unretrieved or stale citation')
        if not 8 <= len(claim['quote']) <= 1000 or claim['quote'] not in chunk.text:
            raise ValueError('quote is not in the cited evidence')
        if not 1 <= len(claim['text']) <= 1000:
            raise ValueError('invalid claim text')
    return answer


class ToolBudget:
    """Allow only the read-only search tool, with no model-chosen identity.

    The callback must enforce its own I/O timeout from remaining_seconds.
    A deadline checked here cannot interrupt an arbitrary blocking callback.
    """
    def __init__(self, principal, search, seconds=4.0, calls=2, deadline=None):
        # seconds is a cap inside the 5 s request deadline; deadline (time.monotonic()) is the request's own.
        if not 0 < seconds <= 5 or type(calls) is not int or not 1 <= calls <= 3:
            raise ValueError('invalid tool budget')
        now = time.monotonic()
        if deadline is not None and (type(deadline) not in (int, float) or not now < deadline < now + 3600):
            raise ValueError('request deadline must be a future time.monotonic() value')
        self.principal = principal
        self.search = search
        self.deadline = now + seconds if deadline is None else min(now + seconds, deadline)
        self.calls = calls

    def invoke(self, name, arguments):
        if name != 'search' or not isinstance(arguments, dict) or set(arguments) != {'query'}:
            raise ValueError('tool or arguments not allowed')
        query = arguments['query']
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2000:
            raise ValueError('invalid search query')
        remaining = self.deadline - time.monotonic()
        if self.calls <= 0 or remaining <= 0:
            raise TimeoutError('agent budget exhausted')
        self.calls -= 1
        result = self.search(self.principal, query, remaining_seconds=remaining)
        if time.monotonic() > self.deadline:
            raise TimeoutError('tool exceeded deadline')
        return result

"""Evaluate a versioned, independently labelled holdout result set.

Run with: python -m fde.release_gate /path/to/results.json
No evaluation results are invented or included as customer measurements.
"""
import json
import math
import re
import sys


def evaluate(report):
    if (not isinstance(report, dict)
            or not isinstance(report.get('dataset_version'), str)
            or not 1 <= len(report['dataset_version']) <= 200
            or not isinstance(report.get('artifact_digest'), str)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', report['artifact_digest'])):
        raise ValueError('missing dataset or artifact identity')
    rows = report.get('rows')
    if not isinstance(rows, list) or not rows:
        raise ValueError('empty evaluation')
    buckets = {'factoid': [], 'semantic': [], 'unanswerable': [], 'forbidden': []}
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'] or row['id'] in seen:
            raise ValueError('invalid or duplicate example id')
        seen.add(row['id'])
        kind = row.get('kind')
        if kind not in buckets:
            raise ValueError('unknown evaluation slice')
        for field in ('retrieval_hit', 'supported', 'abstained', 'leaked'):
            if type(row.get(field)) is not bool:
                raise ValueError('missing boolean outcome')
        latency = row.get('latency_ms')
        if type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0:
            raise ValueError('invalid latency')
        buckets[kind].append(row)
    # Small samples cannot be promoted as a reliable launch gate.
    failures = [f'{kind}: fewer than 30 examples' for kind, group in buckets.items() if len(group) < 30]
    metrics = {}
    for kind in ('factoid', 'semantic'):
        group = buckets[kind]
        hit_rate = sum(r['retrieval_hit'] for r in group) / len(group) if group else 0.0
        answered = [r for r in group if not r['abstained']]
        support = sum(r['supported'] for r in answered) / len(answered) if answered else 0.0
        coverage = len(answered) / len(group) if group else 0.0
        latencies = sorted(r['latency_ms'] for r in group)
        slice_p95 = latencies[math.ceil(.95 * len(latencies)) - 1] if latencies else None
        metrics[kind] = {'hit_at_5': hit_rate, 'support_rate': support, 'coverage': coverage,
                         'p95_ms': slice_p95}
        if hit_rate < .90 or support < .98 or coverage < .80:
            failures.append(f'{kind}: retrieval/support/coverage failed')
        if slice_p95 is None or slice_p95 > 5000:
            failures.append(f'{kind}: p95 exceeds 5 seconds')
    unanswerable = buckets['unanswerable']
    abstention = sum(r['abstained'] for r in unanswerable) / len(unanswerable) if unanswerable else 0.0
    metrics['correct_abstention'] = abstention
    if abstention < .95:
        failures.append('correct abstention below 95%')
    # Any leakage in ANY slice fails, including a seemingly successful answer.
    metrics['leak_count'] = sum(r['leaked'] for r in rows)
    if metrics['leak_count'] or any(not r['abstained'] for r in buckets['forbidden']):
        failures.append('authorization failure')
    ordered = sorted(r['latency_ms'] for r in rows)
    metrics['p95_ms'] = ordered[math.ceil(.95 * len(ordered)) - 1]
    if metrics['p95_ms'] > 5000:
        failures.append('p95 exceeds 5 seconds')
    return {'pass': not failures, 'metrics': metrics, 'failures': failures}


if __name__ == '__main__':
    try:
        with open(sys.argv[1], encoding='utf-8') as stream:
            result = evaluate(json.load(stream))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result['pass'] else 1)
    except (ValueError, KeyError, TypeError, IndexError, OSError) as error:
        print(json.dumps({'pass': False, 'error': str(error)}))
        raise SystemExit(2)

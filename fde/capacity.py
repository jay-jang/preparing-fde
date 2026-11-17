"""Back-of-envelope arithmetic, NOT measured capacity or a serving guarantee.

Every input is an assumption to replace with load-test measurements.
overhead_bytes is one assumed lump for activations, CUDA graphs and workspace
inside the usable fraction; they are not modeled individually, and neither are
fragmentation or an engine's KV block allocator (e.g. paged KV cache).
Measure the runtime peak and replace the lump.
"""
from dataclasses import dataclass
import math


def _integers(*values, minimum=1):
    if any(type(value) is not int or value < minimum for value in values):
        raise ValueError('invalid integer')


def _positive(value):
    if (type(value) not in (int, float) or value <= 0
            or (type(value) is float and not math.isfinite(value))):
        raise ValueError('invalid positive number')


def vector_bytes(count, dims, bytes_per_value=4) -> int:
    """Raw vectors only; exclude index, replicas, text and metadata."""
    _integers(count, dims, bytes_per_value)
    return count * dims * bytes_per_value


def kv_cache_bytes_per_token(layers, kv_heads, head_dim, bytes_per_value=2) -> int:
    """K and V storage; GQA uses KV heads, not attention heads."""
    _integers(layers, kv_heads, head_dim, bytes_per_value)
    return 2 * layers * kv_heads * head_dim * bytes_per_value


def in_flight_requests(arrival_per_second, latency_seconds) -> float:
    """Little's law gives the long-run steady-state mean, not the peak.

    Size with the peak arrival rate and latency budget, then measure bursts.
    """
    _positive(arrival_per_second)
    _positive(latency_seconds)
    try:
        result = float(arrival_per_second * latency_seconds)
    except OverflowError:
        raise ValueError('non-finite in-flight estimate') from None
    _positive(result)
    return result


@dataclass(frozen=True)
class GpuPlan:
    fits: bool
    usable_bytes: int
    weights_bytes: int
    kv_bytes: int
    overhead_bytes: int
    headroom_bytes: int
    max_sequences: int


def gpu_plan(*, gpu_bytes, utilization, weights_bytes, overhead_bytes,
             kv_bytes_per_token, context_tokens, sequences) -> GpuPlan:
    """Single GPU, no tensor parallelism; overhead is an explicit assumed lump.

    Usable memory is gpu_bytes * utilization; weights, overhead and KV for
    sequences at full context_tokens must fit inside it.
    """
    _integers(gpu_bytes, weights_bytes, kv_bytes_per_token, context_tokens, sequences)
    _integers(overhead_bytes, minimum=0)
    _positive(utilization)
    if utilization > 1:
        raise ValueError('invalid utilization')
    try:
        usable = math.floor(gpu_bytes * utilization)
    except OverflowError:
        raise ValueError('non-finite usable memory') from None
    per_sequence = kv_bytes_per_token * context_tokens
    kv = per_sequence * sequences
    remaining = usable - weights_bytes - overhead_bytes
    headroom = remaining - kv
    return GpuPlan(headroom >= 0, usable, weights_bytes, kv, overhead_bytes,
                   headroom, max(0, remaining // per_sequence))

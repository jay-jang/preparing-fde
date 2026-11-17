"""Closed-network TCO arithmetic with placeholder prices, NOT a quote or a market price.

Money is integer KRW. Monthly components round UP to the next won so cost is
never understated; per-unit costs are exact fractions reported half-up to 0.01
KRW. Ratios (PUE, staff share, utilization, GPU-seconds) are int, Fraction or
Decimal, never float. GPU-seconds per answer must be measured as busy GPU time in a window
divided by answers in that window: continuously batched requests share the GPU,
so summing per-request RUNNING times double counts. Power is the metered server
draw, not the GPU board reading.
"""
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
import math


def _integer(value, name, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(f'{name} must be an integer >= {minimum}')
    return value


def _ratio(value, name, *, maximum=None):
    if type(value) in (int, Fraction) or (isinstance(value, Decimal) and value.is_finite()):
        ratio = Fraction(value)
    else:
        raise TypeError(f'{name} must be int, Fraction or finite Decimal, '
                        f'not {type(value).__name__}')
    if ratio <= 0 or (maximum is not None and ratio > maximum):
        raise ValueError(f'{name} out of range')
    return ratio


def _krw(value):
    """Half-up rounding of a non-negative exact fraction to 0.01 KRW, in integers."""
    return Decimal(math.floor(value * 100 + Fraction(1, 2))).scaleb(-2)


@dataclass(frozen=True)
class NodeCost:
    hardware_krw: int
    power_krw: int
    support_krw: int

    @property
    def total_krw(self):
        return self.hardware_krw + self.power_krw + self.support_krw


def node_monthly_cost(*, hardware_krw, amortization_months, average_power_w, hours_per_month,
                      krw_per_kwh, pue, support_yearly_krw) -> NodeCost:
    """One GPU server per month: straight-line amortization, metered power x PUE, support."""
    _integer(hardware_krw, 'hardware_krw', 0)
    _integer(amortization_months, 'amortization_months')
    _integer(average_power_w, 'average_power_w')
    _integer(hours_per_month, 'hours_per_month')
    _integer(krw_per_kwh, 'krw_per_kwh', 0)
    _integer(support_yearly_krw, 'support_yearly_krw', 0)
    efficiency = _ratio(pue, 'pue')
    if efficiency < 1:
        raise ValueError('pue cannot be below 1')
    power = Fraction(average_power_w * hours_per_month * krw_per_kwh, 1_000) * efficiency
    return NodeCost(math.ceil(Fraction(hardware_krw, amortization_months)), math.ceil(power),
                    math.ceil(Fraction(support_yearly_krw, 12)))


def staff_monthly_cost(monthly_krw, share) -> int:
    """The share of an operations team's monthly cost assigned to this service."""
    _integer(monthly_krw, 'monthly_krw', 0)
    return math.ceil(monthly_krw * _ratio(share, 'share', maximum=1))


def gpu_seconds_per_answer(busy_gpu_seconds, answered) -> Fraction:
    """Busy GPU time in a measurement window divided by the answers in that window."""
    _integer(answered, 'answered')
    return _ratio(busy_gpu_seconds, 'busy_gpu_seconds') / answered


def krw_per_busy_gpu_second(monthly_total_krw, available_gpu_seconds, utilization) -> Decimal:
    """Fixed monthly cost spread over the GPU-seconds actually busy at a measured utilization."""
    _integer(monthly_total_krw, 'monthly_total_krw', 0)
    _integer(available_gpu_seconds, 'available_gpu_seconds')
    busy = available_gpu_seconds * _ratio(utilization, 'utilization', maximum=1)
    return _krw(monthly_total_krw / busy)


@dataclass(frozen=True)
class UnitCost:
    answered_per_month: int
    nodes: int
    utilization: Fraction
    monthly_total_krw: int
    krw_per_answer: Decimal
    krw_per_busy_gpu_second: Decimal


def unit_cost(*, node_krw, shared_krw, answered_per_month, gpu_seconds_per_answer,
              seconds_per_month, max_utilization, gpus_per_node=1) -> UnitCost:
    """Cost per answered query at a monthly volume; adds nodes past max_utilization.

    The cost of a busy GPU-second is the fixed cost divided by the busy seconds
    the volume produces, so per answer = gpu_seconds_per_answer x that cost.
    """
    _integer(node_krw, 'node_krw', 0)
    _integer(shared_krw, 'shared_krw', 0)
    _integer(answered_per_month, 'answered_per_month')
    _integer(seconds_per_month, 'seconds_per_month')
    _integer(gpus_per_node, 'gpus_per_node')
    per_answer = _ratio(gpu_seconds_per_answer, 'gpu_seconds_per_answer')
    ceiling = _ratio(max_utilization, 'max_utilization', maximum=1)
    busy = answered_per_month * per_answer
    per_node = gpus_per_node * seconds_per_month
    nodes = max(1, math.ceil(busy / (per_node * ceiling)))
    total = nodes * node_krw + shared_krw
    per_busy_second = Fraction(total) / busy
    return UnitCost(answered_per_month, nodes, busy / (nodes * per_node), total,
                    _krw(per_answer * per_busy_second), _krw(per_busy_second))

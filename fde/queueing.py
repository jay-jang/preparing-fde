"""M/M/c (Erlang C) queueing as a sizing aid, NOT a model of an LLM serving engine.

Each concurrent decode slot is treated as one server with exponential service
times and FCFS order. Continuous batching makes the service time depend on how
many slots are busy, so service_seconds must be the mean RUNNING time measured
at that slot count in a load test. smallest_servers() accepts such measurements
and never extrapolates past the measured slot counts. Erlang B is computed with
the iterative recursion, so large server counts do not overflow factorials.
"""
from collections.abc import Mapping
from dataclasses import dataclass
import math

MAX_SERVERS = 100_000


def _servers(value):
    if type(value) is not int or not 1 <= value <= MAX_SERVERS:
        raise ValueError('servers must be an integer in 1..100000')


def _positive(value, name):
    if (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
        raise ValueError(f'{name} must be a finite positive number')


def erlang_b(servers, offered_load) -> float:
    """Blocking probability of M/M/c/c via B(k) = a*B(k-1) / (k + a*B(k-1))."""
    _servers(servers)
    _positive(offered_load, 'offered_load')
    blocking = 1.0
    for k in range(1, servers + 1):
        blocking = offered_load * blocking / (k + offered_load * blocking)
    return blocking


def erlang_c(servers, offered_load) -> float:
    """Probability that an arrival waits; only defined for offered_load < servers."""
    blocking = erlang_b(servers, offered_load)
    if offered_load >= servers:
        raise ValueError('unstable: offered load must be below the number of servers')
    return servers * blocking / (servers - offered_load * (1 - blocking))


@dataclass(frozen=True)
class QueueEstimate:
    servers: int
    arrival_per_second: float
    service_seconds: float
    offered_load: float
    utilization: float
    wait_probability: float
    mean_wait_seconds: float

    def wait_exceeds(self, seconds) -> float:
        """P(queue wait > seconds) = C * exp(-(c*mu - lambda) * t) for FCFS M/M/c."""
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError('wait must be a finite non-negative number')
        drain = self.servers / self.service_seconds - self.arrival_per_second
        return self.wait_probability * math.exp(-drain * seconds)


def mmc(arrival_per_second, service_seconds, servers) -> QueueEstimate:
    """Steady-state M/M/c estimate; raises instead of reporting an unstable queue."""
    _positive(arrival_per_second, 'arrival_per_second')
    _positive(service_seconds, 'service_seconds')
    _servers(servers)
    offered = arrival_per_second * service_seconds
    if not math.isfinite(offered):
        raise ValueError('non-finite offered load')
    waiting = erlang_c(servers, offered)
    mean_wait = waiting * service_seconds / (servers - offered)
    return QueueEstimate(servers, float(arrival_per_second), float(service_seconds), offered,
                         offered / servers, waiting, mean_wait)


def smallest_servers(arrival_per_second, service_seconds, *, wait_seconds, max_probability,
                     max_servers=1_024) -> QueueEstimate:
    """Smallest slot count with P(wait > wait_seconds) <= max_probability.

    service_seconds is one assumed mean for every slot count, or a mapping
    {slots: mean RUNNING seconds measured at that many slots}. With a mapping
    only measured slot counts are tried; none passing raises ValueError.
    """
    _positive(arrival_per_second, 'arrival_per_second')
    if (type(wait_seconds) not in (int, float) or not math.isfinite(wait_seconds)
            or wait_seconds < 0):
        raise ValueError('wait_seconds must be a finite non-negative number')
    if type(max_probability) is not float or not 0 < max_probability < 1:
        raise ValueError('max_probability must be a float in (0, 1)')
    if isinstance(service_seconds, Mapping):
        if not service_seconds:
            raise ValueError('no measured slot counts')
        for servers, seconds in service_seconds.items():
            _servers(servers)
            _positive(seconds, 'service_seconds')
        candidates = sorted(service_seconds.items())
    else:
        _positive(service_seconds, 'service_seconds')
        _servers(max_servers)
        candidates = [(servers, service_seconds) for servers in range(1, max_servers + 1)]
    for servers, seconds in candidates:
        if arrival_per_second * seconds >= servers:
            continue
        estimate = mmc(arrival_per_second, seconds, servers)
        if estimate.wait_exceeds(wait_seconds) <= max_probability:
            return estimate
    raise ValueError('no allowed or measured slot count meets the wait target')

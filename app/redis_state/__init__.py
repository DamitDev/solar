from .connection import init_redis, close_redis, redis_client
from .registry import RegistryStore
from .health import HealthStore
from .routing import RoutingStore
from .hosts import HostConnectionStore

registry_store = RegistryStore()
health_store = HealthStore()
routing_store = RoutingStore()
host_store = HostConnectionStore()

__all__ = [
    "init_redis",
    "close_redis",
    "redis_client",
    "registry_store",
    "health_store",
    "routing_store",
    "host_store",
]

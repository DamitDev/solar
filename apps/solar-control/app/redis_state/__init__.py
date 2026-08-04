from .connection import close_redis, init_redis, redis_client
from .health import HealthStore
from .hosts import HostConnectionStore
from .registry import RegistryStore
from .routing import RoutingStore

registry_store = RegistryStore()
health_store = HealthStore()
routing_store = RoutingStore()
host_store = HostConnectionStore()

__all__ = [
    "close_redis",
    "health_store",
    "host_store",
    "init_redis",
    "redis_client",
    "registry_store",
    "routing_store",
]

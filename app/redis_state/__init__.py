from .connection import init_redis, close_redis, redis_client
from .registry import RegistryStore
from .health import HealthStore
from .routing import RoutingStore

registry_store = RegistryStore()
health_store = HealthStore()
routing_store = RoutingStore()

__all__ = [
    "init_redis",
    "close_redis",
    "redis_client",
    "registry_store",
    "health_store",
    "routing_store",
]

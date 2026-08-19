"""The cursor_proxy package: /cursor/v1 DeepSeek-Cursor compatibility proxy.

Ported from yxlao/deepseek-cursor-proxy (MIT) and adapted to run inside
solar-control: the HTTP handler becomes FastAPI routes (app.routes.cursor),
the SQLite reasoning cache becomes Redis (app.cursor_proxy.reasoning_store),
and the upstream target is solar-control's own /v1 gateway via a self-HTTP
call that reuses the caller's API key.
"""

__all__ = [
    "ProxyConfig",
    "ReasoningStore",
    "alias_model_entries",
    "proxy_config_from_settings",
]

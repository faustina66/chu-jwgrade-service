from .base import (
    Adapter,
    LoginFailed,
    LoginNeedsHuman,
    LoginTransient,
    SessionExpired,
)
from .chd import ChdAdapter
from .mock import MockAdapter

_REGISTRY: dict[str, type[Adapter]] = {
    "chd": ChdAdapter,
    "mock": MockAdapter,
}


def available_names() -> tuple[str, ...]:
    """返回当前实际注册的适配器名，供配置边界复用。"""
    return tuple(_REGISTRY)


def get(name: str, cfg: dict) -> Adapter:
    try:
        return _REGISTRY[name](cfg)
    except KeyError:
        available = ", ".join(_REGISTRY)
        raise ValueError(f"未知的适配器 {name!r}，可选: {available}") from None


__all__ = ["Adapter", "LoginFailed", "LoginNeedsHuman", "LoginTransient",
           "SessionExpired",
           "available_names", "get"]

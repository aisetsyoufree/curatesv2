"""Stored-evidence pipeline; the synthetic slice is loaded only when requested."""

from typing import Any

__all__ = ["SliceRun", "run_fixture"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from cu_rates.pipeline import slice

        return getattr(slice, name)
    raise AttributeError(name)

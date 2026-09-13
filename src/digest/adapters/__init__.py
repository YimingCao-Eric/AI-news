"""Source adapters: fetch one source, map it to `Item`. Nothing else -- see `base`."""

from digest.adapters.base import Adapter
from digest.adapters.hn import HNAdapter

__all__ = ["Adapter", "HNAdapter"]

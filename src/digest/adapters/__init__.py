"""Source adapters: fetch one source, map it to `Item`. Nothing else -- see `base`."""

from digest.adapters.ai_blogs import AIBlogsAdapter
from digest.adapters.arxiv import ArxivAdapter
from digest.adapters.base import Adapter
from digest.adapters.gh_trending import GhTrendingAdapter
from digest.adapters.hf_papers import HFPapersAdapter
from digest.adapters.hn import HNAdapter

__all__ = [
    "AIBlogsAdapter",
    "Adapter",
    "ArxivAdapter",
    "GhTrendingAdapter",
    "HFPapersAdapter",
    "HNAdapter",
]

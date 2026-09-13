"""AI-news: a personal daily digest.

Pipeline: fetch -> normalise -> store -> rank -> summarise -> render -> deliver.
Each stage is a separate module and a separate CLI subcommand; see CLAUDE.md for the
layering rules that keep them from leaking into each other.
"""

__version__ = "0.1.0"

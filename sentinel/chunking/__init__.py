"""Chunking strategies.

Importing this package populates the strategy registry. The `@register` decorators in
`strategies.py` only run when that module is imported, so without this line `get_chunker("...")`
raises "unknown strategy" against an empty registry — a failure that looks like a typo and is
really an import-order problem.

Kept here rather than inside `get_chunker` so the registry is complete for anything that inspects
it, including the benchmark sweeps that enumerate strategies.
"""

from sentinel.chunking import strategies as _strategies  # noqa: F401  (registers strategies)
from sentinel.chunking.base import REGISTRY, BaseChunker, get_chunker, ntokens, strategies

__all__ = ["REGISTRY", "BaseChunker", "get_chunker", "ntokens", "strategies"]

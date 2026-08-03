"""Read the local Microsoft Teams (v2) message cache — no Graph, no network."""
from .reader import Account, Message, TeamsCacheReader, find_cache

__all__ = ["TeamsCacheReader", "Account", "Message", "find_cache"]
__version__ = "0.1.0"

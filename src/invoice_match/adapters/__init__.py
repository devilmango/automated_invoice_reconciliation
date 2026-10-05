"""Provider-neutral accounts-payable adapter contracts and implementations."""

from .base import APAdapter
from .factory import create_adapter

__all__ = ["APAdapter", "create_adapter"]

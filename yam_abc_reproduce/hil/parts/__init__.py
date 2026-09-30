"""Optional PARTS client. No module in this package constructs a motor SDK."""

from .config import PartsConfig
from .controller import PartsClient

__all__ = ["PartsConfig", "PartsClient"]

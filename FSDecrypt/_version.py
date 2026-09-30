"""Single source of truth for the package version.

Kept free of imports on purpose: ``pyproject.toml`` reads this attribute while
building, and setuptools must be able to resolve it statically.
"""

__version__ = "0.3.0"

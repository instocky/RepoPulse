"""Allow ``python -m repo_pulse`` to invoke the CLI.

The CLI itself is wired in ticket 11. Until then, ``python -m repo_pulse``
prints the package version and exits 0 — enough for ticket 01's
"importable scaffold" acceptance check.
"""

from __future__ import annotations

import sys

from repo_pulse import __version__


def main() -> int:
    """Print the package version. Returns 0."""
    print(f"repo-pulse {__version__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

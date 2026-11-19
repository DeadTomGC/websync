"""Local .env loading (cross-platform: Linux, macOS, Windows).

Credentials and server addresses come only from environment variables. In
GitLab CI they are CI/CD variables; for local development (where sourcing a
``.env`` file is not an option, especially on Windows) they live in ``.env``
next to the repo checkout and are loaded automatically by the CLI and the
test suite.

Real environment variables always win over ``.env`` values, so CI keeps
working unchanged.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import find_dotenv, load_dotenv


def load_env() -> Path | None:
    """Load ``.env`` from the current directory or the nearest parent.

    Uses the CWD (not this module's location) so it works both from a source
    checkout and from an installed package, on any OS. Existing environment
    variables are never overridden.

    Returns the loaded file's path, or ``None`` if no ``.env`` was found.
    """
    # find_dotenv(usecwd=True) walks up from the current working directory
    # until it finds a ".env" file; returns "" when there is none.
    found = find_dotenv(usecwd=True)
    if not found:
        return None
    load_dotenv(found, override=False)
    return Path(found)

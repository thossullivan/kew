"""Public URLs and install commands that runtime messages show to users.

Keep them here so the public launch changes one line, not every message
(#2004). kew installs two ways: the published ``kew-cli`` package on PyPI,
or a local source checkout. Upgrade hints name the PyPI command and tell a
source install to reinstall from its checkout. Never point a user at a git
install of the development repository: that URL 404s for public users. The
docs live on kew.dev.
"""

DOCS_BASE_URL = "https://kew.dev/docs"
UPGRADE_COMMAND = "uv tool install --upgrade kew-cli"
UPGRADE_HINT = (
    f"{UPGRADE_COMMAND} "
    "(if you installed kew from a source checkout, reinstall it from that checkout)"
)


def docs_url(page: str) -> str:
    """Return the kew.dev docs URL for *page* (for example ``authentication``)."""
    return f"{DOCS_BASE_URL}/{page.strip('/')}/"

"""Public URLs and install commands that runtime messages show to users.

Keep them here so the public launch changes one line, not every message
(#2004). kew installs two ways: the published ``kew-cli`` package on PyPI,
or a local source checkout. Upgrade hints name the PyPI command and tell a
source install to reinstall from its checkout. The upgrade command names
the ``serve`` extra and replaces the requirement: ``uv tool install --upgrade
kew-cli`` would drop the extra and remove the web console, and ``uv tool
upgrade`` keeps the install's version limit, so a ``create-kew`` install
(``kew-cli[serve]>=1,<2``) would never reach the next major release. Never
point a user at a git install of the development repository: that URL 404s
for public users. The docs live on kew.dev.
"""

DOCS_BASE_URL = "https://kew.dev/docs"
UPGRADE_COMMAND = 'uv tool install --upgrade "kew-cli[serve]"'
UPGRADE_HINT = (
    f"{UPGRADE_COMMAND} "
    "(if you installed kew from a source checkout, reinstall it from that checkout)"
)
# What `kew serve` prints when the web console's dependencies are missing.
# Not `pipx install --force`: it rebuilds the venv with pipx's own Python and
# ignores `--python`, which breaks a 3.13 install when pipx runs on 3.14.
SERVE_EXTRA_INSTALL = (
    'uv tool install "kew-cli[serve]" (with pipx: pipx uninstall kew-cli, then '
    'pipx install --python python3.13 "kew-cli[serve]")'
)


def docs_url(page: str) -> str:
    """Return the kew.dev docs URL for *page* (for example ``authentication``)."""
    return f"{DOCS_BASE_URL}/{page.strip('/')}/"

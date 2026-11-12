"""Run kew as ``python -m kew``, the same Typer app as the ``kew`` script."""

from kew.cli import app

if __name__ == "__main__":
    app(prog_name="kew")

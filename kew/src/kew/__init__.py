"""kew — Autonomous issue-to-PR dispatch for Claude Code."""

from pathlib import Path

from kew.budget import BudgetExceeded, BudgetStatus, check_budget
from kew.pricing import estimate_cost
from kew.run_store import RunRecord, RunStore, SqliteRunStore

__version__ = "1.0.0"

_TEMPLATES_DIR = Path(__file__).parent / "_templates"

__all__ = [
    "BudgetExceeded",
    "BudgetStatus",
    "RunRecord",
    "RunStore",
    "SqliteRunStore",
    "_TEMPLATES_DIR",
    "check_budget",
    "estimate_cost",
]

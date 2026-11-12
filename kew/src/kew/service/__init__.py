"""The single read/query surface over runs + flight recordings.

kew.tui and kew.serve consume runs ONLY through this package (kew.serve is
guarded by an import-linter contract). All business logic stays here, in
Python — frontends are pure views.
"""

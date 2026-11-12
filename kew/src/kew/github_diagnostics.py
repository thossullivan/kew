"""Classify confirmed GitHub CLI absence without accepting query failures."""


def is_missing_branch_pr(returncode: int, stderr: object, branch: str) -> bool:
    """Accept only gh's complete branch-specific diagnostic with status 1."""
    return (
        returncode == 1
        and isinstance(stderr, str)
        and stderr.strip() == f'no pull requests found for branch "{branch}"'
    )

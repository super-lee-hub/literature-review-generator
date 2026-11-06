"""Verify the Git identity used to authorize provider execution."""

from __future__ import annotations

from pathlib import Path
import subprocess


class CheckoutIdentityError(ValueError):
    """The current checkout cannot be bound to a trusted Git commit."""


def read_checkout_sha(repo_root: Path, *, require_clean: bool = False) -> str:
    """Read HEAD, rejecting tracked or untracked changes when authorization needs it."""

    root = Path(repo_root).expanduser().resolve()
    if require_clean:
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=str(root),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CheckoutIdentityError(
                f"acceptance cannot verify checkout cleanliness: {type(exc).__name__}"
            ) from exc
        if status.returncode != 0:
            raise CheckoutIdentityError("acceptance cannot verify checkout cleanliness")
        if str(status.stdout or "").strip():
            raise CheckoutIdentityError(
                "acceptance requires a clean checkout; commit or isolate all worktree changes first"
            )
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckoutIdentityError(
            f"acceptance plan cannot read checkout SHA: {type(exc).__name__}"
        ) from exc
    sha = str(completed.stdout or "").strip().splitlines()[-1] if completed.returncode == 0 else ""
    if len(sha) not in {40, 64} or any(char not in "0123456789abcdef" for char in sha.lower()):
        raise CheckoutIdentityError("acceptance plan cannot bind to the current checkout SHA")
    return sha.lower()


__all__ = ["CheckoutIdentityError", "read_checkout_sha"]

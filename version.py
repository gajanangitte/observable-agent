"""Deploy / version correlation: stamp every trace with the code it ran.

Every competitor that correlates deploys with observability shares one primitive:
tie each signal to the exact build that produced it, so a regression can be pinned
to a release. This module derives that identity from git -- the short commit SHA,
whether the tree was dirty, and the branch -- with zero new dependencies (it shells
out to ``git`` and degrades gracefully to a static fallback when git is absent, so
it never fails a run). The values become OpenTelemetry resource attributes on the
service, so in SigNoz you can group any trace, metric, or log by ``service.version``
or ``deployment.commit`` and answer "which deploy started this?".

Resolution order for the version string:

  1. ``SERVICE_VERSION`` env var, if set (an explicit release tag wins).
  2. ``git rev-parse --short HEAD`` (plus ``+dirty`` when the tree has uncommitted
     changes), so a working build is self-identifying.
  3. the static ``FALLBACK_VERSION`` (a shipped tarball with no .git).

All git calls are bounded by a short timeout and never raise -- observability
plumbing must not be able to crash the workload it observes.
"""
import os
import subprocess

FALLBACK_VERSION = "1.0.0"
_HERE = os.path.dirname(os.path.abspath(__file__))
_TIMEOUT_S = 3


def _git(*args):
    """Run a git command in the repo root, returning stripped stdout or None.

    Never raises: a missing git binary, a non-repo checkout, or a timeout all fold
    to None so callers can fall back cleanly."""
    try:
        out = subprocess.run(
            ["git", *args], cwd=_HERE, capture_output=True, text=True,
            timeout=_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def git_commit():
    """The short HEAD commit SHA, or None outside a git checkout."""
    return _git("rev-parse", "--short", "HEAD")


def git_dirty():
    """True if the working tree has uncommitted changes (best effort; False when
    git is unavailable, since a shipped build with no .git is 'clean')."""
    status = _git("status", "--porcelain")
    if status is None:
        return False
    return bool(status.strip())


def git_branch():
    """The current branch name (or None). A detached HEAD reports 'HEAD'."""
    return _git("rev-parse", "--abbrev-ref", "HEAD")


def resolve_version():
    """The service version string, per the documented resolution order."""
    env = os.getenv("SERVICE_VERSION")
    if env:
        return env
    sha = git_commit()
    if sha:
        return sha + ("+dirty" if git_dirty() else "")
    return FALLBACK_VERSION


def deployment_attributes():
    """Resource attributes describing the running build, for the OTel Resource.

    Always includes ``service.version``; adds ``deployment.commit`` /
    ``deployment.branch`` / ``deployment.dirty`` when git is available. Only
    primitive values, so they map cleanly onto a Resource.
    """
    attrs = {"service.version": resolve_version()}
    sha = git_commit()
    if sha:
        attrs["deployment.commit"] = sha
        attrs["deployment.dirty"] = git_dirty()
        branch = git_branch()
        if branch:
            attrs["deployment.branch"] = branch
    return attrs


def _main():
    for k, v in deployment_attributes().items():
        print(f"{k} = {v}")


if __name__ == "__main__":
    _main()

"""Unit tests for version.py (deploy / version correlation).

Network-free and git-free: the git-calling helpers are monkeypatched so the
resolution order, dirty suffix, env override, and fallback are all tested
deterministically without invoking a real repo.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import version


def _patch(monkey):
    """Install fake git_commit/git_dirty/git_branch from a dict; return an undo."""
    saved = (version.git_commit, version.git_dirty, version.git_branch)
    version.git_commit = lambda: monkey.get("commit")
    version.git_dirty = lambda: monkey.get("dirty", False)
    version.git_branch = lambda: monkey.get("branch")

    def undo():
        version.git_commit, version.git_dirty, version.git_branch = saved
    return undo


def test_env_override_wins():
    os.environ["SERVICE_VERSION"] = "v9.9.9"
    undo = _patch({"commit": "abc1234", "dirty": True})
    try:
        assert version.resolve_version() == "v9.9.9"
    finally:
        undo()
        del os.environ["SERVICE_VERSION"]


def test_clean_commit_is_bare_sha():
    os.environ.pop("SERVICE_VERSION", None)
    undo = _patch({"commit": "abc1234", "dirty": False})
    try:
        assert version.resolve_version() == "abc1234"
    finally:
        undo()


def test_dirty_commit_gets_suffix():
    os.environ.pop("SERVICE_VERSION", None)
    undo = _patch({"commit": "abc1234", "dirty": True})
    try:
        assert version.resolve_version() == "abc1234+dirty"
    finally:
        undo()


def test_fallback_when_no_git():
    os.environ.pop("SERVICE_VERSION", None)
    undo = _patch({"commit": None})
    try:
        assert version.resolve_version() == version.FALLBACK_VERSION
    finally:
        undo()


def test_deployment_attributes_with_git():
    os.environ.pop("SERVICE_VERSION", None)
    undo = _patch({"commit": "deadbee", "dirty": False, "branch": "main"})
    try:
        attrs = version.deployment_attributes()
        assert attrs["service.version"] == "deadbee"
        assert attrs["deployment.commit"] == "deadbee"
        assert attrs["deployment.branch"] == "main"
        assert attrs["deployment.dirty"] is False
    finally:
        undo()


def test_deployment_attributes_without_git():
    os.environ.pop("SERVICE_VERSION", None)
    undo = _patch({"commit": None})
    try:
        attrs = version.deployment_attributes()
        assert attrs == {"service.version": version.FALLBACK_VERSION}
        assert "deployment.commit" not in attrs
    finally:
        undo()


def test_attributes_are_primitive():
    # OTel resource attributes must be primitives (str/bool/number).
    os.environ.pop("SERVICE_VERSION", None)
    undo = _patch({"commit": "abc", "dirty": True, "branch": "feat/x"})
    try:
        for v in version.deployment_attributes().values():
            assert isinstance(v, (str, bool, int, float))
    finally:
        undo()


def test_git_helpers_never_raise():
    # The real helpers must degrade to None/False, never raise, even if git is
    # missing -- they run inside telemetry setup and must not crash a workload.
    # (Calls the real functions; asserts only on type, not value.)
    assert version.git_commit() is None or isinstance(version.git_commit(), str)
    assert isinstance(version.git_dirty(), bool)
    assert version.git_branch() is None or isinstance(version.git_branch(), str)

"""_git_sha reads the git metadata files directly (no subprocess; the subprocess version hung the worker when
it was started from the web server)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from reimburse_agent import audit as A

SHA = "0123456789abcdef0123456789abcdef01234567"
SHA2 = "fedcba9876543210fedcba9876543210fedcba98"


@pytest.fixture
def at(monkeypatch):
    """Point audit.__file__ at a fake checkout and return _git_sha() fresh."""
    def run(repo: Path):
        f = repo / "agent" / "reimburse_agent" / "audit.py"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("", encoding="utf-8")
        monkeypatch.setattr(A, "__file__", str(f))
        A._git_sha.cache_clear()
        try:
            return A._git_sha()
        finally:
            A._git_sha.cache_clear()
    yield run
    A._git_sha.cache_clear()


def test_loose_ref(tmp_path, at):
    g = tmp_path / ".git"
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    (g / "refs" / "heads" / "main").write_text(SHA + "\n")
    assert at(tmp_path) == SHA


def test_packed_ref(tmp_path, at):
    g = tmp_path / ".git"
    g.mkdir()
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    (g / "packed-refs").write_text(f"# pack-refs with: peeled\n{SHA2} refs/heads/other\n^{SHA}\n{SHA} refs/heads/main\n")
    assert at(tmp_path) == SHA


def test_detached_head(tmp_path, at):
    g = tmp_path / ".git"
    g.mkdir()
    (g / "HEAD").write_text(SHA + "\n")
    assert at(tmp_path) == SHA


def test_worktree_gitfile_with_commondir(tmp_path, at):
    common = tmp_path / "main" / ".git"
    (common / "refs" / "heads").mkdir(parents=True)
    (common / "refs" / "heads" / "feat").write_text(SHA + "\n")
    wt_git = common / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/feat\n")
    (wt_git / "commondir").write_text("../..\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n")
    assert at(wt) == SHA


def test_worktree_packed_ref_in_commondir(tmp_path, at):
    common = tmp_path / "main" / ".git"
    wt_git = common / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (common / "packed-refs").write_text(f"{SHA} refs/heads/feat\n")
    (wt_git / "HEAD").write_text("ref: refs/heads/feat\n")
    (wt_git / "commondir").write_text("../..\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_git}\n")
    assert at(wt) == SHA


def test_no_git_returns_none(tmp_path, at):
    # tmp_path sits under the system temp dir, which is not inside a git checkout
    assert at(tmp_path) is None


def test_unresolvable_ref_returns_none(tmp_path, at):
    g = tmp_path / ".git"
    g.mkdir()
    (g / "HEAD").write_text("ref: refs/heads/missing\n")
    assert at(tmp_path) is None


def test_matches_git_rev_parse_in_real_checkout():
    if not shutil.which("git"):
        pytest.skip("git not available")
    here = Path(A.__file__).resolve().parent
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=here, capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        pytest.skip("not inside a git checkout")
    A._git_sha.cache_clear()
    assert A._git_sha() == r.stdout.strip()

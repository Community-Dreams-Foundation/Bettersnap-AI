"""Build-hygiene guard for Dockerfile.unified (regression test for the build_provenance.json bug).

Two invariants that keep the image reproducibly buildable from a CLEAN checkout:
  1. No reference to the removed, unread build_provenance.json / BUILD_PROVENANCE anywhere in
     Dockerfile.unified (it had no runtime consumer and broke clean builds).
  2. EVERY `COPY <src>` source in Dockerfile.unified is git-TRACKED — so a fresh clone / CI /
     detached worktree build can never fail on a missing untracked file again.
Pure static checks: read the Dockerfile + ask git. No Docker, no network.
"""
import re, subprocess, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile.unified"


def _copy_sources(text):
    srcs = []
    for line in text.splitlines():
        m = re.match(r"\s*COPY\s+(.*)", line, re.IGNORECASE)
        if not m:
            continue
        toks = m.group(1).split()
        toks = [t for t in toks if not t.startswith("--")]   # drop --chown / --from flags
        if any("--from" in t for t in m.group(1).split()):    # skip multi-stage copies
            continue
        if len(toks) >= 2:
            srcs.extend(toks[:-1])                             # everything but the dest
    return srcs


def _git_tracked(path):
    r = subprocess.run(["git", "ls-files", "--", path], cwd=ROOT,
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


def test_no_build_provenance_reference():
    # Only ACTIVE directives create a dependency; an explanatory comment is fine (and useful).
    text = DOCKERFILE.read_text(encoding="utf-8")
    active = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "build_provenance" not in active.lower(), \
        "Dockerfile.unified still has an ACTIVE build_provenance directive (COPY/ENV)"
    assert "BUILD_PROVENANCE" not in active, "BUILD_PROVENANCE env entry must be gone"


def test_all_copy_sources_are_git_tracked():
    missing = [s for s in _copy_sources(DOCKERFILE.read_text(encoding="utf-8"))
               if not _git_tracked(s)]
    assert not missing, (
        f"Dockerfile.unified COPYs UNTRACKED source(s) -> clean/CI builds will fail: {missing}")


if __name__ == "__main__":
    test_no_build_provenance_reference()
    test_all_copy_sources_are_git_tracked()
    print("PASS: no build_provenance reference; all COPY sources git-tracked (clean build safe)")

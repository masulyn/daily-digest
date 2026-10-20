"""Text-level guards on the CI workflow (no YAML dependency, no network)."""

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def _jobs() -> dict[str, str]:
    text = WORKFLOW.read_text()
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([\w-]+):\n", body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2]))


def test_secret_scan_job_runs_pinned_gitleaks():
    job = _jobs()["secret-scan"]
    assert "gitleaks" in job and "--log-opts" in job
    assert re.search(r"GITLEAKS_VERSION: \d+\.\d+\.\d+\n", job)
    assert re.search(r"GITLEAKS_SHA256: [0-9a-f]{64}\n", job)
    assert "sha256sum -c" in job
    assert "fetch-depth: 0" in job
    # runs-on: ubuntu-latest is fine; the tool itself must not float.
    assert "releases/latest" not in job and "@latest" not in job


def test_actions_are_pinned():
    for ref in re.findall(r"uses:\s*(\S+)", WORKFLOW.read_text()):
        _, _, version = ref.partition("@")
        # Version tags of first-party/uv actions, or a full commit SHA.
        assert re.fullmatch(r"[0-9a-f]{40}|v\d+(\.\d+){0,2}", version), ref


def test_permissions_are_read_only():
    text = WORKFLOW.read_text()
    assert re.search(r"^permissions:\n  contents: read\n", text, re.MULTILINE)
    assert "write" not in text
    assert "contents: read" in _jobs()["secret-scan"]


def test_no_secrets_anywhere():
    text = WORKFLOW.read_text()
    assert "secrets." not in text
    assert "API_KEY" not in text.upper()
    assert "env:" not in _jobs()["test"]

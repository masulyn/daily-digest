"""Text-level guards on the CI workflow (no YAML dependency, no network)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _jobs() -> dict[str, str]:
    text = WORKFLOW.read_text()
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([\w-]+):\n", body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2]))


def _workflow_version() -> str:
    match = re.search(r"GITLEAKS_VERSION: (\d+\.\d+\.\d+)\n", WORKFLOW.read_text())
    assert match
    return match.group(1)


def test_secret_scan_job_runs_pinned_gitleaks():
    job = _jobs()["secret-scan"]
    assert "--log-opts" in job and "--redact" in job
    assert re.search(r"GITLEAKS_SHA256: [0-9a-f]{64}\n", job)
    assert re.search(r'echo "\$GITLEAKS_SHA256 .*" \| sha256sum -c -', job)
    assert "fetch-depth: 0" in job
    # runs-on: ubuntu-latest is fine; the tool itself must not float.
    assert "releases/latest" not in job and "@latest" not in job


def test_every_action_has_an_explicit_version_ref():
    # First-party actions use major tags on purpose; no branch or latest refs.
    for line in WORKFLOW.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        for ref in re.findall(r"uses:\s*(\S+)", line):
            _, _, version = ref.partition("@")
            assert re.fullmatch(r"[0-9a-f]{40}|v\d+(\.\d+){0,2}", version), ref


def test_permissions_are_read_only():
    text = WORKFLOW.read_text()
    assert re.search(r"^permissions:\n  contents: read\n", text, re.MULTILINE)
    assert not re.search(r"^\s*\w[\w-]*:\s*write\b", text, re.MULTILINE)
    assert "contents: read" in _jobs()["secret-scan"]


def test_no_secrets_anywhere():
    text = WORKFLOW.read_text()
    assert "secrets." not in text
    assert "API_KEY" not in text.upper()
    assert "env:" not in _jobs()["test"]


def test_security_notes_gitleaks_version_matches_workflow():
    notes = (ROOT / "docs" / "SECURITY.md").read_text()
    section = notes.split("## Secret scanning", 1)[1]
    versions = set(re.findall(r"\d+\.\d+\.\d+", section))
    assert versions == {_workflow_version()}

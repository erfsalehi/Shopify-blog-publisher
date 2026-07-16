"""The workflows hand-plumb config as env vars, which silently drifts from
Settings. Two typos are invisible in a green run:

  * a key Settings doesn't have (BUISNESS_NAME) is dropped by extra="ignore"
  * a `${{ vars.X }}` whose name doesn't match its key feeds in "" forever

Neither fails the job — it just quietly runs without that setting, which is
how the business context went missing from daily-publish.yml unnoticed.

The extras drift the same way, but louder: weekly-calendar.yml gained
sync-performance without gaining the [gsc] extra that installs google-auth,
so the step died on an ImportError and took run-calendar down with it.
"""

import re
from pathlib import Path

import pytest
import yaml

from blog_pipeline.config import Settings

_WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"
_WORKFLOWS = sorted(_WORKFLOW_DIR.glob("*.yml"))
_REF = re.compile(r"^\$\{\{\s*(?:vars|secrets)\.([A-Z_0-9]+)\s*\}\}$")

# CLI command -> the pyproject extra it can't run without.
_COMMAND_EXTRAS = {
    "sync-performance": "gsc",
    "sync-analytics": "gsc",
    "serve": "whatsapp",
}


def _env(path: Path) -> dict[str, str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    env: dict[str, str] = {}
    for job in doc["jobs"].values():
        for step in job["steps"]:
            env.update(step.get("env") or {})
    return env


def _run_scripts(path: Path) -> str:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return "\n".join(
        str(step.get("run") or "")
        for job in doc["jobs"].values()
        for step in job["steps"]
    )


def test_workflows_were_found():
    assert _WORKFLOWS, f"no workflows under {_WORKFLOW_DIR}"


@pytest.mark.parametrize("wf", _WORKFLOWS, ids=lambda p: p.name)
def test_env_keys_are_real_settings_fields(wf):
    fields = set(Settings.model_fields)
    unknown = sorted(k for k in _env(wf) if k.lower() not in fields)
    assert not unknown, f"{wf.name} sets env Settings ignores: {unknown}"


@pytest.mark.parametrize("wf", _WORKFLOWS, ids=lambda p: p.name)
def test_each_env_key_reads_the_same_named_var_or_secret(wf):
    """KEY: ${{ vars.KEY }} — a mismatch means the job silently gets ""."""
    mismatched = {
        key: value
        for key, value in _env(wf).items()
        if (m := _REF.match(str(value).strip())) and m.group(1) != key
    }
    assert not mismatched, f"{wf.name} key/reference mismatch: {mismatched}"


@pytest.mark.parametrize("wf", _WORKFLOWS, ids=lambda p: p.name)
def test_commands_have_the_extras_they_need_installed(wf):
    """A workflow calling sync-performance must install [gsc].

    The real failure: weekly-calendar.yml called sync-performance while
    installing only [postgres], so google-auth was absent and the step raised
    on import. `bash -e` then skipped run-calendar, and the week's whole
    reason for running was lost to a missing dependency.
    """
    script = _run_scripts(wf)
    missing = [
        f"{cmd} needs [{extra}]"
        for cmd, extra in _COMMAND_EXTRAS.items()
        if re.search(rf"blog-pipeline\s+{re.escape(cmd)}\b", script)
        and not re.search(rf"pip install[^\n]*\[[^\]\n]*{re.escape(extra)}", script)
    ]
    assert not missing, f"{wf.name}: {missing}"


@pytest.mark.parametrize("wf", _WORKFLOWS, ids=lambda p: p.name)
def test_optional_enrichment_never_gates_the_primary_job(wf):
    """sync-performance / sync-analytics only enrich; they must not be able to
    abort the run. GitHub runs `bash -e`, so an unguarded failure — a Google
    outage, a quota trip, an expired key — would skip everything after them.
    """
    script = _run_scripts(wf)
    ungated = []
    for line in script.splitlines():
        for cmd in ("sync-performance", "sync-analytics"):
            if re.search(rf"blog-pipeline\s+{cmd}\b", line) and "||" not in line:
                # A line continuation carries the guard onto the next line.
                if not line.rstrip().endswith("\\"):
                    ungated.append(line.strip())
    assert not ungated, f"{wf.name}: unguarded enrichment steps: {ungated}"

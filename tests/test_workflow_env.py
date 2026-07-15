"""The workflows hand-plumb config as env vars, which silently drifts from
Settings. Two typos are invisible in a green run:

  * a key Settings doesn't have (BUISNESS_NAME) is dropped by extra="ignore"
  * a `${{ vars.X }}` whose name doesn't match its key feeds in "" forever

Neither fails the job — it just quietly runs without that setting, which is
how the business context went missing from daily-publish.yml unnoticed.
"""

import re
from pathlib import Path

import pytest
import yaml

from blog_pipeline.config import Settings

_WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"
_WORKFLOWS = sorted(_WORKFLOW_DIR.glob("*.yml"))
_REF = re.compile(r"^\$\{\{\s*(?:vars|secrets)\.([A-Z_0-9]+)\s*\}\}$")


def _env(path: Path) -> dict[str, str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    env: dict[str, str] = {}
    for job in doc["jobs"].values():
        for step in job["steps"]:
            env.update(step.get("env") or {})
    return env


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

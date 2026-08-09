"""What a job is, and the list of them.

Split from `jobs/__init__.py` so job modules can import the decorator without
importing their siblings — `__init__` imports every job module to trigger
registration, and a job module importing `__init__` back would be a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class JobResult:
    """What a job hands back to the runner.

    `rows` is the headline number on the jobs page — how much was actually
    written. `detail` is free-form and rendered as-is, so put the things you'd
    want when a run looks wrong (windows fetched, API calls made) in there.
    """

    rows: int = 0
    detail: dict = field(default_factory=dict)
    # Ran correctly and deliberately did nothing — usually a missing optional
    # credential. Reported separately so an unconfigured integration doesn't
    # sit on the dashboard looking like a failure.
    skipped: bool = False
    skip_reason: str | None = None


@dataclass(frozen=True)
class JobSpec:
    name: str
    title: str
    description: str
    fn: Callable[[], JobResult]
    # Setting keys controlling the schedule. None means the job is manual-only.
    enabled_key: str | None = None
    hour_key: str | None = None
    minute: int = 0
    # Transient failures are the proxy's fault, not ours; a few attempts with
    # backoff usually gets through. Raise for jobs whose upstream is flakier.
    max_attempts: int = 3


_REGISTRY: dict[str, JobSpec] = {}


def register(spec: JobSpec) -> JobSpec:
    if spec.name in _REGISTRY:
        raise ValueError(f"job {spec.name!r} is already registered")
    _REGISTRY[spec.name] = spec
    return spec


def get_job(name: str) -> JobSpec:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown job {name!r}") from None


def all_jobs() -> list[JobSpec]:
    """Registration order — which is also the order they'd sensibly run in."""
    return list(_REGISTRY.values())

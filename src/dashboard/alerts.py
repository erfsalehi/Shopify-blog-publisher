"""Alert rules, evaluation, and the inbox.

Three design choices carry most of the weight:

**Rules are rows, not code.** A threshold that needs an edit-and-restart to
tune is a threshold that gets left wrong.

**Alerts deduplicate on a fingerprint.** A condition that persists for three
weeks should be one row you haven't dealt with, not twenty-one. The
fingerprint identifies the condition — rule plus subject — and deliberately
*not* the date it was noticed: including the date mints a fresh fingerprint
every night, which is the per-night duplication the fingerprint exists to
prevent.

**"Acknowledged" means until it changes, not until tomorrow.** Each run
records which fingerprints are still firing; anything absent is marked
resolved. A finding against a resolved alert reopens it, and a finding against
an acknowledged-but-still-present one just bumps the counter. Without that
distinction, acknowledging either hides a problem forever or lasts one day.

**Nothing is evaluated against unsettled data.** Every source here restates
its recent days, so a rule reading the last three days would fire on Google's
own bookkeeping every single morning, and an alert that cries wolf daily gets
muted within a week — taking the real ones with it.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import func

from dashboard.db import get_session
from dashboard.jobs.gsc import settled_through
from dashboard.models import (
    AdsCampaignDaily,
    Alert,
    AlertRule,
    Ga4EventDaily,
    GscSiteDaily,
    JobRun,
    JobStatus,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Finding:
    """One thing worth telling the owner about."""

    kind: str
    subject: str          # what it's about — a campaign name, "site", a job
    title: str
    body: str
    link: str | None = None
    severity: str = "warn"


@dataclass(frozen=True)
class RuleKind:
    key: str
    label: str
    help: str
    unit: str
    default_threshold: float
    evaluate: Callable[[float, date], list[Finding]]


def _window(end: date, days: int) -> tuple[date, date]:
    return end - timedelta(days=days - 1), end


def _site_metrics(session, start: date, end: date) -> tuple[int, int, float]:
    row = session.query(
        func.coalesce(func.sum(GscSiteDaily.clicks), 0),
        func.coalesce(func.sum(GscSiteDaily.impressions), 0),
        func.coalesce(
            func.sum(GscSiteDaily.position * GscSiteDaily.impressions), 0.0
        ),
    ).filter(GscSiteDaily.date >= start, GscSiteDaily.date <= end).one()
    return int(row[0]), int(row[1]), float(row[2])


# ── Evaluators ──────────────────────────────────────────────────────


def _clicks_drop(threshold: float, today: date) -> list[Finding]:
    """Site clicks down more than `threshold` percent, week over week."""
    end = settled_through(today)
    cur_start, cur_end = _window(end, 7)
    prev_start, prev_end = _window(cur_start - timedelta(days=1), 7)
    with get_session() as session:
        cur_clicks, _, _ = _site_metrics(session, cur_start, cur_end)
        prev_clicks, _, _ = _site_metrics(session, prev_start, prev_end)
    if not prev_clicks:
        return []  # No baseline; a drop from nothing isn't a drop.
    change = (cur_clicks - prev_clicks) / prev_clicks * 100
    if change > -threshold:
        return []
    return [Finding(
        kind="clicks_drop",
        subject="site",
        title=f"Organic clicks down {abs(change):.0f}% week over week",
        body=(
            f"{cur_clicks:,} clicks in {cur_start}..{cur_end}, against "
            f"{prev_clicks:,} in {prev_start}..{prev_end}. Threshold is "
            f"{threshold:.0f}%."
        ),
        link="/",
        severity="warn" if abs(change) < threshold * 2 else "high",
    )]


def _position_drop(threshold: float, today: date) -> list[Finding]:
    """Average position worsened by more than `threshold` places.

    Worsening means the number going *up*, which is the one metric here where
    the intuitive direction is inverted.
    """
    end = settled_through(today)
    cur_start, cur_end = _window(end, 28)
    prev_start, prev_end = _window(cur_start - timedelta(days=1), 28)
    with get_session() as session:
        _, cur_impr, cur_weight = _site_metrics(session, cur_start, cur_end)
        _, prev_impr, prev_weight = _site_metrics(session, prev_start, prev_end)
    if not cur_impr or not prev_impr:
        return []
    cur_pos = cur_weight / cur_impr
    prev_pos = prev_weight / prev_impr
    slip = cur_pos - prev_pos
    if slip < threshold:
        return []
    return [Finding(
        kind="position_drop",
        subject="site",
        title=f"Average position slipped {slip:.1f} places",
        body=(
            f"Now {cur_pos:.1f}, was {prev_pos:.1f} in the previous 28 days. "
            f"Both figures are impression-weighted, as Google's own are."
        ),
        link="/",
    )]


def _spend_without_conversions(threshold: float, today: date) -> list[Finding]:
    """A campaign that spent more than `threshold` and converted nothing.

    The one rule here that can find money already on the floor.
    """
    end = settled_through(today)
    start, stop = _window(end, 28)
    findings = []
    with get_session() as session:
        rows = session.query(
            AdsCampaignDaily.campaign,
            func.coalesce(func.sum(AdsCampaignDaily.spend), 0.0),
            func.coalesce(func.sum(AdsCampaignDaily.conversions), 0.0),
        ).filter(
            AdsCampaignDaily.date >= start, AdsCampaignDaily.date <= stop
        ).group_by(AdsCampaignDaily.campaign).all()
    for campaign, spend, conversions in rows:
        if float(spend) < threshold or float(conversions) > 0:
            continue
        findings.append(Finding(
            kind="spend_without_conversions",
            subject=campaign,
            title=f"{campaign} spent ${float(spend):,.2f} with no conversions",
            body=(
                f"Over {start}..{stop}. Threshold is ${threshold:,.0f}. Check "
                "the campaign before assuming the tracking is broken — both "
                "are possible, and they look identical from here."
            ),
            link="/ads",
            severity="high",
        ))
    return findings


def _cost_per_conversion(threshold: float, today: date) -> list[Finding]:
    """A campaign paying more than `threshold` per conversion."""
    end = settled_through(today)
    start, stop = _window(end, 28)
    findings = []
    with get_session() as session:
        rows = session.query(
            AdsCampaignDaily.campaign,
            func.coalesce(func.sum(AdsCampaignDaily.spend), 0.0),
            func.coalesce(func.sum(AdsCampaignDaily.conversions), 0.0),
        ).filter(
            AdsCampaignDaily.date >= start, AdsCampaignDaily.date <= stop
        ).group_by(AdsCampaignDaily.campaign).all()
    for campaign, spend, conversions in rows:
        if not float(conversions):
            continue  # covered by the rule above
        cost = float(spend) / float(conversions)
        if cost < threshold:
            continue
        findings.append(Finding(
            kind="cost_per_conversion",
            subject=campaign,
            title=f"{campaign} is paying ${cost:,.2f} per conversion",
            body=(
                f"${float(spend):,.2f} for {float(conversions):.1f} conversions "
                f"over {start}..{stop}. Threshold is ${threshold:,.0f}."
            ),
            link="/ads",
        ))
    return findings


def _conversions_drop(threshold: float, today: date) -> list[Finding]:
    """Call/WhatsApp events down more than `threshold` percent week on week."""
    end = settled_through(today)
    cur_start, cur_end = _window(end, 7)
    prev_start, prev_end = _window(cur_start - timedelta(days=1), 7)

    def total(session, start, stop) -> int:
        return int(session.query(
            func.coalesce(func.sum(Ga4EventDaily.event_count), 0)
        ).filter(
            Ga4EventDaily.date >= start, Ga4EventDaily.date <= stop
        ).scalar() or 0)

    with get_session() as session:
        current = total(session, cur_start, cur_end)
        previous = total(session, prev_start, prev_end)
        earliest = session.query(func.min(Ga4EventDaily.date)).scalar()

    # The conversion tags went live partway through 2026-07. Comparing against
    # a window that predates them measures the install, not the business.
    if not previous or (earliest and earliest > prev_start):
        return []
    change = (current - previous) / previous * 100
    if change > -threshold:
        return []
    return [Finding(
        kind="conversions_drop",
        subject="calls",
        title=f"Call and WhatsApp events down {abs(change):.0f}% week over week",
        body=(
            f"{current} in {cur_start}..{cur_end}, against {previous} the week "
            "before. These are the store's real conversions — nothing is "
            "checked out on the site."
        ),
        link="/ads",
        severity="high",
    )]


def _job_failures(threshold: float, today: date) -> list[Finding]:
    """Any sync whose most recent run errored.

    Threshold is unused; the rule exists so a silent pipeline can't masquerade
    as a quiet week. Stale numbers with no error on screen is the failure mode
    this whole dashboard is trying to avoid.
    """
    findings = []
    with get_session() as session:
        rows = session.query(JobRun).order_by(
            JobRun.started_at.desc(), JobRun.id.desc()
        ).all()
        latest: dict[str, JobRun] = {}
        for row in rows:
            latest.setdefault(row.job, row)
        for name, run in latest.items():
            if run.status != JobStatus.error.value:
                continue
            findings.append(Finding(
                kind="job_failure",
                subject=f"{name}:{run.id}",
                title=f"Sync '{name}' failed",
                body=(run.error or "No error recorded.")[:800],
                link="/jobs",
                severity="high",
            ))
    return findings


KINDS: tuple[RuleKind, ...] = (
    RuleKind(
        key="clicks_drop", label="Organic clicks drop (week over week)",
        help="Fires when site clicks fall by more than this percentage against "
             "the previous 7 days. Both windows end on settled data.",
        unit="%", default_threshold=25, evaluate=_clicks_drop,
    ),
    RuleKind(
        key="position_drop", label="Average position slips",
        help="Fires when impression-weighted average position worsens by more "
             "than this many places over 28 days.",
        unit="places", default_threshold=2.0, evaluate=_position_drop,
    ),
    RuleKind(
        key="conversions_drop", label="Call/WhatsApp events drop",
        help="Fires when conversion events fall by more than this percentage "
             "week over week. Silent until both windows post-date the GTM tags.",
        unit="%", default_threshold=40, evaluate=_conversions_drop,
    ),
    RuleKind(
        key="spend_without_conversions", label="Ad spend with no conversions",
        help="Fires when a campaign spends more than this over 28 days and "
             "reports zero conversions.",
        unit="$", default_threshold=100, evaluate=_spend_without_conversions,
    ),
    RuleKind(
        key="cost_per_conversion", label="Cost per conversion too high",
        help="Fires when a campaign's 28-day cost per conversion exceeds this.",
        unit="$", default_threshold=50, evaluate=_cost_per_conversion,
    ),
    RuleKind(
        key="job_failure", label="A sync job failed",
        help="Fires when any job's most recent run errored. Threshold unused — "
             "stale numbers with no error on screen is the failure this whole "
             "dashboard exists to prevent.",
        unit="", default_threshold=0, evaluate=_job_failures,
    ),
)

_BY_KIND = {k.key: k for k in KINDS}


def kind(key: str) -> RuleKind:
    return _BY_KIND[key]


def default_rules() -> list[dict]:
    """Seeded on first run so the inbox works before anyone visits settings."""
    return [
        {"name": k.label, "kind": k.key, "threshold": k.default_threshold,
         "enabled": True, "notify": False}
        for k in KINDS
    ]


def ensure_default_rules() -> int:
    """Create the default rule set once. Never re-creates a deleted rule —
    removing a rule the owner deleted every night would be its own bug."""
    with get_session() as session:
        if session.query(AlertRule).count():
            return 0
        for spec in default_rules():
            session.add(AlertRule(**spec))
        return len(KINDS)


def _fingerprint(rule_id: int | None, finding: Finding) -> str:
    raw = f"{rule_id}|{finding.kind}|{finding.subject}"
    return hashlib.sha1(raw.encode()).hexdigest()


def evaluate(today: date | None = None, *, notify: bool = True) -> dict:
    """Run every enabled rule and record what it found."""
    today = today or date.today()
    ensure_default_rules()
    now = datetime.now(timezone.utc)

    with get_session() as session:
        rules = session.query(AlertRule).filter(AlertRule.enabled.is_(True)).all()
        for r in rules:
            session.expunge(r)

    fired = 0
    new = 0
    reopened = 0
    seen_prints: set[str] = set()
    to_notify: list[tuple[Alert, AlertRule]] = []

    for rule in rules:
        spec = _BY_KIND.get(rule.kind)
        if spec is None:
            log.warning("alert rule %s has unknown kind %r", rule.id, rule.kind)
            continue
        try:
            findings = spec.evaluate(rule.threshold, today)
        except Exception:  # noqa: BLE001
            # One broken rule must not stop the rest from being evaluated.
            log.exception("alert rule %s (%s) failed to evaluate", rule.id, rule.kind)
            continue

        for finding in findings:
            fired += 1
            print_id = _fingerprint(rule.id, finding)
            seen_prints.add(print_id)
            with get_session() as session:
                existing = session.query(Alert).filter(
                    Alert.fingerprint == print_id
                ).one_or_none()
                if existing is None:
                    alert = Alert(
                        rule_id=rule.id, kind=finding.kind, fingerprint=print_id,
                        title=finding.title, body=finding.body, link=finding.link,
                        severity=finding.severity, first_seen=now, last_seen=now,
                    )
                    session.add(alert)
                    session.flush()
                    new += 1
                    should_notify = rule.notify
                else:
                    was_resolved = existing.resolved_at is not None
                    existing.last_seen = now
                    existing.times_seen += 1
                    existing.title = finding.title
                    existing.body = finding.body
                    existing.severity = finding.severity
                    if was_resolved:
                        # It went away and came back: a genuinely new event,
                        # so it returns to the inbox and pings again.
                        existing.resolved_at = None
                        existing.acknowledged_at = None
                        reopened += 1
                    alert = existing
                    # A condition that simply never stopped does not ping
                    # again — that's the every-night noise that gets alerting
                    # muted wholesale.
                    should_notify = rule.notify and was_resolved
                session.flush()
                if should_notify:
                    to_notify.append((
                        Alert(
                            id=alert.id, title=alert.title, body=alert.body,
                            severity=alert.severity, kind=alert.kind,
                            fingerprint=alert.fingerprint,
                        ),
                        rule,
                    ))
                session.expunge_all()

    # Anything that didn't fire this run is no longer happening. Marking it
    # resolved is what makes "acknowledged" mean "until it changes" — and it
    # clears the inbox on its own when a problem fixes itself, which is most
    # of them.
    resolved = 0
    if rules:
        evaluated_kinds = {r.kind for r in rules}
        with get_session() as session:
            stale = session.query(Alert).filter(
                Alert.resolved_at.is_(None),
                Alert.kind.in_(evaluated_kinds),
            ).all()
            for row in stale:
                if row.fingerprint not in seen_prints:
                    row.resolved_at = now
                    resolved += 1

    sent = 0
    if notify and to_notify:
        sent = _send(to_notify)

    return {
        "rules_evaluated": len(rules),
        "findings": fired,
        "new_alerts": new,
        "reopened": reopened,
        "resolved": resolved,
        "notifications_sent": sent,
        "open_alerts": open_count(),
    }


def _send(items: list[tuple[Alert, AlertRule]]) -> int:
    """Push to Slack, using the webhook the pipeline already has configured.

    Slack rather than a Windows toast: a toast is gone the moment it's
    dismissed and invisible if the machine is locked, which is most of the
    time this runs. If the webhook isn't set, `notify.py` logs instead and
    the alert is still in the inbox — the inbox is the system of record.
    """
    from blog_pipeline.notify import _post

    sent = 0
    now = datetime.now(timezone.utc)
    for alert, _rule in items:
        icon = "🔴" if alert.severity == "high" else "🟠"
        text = f"{icon} *{alert.title}*\n{alert.body or ''}"
        try:
            if _post(text):
                sent += 1
                with get_session() as session:
                    row = session.get(Alert, alert.id)
                    if row is not None:
                        row.notified_at = now
        except Exception:  # noqa: BLE001
            log.exception("could not send alert %s", alert.id)
    return sent


def open_count() -> int:
    """Cheap enough to run on every page render — one indexed COUNT."""
    with get_session() as session:
        return session.query(Alert).filter(
            Alert.acknowledged_at.is_(None), Alert.resolved_at.is_(None)
        ).count()


def open_alerts(limit: int = 100) -> list[Alert]:
    with get_session() as session:
        rows = session.query(Alert).filter(
            Alert.acknowledged_at.is_(None), Alert.resolved_at.is_(None)
        ).order_by(Alert.severity.desc(), Alert.last_seen.desc()).limit(limit).all()
        for row in rows:
            session.expunge(row)
    return rows


def recent_alerts(limit: int = 50) -> list[Alert]:
    with get_session() as session:
        rows = session.query(Alert).order_by(
            Alert.last_seen.desc()
        ).limit(limit).all()
        for row in rows:
            session.expunge(row)
    return rows


def acknowledge(alert_id: int) -> None:
    with get_session() as session:
        row = session.get(Alert, alert_id)
        if row is not None:
            row.acknowledged_at = datetime.now(timezone.utc)

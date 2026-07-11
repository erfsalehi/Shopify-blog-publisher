"""Linear GraphQL client.

Linear is the pipeline's destination now: the content calendar lives there as
issues (one per blog topic), and the finished draft — body, SEO meta, images,
QA notes — gets written into that same issue for a human to review and
publish by hand. There is no "publish" API call; `sync_issue` is the last
thing the pipeline ever does to an article.

Covers exactly what the pipeline needs:
  * team_id / project_id  — resolve by name, auto-creating the project (and
    the "Blog" label) on first use so `run-calendar` can stand up the whole
    board with zero manual setup beyond an API key.
  * state_id / label_id   — workflow state + label lookup, cached per client.
  * create_issue / update_issue — issueCreate/issueUpdate mutations.
  * add_comment           — surfaces QA notes as a comment thread on blocks.

The client raises LinearError on GraphQL userErrors so callers can mark the
Article row failed with a real reason. `dry_run` short-circuits create_issue
and returns the payload instead of calling the API.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from blog_pipeline.config import get_settings

_ENDPOINT = "https://api.linear.app/graphql"


class LinearError(RuntimeError):
    pass


@dataclass
class IssueResult:
    id: str | None
    identifier: str | None
    url: str | None
    dry_run: bool = False
    payload: dict | None = None


class LinearClient:
    def __init__(
        self,
        api_key: str | None = None,
        team: str | None = None,
        project: str | None = None,
    ) -> None:
        s = get_settings()
        self.api_key = api_key or s.linear_api_key
        self.team_name = team or s.linear_team
        self.project_name = project if project is not None else s.linear_project
        if not self.api_key:
            raise LinearError("Linear not configured: set LINEAR_API_KEY.")
        if not self.team_name:
            raise LinearError("Linear not configured: set LINEAR_TEAM.")
        self._client = httpx.Client(
            headers={"Authorization": self.api_key, "Content-Type": "application/json"},
            timeout=30.0,
        )
        self._team_id: str | None = None
        self._project_id: str | None = None
        self._states: dict[str, str] | None = None
        self._states_by_type: dict[str, str] = {}
        self._labels: dict[str, str] | None = None

    # ── core request with basic throttle backoff ─────────────────
    def graphql(self, query: str, variables: dict | None = None) -> dict:
        for attempt in range(5):
            resp = self._client.post(
                _ENDPOINT, json={"query": query, "variables": variables or {}}
            )
            if resp.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise LinearError(f"GraphQL errors: {data['errors']}")
            return data["data"]
        raise LinearError("Linear API throttled after retries (429).")

    @staticmethod
    def _check_user_errors(node: dict, key: str) -> None:
        if not node.get("success", True):
            raise LinearError(f"{key} failed (no userErrors detail returned).")

    # ── team / project resolution ─────────────────────────────────
    def team_id(self) -> str:
        if self._team_id:
            return self._team_id
        data = self.graphql("query { teams(first: 100) { nodes { id name key } } }")
        target = self.team_name.strip().lower()
        for node in data["teams"]["nodes"]:
            if node["name"].strip().lower() == target or node["key"].strip().lower() == target:
                self._team_id = node["id"]
                return self._team_id
        raise LinearError(
            f"Linear team '{self.team_name}' not found in this workspace "
            "(check LINEAR_API_KEY belongs to the right workspace and "
            "LINEAR_TEAM matches the team's name or key exactly)."
        )

    def project_id(self) -> str | None:
        if not self.project_name:
            return None
        if self._project_id:
            return self._project_id
        team_id = self.team_id()
        data = self.graphql(
            "query($id: String!) { team(id: $id) { projects(first: 100) "
            "{ nodes { id name } } } }",
            {"id": team_id},
        )
        target = self.project_name.strip().lower()
        for node in data["team"]["projects"]["nodes"]:
            if node["name"].strip().lower() == target:
                self._project_id = node["id"]
                return self._project_id
        # Not found — create it so `run-calendar` can stand up the board cold.
        created = self.graphql(
            """
            mutation($input: ProjectCreateInput!) {
              projectCreate(input: $input) {
                success
                project { id }
              }
            }
            """,
            {"input": {"name": self.project_name, "teamIds": [team_id]}},
        )["projectCreate"]
        self._check_user_errors(created, "projectCreate")
        self._project_id = created["project"]["id"]
        return self._project_id

    # ── workflow state / label resolution ───────────────────────
    def _load_states(self) -> dict[str, str]:
        if self._states is None:
            data = self.graphql(
                "query($id: String!) { team(id: $id) { states(first: 100) "
                "{ nodes { id name type } } } }",
                {"id": self.team_id()},
            )
            nodes = data["team"]["states"]["nodes"]
            self._states = {n["name"].strip().lower(): n["id"] for n in nodes}
            # Keep a type -> id map for graceful fallback when a configured
            # state name doesn't exist on this team.
            self._states_by_type: dict[str, str] = {}
            for n in nodes:
                self._states_by_type.setdefault(n["type"], n["id"])
        return self._states

    # Map an unmatched requested name to a plausible state type, so a stock
    # Linear team (Backlog/Todo/In Progress/Done) still gets a sensible move
    # instead of the issue being left in its default state.
    _NAME_TYPE_HINTS = (
        ("done", "completed"), ("publish", "completed"), ("live", "completed"),
        ("cancel", "canceled"), ("block", "canceled"),
        ("progress", "started"), ("review", "started"), ("doing", "started"),
        ("adjust", "unstarted"), ("needs", "unstarted"), ("edit", "unstarted"),
        ("fix", "unstarted"), ("pending", "unstarted"), ("await", "unstarted"),
        ("todo", "unstarted"), ("to do", "unstarted"), ("ready", "unstarted"),
        ("backlog", "backlog"),
    )

    def state_id(self, name: str) -> str | None:
        if not name:
            return None
        self._load_states()
        exact = self._states.get(name.strip().lower())
        if exact:
            return exact
        low = name.strip().lower()
        for needle, stype in self._NAME_TYPE_HINTS:
            if needle in low and stype in self._states_by_type:
                return self._states_by_type[stype]
        return None

    def _load_labels(self) -> dict[str, str]:
        if self._labels is None:
            data = self.graphql(
                "query($id: String!) { team(id: $id) { labels(first: 200) "
                "{ nodes { id name } } } }",
                {"id": self.team_id()},
            )
            self._labels = {
                n["name"].strip().lower(): n["id"] for n in data["team"]["labels"]["nodes"]
            }
        return self._labels

    def label_id(self, name: str, create_if_missing: bool = True) -> str | None:
        labels = self._load_labels()
        existing = labels.get(name.strip().lower())
        if existing or not create_if_missing:
            return existing
        created = self.graphql(
            """
            mutation($input: IssueLabelCreateInput!) {
              issueLabelCreate(input: $input) {
                success
                issueLabel { id name }
              }
            }
            """,
            {"input": {"name": name, "teamId": self.team_id()}},
        )["issueLabelCreate"]
        self._check_user_errors(created, "issueLabelCreate")
        label = created["issueLabel"]
        labels[label["name"].strip().lower()] = label["id"]
        return label["id"]

    # ── issues ───────────────────────────────────────────────────
    def create_issue(
        self,
        *,
        title: str,
        description: str | None = None,
        state: str | None = None,
        due_date: str | None = None,
        labels: list[str] | None = None,
        dry_run: bool = False,
    ) -> IssueResult:
        if dry_run:
            # No network calls at all in dry-run — build the preview from the
            # raw inputs rather than resolving team/project/state/label ids.
            payload: dict = {"team": self.team_name, "title": title}
            if description is not None:
                payload["description"] = description
            if due_date:
                payload["dueDate"] = due_date
            if self.project_name:
                payload["project"] = self.project_name
            if state:
                payload["state"] = state
            if labels:
                payload["labels"] = labels
            return IssueResult(id=None, identifier=None, url=None, dry_run=True,
                                payload={"issue": payload})

        input_: dict = {"teamId": self.team_id(), "title": title}
        if description is not None:
            input_["description"] = description
        if due_date:
            input_["dueDate"] = due_date
        project_id = self.project_id()
        if project_id:
            input_["projectId"] = project_id
        if state:
            sid = self.state_id(state)
            if sid:
                input_["stateId"] = sid
        if labels:
            label_ids = [lid for lid in (self.label_id(n) for n in labels) if lid]
            if label_ids:
                input_["labelIds"] = label_ids

        data = self.graphql(
            """
            mutation($input: IssueCreateInput!) {
              issueCreate(input: $input) {
                success
                issue { id identifier url }
              }
            }
            """,
            {"input": input_},
        )["issueCreate"]
        self._check_user_errors(data, "issueCreate")
        issue = data["issue"]
        return IssueResult(id=issue["id"], identifier=issue["identifier"], url=issue["url"])

    def update_issue(
        self,
        issue_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        state: str | None = None,
        due_date: str | None = None,
        labels: list[str] | None = None,
        dry_run: bool = False,
    ) -> IssueResult:
        if dry_run:
            payload: dict = {}
            if title is not None:
                payload["title"] = title
            if description is not None:
                payload["description"] = description
            if due_date is not None:
                payload["dueDate"] = due_date
            if state:
                payload["state"] = state
            if labels is not None:
                payload["labels"] = labels
            return IssueResult(id=issue_id, identifier=None, url=None, dry_run=True,
                                payload={"issue": payload})

        input_: dict = {}
        if title is not None:
            input_["title"] = title
        if description is not None:
            input_["description"] = description
        if due_date is not None:
            input_["dueDate"] = due_date
        if state:
            sid = self.state_id(state)
            if sid:
                input_["stateId"] = sid
        if labels is not None:
            label_ids = [lid for lid in (self.label_id(n) for n in labels) if lid]
            input_["labelIds"] = label_ids

        data = self.graphql(
            """
            mutation($id: String!, $input: IssueUpdateInput!) {
              issueUpdate(id: $id, input: $input) {
                success
                issue { id identifier url }
              }
            }
            """,
            {"id": issue_id, "input": input_},
        )["issueUpdate"]
        self._check_user_errors(data, "issueUpdate")
        issue = data["issue"]
        return IssueResult(id=issue["id"], identifier=issue["identifier"], url=issue["url"])

    def upload_file(
        self, file_bytes: bytes, filename: str, content_type: str = "image/png"
    ) -> str:
        """Upload bytes to Linear's own storage, return a publicly-fetchable URL.

        Two-step flow: request a signed upload URL via `fileUpload`, PUT the
        bytes to it, then return `assetUrl`. `makePublic: true` is required —
        without it `assetUrl` only resolves inside an authenticated Linear
        session (fine for viewing in the app, useless for embedding a plain
        markdown image that should render for anyone with the issue link).
        """
        data = self.graphql(
            """
            mutation($contentType: String!, $filename: String!, $size: Int!, $makePublic: Boolean) {
              fileUpload(contentType: $contentType, filename: $filename, size: $size, makePublic: $makePublic) {
                success
                uploadFile { uploadUrl assetUrl headers { key value } }
              }
            }
            """,
            {
                "contentType": content_type,
                "filename": filename,
                "size": len(file_bytes),
                "makePublic": True,
            },
        )["fileUpload"]
        self._check_user_errors(data, "fileUpload")
        upload = data["uploadFile"]
        put_headers = {h["key"]: h["value"] for h in upload["headers"]}
        put_headers["Content-Type"] = content_type
        # Plain request, deliberately not through self._client — the presigned
        # GCS URL is pre-signed for an exact header set and doesn't want our
        # Linear Authorization header riding along.
        resp = httpx.put(upload["uploadUrl"], content=file_bytes, headers=put_headers, timeout=60.0)
        resp.raise_for_status()
        return upload["assetUrl"]

    def add_comment(self, issue_id: str, body: str) -> None:
        data = self.graphql(
            """
            mutation($input: CommentCreateInput!) {
              commentCreate(input: $input) { success }
            }
            """,
            {"input": {"issueId": issue_id, "body": body}},
        )["commentCreate"]
        self._check_user_errors(data, "commentCreate")

    def close(self) -> None:
        self._client.close()

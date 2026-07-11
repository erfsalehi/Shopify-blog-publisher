from blog_pipeline.tools.linear import LinearClient


def _client():
    return LinearClient(api_key="lin_api_test", team="Content", project="Blog Content Calendar")


def test_create_issue_dry_run_builds_payload_without_network():
    client = _client()
    result = client.create_issue(
        title="Test Topic",
        description="Body here",
        state="Backlog",
        due_date="2026-08-01",
        labels=["Blog"],
        dry_run=True,
    )
    assert result.dry_run is True
    issue = result.payload["issue"]
    assert issue["title"] == "Test Topic"
    assert issue["team"] == "Content"
    assert issue["project"] == "Blog Content Calendar"
    assert issue["state"] == "Backlog"
    assert issue["dueDate"] == "2026-08-01"
    assert issue["labels"] == ["Blog"]
    client.close()


def test_update_issue_dry_run_builds_partial_payload():
    client = _client()
    result = client.update_issue(
        "issue-123", title="Updated Title", state="Ready to Review", dry_run=True,
    )
    assert result.dry_run is True
    assert result.id == "issue-123"
    issue = result.payload["issue"]
    assert issue["title"] == "Updated Title"
    assert issue["state"] == "Ready to Review"
    assert "description" not in issue
    client.close()


def test_missing_api_key_raises():
    import pytest

    from blog_pipeline.tools.linear import LinearError

    with pytest.raises(LinearError):
        LinearClient(api_key="", team="Content")

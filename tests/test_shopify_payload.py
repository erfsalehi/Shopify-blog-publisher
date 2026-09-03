from blog_pipeline.tools.shopify import ShopifyClient, _as_gid


def _client():
    return ShopifyClient(
        domain="test-store.myshopify.com", token="shpat_test", api_version="2025-01"
    )


def test_as_gid_normalizes_numeric():
    assert _as_gid("123", "Blog") == "gid://shopify/Blog/123"
    assert _as_gid("gid://shopify/Blog/123", "Blog") == "gid://shopify/Blog/123"


def test_dry_run_builds_payload_without_network():
    client = _client()
    result = client.create_article(
        title="Test Title",
        body_html="<p>Body</p>",
        summary="A summary",
        handle="test-title",
        seo_title="SEO Title",
        seo_description="SEO description here",
        blog_id="gid://shopify/Blog/1",
        dry_run=True,
    )
    assert result.dry_run is True
    article = result.payload["article"]
    assert article["title"] == "Test Title"
    assert article["blogId"] == "gid://shopify/Blog/1"
    assert article["isPublished"] is True
    # SEO meta goes through global.title_tag / global.description_tag metafields.
    tags = {m["key"]: m["value"] for m in article["metafields"]}
    assert tags["title_tag"] == "SEO Title"
    assert tags["description_tag"] == "SEO description here"
    assert "seo" not in article
    assert "image" not in article  # no image supplied
    client.close()


def test_dry_run_includes_image_when_file_id_given():
    client = _client()
    result = client.create_article(
        title="T", body_html="<p>x</p>", blog_id="gid://shopify/Blog/1",
        image_file_id="gid://shopify/MediaImage/9", dry_run=True,
    )
    assert result.payload["article"]["image"] == {"id": "gid://shopify/MediaImage/9"}
    client.close()


# ── Metafield definitions ──────────────────────────────────────────
#
# A metafield without a definition is stored by Shopify and displayed by
# nothing: the admin's Metafields section lists only defined keys, and the
# theme editor offers only those as dynamic sources. So the importer defines
# what it writes — and has to do that without tripping over the definitions
# a store already has.


def _recording_client(responses):
    """A client whose `graphql` answers from a script and records the asks."""
    client = _client()
    asked = []

    def graphql(query, variables=None):
        asked.append((query, variables or {}))
        return responses.pop(0)

    client.graphql = graphql
    client.asked = asked
    return client


DEFINITIONS = [
    {"key": "source_url", "name": "Source URL", "type": "url"},
    {"key": "specifications", "name": "Specifications", "type": "json"},
]


def test_only_the_definitions_the_store_is_missing_are_created():
    client = _recording_client([
        {"metafieldDefinitions": {"nodes": [
            {"key": "source_url", "type": {"name": "url"}},
        ]}},
        {"metafieldDefinitionCreate": {
            "createdDefinition": {"key": "specifications"}, "userErrors": [],
        }},
    ])

    created, conflicting = client.ensure_metafield_definitions(
        DEFINITIONS, namespace="custom",
    )
    assert created == ["specifications"]
    assert conflicting == []
    # Two calls: one to ask, one to create the one that was missing.
    assert len(client.asked) == 2
    definition = client.asked[1][1]["definition"]
    assert definition["namespace"] == "custom"
    assert definition["ownerType"] == "PRODUCT"
    assert definition["type"] == "json"
    # Pinned, so it lands on the product page rather than behind "show all".
    assert definition["pin"] is True


def test_a_key_defined_with_another_type_is_reported_not_overwritten():
    """The values written under it will be refused, and that is worth saying.
    Redefining it is destructive and belongs to whoever created it."""
    client = _recording_client([
        {"metafieldDefinitions": {"nodes": [
            {"key": "source_url", "type": {"name": "url"}},
            {"key": "specifications", "type": {"name": "multi_line_text_field"}},
        ]}},
    ])

    created, conflicting = client.ensure_metafield_definitions(
        DEFINITIONS, namespace="custom",
    )
    assert created == []
    assert conflicting == ["specifications (defined as multi_line_text_field)"]
    # Nothing was created, so nothing beyond the one question was asked.
    assert len(client.asked) == 1


def test_losing_the_race_to_create_a_definition_is_not_an_error():
    """Two passes of one import can reach this at the same time. The point
    was that the definition exists, and after a TAKEN it does."""
    client = _recording_client([
        {"metafieldDefinitions": {"nodes": []}},
        {"metafieldDefinitionCreate": {
            "createdDefinition": None,
            "userErrors": [{"field": ["key"], "message": "Key is in use",
                            "code": "TAKEN"}],
        }},
        {"metafieldDefinitionCreate": {
            "createdDefinition": {"key": "specifications"}, "userErrors": [],
        }},
    ])

    created, conflicting = client.ensure_metafield_definitions(
        DEFINITIONS, namespace="custom",
    )
    assert created == ["specifications"]
    assert conflicting == []


def test_a_refusal_that_is_not_a_race_is_raised():
    from blog_pipeline.tools.shopify import ShopifyError

    client = _recording_client([
        {"metafieldDefinitions": {"nodes": []}},
        {"metafieldDefinitionCreate": {
            "createdDefinition": None,
            "userErrors": [{"field": ["type"], "message": "Type is not valid",
                            "code": "INVALID_OPTION"}],
        }},
    ])

    try:
        client.ensure_metafield_definitions(DEFINITIONS, namespace="custom")
    except ShopifyError as exc:
        assert "source_url" in str(exc)
        assert "Type is not valid" in str(exc)
    else:
        raise AssertionError("a refused definition must not pass silently")


def test_the_store_is_asked_once_per_client():
    """Every product in a pass asks the same question and the answer cannot
    change under it — the same reason `list_publications` is cached."""
    client = _recording_client([
        {"metafieldDefinitions": {"nodes": [
            {"key": "source_url", "type": {"name": "url"}},
            {"key": "specifications", "type": {"name": "json"}},
        ]}},
    ])

    for _ in range(3):
        assert client.ensure_metafield_definitions(
            DEFINITIONS, namespace="custom",
        ) == ([], [])
    assert len(client.asked) == 1

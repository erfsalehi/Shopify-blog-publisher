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
    assert article["seo"]["title"] == "SEO Title"
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

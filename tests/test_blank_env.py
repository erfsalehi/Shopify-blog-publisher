"""A blank env var must read as "unset" so the class default applies.

Reproduces the third CI incident of this shape: GitHub Actions expands an
unset `${{ vars.ENABLE_SHOPIFY_PUBLISH }}` to "" rather than omitting the
variable, which pydantic rejected as an invalid bool ("Input should be a
valid boolean, input_value=''") and crashed every scheduled run — naming a
field the user had never set.
"""

import pytest

from blog_pipeline.config import Settings


@pytest.fixture
def env(monkeypatch):
    """Plain monkeypatch. The autouse _isolated_settings fixture in conftest
    already detaches Settings from the repo's .env, so a blank var here falls
    through to the class default rather than to a real value."""
    return monkeypatch


def test_blank_bool_falls_back_to_default(env):
    env.setenv("ENABLE_SHOPIFY_PUBLISH", "")
    assert Settings().enable_shopify_publish is True


def test_blank_float_falls_back_to_default(env):
    env.setenv("CONFIDENCE_THRESHOLD", "")
    assert Settings().confidence_threshold == 0.75


def test_blank_int_falls_back_to_default(env):
    env.setenv("WORD_COUNT_TARGET", "")
    assert Settings().word_count_target == 1500


def test_blank_str_falls_back_to_default(env):
    env.setenv("LINEAR_PROJECT", "")
    assert Settings().linear_project == "Blog Content Calendar"


def test_whitespace_only_is_also_unset(env):
    env.setenv("ENABLE_SHOPIFY_PUBLISH", "   ")
    assert Settings().enable_shopify_publish is True


def test_real_value_still_wins(env):
    env.setenv("ENABLE_SHOPIFY_PUBLISH", "false")
    assert Settings().enable_shopify_publish is False


def test_blank_database_url_is_preserved_not_defaulted(env):
    """database_url is the deliberate exception. Blank must stay blank so
    db/session.py can raise its actionable error, rather than quietly falling
    back to the SQLite default on an ephemeral runner and losing the calendar
    after every run."""
    env.setenv("DATABASE_URL", "")
    assert Settings().database_url == ""

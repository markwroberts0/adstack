import pytest
from conftest import MINIMAL, MULTI_FILE, QUICKSTART, config_text

from adstack.config import load, load_sources
from adstack.errors import ConfigError


def test_examples_are_valid():
    assert load(QUICKSTART).summary() == "1 campaign, 2 ad groups, 5 keywords"
    assert load(MULTI_FILE).summary() == "2 campaigns, 3 ad groups, 8 keywords"


def test_minimal_configuration_normalizes_to_api_shapes():
    desired = config_text(MINIMAL).desired
    assert desired["namespace"] == "test" and desired["customer_id"] is None
    campaign = desired["campaigns"][0]
    assert campaign["name"] == "adstack:test:search"
    assert campaign["budget"] == 10_000_000 and campaign["locations"] == ["2840"]
    assert campaign["negatives"] == [{"text": "jobs", "matchType": "PHRASE"}]
    group = campaign["groups"][0]
    assert group["name"] == "adstack:main" and group["bid"] == 1_000_000 and group["ad_template"] == "basic"
    assert group["keywords"] == [
        {"text": "widgets", "matchType": "EXACT"},
        {"text": "blue widgets", "matchType": "EXACT"},
    ]
    assert group["ad"]["finalUrls"] == ["https://www.example.com/"]


def test_directory_files_are_merged(tmp_path):
    (tmp_path / "provider.yaml").write_text("version: 1\nprovider:\n  google_ads: {namespace: test, currency: USD}\n")
    template = MINIMAL[MINIMAL.index("ad_templates:") : MINIMAL.index("campaigns:")]
    campaigns = MINIMAL[MINIMAL.index("campaigns:") :]
    (tmp_path / "templates.yml").write_text("version: 1\n" + template)
    (tmp_path / "search.yaml").write_text("version: 1\n" + campaigns)
    (tmp_path / "other.yaml").write_text(
        "version: 1\n"
        + campaigns.replace("id: search", "id: second").replace("'[widgets]', '[blue widgets]'", "'[red widgets]'")
    )
    (tmp_path / "notes.txt").write_text("ignored")
    (tmp_path / ".hidden.yaml").write_text("ignored: true")
    config = load(tmp_path)
    assert [c["id"] for c in config.desired["campaigns"]] == ["second", "search"]  # files load in name order
    assert sorted(config.sources) == ["other.yaml", "provider.yaml", "search.yaml", "templates.yml"]


def test_conflicting_definitions_across_files_name_both_files(tmp_path):
    (tmp_path / "a.yaml").write_text(MINIMAL)
    (tmp_path / "b.yaml").write_text("version: 1\nprovider:\n  google_ads: {namespace: other, currency: USD}\n")
    with pytest.raises(ConfigError, match=r"provider.google_ads is defined in both a.yaml and b.yaml"):
        load(tmp_path)
    template = MINIMAL[MINIMAL.index("ad_templates:") : MINIMAL.index("campaigns:")]
    (tmp_path / "b.yaml").write_text("version: 1\n" + template)
    with pytest.raises(ConfigError, match=r"ad_templates.basic is defined in both a.yaml and b.yaml"):
        load(tmp_path)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (MINIMAL + "version: 1\n", "duplicate key 'version'"),
        (MINIMAL.replace("version: 1\n", ""), "'version: 1' is required"),
        (MINIMAL.replace("version: 1", "version: 2"), "'version: 1' is required"),
        (MINIMAL + "1: one\n", "mapping keys must be strings"),
        (MINIMAL + "extras: {}\n", "unknown key"),
        ("- a list\n", "expected a mapping"),
        (MINIMAL.replace("google_ads:", "facebook_ads:"), "unknown provider 'facebook_ads'"),
        (MINIMAL.replace("currency: USD", "currency: usd"), "ISO 4217"),
        (MINIMAL.replace("namespace: test", "namespace: Test_1"), "not a valid id"),
        (MINIMAL.replace("'[widgets]'", "'widgets'"), "must use exact match"),
        (MINIMAL.replace("'[widgets]'", "'[a b c d e f g h i j k]'"), "not a valid keyword"),
        (MINIMAL.replace("'[blue widgets]'", "'[Widgets]'"), "duplicate entry 'widgets'"),
        (MINIMAL.replace("status: PAUSED", "status: LIVE"), "PAUSED or ENABLED"),
        (MINIMAL.replace("locations: [2840]", "locations: [US]"), "geo target constant"),
        (MINIMAL.replace("locations: [2840]", "locations: []"), "expected 1 to 100 items"),
        (MINIMAL.replace("daily_budget: 10", "daily_budget: 10.005"), "at most 2 decimal places"),
        (MINIMAL.replace("daily_budget: 10", "daily_budget: 5000"), "0.01 to 1000.00"),
        (MINIMAL.replace("default_cpc: 1", "default_cpc: true"), "default_cpc"),
        (MINIMAL.replace("ad_template: basic", "ad_template: missing"), "unknown template 'missing'"),
        (MINIMAL.replace("Headline One", "A Headline That Is Far Too Long For Google"), "the limit is 30"),
        (MINIMAL.replace("Headline Two", "headline one"), "duplicate entry"),
        (MINIMAL.replace("https://www.example.com/", "http://www.example.com/"), "https:// URL"),
        (
            MINIMAL.replace("{namespace: test, currency: USD}", "{namespace: test, currency: USD, customer_id: 123}"),
            "10-digit Google Ads customer ID",
        ),
    ],
)
def test_invalid_configurations_are_rejected_with_a_specific_message(text, message):
    with pytest.raises(ConfigError, match=message):
        config_text(text)


def test_keyword_may_not_appear_in_two_ad_groups():
    text = (
        MINIMAL
        + """\
      - id: second
        ad_template: basic
        keywords: ['[widgets]']
"""
    )
    with pytest.raises(ConfigError, match=r"keyword \[widgets\] is in ad groups main and second"):
        config_text(text)


def test_inline_ads_limits_and_account_settings():
    text = (
        MINIMAL.replace(
            "{namespace: test, currency: USD}",
            "{namespace: test, currency: USD, customer_id: 123-456-7890, limits: {daily_budget: 5000, cpc: 2}}",
        )
        .replace("daily_budget: 10", "daily_budget: 4999.99")
        .replace(
            "ad_template: basic",
            "ad: {final_url: 'https://example.com/x', headlines: [One, Two, Three],"
            " descriptions: [First one., Second one.]}",
        )
    )
    desired = config_text(text).desired
    assert desired["customer_id"] == "1234567890"
    assert desired["campaigns"][0]["budget"] == 4_999_990_000
    assert desired["campaigns"][0]["groups"][0]["ad_template"] is None
    with pytest.raises(ConfigError, match=r"0\.01 to 2\.00"):
        config_text(text.replace("default_cpc: 1", "default_cpc: 2.5"))
    with pytest.raises(ConfigError, match="exactly one of ad"):
        config_text(text.replace("ad: {", "ad_template: basic\n        ad: {"))


def test_negative_keywords_accept_all_match_types():
    text = MINIMAL.replace("""['"jobs"']""", """['"jobs"', '[free]', 'cheap']""")
    negatives = config_text(text).desired["campaigns"][0]["negatives"]
    assert [n["matchType"] for n in negatives] == ["PHRASE", "EXACT", "BROAD"]


def test_campaign_ids_must_be_unique():
    campaigns = MINIMAL[MINIMAL.index("  - id: search") :]
    with pytest.raises(ConfigError, match="campaign id 'search' is used more than once"):
        config_text(MINIMAL + campaigns)


def test_missing_path_and_empty_directory(tmp_path):
    with pytest.raises(ConfigError, match="no such file or directory"):
        load(tmp_path / "missing")
    with pytest.raises(ConfigError, match="no configuration files"):
        load(tmp_path)
    with pytest.raises(ConfigError, match="no configuration files"):
        load_sources({})

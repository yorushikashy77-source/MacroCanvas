from core.constants import (
    INTERCEPTION_GITHUB_URL,
    INTERCEPTION_RELEASES_URL,
    KANATA_GITHUB_URL,
    KANATA_RELEASES_URL,
)


def test_official_component_links_point_to_upstream_github_pages():
    assert KANATA_GITHUB_URL == "https://github.com/jtroo/kanata"
    assert KANATA_RELEASES_URL == "https://github.com/jtroo/kanata/releases"
    assert INTERCEPTION_GITHUB_URL == "https://github.com/oblitum/Interception"
    assert (
        INTERCEPTION_RELEASES_URL
        == "https://github.com/oblitum/Interception/releases"
    )

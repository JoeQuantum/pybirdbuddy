"""Canonical read behavior using shared, provenance-labeled API fixtures."""

import json
from pathlib import Path

from graphql import OperationType, parse
from graphql.language.ast import OperationDefinitionNode
import pytest

from birdbuddy.exceptions import UnexpectedResponseError
from birdbuddy.visits import normalize_visit


def fixture(name):
    """Load a fresh public or explicitly synthetic selected payload."""
    return json.loads(
        (Path(__file__).parent / "fixtures" / "visits" / name).read_text()
    )


def test_recognized_preview_preserves_five_images_and_video():
    """A representative image does not replace the full media collection."""
    payload = fixture("public_postcard_analysis.json")
    visit = normalize_visit(payload)
    assert len(visit.medias) == 6
    assert sum(m.is_image for m in visit.medias) == 5
    assert sum(m.is_video for m in visit.medias) == 1
    assert visit.species[0].name
    assert visit.representative_image is not None
    assert visit.raw == payload


def test_sparse_collected_assets_stay_sparse_and_complete():
    """Historical capture has eleven ID-only assets, not invented URLs/dates."""
    visit = normalize_visit(fixture("public_collected_partial.json"))
    assert len(visit.medias) == 11
    assert all(m.id for m in visit.medias)
    assert all(m.created_at is None and m.content_url is None for m in visit.medias)
    assert visit.representative_image is None


def test_nested_species_and_matching_collection_media_attribution():
    """Same-species visits from different feeders retain distinct attribution."""
    nodes = fixture("feed_variants.json")["edges"]
    first, second = [normalize_visit(edge["node"]) for edge in nodes[:2]]
    assert first.species[0].name == second.species[0].name == "Northern Cardinal"
    assert first.feeder_id == "feeder-a"
    assert second.feeder_id == "feeder-b"
    assert first.attribution == "matching_collection_media"
    nodes[0]["node"]["collection"]["coverCollectionMedia"]["media"]["id"] = (
        "other-visit"
    )
    assert normalize_visit(nodes[0]["node"]).feeder_id is None


def test_unidentified_and_video_only_are_valid_visits():
    """Inference confidence alone never invents a recognized species."""
    unknown = normalize_visit(fixture("postcard_unrecognized.json"))
    assert not unknown.species
    assert unknown.medias
    video = normalize_visit(fixture("postcard_video_only.json"))
    assert video.representative_image is None
    assert len(video.medias) == 1
    assert video.medias[0].is_video
    assert video.preview_url == video.medias[0].thumbnail_url
    assert video.preview_url != video.medias[0].content_url


def test_large_payload_normalization_does_not_apply_event_size_limits():
    """All twenty-five assets remain available for detail lookup and sharing."""
    payload = fixture("postcard_long_urls.json")
    visit = normalize_visit(payload)
    assert len(visit.medias) == 25
    assert visit.medias[-1].is_video
    assert visit.raw == payload
    payload["medias"].clear()
    returned_raw = visit.raw
    returned_raw["medias"].clear()
    assert len(visit.medias) == len(visit.raw["medias"]) == 25


@pytest.mark.parametrize(
    "filename", ["feed_unknown_type.json", "feed_malformed_timestamp.json"]
)
def test_unknown_variants_and_missing_dates_remain_addressable(filename):
    """Malformed dates and future union members do not discard valid item IDs."""
    payload = fixture(filename)["edges"][0]["node"]
    visit = normalize_visit(payload)
    assert visit.item_id == payload["id"]
    assert visit.kind == payload["__typename"]
    assert visit.raw == payload


def test_url_hints_require_whole_path_segments_and_are_labeled():
    """Do not infer feeder identity from a signed query or an overlapping ID."""
    payload = {
        "id": "item",
        "media": {
            "id": "asset",
            "__typename": "MediaImage",
            "thumbnailUrl": "https://example.invalid/feeder-a/image?feeder=feeder-b",
        },
    }
    visit = normalize_visit(
        payload, known_feeder_ids=("feeder-a", "feeder-b", "feeder")
    )
    assert visit.feeder_id == "feeder-a"
    assert visit.attribution == "url_path"
    assert (
        normalize_visit(payload, known_feeder_ids=("feeder-b", "feeder")).feeder_id
        is None
    )
    payload["feeder"] = {"id": "explicit-feeder"}
    assert (
        normalize_visit(payload, known_feeder_ids=("feeder-a",)).feeder_id
        == "explicit-feeder"
    )


def test_unknown_attribution_is_not_assigned_to_sole_account_feeder():
    """A remote or unidentified source cannot silently become the sole local feeder."""
    payload = {
        "id": "unknown",
        "media": {"thumbnailUrl": "https://example.invalid/image"},
    }
    assert normalize_visit(payload, known_feeder_ids=("only-feeder",)).feeder_id is None


async def test_detail_lookup_is_read_only_and_preserves_every_asset(
    bbclient, graphql_mock
):
    """Use the actual client boundary, not a pre-normalized test double."""
    payload = fixture("postcard_complete.json")
    graphql_mock.return_value = {"data": {"feedItem": payload}}
    visit = await bbclient.get_feed_item(payload["id"])
    assert len(visit.medias) == 3
    assert visit.feeder_id == payload["feeder"]["id"]
    graphql_mock.assert_awaited_once()
    kwargs = graphql_mock.call_args.kwargs
    assert kwargs["variables"] == {"feedItemId": payload["id"]}
    operations = [
        n
        for n in parse(kwargs["query"]).definitions
        if isinstance(n, OperationDefinitionNode)
    ]
    assert [n.operation for n in operations] == [OperationType.QUERY]


async def test_detail_lookup_rejects_mismatched_identity(bbclient, graphql_mock):
    """A response for another card cannot be cached under the requested ID."""
    graphql_mock.return_value = {"data": {"feedItem": {"id": "wrong"}}}
    with pytest.raises(UnexpectedResponseError):
        await bbclient.get_feed_item("requested")


def test_singular_plural_and_legacy_video_media_are_deduplicated():
    """Legacy cover aliases do not duplicate photos or drop a separate video."""
    image = {"id": "image", "__typename": "MediaImage"}
    video = {"id": "video", "__typename": "MediaVideo"}
    visit = normalize_visit({"medias": [image], "media": image, "videoMedia": video})
    assert [m.id for m in visit.medias] == ["image", "video"]

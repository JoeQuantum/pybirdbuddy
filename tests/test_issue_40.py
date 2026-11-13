"""Retain the fork capture while adopting upstream's species correction.

The old test pinned a known bad Fish Crow selection and skipped the desired
Carolina Wren correction. Upstream now propagates the recognized species to
the ambiguous sighting when BEST_GUESS is allowed.
"""

from unittest.mock import ANY, AsyncMock, call

import pytest

from birdbuddy.client import BirdBuddy
from birdbuddy.sightings import PostcardSighting, SightingFinishStrategy

_POSTCARD_ID = "ea7c32bb-e95b-4fe6-a8ef-64134c1ae97e"
_SPECIES_ID = "35379866-a4c6-4991-a2af-e6da93eaec4f"
_MEDIA_ID = "e579818d-cb68-40d2-876c-fcfdf3483eb6"
_SIGHTING_ID = "233000f8-ecbe-430c-9227-2c826866323f"


@pytest.mark.asyncio
async def test_sighting_recognized_single(
    bbclient: BirdBuddy,
    issue_40: dict,
    graphql_mock: AsyncMock,
):
    """RECOGNIZED preserves the existing no-extra-identification behavior."""
    graphql_mock.return_value = {
        "data": {"sightingReportPostcardFinish": {"success": True}}
    }
    with pytest.deprecated_call():
        result = await bbclient.finish_postcard(
            _POSTCARD_ID,
            PostcardSighting(issue_40["sighting"]),
            strategy=SightingFinishStrategy.RECOGNIZED,
        )
    graphql_mock.assert_awaited_once_with(
        query=ANY,
        variables={
            "sightingReportPostcardFinishInput": {
                "defaultCoverMedia": [{"mediaId": _MEDIA_ID, "speciesId": _SPECIES_ID}],
                "notSelectedMediaIds": [],
                "feedItemId": _POSTCARD_ID,
                "reportToken": ANY,
            }
        },
        headers=ANY,
    )
    assert result is True


@pytest.mark.asyncio
async def test_sighting_best_guess_corrects_anomaly(
    bbclient: BirdBuddy,
    issue_40: dict,
    graphql_mock: AsyncMock,
):
    """BEST_GUESS uses the recognized Wren instead of the conflicting Crow."""
    modified_report = issue_40["sighting"]["sightingReport"].copy()
    modified_report["reportToken"] += ".altered"
    graphql_mock.side_effect = [
        {"data": {"sightingChooseSpecies": modified_report}},
        {"data": {"sightingReportPostcardFinish": {"success": True}}},
    ]
    with pytest.deprecated_call():
        result = await bbclient.finish_postcard(
            _POSTCARD_ID,
            PostcardSighting(issue_40["sighting"]),
            strategy=SightingFinishStrategy.BEST_GUESS,
        )
    graphql_mock.assert_has_awaits(
        [
            call(
                query=ANY,
                variables={
                    "sightingChooseSpeciesInput": {
                        "reportToken": ANY,
                        "speciesId": _SPECIES_ID,
                        "sightingId": _SIGHTING_ID,
                    }
                },
                headers=ANY,
            ),
            call(
                query=ANY,
                variables={
                    "sightingReportPostcardFinishInput": {
                        "defaultCoverMedia": [
                            {"mediaId": _MEDIA_ID, "speciesId": _SPECIES_ID}
                        ],
                        "notSelectedMediaIds": [],
                        "feedItemId": _POSTCARD_ID,
                        "reportToken": modified_report["reportToken"],
                    }
                },
                headers=ANY,
            ),
        ]
    )
    assert graphql_mock.await_count == 2
    assert result is True

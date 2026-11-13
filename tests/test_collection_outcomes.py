"""Collection success, sparse/null results, and uncertain mutation outcomes."""

import asyncio
import copy

from graphql import OperationType, parse
from graphql.language.ast import OperationDefinitionNode
import pytest

from birdbuddy.exceptions import PostcardOutcomeUnknownError, UnexpectedResponseError
from birdbuddy.postcards import CollectedPostcard
from birdbuddy.transport import HttpResponseError
from tests.conftest import load_json_fixture


def _collected():
    """The public capture retains all eleven sparse media IDs."""
    return copy.deepcopy(
        load_json_fixture("collect_flow.json")["postcard_collect"]["postcardCollect"][
            "collectedPostcard"
        ]
    )


def _operations(graphql_mock):
    """Observe real operation documents sent through the public transport boundary."""
    return [
        operation.operation
        for call in graphql_mock.call_args_list
        for operation in parse(call.kwargs["query"]).definitions
        if isinstance(operation, OperationDefinitionNode)
    ]


@pytest.mark.parametrize("first", [True, False])
async def test_replacement_envelope_wins_and_retains_first_flag(
    bbclient, graphql_mock, first
):
    """Do not discard the modern envelope or prefer its deprecated sibling."""
    item = _collected()
    graphql_mock.return_value = {
        "data": {
            "postcardCollect": {
                "postcardCollectedDetails": {
                    "isFirstCollectedPostcard": first,
                    "collectedPostcard": item,
                },
                "collectedPostcard": {"id": "wrong"},
            }
        }
    }
    result = await bbclient.collect_postcard("original", share=True)
    assert result.id == item["id"]
    assert len(result.medias) == 11
    assert result.receipt is not None
    assert result.receipt.is_first_collected_postcard is first
    assert result.receipt.share_requested is True
    assert result.receipt.sharing_confirmed is None
    assert not result.receipt.reconciled_by_read
    assert _operations(graphql_mock) == [OperationType.MUTATION]


async def test_collection_does_not_identify_by_default(bbclient, graphql_mock):
    """Collecting and running inference are independently requested actions."""
    graphql_mock.return_value = {
        "data": {
            "postcardCollect": {
                "collectedPostcard": _collected(),
                "postcardCollectedDetails": None,
            }
        }
    }
    await bbclient.collect_postcard("original")
    graphql_mock.assert_awaited_once()
    assert (
        "inferenceExternalPostcardReanalyze"
        not in graphql_mock.call_args.kwargs["query"]
    )
    assert graphql_mock.call_args.kwargs["variables"]["postcardCollectInput"] == {
        "share": False
    }


async def test_identification_is_an_explicit_precondition(bbclient, graphql_mock):
    """An explicitly requested identification completes before collecting."""
    graphql_mock.side_effect = [
        {
            "data": {
                "inferenceExternalPostcardReanalyze": {
                    "updatedFeedItem": {
                        "id": "original",
                        "__typename": "FeedItemNewPostcard",
                    }
                }
            }
        },
        {"data": {"postcardCollect": {"collectedPostcard": _collected()}}},
    ]
    await bbclient.collect_postcard("original", identify=True)
    assert _operations(graphql_mock) == [OperationType.MUTATION, OperationType.MUTATION]
    assert (
        "inferenceExternalPostcardReanalyze"
        in graphql_mock.call_args_list[0].kwargs["query"]
    )


@pytest.mark.parametrize(
    "response",
    [
        {
            "postcardCollect": {
                "postcardCollectedDetails": None,
                "collectedPostcard": None,
            }
        },
        {
            "postcardCollect": {
                "postcardCollectedDetails": {
                    "isFirstCollectedPostcard": True,
                    "collectedPostcard": None,
                }
            }
        },
        {"postcardCollect": None},
        {},
    ],
)
async def test_null_or_malformed_result_is_not_empty_success(
    bbclient, graphql_mock, response
):
    """A safe read that still sees an uncollected item cannot confirm success."""
    graphql_mock.side_effect = [
        {"data": response},
        {"data": {"feedItem": {"id": "original", "__typename": "FeedItemNewPostcard"}}},
    ]
    with pytest.raises(PostcardOutcomeUnknownError) as raised:
        await bbclient.collect_postcard("original")
    assert raised.value.postcard_id == "original"
    assert _operations(graphql_mock) == [OperationType.MUTATION, OperationType.QUERY]


async def test_timeout_after_acceptance_reconciles_by_read_without_replay(
    bbclient, graphql_mock
):
    """A current collected item is useful evidence, without claiming sharing."""
    item = _collected()
    item["id"] = "original"
    graphql_mock.side_effect = [asyncio.TimeoutError(), {"data": {"feedItem": item}}]
    result = await bbclient.collect_postcard("original", share=True)
    assert result.id == "original"
    assert result.receipt is not None
    assert result.receipt.reconciled_by_read
    assert result.receipt.sharing_confirmed is None
    assert _operations(graphql_mock) == [OperationType.MUTATION, OperationType.QUERY]


async def test_disappeared_original_id_does_not_prove_collection(
    bbclient, graphql_mock
):
    """The API may replace an ID; absence alone is deliberately inconclusive."""
    graphql_mock.side_effect = [
        asyncio.TimeoutError(),
        {"errors": [{"extensions": {"code": "NOT_FOUND"}}]},
    ]
    with pytest.raises(PostcardOutcomeUnknownError):
        await bbclient.collect_postcard("original")
    assert _operations(graphql_mock) == [OperationType.MUTATION, OperationType.QUERY]


async def test_explicit_rate_limit_is_a_rejection_not_an_unknown_result(
    bbclient, graphql_mock
):
    """Keep the server retry hint without sending another mutation."""
    graphql_mock.side_effect = HttpResponseError(429, 60)
    with pytest.raises(HttpResponseError) as raised:
        await bbclient.collect_postcard("original")
    assert raised.value.retry_after == 60
    graphql_mock.assert_awaited_once()


@pytest.mark.parametrize(
    "updated",
    [
        None,
        {},
        {"id": "wrong", "__typename": "FeedItemNewPostcard"},
        {"id": "original", "__typename": "FeedItemCollectedPostcard"},
    ],
)
async def test_bad_identification_prevents_following_collection(
    bbclient, graphql_mock, updated
):
    """Malformed analysis is never a successful prerequisite."""
    graphql_mock.return_value = {
        "data": {"inferenceExternalPostcardReanalyze": {"updatedFeedItem": updated}}
    }
    with pytest.raises(UnexpectedResponseError):
        await bbclient.collect_postcard("original", identify=True)
    graphql_mock.assert_awaited_once()


@pytest.mark.parametrize(
    "options", [{"share": "yes"}, {"identify": 1}, {"share": None}]
)
async def test_invalid_mutation_options_fail_before_requests(
    bbclient, graphql_mock, options
):
    """Python truthiness cannot silently authorize sharing or identification."""
    with pytest.raises(TypeError):
        await bbclient.collect_postcard("original", **options)
    graphql_mock.assert_not_called()


@pytest.mark.parametrize(
    "payload", [None, {}, {"id": "x"}, {"id": "x", "__typename": "FeedItemNewPostcard"}]
)
def test_empty_or_wrong_models_cannot_represent_collected_success(payload):
    """Model construction itself enforces its result identity."""
    with pytest.raises(UnexpectedResponseError):
        CollectedPostcard(payload)

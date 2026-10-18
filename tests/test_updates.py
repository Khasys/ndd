import asyncio

import httpx
import pytest

from app.updates import latest_version, newer


def hub(payload, status=200):
    def handler(request):
        assert request.url.path == "/v2/repositories/khasys/domain-watch/tags"
        return httpx.Response(status, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_latest_version_picks_highest_semver_tag():
    payload = {"results": [{"name": "latest"}, {"name": "v0.2.10"}, {"name": "v0.2.9"}, {"name": "ci"}, {"name": "v0.10.0"}]}
    assert asyncio.run(latest_version(hub(payload), "khasys/domain-watch")) == "v0.10.0"


def test_latest_version_none_when_unreachable():
    assert asyncio.run(latest_version(hub({}, status=503), "khasys/domain-watch")) is None
    assert asyncio.run(latest_version(hub({"results": [{"name": "latest"}]}), "khasys/domain-watch")) is None


@pytest.mark.parametrize(
    "current, latest, expected",
    [
        ("v0.2.2", "v0.2.3", "v0.2.3"),
        ("v0.2.2", "v0.10.0", "v0.10.0"),
        ("v0.2.2", "v0.2.2", None),
        ("v0.3.0", "v0.2.9", None),
        ("dev", "v0.3.0", None),
        ("v0.2.2", None, None),
    ],
)
def test_newer(current, latest, expected):
    assert newer(current, latest) == expected


@pytest.mark.parametrize("payload", [[], {"results": None}, {"results": ["v1.2.3"]}, {"results": [{"name": None}, 5]}])
def test_latest_version_tolerates_an_unexpected_json_shape(payload):
    assert asyncio.run(latest_version(hub(payload), "khasys/domain-watch")) is None

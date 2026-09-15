"""Session-wide guarantee that the test suite cannot reach the internet.

CLAUDE.md says every adapter test runs against a recorded fixture. That was true by accident
until phase 1: a phase 0 test parametrised over all four CLI subcommands, and the moment
`digest fetch` grew a real implementation underneath it, that test started making live HTTP
calls to Algolia -- and passed, so nothing complained. Four more adapters arrive in phase 3
and the same thing would happen again.

So this is enforced rather than asserted. A single test asserting "the suite is offline" only
covers the code paths someone thought to point it at; an autouse fixture makes reaching the
network structurally impossible for every test, including ones written by a future session
that never saw this file.

Loopback is deliberately exempt. asyncio's event loop builds its own self-pipe from a
127.0.0.1 socketpair on Windows, so blocking `socket.connect` outright breaks `asyncio.run`
itself rather than catching anything -- the guard would take down the tests it exists to
protect. Only non-loopback destinations raise.
"""

import asyncio
import json
import socket
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from digest.adapters.base import Adapter
from digest.config import Config, InterestProfile, Source, load_config
from digest.errors import DigestControlError
from digest.fetch import KNOWN_KINDS
from digest.models import Item

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    """Read a recorded fixture. Record with scripts/record_fixtures.py."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


def load_repo_config() -> Config:
    """The shipped config, with the registry's kinds supplied.

    `load_config` requires `known_kinds` rather than importing the registry, because
    `adapters/base.py` imports `Source` from `config` and reaching back would be a cycle.
    Required rather than optional so a caller who forgets gets a TypeError instead of
    silently unvalidated config.
    """
    return load_config(REPO_ROOT, known_kinds=KNOWN_KINDS)


def configured_source(name: str) -> Source:
    """The real entry from sources.yaml -- tests the shipped config, not a stand-in."""
    return load_repo_config().sources.by_name(name)


def configured_interests() -> InterestProfile:
    return load_repo_config().interests


def fixture_captured_at() -> datetime:
    """When the recorded fixtures were captured. The clock any fixture-reading test must use.

    Time-windowed adapters measure against `now`, so a test that reads frozen fixtures with
    a live clock has a shelf life. Before this existed, four `ai_blogs` tests were dated to
    fail on 2026-09-21 and 2026-10-01 with no code change -- and their failure mode was
    worse than a red suite: it trains you to re-record fixtures, which is also the correct
    response to a feed genuinely dying, so the two stop being distinguishable.

    R0 already solved this for the pipeline snapshot. This is the same solution, wired into
    the unit tests that needed it most.
    """
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    return datetime.fromisoformat(manifest["captured_at"]).astimezone(UTC)


def run_adapter(
    adapter: Adapter,
    source: Source,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[Item]:
    """Drive a real adapter against a mocked transport.

    `asyncio.run` rather than pytest-asyncio: the stdlib does this in one line and a test
    plugin would be a new dev dependency.
    """

    async def go() -> list[Item]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.fetch(client, source)

    return asyncio.run(go())


def serve(body: str, status: int = 200, content_type: str = "application/xml") -> Callable:
    """A handler that returns the same body for every request."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body, headers={"content-type": content_type})

    return handler


def serve_by_url(bodies: dict[str, httpx.Response | str]) -> Callable:
    """Route each feed URL to its own response, so bundle sources can be exercised properly.

    A URL mapped to a string gets a 200; map one to an `httpx.Response` for other statuses,
    or to an exception instance to make that one feed fail while the others answer.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        for url, body in bodies.items():
            if str(request.url) == url:
                if isinstance(body, BaseException):
                    raise body
                if isinstance(body, httpx.Response):
                    return body
                return httpx.Response(200, text=body)
        raise AssertionError(f"unmocked URL in test: {request.url}")

    return handler


#: Loopback only. Everything else is the internet as far as this suite is concerned.
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "", None})

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo
_real_create_connection = socket.create_connection


class NetworkAccessInTestError(DigestControlError):
    """Raised when a test tries to open a non-loopback connection.

    A `DigestControlError`: the suite guarantees it cannot reach the network, and that
    assumption about its own execution has been violated -- no feed did anything wrong.
    The base carries the BaseException reasoning that used to be duplicated here and in
    `scripts/snapshot_pipeline.py`, discovered independently both times by a test failing
    with the wrong message.
    """

    def __init__(self, target: object) -> None:
        super().__init__(
            f"Test tried to reach the network ({target!r}). Tests must run against recorded "
            f"fixtures in tests/fixtures/ -- see CLAUDE.md. Drive adapters through "
            f"httpx.MockTransport, and re-record a fixture with the recipe in the relevant "
            f"test module's docstring if the source's shape has changed."
        )


def _host_of(address: object) -> object:
    return address[0] if isinstance(address, tuple) and address else address


def _guard(address: object) -> None:
    if _host_of(address) not in _ALLOWED_HOSTS:
        raise NetworkAccessInTestError(address)


@pytest.fixture(autouse=True, scope="session")
def block_network() -> Iterator[None]:
    """Autouse and session-scoped: no test can opt out, and none has to opt in."""

    def connect(self: socket.socket, address: Any) -> Any:
        _guard(address)
        return _real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> Any:
        _guard(address)
        return _real_connect_ex(self, address)

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        _guard(host)
        return _real_getaddrinfo(host, port, *args, **kwargs)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        _guard(address)
        return _real_create_connection(address, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "connect", connect)
        patch.setattr(socket.socket, "connect_ex", connect_ex)
        patch.setattr(socket, "getaddrinfo", getaddrinfo)
        patch.setattr(socket, "create_connection", create_connection)
        yield

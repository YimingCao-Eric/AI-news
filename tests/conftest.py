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

import socket
from collections.abc import Iterator
from typing import Any

import pytest

#: Loopback only. Everything else is the internet as far as this suite is concerned.
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "", None})

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo
_real_create_connection = socket.create_connection


class NetworkAccessInTestError(BaseException):
    """Raised when a test tries to open a non-loopback connection.

    Derives from BaseException, not Exception, on purpose. `fetch.py` catches every
    `Exception` so that no source can abort a run -- which is correct in production but
    would turn a leaking test into a silently empty result rather than a failure. Sitting
    outside `Exception`, like KeyboardInterrupt, means this propagates through the
    resilience layer and fails the test that leaked.
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

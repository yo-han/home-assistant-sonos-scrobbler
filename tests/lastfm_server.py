"""A real loopback HTTP server that plays the Last.fm auth endpoint in tests."""

import asyncio
import json
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from aiohttp import web
from aiohttp.test_utils import TestServer

API_KEY = "0123456789abcdef0123456789abcdef"
SHARED_SECRET = "synthetic shared secret"
TOKEN = "fedcba9876543210fedcba9876543210"
SESSION_KEY = "d580d57f32848f5dcf574d1ce18d78b2"
ACCOUNT_NAME = "synthetic_listener"


@dataclass(frozen=True)
class RecordedRequest:
    """What the server received, for assertions on the wire format."""

    http_method: str
    path: str
    query: str
    form: Mapping[str, str]
    headers: Mapping[str, str]
    received_at: float = 0.0


@dataclass(frozen=True)
class Reply:
    """One scripted response; chunks are streamed without a Content-Length.

    A dropped reply closes the transport without answering, after the server has
    stored the scrobble: the write happened, the client cannot know. A delayed
    reply stores a scrobble only when store is set.
    """

    status: int = 200
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: Sequence[bytes] = ()
    drop: bool = False
    delay: float = 0.0
    store: bool = False


def json_reply(payload: object, status: int = 200) -> Reply:
    """Return a Reply with a JSON body."""
    return Reply(status=status, body=json.dumps(payload).encode())


def session_reply(name: str = ACCOUNT_NAME, key: str = SESSION_KEY) -> Reply:
    """Return the documented auth.getSession success shape."""
    return json_reply({"session": {"name": name, "key": key, "subscriber": 0}})


def token_reply(token: str = TOKEN) -> Reply:
    """Return the assumed auth.getToken success shape."""
    return json_reply({"token": token})


def now_playing_reply(code: str = "0") -> Reply:
    """Return the documented track.updateNowPlaying shape as JSON."""
    return json_reply({"nowplaying": {"ignoredMessage": {"code": code, "#text": ""}}})


class LastFmServer:
    """Script replies per API method and record every request."""

    def __init__(self) -> None:
        """Prepare an unstarted server."""
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._server = TestServer(app)
        self._replies: dict[str, deque[Reply]] = {}
        self._holds: dict[str, asyncio.Event] = {}
        self.held = asyncio.Event()
        self.requests: list[RecordedRequest] = []
        self.history: list[Mapping[str, str]] = []

    async def start(self) -> None:
        """Bind a loopback port."""
        await self._server.start_server(access_log=None)

    async def stop(self) -> None:
        """Release held requests and close the port."""
        for gate in self._holds.values():
            gate.set()
        await self._server.close()

    @property
    def url(self) -> str:
        """Return the base URL with a trailing slash."""
        return str(self._server.make_url("/"))

    def queue(self, method: str, *replies: Reply) -> None:
        """Serve these replies for the method before the defaults."""
        self._replies.setdefault(method, deque()).extend(replies)

    def hold(self, method: str) -> None:
        """Keep the next requests for the method waiting until release."""
        self._holds[method] = asyncio.Event()

    def release(self, method: str) -> None:
        """Let waiting requests for the method continue."""
        self.held.clear()
        self._holds.pop(method).set()

    def calls(self, method: str) -> list[RecordedRequest]:
        """Return the recorded requests of one API method."""
        return [item for item in self.requests if item.form.get("method") == method]

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        form = {key: str(value) for key, value in (await request.post()).items()}
        self.requests.append(
            RecordedRequest(
                request.method,
                request.path,
                request.query_string,
                form,
                dict(request.headers),
                time.time(),
            )
        )
        method = form.get("method", "")
        if gate := self._holds.get(method):
            self.held.set()
            await gate.wait()
        reply = self._next_reply(method)
        if method == "track.scrobble" and (reply.drop or reply.store):
            self.history.append(form)
        if reply.delay:
            await asyncio.sleep(reply.delay)
        if reply.drop and request.transport is not None:
            request.transport.close()
            return web.Response()
        if reply.chunks:
            return await self._stream(request, reply)
        return web.Response(
            status=reply.status, body=reply.body, headers=dict(reply.headers)
        )

    def _next_reply(self, method: str) -> Reply:
        if queued := self._replies.get(method):
            return queued.popleft()
        if method == "track.updateNowPlaying":
            return now_playing_reply()
        return session_reply() if method == "auth.getSession" else token_reply()

    @staticmethod
    async def _stream(request: web.Request, reply: Reply) -> web.StreamResponse:
        response = web.StreamResponse(status=reply.status, headers=dict(reply.headers))
        response.enable_chunked_encoding()
        await response.prepare(request)
        try:
            for chunk in reply.chunks:
                await response.write(chunk)
                await asyncio.sleep(0)
        except ConnectionError:
            return response
        await response.write_eof()
        return response

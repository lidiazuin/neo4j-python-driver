# Copyright (c) "Neo4j"
# Neo4j Sweden AB [https://neo4j.com]
#
# This file is part of Neo4j.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from logging import getLogger
from ssl import SSLSocket

from ..._async_compat.util import AsyncUtil
from ..._exceptions import BoltProtocolError
from ...api import (
    READ_ACCESS,
    SYSTEM_DATABASE,
    Version,
)
from ...exceptions import (
    ConfigurationError,
    DatabaseUnavailable,
    ForbiddenOnReadOnlyDatabase,
    Neo4jError,
    NotALeader,
    ServiceUnavailable,
)
from ._bolt3 import (
    ServerStateManager,
    ServerStates,
)
from ._bolt import AsyncBolt
from ._common import (
    check_supported_server_product,
    CommitResponse,
    InitResponse,
    Response,
)


log = getLogger("neo4j")


class AsyncBolt4x0(AsyncBolt):
    """ Protocol handler for Bolt 4.0.

    This is supported by Neo4j versions 4.0-4.4.
    """

    PROTOCOL_VERSION = Version(4, 0)

    supports_multiple_results = True

    supports_multiple_databases = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._server_state_manager = ServerStateManager(
            ServerStates.CONNECTED, on_change=self._on_server_state_change
        )

    def _on_server_state_change(self, old_state, new_state):
        log.debug("[#%04X]  State: %s > %s", self.local_port,
                  old_state.name, new_state.name)

    @property
    def is_reset(self):
        # We can't be sure of the server's state if there are still pending
        # responses. Unless the last message we sent was RESET. In that case
        # the server state will always be READY when we're done.
        if (self.responses and self.responses[-1]
                and self.responses[-1].message == "reset"):
            return True
        return self._server_state_manager.state == ServerStates.READY

    @property
    def encrypted(self):
        return isinstance(self.socket, SSLSocket)

    @property
    def der_encoded_server_certificate(self):
        return self.socket.getpeercert(binary_form=True)

    @property
    def local_port(self):
        try:
            return self.socket.getsockname()[1]
        except OSError:
            return 0

    def get_base_headers(self):
        return {
            "user_agent": self.user_agent,
        }

    async def hello(self):
        headers = self.get_base_headers()
        headers.update(self.auth_dict)
        logged_headers = dict(headers)
        if "credentials" in logged_headers:
            logged_headers["credentials"] = "*******"
        log.debug("[#%04X]  C: HELLO %r", self.local_port, logged_headers)
        self._append(b"\x01", (headers,),
                     response=InitResponse(self, "hello",
                     on_success=self.server_info.update))
        await self.send_all()
        await self.fetch_all()
        check_supported_server_product(self.server_info.agent)

    async def route(self, database=None, imp_user=None, bookmarks=None):
        if imp_user is not None:
            raise ConfigurationError(
                "Impersonation is not supported in Bolt Protocol {!r}. "
                "Trying to impersonate {!r}.".format(
                    self.PROTOCOL_VERSION, imp_user
                )
            )
        metadata = {}
        records = []

        if database is None:  # default database
            self.run(
                "CALL dbms.routing.getRoutingTable($context)",
                {"context": self.routing_context},
                mode="r",
                bookmarks=bookmarks,
                db=SYSTEM_DATABASE,
                on_success=metadata.update
            )
        else:
            self.run(
                "CALL dbms.routing.getRoutingTable($context, $database)",
                {"context": self.routing_context, "database": database},
                mode="r",
                bookmarks=bookmarks,
                db=SYSTEM_DATABASE,
                on_success=metadata.update
            )
        self.pull(on_success=metadata.update, on_records=records.extend)
        await self.send_all()
        await self.fetch_all()
        routing_info = [dict(zip(metadata.get("fields", ()), values)) for values in records]
        return routing_info

    def run(self, query, parameters=None, mode=None, bookmarks=None,
            metadata=None, timeout=None, db=None, imp_user=None, **handlers):
        if imp_user is not None:
            raise ConfigurationError(
                "Impersonation is not supported in Bolt Protocol {!r}. "
                "Trying to impersonate {!r}.".format(
                    self.PROTOCOL_VERSION, imp_user
                )
            )
        if not parameters:
            parameters = {}
        extra = {}
        if mode in (READ_ACCESS, "r"):
            extra["mode"] = "r"  # It will default to mode "w" if nothing is specified
        if db:
            extra["db"] = db
        if bookmarks:
            try:
                extra["bookmarks"] = list(bookmarks)
            except TypeError:
                raise TypeError("Bookmarks must be provided within an iterable")
        if metadata:
            try:
                extra["tx_metadata"] = dict(metadata)
            except TypeError:
                raise TypeError("Metadata must be coercible to a dict")
        if timeout is not None:
            try:
                extra["tx_timeout"] = int(1000 * float(timeout))
            except TypeError:
                raise TypeError("Timeout must be specified as a number of seconds")
            if extra["tx_timeout"] < 0:
                raise ValueError("Timeout must be a positive number or 0.")
        fields = (query, parameters, extra)
        log.debug("[#%04X]  C: RUN %s", self.local_port, " ".join(map(repr, fields)))
        self._append(b"\x10", fields, Response(self, "run", **handlers))

    def discard(self, n=-1, qid=-1, **handlers):
        extra = {"n": n}
        if qid != -1:
            extra["qid"] = qid
        log.debug("[#%04X]  C: DISCARD %r", self.local_port, extra)
        self._append(b"\x2F", (extra,), Response(self, "discard", **handlers))

    def pull(self, n=-1, qid=-1, **handlers):
        extra = {"n": n}
        if qid != -1:
            extra["qid"] = qid
        log.debug("[#%04X]  C: PULL %r", self.local_port, extra)
        self._append(b"\x3F", (extra,), Response(self, "pull", **handlers))

    def begin(self, mode=None, bookmarks=None, metadata=None, timeout=None,
              db=None, imp_user=None, **handlers):
        if imp_user is not None:
            raise ConfigurationError(
                "Impersonation is not supported in Bolt Protocol {!r}. "
                "Trying to impersonate {!r}.".format(
                    self.PROTOCOL_VERSION, imp_user
                )
            )
        extra = {}
        if mode in (READ_ACCESS, "r"):
            extra["mode"] = "r"  # It will default to mode "w" if nothing is specified
        if db:
            extra["db"] = db
        if bookmarks:
            try:
                extra["bookmarks"] = list(bookmarks)
            except TypeError:
                raise TypeError("Bookmarks must be provided within an iterable")
        if metadata:
            try:
                extra["tx_metadata"] = dict(metadata)
            except TypeError:
                raise TypeError("Metadata must be coercible to a dict")
        if timeout is not None:
            try:
                extra["tx_timeout"] = int(1000 * float(timeout))
            except TypeError:
                raise TypeError("Timeout must be specified as a number of seconds")
            if extra["tx_timeout"] < 0:
                raise ValueError("Timeout must be a positive number or 0.")
        log.debug("[#%04X]  C: BEGIN %r", self.local_port, extra)
        self._append(b"\x11", (extra,), Response(self, "begin", **handlers))

    def commit(self, **handlers):
        log.debug("[#%04X]  C: COMMIT", self.local_port)
        self._append(b"\x12", (), CommitResponse(self, "commit", **handlers))

    def rollback(self, **handlers):
        log.debug("[#%04X]  C: ROLLBACK", self.local_port)
        self._append(b"\x13", (), Response(self, "rollback", **handlers))

    async def reset(self):
        """ Add a RESET message to the outgoing queue, send
        it and consume all remaining messages.
        """

        def fail(metadata):
            raise BoltProtocolError("RESET failed %r" % metadata, self.unresolved_address)

        log.debug("[#%04X]  C: RESET", self.local_port)
        self._append(b"\x0F", response=Response(self, "reset", on_failure=fail))
        await self.send_all()
        await self.fetch_all()

    def goodbye(self):
        log.debug("[#%04X]  C: GOODBYE", self.local_port)
        self._append(b"\x02", ())

    async def _process_message(self, details, summary_signature,
                               summary_metadata):
        """ Process at most one message from the server, if available.

        :return: 2-tuple of number of detail messages and number of summary
                 messages fetched
        """
        if details:
            log.debug("[#%04X]  S: RECORD * %d", self.local_port, len(details))  # Do not log any data
            await self.responses[0].on_records(details)

        if summary_signature is None:
            return len(details), 0

        response = self.responses.popleft()
        response.complete = True
        if summary_signature == b"\x70":
            log.debug("[#%04X]  S: SUCCESS %r", self.local_port, summary_metadata)
            self._server_state_manager.transition(response.message,
                                                  summary_metadata)
            await response.on_success(summary_metadata or {})
        elif summary_signature == b"\x7E":
            log.debug("[#%04X]  S: IGNORED", self.local_port)
            await response.on_ignored(summary_metadata or {})
        elif summary_signature == b"\x7F":
            log.debug("[#%04X]  S: FAILURE %r", self.local_port, summary_metadata)
            self._server_state_manager.state = ServerStates.FAILED
            try:
                await response.on_failure(summary_metadata or {})
            except (ServiceUnavailable, DatabaseUnavailable):
                if self.pool:
                    await self.pool.deactivate(address=self.unresolved_address)
                raise
            except (NotALeader, ForbiddenOnReadOnlyDatabase):
                if self.pool:
                    self.pool.on_write_failure(address=self.unresolved_address)
                raise
            except Neo4jError as e:
                if self.pool and e.invalidates_all_connections():
                    await self.pool.mark_all_stale()
                raise
        else:
            raise BoltProtocolError("Unexpected response message with signature "
                                    "%02X" % ord(summary_signature), self.unresolved_address)

        return len(details), 1


class AsyncBolt4x1(AsyncBolt4x0):
    """ Protocol handler for Bolt 4.1.

    This is supported by Neo4j versions 4.1 - 4.4.
    """

    PROTOCOL_VERSION = Version(4, 1)

    def get_base_headers(self):
        """ Bolt 4.1 passes the routing context, originally taken from
        the URI, into the connection initialisation message. This
        enables server-side routing to propagate the same behaviour
        through its driver.
        """
        headers = {
            "user_agent": self.user_agent,
        }
        if self.routing_context is not None:
            headers["routing"] = self.routing_context
        return headers


class AsyncBolt4x2(AsyncBolt4x1):
    """ Protocol handler for Bolt 4.2.

    This is supported by Neo4j version 4.2 - 4.4.
    """

    PROTOCOL_VERSION = Version(4, 2)


class AsyncBolt4x3(AsyncBolt4x2):
    """ Protocol handler for Bolt 4.3.

    This is supported by Neo4j version 4.3 - 4.4.
    """

    PROTOCOL_VERSION = Version(4, 3)

    async def route(self, database=None, imp_user=None, bookmarks=None):
        if imp_user is not None:
            raise ConfigurationError(
                "Impersonation is not supported in Bolt Protocol {!r}. "
                "Trying to impersonate {!r}.".format(
                    self.PROTOCOL_VERSION, imp_user
                )
            )

        routing_context = self.routing_context or {}
        log.debug("[#%04X]  C: ROUTE %r %r %r", self.local_port,
                  routing_context, bookmarks, database)
        metadata = {}
        if bookmarks is None:
            bookmarks = []
        else:
            bookmarks = list(bookmarks)
        self._append(b"\x66", (routing_context, bookmarks, database),
                     response=Response(self, "route",
                                       on_success=metadata.update))
        await self.send_all()
        await self.fetch_all()
        return [metadata.get("rt")]

    async def hello(self):
        def on_success(metadata):
            self.configuration_hints.update(metadata.pop("hints", {}))
            self.server_info.update(metadata)
            if "connection.recv_timeout_seconds" in self.configuration_hints:
                recv_timeout = self.configuration_hints[
                    "connection.recv_timeout_seconds"
                ]
                if isinstance(recv_timeout, int) and recv_timeout > 0:
                    self.socket.settimeout(recv_timeout)
                else:
                    log.info("[#%04X]  Server supplied an invalid value for "
                             "connection.recv_timeout_seconds (%r). Make sure "
                             "the server and network is set up correctly.",
                             self.local_port, recv_timeout)

        headers = self.get_base_headers()
        headers.update(self.auth_dict)
        logged_headers = dict(headers)
        if "credentials" in logged_headers:
            logged_headers["credentials"] = "*******"
        log.debug("[#%04X]  C: HELLO %r", self.local_port, logged_headers)
        self._append(b"\x01", (headers,),
                     response=InitResponse(self, "hello",
                                           on_success=on_success))
        await self.send_all()
        await self.fetch_all()
        check_supported_server_product(self.server_info.agent)


class AsyncBolt4x4(AsyncBolt4x3):
    """ Protocol handler for Bolt 4.4.

    This is supported by Neo4j version 4.4.
    """

    PROTOCOL_VERSION = Version(4, 4)

    async def route(self, database=None, imp_user=None, bookmarks=None):
        routing_context = self.routing_context or {}
        db_context = {}
        if database is not None:
            db_context.update(db=database)
        if imp_user is not None:
            db_context.update(imp_user=imp_user)
        log.debug("[#%04X]  C: ROUTE %r %r %r", self.local_port,
                  routing_context, bookmarks, db_context)
        metadata = {}
        if bookmarks is None:
            bookmarks = []
        else:
            bookmarks = list(bookmarks)
        self._append(b"\x66", (routing_context, bookmarks, db_context),
                     response=Response(self, "route",
                                       on_success=metadata.update))
        await self.send_all()
        await self.fetch_all()
        return [metadata.get("rt")]

    def run(self, query, parameters=None, mode=None, bookmarks=None,
            metadata=None, timeout=None, db=None, imp_user=None, **handlers):
        if not parameters:
            parameters = {}
        extra = {}
        if mode in (READ_ACCESS, "r"):
            # It will default to mode "w" if nothing is specified
            extra["mode"] = "r"
        if db:
            extra["db"] = db
        if imp_user:
            extra["imp_user"] = imp_user
        if bookmarks:
            try:
                extra["bookmarks"] = list(bookmarks)
            except TypeError:
                raise TypeError("Bookmarks must be provided within an iterable")
        if metadata:
            try:
                extra["tx_metadata"] = dict(metadata)
            except TypeError:
                raise TypeError("Metadata must be coercible to a dict")
        if timeout is not None:
            try:
                extra["tx_timeout"] = int(1000 * float(timeout))
            except TypeError:
                raise TypeError("Timeout must be specified as a number of seconds")
            if extra["tx_timeout"] < 0:
                raise ValueError("Timeout must be a positive number or 0.")
        fields = (query, parameters, extra)
        log.debug("[#%04X]  C: RUN %s", self.local_port,
                  " ".join(map(repr, fields)))
        self._append(b"\x10", fields, Response(self, "run", **handlers))

    def begin(self, mode=None, bookmarks=None, metadata=None, timeout=None,
              db=None, imp_user=None, **handlers):
        extra = {}
        if mode in (READ_ACCESS, "r"):
            # It will default to mode "w" if nothing is specified
            extra["mode"] = "r"
        if db:
            extra["db"] = db
        if imp_user:
            extra["imp_user"] = imp_user
        if bookmarks:
            try:
                extra["bookmarks"] = list(bookmarks)
            except TypeError:
                raise TypeError("Bookmarks must be provided within an iterable")
        if metadata:
            try:
                extra["tx_metadata"] = dict(metadata)
            except TypeError:
                raise TypeError("Metadata must be coercible to a dict")
        if timeout is not None:
            try:
                extra["tx_timeout"] = int(1000 * float(timeout))
            except TypeError:
                raise TypeError("Timeout must be specified as a number of seconds")
            if extra["tx_timeout"] < 0:
                raise ValueError("Timeout must be a positive number or 0.")
        log.debug("[#%04X]  C: BEGIN %r", self.local_port, extra)
        self._append(b"\x11", (extra,), Response(self, "begin", **handlers))

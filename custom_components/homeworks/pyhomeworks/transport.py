"""Async transport layer for Homeworks RS-232 communication.

This module handles:
- Async TCP socket connection
- Login sequence
- Read/write operations
- Reconnection logic

No message parsing here - just bytes in/out.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .exceptions import (
    HomeworksConnectionFailed,
    HomeworksConnectionLost,
    HomeworksInvalidCredentialsProvided,
    HomeworksNoCredentialsProvided,
)

_LOGGER = logging.getLogger(__name__)

# Protocol constants
CRLF = b"\r\n"
LOGIN_REQUEST = b"LOGIN: "
LOGIN_INCORRECT = b"login incorrect"
LOGIN_SUCCESSFUL = b"login successful"

# Timeouts
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 1.0
LOGIN_PROMPT_WAIT = 0.2


class HomeworksTransport:
    """Async transport for Homeworks controller communication.

    Handles low-level socket operations:
    - Connection establishment
    - Login sequence
    - Async read/write
    - Connection state tracking

    Does NOT handle:
    - Message parsing (use MessageParser)
    - Command building (use commands module)
    - Reconnection (handled by higher layer)
    """

    def __init__(
        self,
        host: str,
        port: int,
        credentials: str | None = None,
    ) -> None:
        """Initialize transport.

        Args:
            host: Controller hostname or IP
            port: Controller port
            credentials: Login credentials (password or "user, password")
        """
        self._host = host
        self._port = port
        self._credentials = credentials

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        """Return True if connected."""
        return self._connected and self._writer is not None

    @property
    def host(self) -> str:
        """Return host."""
        return self._host

    @property
    def port(self) -> int:
        """Return port."""
        return self._port

    async def connect(self) -> None:
        """Connect to the controller.

        Raises:
            HomeworksConnectionFailed: If connection fails
            HomeworksNoCredentialsProvided: If login required but no credentials
            HomeworksInvalidCredentialsProvided: If login fails
        """
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=CONNECT_TIMEOUT,
            )
        except asyncio.TimeoutError as err:
            raise HomeworksConnectionFailed(
                f"Connection to {self._host}:{self._port} timed out"
            ) from err
        except OSError as err:
            raise HomeworksConnectionFailed(
                f"Failed to connect to {self._host}:{self._port}: {err}"
            ) from err

        _LOGGER.info("Connected to %s:%s", self._host, self._port)

        # Check for login prompt
        await asyncio.sleep(LOGIN_PROMPT_WAIT)
        initial_data = await self._read_available()

        # Strip leading CRLF
        while initial_data.startswith(CRLF):
            initial_data = initial_data[len(CRLF) :]

        if initial_data.startswith(LOGIN_REQUEST):
            await self._handle_login()

        self._connected = True

    async def _read_available(self) -> bytes:
        """Read available data without blocking."""
        if not self._reader:
            return b""

        try:
            return await asyncio.wait_for(
                self._reader.read(1024),
                timeout=READ_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return b""

    async def _handle_login(self) -> None:
        """Handle login sequence.

        Raises:
            HomeworksNoCredentialsProvided: If no credentials configured
            HomeworksInvalidCredentialsProvided: If login fails
        """
        if not self._credentials:
            raise HomeworksNoCredentialsProvided("Login required but no credentials")

        # Send credentials
        await self.write(self._credentials)

        # Read response
        response = await self._read_available()

        # Strip CRLF
        while response.startswith(CRLF):
            response = response[len(CRLF) :]

        if response.startswith(LOGIN_INCORRECT):
            raise HomeworksInvalidCredentialsProvided("Login failed")

        if response.startswith(LOGIN_SUCCESSFUL):
            _LOGGER.debug("Login successful")

    async def write(self, command: str) -> bool:
        """Write a command to the controller.

        Args:
            command: Command string (without CRLF)

        Returns:
            True if write succeeded, False otherwise
        """
        if not self._writer:
            return False

        try:
            data = command.encode("utf-8") + CRLF
            self._writer.write(data)
            await self._writer.drain()
            _LOGGER.debug("Sent: %s", command)
            return True
        except (ConnectionError, OSError) as err:
            _LOGGER.debug("Write failed: %s", err)
            await self.close()
            return False

    async def read(self, timeout: float = READ_TIMEOUT) -> bytes:
        """Read data from the controller.

        Args:
            timeout: Read timeout in seconds

        Returns:
            Bytes read (may be empty on timeout)

        Raises:
            HomeworksConnectionLost: If connection is lost
        """
        if not self._reader:
            raise HomeworksConnectionLost("Not connected")

        try:
            data = await asyncio.wait_for(
                self._reader.read(4096),
                timeout=timeout,
            )
            if not data:
                raise HomeworksConnectionLost("Connection closed by controller")
            _LOGGER.debug("Received: %s", data)
            return data
        except asyncio.TimeoutError:
            return b""

    async def close(self) -> None:
        """Close the connection."""
        self._connected = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._writer = None
                self._reader = None
        _LOGGER.debug("Connection closed")

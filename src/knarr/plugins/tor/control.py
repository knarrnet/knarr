"""Async Tor control port client — SPEC-tor-plugin.md v1.1 §2.2.1.

Synthesized from panel submissions (2026-04-11):
- GPT base structure (idempotent connect, proper 250+ multi-line handling, lean)
- Opus additions: _consensus_lost edge tracking, DANGEROUS_SOCKS detection,
  ControlPortError custom exception, strict Tor/ prefix validation
- Sonnet additions: 30s keepalive wait_for in run_event_loop (silent-disconnect
  detection), BUILD_TIME_ELAPSED parser for slow-circuit events

Minimal client for:
- PROTOCOLINFO 1 handshake (O-6 daemon identity verification)
- AUTHENTICATE (cookie / password / none)
- SETEVENTS NEWCONSENSUS STATUS_GENERAL CIRC
- GETINFO hs/service/status/{name}
- Persistent event loop for NEWCONSENSUS / STATUS_GENERAL / CIRC lines

Design notes:
- Persistent background task (run_event_loop) with 30s keepalive timeout.
- Reconnect is owned by the caller (TorPlugin handles retry in on_tick).
- No python-socks dependency here — control port is a plain TCP protocol.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


log = logging.getLogger("knarr.plugin.tor.control")


# Events subscribed to on the control port.
_DEFAULT_EVENTS: List[str] = ["NEWCONSENSUS", "STATUS_GENERAL", "CIRC"]

# Event loop keepalive timeout (Sonnet's contribution — catches silent
# disconnects that would otherwise hang readline() forever).
_EVENT_LOOP_KEEPALIVE = 30.0

# Slow-circuit threshold default (ms), overridden at construction time.
_DEFAULT_SLOW_CIRCUIT_MS = 5000


class ControlPortError(Exception):
    """Protocol-level control port error (auth failure, non-Tor reply, etc.).

    TCP-level errors surface as the underlying OSError / ConnectionError so
    callers can distinguish "port is Tor but something is wrong" from "port
    is not reachable at all".
    """


class AsyncControlPort:
    """Persistent async client for the Tor control protocol.

    Usage::

        client = AsyncControlPort("127.0.0.1", 9051, auth_method="cookie")
        await client.connect()
        info = await client.protocol_info()    # verify Tor/ version prefix
        await client.authenticate()
        await client.subscribe_events()
        asyncio.create_task(client.run_event_loop())
        ...
        await client.disconnect()

    For the §2.2 step 1 daemon identity test (O-6 fix), only connect() +
    protocol_info() are required. The client refuses to return a usable
    handshake unless the peer replies with a recognizable 250-PROTOCOLINFO
    block containing a Tor/ version token — a bare TCP listener can't fake
    this format.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9051,
        auth_method: str = "cookie",
        cookie_file: str = "",
        password: str = "",
        bus_emit: Optional[Callable[..., None]] = None,
        connect_timeout: float = 3.0,
        slow_circuit_warning_ms: int = _DEFAULT_SLOW_CIRCUIT_MS,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.auth_method = (auth_method or "none").lower()
        self.cookie_file = cookie_file
        self.password = password
        self._bus_emit = bus_emit
        self._connect_timeout = float(connect_timeout)
        self._slow_circuit_ms = int(slow_circuit_warning_ms)

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._authenticated = False

        # Consensus state tracks the lost → recovered edge (Opus pattern).
        # _dispatch_event emits tor.consensus_recovered only when we were
        # previously in the lost state.
        self._consensus_lost = False

        # Phase 6 O-3 fix: reader-race elimination via command/event demux.
        # Before the background event loop starts (_event_loop_active=False),
        # `_send_command` reads the reply directly from the stream — there is
        # no other reader. Once `run_event_loop` starts, it becomes the SOLE
        # reader: it classifies each line as 650-prefixed async event (dispatch)
        # or 2xx/5xx synchronous reply (push into the pending-command queue).
        # `_send_command` in event-loop-active mode installs a Future, awaits
        # it, and trusts `run_event_loop` to resolve it with the accumulated
        # reply. A command lock ensures only one command is in flight at a
        # time.
        self._event_loop_active: bool = False
        self._cmd_lock: asyncio.Lock = asyncio.Lock()
        self._pending_reply: Optional[asyncio.Future] = None
        self._pending_reply_lines: List[str] = []

        # Exposed state for handler consumption / tests
        self.version: str = ""
        self.auth_methods: List[str] = []
        self._protocol_info: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Low-level I/O
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Open the TCP connection. Idempotent — returns True if already open.

        Raises ConnectionError / OSError / TimeoutError on network failure.
        Does NOT verify Tor identity here — that's protocol_info()'s job.
        """
        if self._reader is not None and self._writer is not None and not self._writer.is_closing():
            return True
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self._connect_timeout,
            )
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"control port {self.host}:{self.port} timeout") from e
        log.debug("TOR_CTRL_CONNECT host=%s port=%d", self.host, self.port)
        return True

    async def _send_command(self, command: str) -> List[str]:
        """Send a command line and read the reply block.

        Two modes (Phase 6 O-3 fix):
          - Event-loop inactive: direct read from stream (used for connect-time
            handshake: PROTOCOLINFO, AUTHENTICATE, SETEVENTS).
          - Event-loop active: install a Future, await the demux-pushed reply.

        The command lock serializes callers — only one command is in flight at
        a time regardless of mode, so `_pending_reply` is safe without further
        coordination.
        """
        async with self._cmd_lock:
            if self._writer is None:
                raise ControlPortError("control port not connected")
            self._writer.write(command.encode("ascii") + b"\r\n")
            try:
                await asyncio.wait_for(
                    self._writer.drain(), timeout=self._connect_timeout
                )
            except asyncio.TimeoutError as e:
                raise TimeoutError("control port write timeout") from e

            if not self._event_loop_active:
                return await self._read_reply()

            # Event loop is the sole reader — install a Future and wait for
            # `run_event_loop` to fulfill it with the accumulated reply.
            loop = asyncio.get_event_loop()
            fut: asyncio.Future = loop.create_future()
            self._pending_reply = fut
            self._pending_reply_lines = []
            try:
                return await asyncio.wait_for(fut, timeout=self._connect_timeout)
            except asyncio.TimeoutError as e:
                raise TimeoutError("control port reply timeout") from e
            finally:
                self._pending_reply = None
                self._pending_reply_lines = []

    async def _read_reply(self) -> List[str]:
        """Read one Tor control-protocol reply block.

        Handles three reply line formats (control-spec.txt §3):
          - "NNN KEY=VAL"   — end of reply (single line or last of multi-line)
          - "NNN-KEY=VAL"   — continuation (more lines follow)
          - "NNN+KEY=VAL\\r\\n...data..\\r\\n.\\r\\n"  — multi-line data block

        GPT's contribution: the "250+" data block format with "." terminator.
        Opus/Sonnet don't handle this format at all — real spec gap.
        """
        if self._reader is None:
            raise ControlPortError("control port not connected")
        lines: List[str] = []
        while True:
            try:
                raw = await asyncio.wait_for(
                    self._reader.readline(), timeout=self._connect_timeout
                )
            except asyncio.TimeoutError as e:
                raise TimeoutError(f"control port readline timeout") from e
            if not raw:
                raise ControlPortError(
                    f"control port {self.host}:{self.port} closed before reply complete"
                )
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            lines.append(line)

            # Protocol floor: reply codes are 3 digits; a line that doesn't
            # start with 3 digits is not Tor. Fail closed.
            if len(line) < 4 or not line[:3].isdigit():
                raise ControlPortError(
                    f"control port {self.host}:{self.port} non-Tor reply: {line!r}"
                )

            sep = line[3]
            if sep == "+":
                # Multi-line data block — read until a "." on its own line.
                while True:
                    try:
                        block_raw = await asyncio.wait_for(
                            self._reader.readline(), timeout=self._connect_timeout
                        )
                    except asyncio.TimeoutError as e:
                        raise TimeoutError(f"control port data block timeout") from e
                    if not block_raw:
                        raise ControlPortError("control port closed during data block")
                    block_line = block_raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    lines.append(block_line)
                    if block_line == ".":
                        break
                continue
            if sep == " ":
                return lines
            # sep == "-" means another line follows
            if sep != "-":
                raise ControlPortError(
                    f"control port {self.host}:{self.port} bad separator {sep!r} in: {line!r}"
                )

    # ------------------------------------------------------------------
    # High-level commands
    # ------------------------------------------------------------------

    async def protocol_info(self) -> Dict[str, str]:
        """Issue PROTOCOLINFO 1 and parse the Tor/ version + auth methods.

        O-6 fix: raises ControlPortError if the peer does not reply with a
        recognizable 250-PROTOCOLINFO block including a Tor/ version token.
        A bare TCP listener on 9051 cannot fake this format.
        """
        lines = await self._send_command("PROTOCOLINFO 1")
        if not lines or not lines[0].startswith("250-PROTOCOLINFO"):
            raise ControlPortError(
                f"PROTOCOLINFO reply not recognized: {lines[0] if lines else '<empty>'}"
            )

        info: Dict[str, str] = {}
        for line in lines:
            # Auth methods line: "250-AUTH METHODS=COOKIE,SAFECOOKIE COOKIEFILE=\"...\""
            if line.startswith("250-AUTH "):
                methods_match = re.search(r"METHODS=([^ ]+)", line)
                cookie_match = re.search(r'COOKIEFILE="([^"]+)"', line)
                if methods_match:
                    info["auth_methods"] = methods_match.group(1)
                if cookie_match:
                    info["cookie_file"] = cookie_match.group(1)
            # Version line: "250-VERSION Tor=\"0.4.7.13\""
            elif line.startswith("250-VERSION "):
                match = re.search(r'Tor="?([^" ]+)"?', line)
                if match:
                    info["version"] = f"Tor/{match.group(1)}"

        version = info.get("version", "")
        if not version.startswith("Tor/"):
            raise ControlPortError(
                f"PROTOCOLINFO did not return a Tor/ version: {lines!r}"
            )

        self.version = version
        self.auth_methods = info.get("auth_methods", "").split(",") if info.get("auth_methods") else []
        self._protocol_info = info
        return info

    async def authenticate(self) -> bool:
        """Run AUTHENTICATE based on self.auth_method. Returns True on success."""
        if not self._protocol_info:
            await self.protocol_info()

        method = self.auth_method
        if method == "none":
            lines = await self._send_command("AUTHENTICATE")
        elif method == "password":
            escaped = self.password.replace("\\", "\\\\").replace('"', '\\"')
            lines = await self._send_command(f'AUTHENTICATE "{escaped}"')
        elif method == "cookie":
            cookie_path = self.cookie_file or self._protocol_info.get("cookie_file", "") or self._default_cookie_path()
            try:
                data = Path(os.path.expanduser(cookie_path)).read_bytes()
            except OSError as e:
                raise ControlPortError(
                    f"cookie file unreadable: {cookie_path} ({e})"
                ) from e
            lines = await self._send_command(f"AUTHENTICATE {data.hex()}")
        else:
            raise ControlPortError(f"unknown auth_method: {method!r}")

        # Accept any "250 ..." or "250 OK" as success (last line after hyphen continuations)
        if not lines or not lines[-1].startswith("250"):
            raise ControlPortError(f"AUTHENTICATE failed: {lines!r}")
        self._authenticated = True
        return True

    @staticmethod
    def _default_cookie_path() -> str:
        """Best-effort default for the Tor control auth cookie file."""
        if os.name == "nt":
            appdata = os.environ.get("APPDATA", "")
            return str(Path(appdata) / "tor" / "control_auth_cookie")
        # POSIX: Debian/Ubuntu /run/tor/control.authcookie
        for candidate in (
            "/run/tor/control.authcookie",
            "/var/run/tor/control.authcookie",
            "/var/lib/tor/control_auth_cookie",
        ):
            if Path(candidate).is_file():
                return candidate
        return "/run/tor/control.authcookie"

    async def subscribe_events(self, events: Optional[List[str]] = None) -> None:
        """Issue SETEVENTS to subscribe. Default: NEWCONSENSUS + STATUS_GENERAL + CIRC."""
        if events is None:
            events = list(_DEFAULT_EVENTS)
        if not events:
            return
        lines = await self._send_command("SETEVENTS " + " ".join(events))
        if not lines or not lines[-1].startswith("250"):
            raise ControlPortError(f"SETEVENTS failed: {lines!r}")

    async def get_hs_status(self, service_name: str) -> Optional[Dict[str, str]]:
        """GETINFO hs/service/status/{name}.

        Returns a parsed {"raw": ..., "state": ...} dict, or None if the info
        key is unavailable (older Tor versions don't expose this namespace).
        """
        try:
            lines = await self._send_command(f"GETINFO hs/service/status/{service_name}")
        except ControlPortError:
            return None

        # 552 = unrecognized info key
        if any(line.startswith("552") for line in lines):
            return None

        raw = ""
        for line in lines:
            if line.startswith("250-") or line.startswith("250+"):
                raw = line[4:]
                break
        if not raw:
            return None

        state = ""
        match = re.search(r"state=([A-Za-z_]+)", raw)
        if match:
            state = match.group(1).lower()
        return {"raw": raw, "state": state}

    async def get_clock_skew(self) -> Optional[float]:
        """Placeholder — Tor has no stable scalar GETINFO for clock skew.

        Authoritative clock-skew events arrive via STATUS_GENERAL CLOCK_SKEW
        in the event loop instead. Returns None to acknowledge no polling path.
        """
        return None

    # ------------------------------------------------------------------
    # Event loop
    # ------------------------------------------------------------------

    def start_event_loop(self) -> asyncio.Task:
        """Schedule `run_event_loop` and synchronously set the active flag.

        Callers MUST use this helper instead of `create_task(run_event_loop())`
        directly. The flag must flip to True BEFORE the task is scheduled so
        that any `_send_command` caller racing on the main event loop takes
        the demux path (futures) rather than trying to read the stream
        directly in parallel with the background reader.
        """
        self._event_loop_active = True
        return asyncio.create_task(self.run_event_loop())

    async def run_event_loop(self) -> None:
        """Persistent read loop — SOLE reader of the control stream.

        Sonnet's contribution: the 30s keepalive wait_for. Without it,
        readline() would hang forever on a silent disconnect.

        Phase 6 O-2 + O-3 fix: this loop now handles the `650+` multi-line
        async event format (NEWCONSENSUS in particular — Tor delivers it as
        `650+NEWCONSENSUS\\r\\n<body>\\r\\n.\\r\\n650 OK` per control-spec.txt
        §4.1.1). It also demultiplexes synchronous command replies onto
        `_pending_reply`, so there is no longer any race between this task
        and `_send_command` calling readline() on the same StreamReader.

        Exits on EOF / connection error / cancel. On exit, any in-flight
        pending reply is failed so callers wake up with an error instead of
        hanging.
        """
        if self._reader is None:
            return
        # Flag may already be True (set by start_event_loop before scheduling),
        # but set it here too for the out-of-band `create_task(run_event_loop())`
        # call path and to document the state transition.
        self._event_loop_active = True
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(
                        self._reader.readline(), timeout=_EVENT_LOOP_KEEPALIVE
                    )
                except asyncio.TimeoutError:
                    # Keepalive tick — check if socket is still open
                    if self._writer is None or self._writer.is_closing():
                        return
                    continue
                except (ConnectionError, OSError, asyncio.IncompleteReadError,
                        RuntimeError):
                    # Phase 6 O-15 fix: RuntimeError covers the historic
                    # "readuntil() called while another coroutine is already
                    # waiting" case so a regression can't silently crash the
                    # task via a never-retrieved exception.
                    return
                except asyncio.CancelledError:
                    return
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")

                # Classify the line. All valid control-port lines start with
                # a 3-digit code followed by ' ', '-', or '+'.
                if len(line) < 4 or not line[:3].isdigit():
                    continue  # ignore garbage — don't tear down the stream
                code = line[:3]
                sep = line[3]

                if code.startswith("6"):
                    # Async event (650). sep: ' ' single-line, '-' continuation,
                    # '+' multi-line data block terminated by '.'
                    payload = line[4:]
                    if sep == "+":
                        # Phase 6 O-2 fix: accumulate until a bare "." line.
                        # Real Tor delivers NEWCONSENSUS as 650+NEWCONSENSUS
                        # + router body lines + "." + trailing "650 OK".
                        body_lines: List[str] = [payload]
                        try:
                            while True:
                                block_raw = await asyncio.wait_for(
                                    self._reader.readline(),
                                    timeout=_EVENT_LOOP_KEEPALIVE,
                                )
                                if not block_raw:
                                    return
                                block_line = block_raw.decode(
                                    "utf-8", errors="replace"
                                ).rstrip("\r\n")
                                if block_line == ".":
                                    break
                                body_lines.append(block_line)
                        except (asyncio.TimeoutError, ConnectionError,
                                OSError, asyncio.IncompleteReadError,
                                RuntimeError):
                            return
                        # Dispatch the block. We pass the header payload so
                        # _dispatch_event's NEWCONSENSUS-vs-CONSENSUS=
                        # classification still works; body content is the
                        # signal of "consensus present" vs empty.
                        full_payload = body_lines[0]
                        if full_payload.startswith("NEWCONSENSUS") and len(body_lines) > 1:
                            # Force the "full body" branch in _dispatch_event
                            # by appending a non-empty marker.
                            full_payload = "NEWCONSENSUS CONSENSUS=present"
                        try:
                            self._dispatch_event(full_payload)
                        except Exception as e:
                            log.debug("TOR_CTRL_DISPATCH_ERR err=%s line=%s",
                                      e, line)
                    elif sep in (" ", "-"):
                        try:
                            self._dispatch_event(payload)
                        except Exception as e:
                            log.debug("TOR_CTRL_DISPATCH_ERR err=%s line=%s",
                                      e, line)
                    # else: bad separator — skip
                    continue

                # Synchronous command reply (2xx/4xx/5xx). Push onto the
                # pending reply buffer if there is one, else drop.
                if self._pending_reply is None:
                    continue
                self._pending_reply_lines.append(line)
                if sep == "+":
                    # Multi-line data block on a command reply — accumulate
                    # until "." line, stay in the same command.
                    try:
                        while True:
                            block_raw = await asyncio.wait_for(
                                self._reader.readline(),
                                timeout=_EVENT_LOOP_KEEPALIVE,
                            )
                            if not block_raw:
                                self._fail_pending_reply(
                                    ControlPortError(
                                        "control port closed during data block"
                                    )
                                )
                                return
                            block_line = block_raw.decode(
                                "utf-8", errors="replace"
                            ).rstrip("\r\n")
                            self._pending_reply_lines.append(block_line)
                            if block_line == ".":
                                break
                    except (asyncio.TimeoutError, ConnectionError,
                            OSError, asyncio.IncompleteReadError,
                            RuntimeError) as exc:
                        self._fail_pending_reply(exc)
                        return
                    continue
                if sep == " ":
                    # End of reply — resolve the pending future
                    if self._pending_reply is not None and not self._pending_reply.done():
                        self._pending_reply.set_result(
                            list(self._pending_reply_lines)
                        )
                    continue
                # sep == "-" means continuation, buffer and keep reading
        finally:
            self._event_loop_active = False
            self._fail_pending_reply(
                ControlPortError("control port event loop exited")
            )

    def _fail_pending_reply(self, exc: BaseException) -> None:
        """Wake any in-flight _send_command caller with an error."""
        fut = self._pending_reply
        if fut is not None and not fut.done():
            fut.set_exception(exc)

    def _dispatch_event(self, payload: str) -> None:
        """Map a Tor event payload line to a bus event.

        Opus contribution: DANGEROUS_SOCKS detection + consensus_lost state
        edge tracking for proper recovered transitions.
        Sonnet contribution: BUILD_TIME_ELAPSED parsing for slow circuits.
        """
        if not self._bus_emit:
            return

        if payload.startswith("NEWCONSENSUS"):
            body = payload[len("NEWCONSENSUS"):].strip()
            if body in ("", "CONSENSUS="):
                # Empty body → consensus lost
                if not self._consensus_lost:
                    self._consensus_lost = True
                    self._bus_emit("tor.consensus_lost")
            else:
                # Full body → consensus present
                if self._consensus_lost:
                    self._consensus_lost = False
                    self._bus_emit("tor.consensus_recovered")
            return

        if payload.startswith("STATUS_GENERAL"):
            rest = payload[len("STATUS_GENERAL"):].strip()
            if "DANGEROUS_SOCKS" in rest:
                if not self._consensus_lost:
                    self._consensus_lost = True
                    self._bus_emit("tor.consensus_lost")
                return
            if "CLOCK_SKEW" in rest:
                skew = self._parse_kv(rest, "SKEW") or "0"
                try:
                    skew_s = float(skew)
                except (TypeError, ValueError):
                    skew_s = 0.0
                self._bus_emit("tor.clock_skew_warning", skew_seconds=skew_s)
                return
            return

        if payload.startswith("CIRC"):
            parts = payload.split()
            if len(parts) < 3:
                return
            circ_id = parts[1]
            status = parts[2]
            if status == "FAILED":
                reason = self._parse_kv(payload, "REASON") or "unknown"
                self._bus_emit("tor.circuit_failed", circ_id=circ_id, reason=reason)
                return
            if status == "BUILT":
                # Sonnet's BUILD_TIME_ELAPSED parser — slow-circuit detection
                elapsed_ms = self._extract_elapsed_ms(parts)
                if elapsed_ms is not None and elapsed_ms > self._slow_circuit_ms:
                    self._bus_emit(
                        "tor.circuit_slow",
                        circ_id=circ_id,
                        elapsed_ms=elapsed_ms,
                    )
                return
            return

    @staticmethod
    def _parse_kv(s: str, key: str) -> Optional[str]:
        """Parse a single KEY=VALUE token from a space-separated payload."""
        for tok in s.split():
            if tok.startswith(key + "="):
                return tok[len(key) + 1:]
        return None

    @staticmethod
    def _extract_elapsed_ms(parts: List[str]) -> Optional[float]:
        """Extract BUILD_TIME_ELAPSED (seconds) from CIRC BUILT event parts.

        Tor reports the elapsed circuit build time in seconds. Convert to
        milliseconds for the slow_circuit_warning_ms threshold comparison.
        """
        for part in parts:
            if part.startswith("BUILD_TIME_ELAPSED="):
                try:
                    return float(part[len("BUILD_TIME_ELAPSED="):]) * 1000.0
                except ValueError:
                    return None
        return None

    async def disconnect(self) -> None:
        """Close the control port connection. Idempotent."""
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None
        self._authenticated = False
        self._protocol_info = {}

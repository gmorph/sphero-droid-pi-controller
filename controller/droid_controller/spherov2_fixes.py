"""Targeted fixes for spherov2 0.12.1 behaviour that breaks on real droids.

`install()` is called once when the droid module is imported. Each fix is a small, documented
replacement of one library function so that a future spherov2 upgrade can drop it.
"""

import logging
from collections.abc import Callable

from spherov2.controls import PacketDecodingException
from spherov2.controls.v2 import Packet

log = logging.getLogger(__name__)

START = Packet.Encoding.start
END = Packet.Encoding.end
REPORT_EVERY = 500


class _DecodeErrors:
    """Counts undecodable traffic and logs it sparingly; the flood itself is the news, not each packet."""

    def __init__(self) -> None:
        self.count = 0

    def note(self, reason: str) -> None:
        self.count += 1
        if self.count == 1:
            log.warning("Undecodable packet from the droid (%s); further ones are counted, not logged", reason)
        elif self.count % REPORT_EVERY == 0:
            log.warning("%d undecodable packets so far (latest: %s)", self.count, reason)


decode_errors = _DecodeErrors()


def resilient_collector_add(self: Packet.Collector, data: bytes) -> None:
    """Replacement for spherov2's Packet.Collector.add that survives a bad packet.

    The original raises on the first bad packet and so never appends the rest of that BLE chunk.
    Packets straddle 20-byte chunks, so the dropped tail holds the start of the next packet, the
    following chunk then begins mid-packet, and every packet after that fails too. One glitch
    turns into a permanent flood of "Unexpected start of packet" errors, each with a traceback.
    This version skips bytes until a start marker when out of sync and keeps going after a failure.
    """
    buffer: list[int] = self._Collector__data  # type: ignore[attr-defined]
    callback: Callable[[Packet], None] = self._Collector__callback  # type: ignore[attr-defined]
    for byte in data:
        if not buffer and byte != START:
            decode_errors.note(f"stray byte 0x{byte:02X} outside a packet")
            continue
        buffer.append(byte)
        if byte != END:
            continue
        packet, buffer = buffer, []
        self._Collector__data = buffer  # type: ignore[attr-defined]
        try:
            parsed = Packet.parse_response(packet)
        except PacketDecodingException as exc:
            decode_errors.note(str(exc))
            continue
        try:
            callback(parsed)
        except Exception:
            log.exception("A packet listener failed")


_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    Packet.Collector.add = resilient_collector_add  # type: ignore[method-assign]
    _installed = True

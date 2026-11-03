from spherov2.controls.v2 import Packet

from droid_controller import spherov2_fixes

GOOD = bytes.fromhex("8d081812ff0007fe58000002ffffffff00000011ab23643d16d8")  # a real R2-D2 collision packet


def collector(received: list) -> Packet.Collector:
    spherov2_fixes.install()
    return Packet.Collector(received.append)


def test_packet_split_across_ble_chunks_is_reassembled() -> None:
    received: list = []
    c = collector(received)
    c.add(GOOD[:20])
    c.add(GOOD[20:])
    assert len(received) == 1 and received[0].did == 24 and received[0].cid == 18


def test_bad_packet_does_not_lose_the_rest_of_the_chunk() -> None:
    received: list = []
    c = collector(received)
    corrupt = bytearray(GOOD)
    corrupt[-2] ^= 0xFF  # break the checksum
    before = spherov2_fixes.decode_errors.count
    c.add(bytes(corrupt) + GOOD[:20])  # the next packet starts in the same chunk
    c.add(GOOD[20:])
    assert len(received) == 1
    assert spherov2_fixes.decode_errors.count == before + 1


def test_stream_resyncs_after_a_missed_chunk() -> None:
    received: list = []
    c = collector(received)
    c.add(GOOD[20:])  # mid-packet garbage, as after a dropped notification
    c.add(GOOD)
    assert len(received) == 1


def test_listener_errors_do_not_break_the_stream() -> None:
    received: list = []

    def flaky(packet: Packet) -> None:
        received.append(packet)
        if len(received) == 1:
            raise RuntimeError("listener bug")

    spherov2_fixes.install()
    c = Packet.Collector(flaky)
    c.add(GOOD)
    c.add(GOOD)
    assert len(received) == 2

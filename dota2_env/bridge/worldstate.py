"""Game -> Python channel: length-prefixed CMsgBotWorldState protobufs over TCP."""

import contextlib
import logging
import socket
import time
from multiprocessing.queues import Queue
from struct import unpack

from google.protobuf.message import DecodeError

from dota2_env.bridge.constants import ACTIONABLE_GAME_STATES, DOTA_GAMERULES_STATE_POST_GAME
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState

logger = logging.getLogger('dota2_env')

HEADER_BYTES = 4
# Note (ruidu): a frame is about 45 KB in a 1v1, while 98% of the 4-byte windows inside a frame read as
# more than this, so a longer length prefix means the stream lost its framing (docs/VERSION_DIFF.md 1.5).
MAX_FRAME_BYTES = 4 * 1024 * 1024


def _recv_exact(sock, n_bytes):
    chunks = []
    while n_bytes > 0:
        chunk = sock.recv(n_bytes)
        if not chunk:
            raise ConnectionError('worldstate socket closed')
        chunks.append(chunk)
        n_bytes -= len(chunk)
    return b''.join(chunks)


def read_raw_world_state(sock: socket.socket) -> bytes:
    """One serialized CMsgBotWorldState payload (without the length prefix).

    Raises ConnectionError on a length prefix longer than any frame: those bytes came from inside a frame,
    and only a new connection starts again at a frame boundary.
    """
    n_bytes = unpack('<I', _recv_exact(sock, HEADER_BYTES))[0]
    if n_bytes > MAX_FRAME_BYTES:
        logger.warning(f'worldstate stream out of sync: a length prefix of {n_bytes} bytes')
        raise ConnectionError('worldstate stream out of sync')
    return _recv_exact(sock, n_bytes)


def parse_world_state(raw):
    world_state = CMsgBotWorldState()
    world_state.ParseFromString(raw)
    return world_state


def tree_events_only(world_state: CMsgBotWorldState) -> bytes:
    """The tree events of world_state alone, serialized; empty when it has none.

    Protobuf parses concatenated messages as one merged message, so these bytes put in front of a later
    frame hand the events on to it, ahead of that frame's own.
    """
    return CMsgBotWorldState(tree_events=world_state.tree_events).SerializeToString()


def top_level_fields(raw: bytes) -> tuple[list[tuple[int, bytes]], int]:
    """Each whole top-level field at the start of raw as (field number, encoded field), and where they end.

    Sub-messages are not looked into. The end is len(raw) unless a tag, varint or length runs past it or
    is one this proto never writes (field 0, a group, wire type 6 or 7): there the frame's framing broke.
    """
    fields, start = [], 0
    while start < len(raw):
        tag, end = read_varint(raw, start)
        wire_type = tag & 7
        if wire_type == 0:
            end = read_varint(raw, end)[1]
        elif wire_type == 2:
            length, end = read_varint(raw, end)
            end += length
        elif wire_type in (1, 5):
            end += 8 if wire_type == 1 else 4
        else:
            break
        if tag >> 3 == 0 or end > len(raw):
            break
        fields.append((tag >> 3, raw[start:end]))
        start = end
    return fields, start


def read_varint(raw: bytes, position: int) -> tuple[int, int]:
    """The base-128 varint at position and the position after it, past len(raw) when it does not end in time."""
    value = 0
    for i, byte in enumerate(raw[position : position + 10]):
        value |= (byte & 0x7F) << (7 * i)
        if byte < 0x80:
            return value, position + i + 1
    return value, len(raw) + 1


def read_world_state(sock):
    return parse_world_state(read_raw_world_state(sock))


def connect(port, host='127.0.0.1', timeout=None, retry_interval=1.0):
    """Block until Dota opens the worldstate port (it only listens once a game is being hosted)."""
    deadline = None if timeout is None else time.time() + timeout
    while True:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sock.connect_ex((host, port)) == 0:
            return sock
        sock.close()
        if deadline is not None and time.time() > deadline:
            raise TimeoutError(f'could not connect to worldstate port {port}')
        time.sleep(retry_interval)


def worldstate_listener(port: int, queue: Queue, only_actionable: bool = True) -> None:
    """Child-process target: keep the socket drained and put every frame into queue, oldest first.

    The queue carries serialized bytes (decode with parse_world_state), cheaper and safer to pickle than protobuf
    messages. Left out are the frames before the game can be played and any that does not decode (a client quirk,
    docs/VERSION_DIFF.md 1.5); tree events are deltas, so those of a frame left out ride along with the next one out.
    """
    sock = connect(port)
    carried = b''
    while True:
        try:
            raw = read_raw_world_state(sock)
        except (ConnectionError, OSError) as e:
            logger.debug(f'worldstate connection lost ({e}), reconnecting')
            sock.close()
            sock = connect(port, timeout=30, retry_interval=0.5)
            continue
        try:
            world_state = parse_world_state(raw)
        except DecodeError as e:
            fields, framing_end = top_level_fields(raw)
            logger.warning(
                f'dropped a world state that does not decode ({e}): {len(raw)} bytes, whole top-level fields up to '
                f'byte {framing_end} (the last one field {fields[-1][0] if fields else None}), '
                f'then {raw[framing_end : framing_end + 8].hex(" ") or "nothing"}'
            )
            for number, field in fields:
                if number == CMsgBotWorldState.TREE_EVENTS_FIELD_NUMBER:
                    with contextlib.suppress(DecodeError):
                        carried += tree_events_only(parse_world_state(field))
            continue
        # POST_GAME is forwarded too, so consumers can see the episode end.
        playing = world_state.game_state in ACTIONABLE_GAME_STATES and len(world_state.units) > 0
        if only_actionable and not playing and world_state.game_state != DOTA_GAMERULES_STATE_POST_GAME:
            carried += tree_events_only(world_state)
            continue
        queue.put(carried + raw)
        carried = b''

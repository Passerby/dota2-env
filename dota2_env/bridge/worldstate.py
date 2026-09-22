"""Game -> Python channel: length-prefixed CMsgBotWorldState protobufs over TCP."""

import logging
import socket
import time
from multiprocessing.queues import Queue
from struct import unpack

from dota2_env.bridge.constants import ACTIONABLE_GAME_STATES, DOTA_GAMERULES_STATE_POST_GAME
from dota2_env.bridge.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState

logger = logging.getLogger('dota2_env')

HEADER_BYTES = 4


def _recv_exact(sock, n_bytes):
    chunks = []
    while n_bytes > 0:
        chunk = sock.recv(n_bytes)
        if not chunk:
            raise ConnectionError('worldstate socket closed')
        chunks.append(chunk)
        n_bytes -= len(chunk)
    return b''.join(chunks)


def read_raw_world_state(sock):
    """One serialized CMsgBotWorldState payload (without the length prefix)."""
    n_bytes = unpack('<I', _recv_exact(sock, HEADER_BYTES))[0]
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


def worldstate_listener(port: int, queue: Queue, max_queue_size: int = 2, only_actionable: bool = True) -> None:
    """Child-process target: keep the socket drained and feed the newest states into queue.

    The queue carries serialized bytes (decode with parse_world_state), which is cheaper and
    safer to pickle across processes than protobuf message objects. Tree events are deltas, so
    those of a frame that is not forwarded ride along with the next one that is.
    """
    sock = connect(port)
    carried = b''
    while True:
        try:
            raw = read_raw_world_state(sock)
            world_state = parse_world_state(raw)
        except (ConnectionError, OSError) as e:
            logger.debug(f'worldstate connection lost ({e}), reconnecting')
            sock.close()
            sock = connect(port, timeout=30, retry_interval=0.5)
            continue
        # POST_GAME is forwarded too, so consumers can see the episode end.
        playing = world_state.game_state in ACTIONABLE_GAME_STATES and len(world_state.units) > 0
        if only_actionable and not playing and world_state.game_state != DOTA_GAMERULES_STATE_POST_GAME:
            carried += tree_events_only(world_state)
            continue
        # qsize() is not implemented on macOS.
        try:
            if queue.qsize() >= max_queue_size:
                carried += tree_events_only(world_state)
                continue
        except NotImplementedError:
            pass
        queue.put(carried + raw)
        carried = b''

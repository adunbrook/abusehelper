from __future__ import absolute_import

import os
import sys
import uuid
import struct
import shutil
import cPickle
import idiokit
import tempfile
import subprocess
import contextlib
from idiokit import socket, select
from . import events, rules, taskfarm, bot


@contextlib.contextmanager
def temporary_directory(*args, **keys):
    tmpdir = tempfile.mkdtemp(*args, **keys)
    try:
        yield tmpdir
    finally:
        shutil.rmtree(tmpdir)


@idiokit.stream
def recvall(sock, amount, timeout=None):
    data = []
    while amount > 0:
        piece = yield sock.recv(amount, timeout=timeout)
        if not piece:
            raise RuntimeError("could not recv all bytes")
        data.append(piece)
        amount -= len(piece)
    idiokit.stop("".join(data))


@idiokit.stream
def encode(sock):
    while True:
        msg = yield idiokit.next()
        msg_bytes = cPickle.dumps(msg, cPickle.HIGHEST_PROTOCOL)
        data = struct.pack("!I", len(msg_bytes)) + msg_bytes
        yield sock.sendall(data)


@idiokit.stream
def decode(sock):
    while True:
        length_bytes = yield recvall(sock, 4)
        length, = struct.unpack("!I", length_bytes)

        msg_bytes = yield recvall(sock, length)
        msg = cPickle.loads(msg_bytes)

        yield idiokit.send(msg)


@idiokit.stream
def distribute_encode(socks):
    writable = []

    while True:
        to_all, msg = yield idiokit.next()
        msg_bytes = cPickle.dumps(msg, cPickle.HIGHEST_PROTOCOL)
        data = struct.pack("!I", len(msg_bytes)) + msg_bytes

        if to_all:
            for sock in socks:
                yield sock.sendall(data)
            writable = []
        else:
            while not writable:
                _, writable, _ = yield select.select((), socks, ())
                writable = list(writable)
            yield writable.pop().sendall(data)


@idiokit.stream
def collect_decode(socks):
    readable = []

    while True:
        while not readable:
            readable, _, _ = yield select.select(socks, (), ())
            readable = list(readable)

        sock = readable.pop()

        length_bytes = yield recvall(sock, 4)
        length, = struct.unpack("!I", length_bytes)

        msg_bytes = yield recvall(sock, length)
        msg = cPickle.loads(msg_bytes)

        yield idiokit.send(msg)


def run():
    @idiokit.stream
    def main(socket_path, process_id, callable, args, keys):
        sock = socket.Socket(socket.AF_UNIX)
        try:
            yield sock.connect(socket_path)
            yield sock.sendall(process_id)
            yield decode(sock) | callable(*args, **keys) | encode(sock)
        finally:
            yield sock.close()

    socket_path, process_id, callable, args, keys = cPickle.load(sys.stdin)

    try:
        idiokit.main_loop(main(socket_path, process_id, callable, args, keys))
    except idiokit.Signal:
        pass


class RoomGraphBot(bot.ServiceBot):
    concurrency = bot.IntParam("""
        the number of worker processes used for rule matching
        (default: %default)
        """, default=1)

    def __init__(self, *args, **keys):
        bot.ServiceBot.__init__(self, *args, **keys)

        self._rooms = taskfarm.TaskFarm(self._handle_room)
        self._srcs = {}
        self._processes = {}
        self._socket_path = None
        self._ready = idiokit.Event()

    @idiokit.stream
    def _distribute(self):
        waiters = dict()

        while True:
            event, dsts = yield idiokit.next()
            for dst in dsts:
                dst_room = self._rooms.get(dst)

                if dst_room in waiters:
                    yield waiters.pop(dst_room)

                if dst is not None:
                    waiters[dst_room] = dst_room.send(event.to_elements())

    @idiokit.stream
    def _handle_room(self, room_name):
        room = yield self.xmpp.muc.join(room_name, self.bot_name)
        distributor = yield self._ready.fork()
        yield idiokit.pipe(
            room,
            idiokit.map(self._map, room_name),
            distributor.fork(),
            idiokit.Event()
        )

    def _map(self, elements, room_name):
        if room_name not in self._srcs:
            return

        for event in events.Event.from_elements(elements):
            yield False, ("event", (room_name, event))

    @idiokit.stream
    def session(self, _, src_room, dst_room, rule=None, **keys):
        rule = rules.Anything() if rule is None else rules.rule(rule)
        src_room = yield self.xmpp.muc.get_full_room_jid(src_room)
        dst_room = yield self.xmpp.muc.get_full_room_jid(dst_room)

        distributor = yield self._ready.fork()
        yield distributor.send(True, ("inc_rule", (src_room, rule, dst_room)))
        try:
            self._srcs[src_room] = self._srcs.get(src_room, 0) + 1
            try:
                yield self._rooms.inc(src_room) | self._rooms.inc(dst_room)
            finally:
                self._srcs[src_room] = self._srcs[src_room] - 1
                if self._srcs[src_room] <= 0:
                    del self._srcs[src_room]
        finally:
            distributor.send(True, ("dec_rule", (src_room, rule, dst_room)))

    def run(self):
        with temporary_directory() as tmpdir:
            processes = {}
            for _ in xrange(self.concurrency):
                while True:
                    process_id = uuid.uuid4().hex
                    if process_id not in processes:
                        break

                env = dict(os.environ)
                env["ABUSEHELPER_SUBPROCESS"] = ""
                process = subprocess.Popen(
                    [sys.executable, "-m", __loader__.fullname],
                    stdin=subprocess.PIPE,
                    close_fds=True,
                    env=env
                )
                processes[process_id] = process

            self._socket_path = os.path.join(tmpdir, "socket")
            self._processes = processes
            return bot.ServiceBot.run(self)

    @idiokit.stream
    def main(self, _):
        sock = socket.Socket(socket.AF_UNIX)
        try:
            yield sock.bind(self._socket_path)
            yield sock.listen(self.concurrency)

            for process_id, process in self._processes.iteritems():
                cPickle.dump([self._socket_path, process_id, roomgraph, [], {}], process.stdin)

            connections = []
            waiting = set(self._processes)
            while waiting:
                conn, addr = yield sock.accept()
                process_id = yield recvall(conn, 32, timeout=10.0)
                try:
                    waiting.remove(process_id)
                except KeyError:
                    raise RuntimeError("unknown process id")
                connections.append(conn)

            if self.concurrency == 1:
                self.log.info("Started 1 worker process")
            else:
                self.log.info("Started {0} worker processes".format(self.concurrency))

            self._ready.succeed(distribute_encode(connections))
            yield collect_decode(connections) | self._distribute()
        finally:
            yield sock.close()


@idiokit.stream
def roomgraph():
    srcs = {}

    while True:
        type_id, args = yield idiokit.next()
        if type_id == "event":
            src, event = args
            if src in srcs:
                dsts = set(srcs[src].classify(event))
                if dsts:
                    yield idiokit.send(event, dsts)
        elif type_id == "inc_rule":
            src, rule, dst = args
            if src not in srcs:
                srcs[src] = rules.Classifier()
            srcs[src].inc(rule, dst)
        elif type_id == "dec_rule":
            src, rule, dst = args
            if src in srcs:
                srcs[src].dec(rule, dst)
                if srcs[src].is_empty():
                    del srcs[src]
        else:
            raise RuntimeError("unknown type id {0!r}".format(type_id))


if __name__ == "__main__":
    if "ABUSEHELPER_SUBPROCESS" in os.environ:
        run()
    else:
        RoomGraphBot.from_command_line().execute()

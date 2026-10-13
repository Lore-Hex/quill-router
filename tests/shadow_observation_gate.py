"""One-connection, deadline-bounded parent-owned emulator forwarding gate."""
import select
import socket
import threading
import time


class CommitGate:
    def __init__(self, upstream, timeout=30, *, listener=None, connect=socket.create_connection):
        host, port = upstream.rsplit(":", 1)
        assert host in {"localhost", "127.0.0.1", "[::1]"}
        self.upstream = (host.strip("[]"), int(port))
        self.connect = connect
        self.listener = listener if listener is not None else socket.socket()
        if listener is None:
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(1)
        self.listener.settimeout(min(2, timeout))
        self.address = "127.0.0.1:"+str(self.listener.getsockname()[1])
        self.deadline = time.monotonic()+timeout
        self.armed = threading.Event()
        self.pending = threading.Event()
        self.finish = threading.Event()
        self.deliver = False
        self.buffer = bytearray()
        self.error = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def arm(self):
        self.armed.set()

    def close(self, *, deliver=False):
        self.deliver = deliver
        self.finish.set()
        self.thread.join(timeout=max(0, self.deadline-time.monotonic())+1)
        assert not self.thread.is_alive(), "gate thread deadline"
        self.listener.close()
        if self.error is not None:
            raise self.error

    def run(self):
        try:
            with self.listener:
                while not self.finish.is_set():
                    assert time.monotonic() < self.deadline, "gate accept deadline"
                    try:
                        client, _ = self.listener.accept()
                        break
                    except TimeoutError:
                        continue
                else:
                    return
            with client, self.connect(self.upstream, timeout=2) as server:
                client.settimeout(2)
                server.settimeout(2)
                incoming = bytearray()
                preface = True
                client_open = True
                while not self.finish.is_set():
                    assert time.monotonic() < self.deadline, "gate forwarding deadline"
                    ready, _, _ = select.select([server, *([client] if client_open else [])], [], [], .02)
                    for source in ready:
                        try:
                            data = source.recv(65536)
                        except ConnectionResetError:
                            if source is client and self.pending.is_set():
                                client_open = False
                                continue
                            raise
                        if source is server:
                            if not data:
                                return
                            if client_open:
                                try:
                                    client.sendall(data)
                                except OSError:
                                    client_open = False
                            continue
                        if not data:
                            client_open = False
                            continue
                        incoming.extend(data)
                        assert len(incoming)+len(self.buffer) <= 512*1024, "gate byte bound"
                        # Parse frame boundaries only, never protobuf or payload.
                        # A complete DATA END_STREAM proves the request left the
                        # client; HTTP/2 PING/WINDOW_UPDATE alone cannot trigger it.
                        if preface:
                            if len(incoming) < 24:
                                continue
                            server.sendall(incoming[:24])
                            del incoming[:24]
                            preface = False
                        while len(incoming) >= 9:
                            size = int.from_bytes(incoming[:3], "big")+9
                            if len(incoming) < size:
                                break
                            frame = bytes(incoming[:size])
                            del incoming[:size]
                            if not self.armed.is_set():
                                server.sendall(frame)
                            elif not self.pending.is_set():
                                self.buffer.extend(frame)
                                if frame[3] == 0 and frame[4] & 1:
                                    self.pending.set()
                            # Once frozen, do not append later client cancellation.
                if self.deliver:
                    assert self.pending.is_set() and self.buffer, "no pending request"
                    server.sendall(self.buffer)
                    # Closing after send permits either server landing or cancellation.
        except BaseException as error:
            self.error = error

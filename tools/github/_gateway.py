# tools/github/_gateway.py — the gateway: real git and GitHub calls inside the sandbox, with the token outside of it.
# (the _ in front: not a tool. github_open and github_close next to it start and stop it.)
#
# a token inside a sandbox can always be read: the agent is root in there. so it is never in there, not in a file, not
# in a variable, not hidden, not for a moment. instead:
#
#   sandbox:  git push / curl  ->  127.0.0.1:8484  a tiny listener, which knows nothing
#        |    every connection is carried as a stream through its own `podman exec -i`
#   your computer:  this file reads the request, throws away every login in it, adds the token, and sends it on
#        |    only ever to github.com and api.github.com: there is no way to name another host
#   GitHub
#
# two switches, both the user's, both checked anew for each request: /github on|off (clients/github_switch.py, off at
# every start), and the sandbox's network (/sandbox net on|off). either one off: every request is refused.
# what comes back goes through scrub, so even an answer with the token in it reaches the sandbox without it.
# anything can come out of a sandbox, also through this stream: everything from it is checked here, nothing is trusted.
# a web page in the browser sandbox could also reach 127.0.0.1:8484, so requests from a browser are refused.

import re
import base64
import queue
import struct
import tempfile
import threading
import subprocess
import http.client

from config import ENV_FILE
from functions.environment import read_key

PORT = 8484                                            # inside the sandbox
API_HOST, API_BASE = "api.github.com", ""              # /api/<path>  goes to https://api.github.com/<path>
GIT_HOST = "github.com"                                # /<owner>/<repo>.git/... goes to https://github.com/...
BIGGEST_FRAME = 1024 * 1024                            # bytes of one piece on the stream. the listener sends 64 KB
BIGGEST_HEAD = 64 * 1024                               # bytes of a request's headers
BIGGEST_BODY = 2 * 1024 * 1024 * 1024                  # bytes of a request's body
TIMEOUT = 300                                          # seconds without a sign of life from GitHub
PIECE = 65536
OPEN, DATA, CLOSE = 0, 1, 2                            # what one frame on the stream is
GIT_PATH = re.compile(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(info/refs|git-upload-pack|git-receive-pack|info/lfs/[^?]*)")
ODD_PATH = re.compile(r"[\s\\]|\.\.|%2e|%2f|%5c|%00", re.IGNORECASE)   # no way to climb out of the path
FORWARD = ("accept", "content-type", "content-encoding", "git-protocol", "user-agent", "x-github-api-version")   # only
                                                       # these go to GitHub. no Authorization, no Cookie, nothing else
BACK = ("content-type", "content-encoding", "location", "link", "etag", "retry-after")   # only these come back,
BACK_ALSO = ("x-ratelimit-", "x-github-")              # and these

# what runs in the sandbox: python's standard library, and no secret. it moves bytes between sockets and its stdin and
# stdout, in frames: connection number, kind, length. when the program ends its stdin ends, and so does this
LISTENER = r"""
import os, socket, struct, sys, threading
port = int(sys.argv[1])
out = sys.stdout.buffer
guard = threading.Lock()
open_sockets = {}
def send(number, kind, data=b""):
    with guard:
        out.write(struct.pack(">IBI", number, kind, len(data)) + data)
        out.flush()
def up(number, connection):
    try:
        while True:
            data = connection.recv(65536)
            if not data:
                break
            send(number, 1, data)
    except OSError:
        pass
    send(number, 2)
def down():
    stream = sys.stdin.buffer
    while True:
        head = stream.read(9)
        if len(head) < 9:
            os._exit(0)
        number, kind, length = struct.unpack(">IBI", head)
        data = stream.read(length) if length else b""
        connection = open_sockets.get(number)
        if connection is None:
            continue
        try:
            if kind == 1:
                connection.sendall(data)
            else:
                open_sockets.pop(number, None)
                connection.shutdown(socket.SHUT_RDWR)
                connection.close()
        except OSError:
            open_sockets.pop(number, None)
threading.Thread(target=down, daemon=True).start()
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    server.bind(("127.0.0.1", port))
    server.listen(32)
except OSError as e:
    sys.stderr.write(str(e))
    os._exit(1)
send(0, 0, b"ready")
number = 0
while True:
    connection, _ = server.accept()
    number += 1
    open_sockets[number] = connection
    send(number, 0)
    threading.Thread(target=up, args=(number, connection), daemon=True).start()
"""


def token():
    return read_key("GITHUB", ENV_FILE)


def secrets(secret):
    """the token in every shape it travels in"""
    return [secret, base64.b64encode(("x-access-token:" + secret).encode()).decode()]


def scrub(data, secret):
    for shape in secrets(secret):
        data = data.replace(shape.encode(), b"***")
    return data


def upstream(host):
    """the only place that makes a connection out. the host never comes from the sandbox"""
    return http.client.HTTPSConnection(host, timeout=TIMEOUT)


def route(path):
    """(host, path there, how to log in) for a path, or None. a path can't name a host: it is always one of these two"""
    if ODD_PATH.search(path):
        return None
    if path.startswith("/api/"):
        return API_HOST, API_BASE + path[len("/api"):], "Bearer"
    if GIT_PATH.fullmatch(path.split("?")[0]):
        return GIT_HOST, path, "Basic"
    return None


class Connection:
    """one request from the sandbox, on its own thread"""
    def __init__(self, gateway, number):
        self.gateway, self.number = gateway, number
        self.incoming = queue.Queue()
        self.buffer = b""

    def more(self):
        piece = self.incoming.get(timeout=TIMEOUT)
        if piece is None:
            raise EOFError
        self.buffer += piece

    def take(self, size):
        while len(self.buffer) < size:
            self.more()
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def line(self):
        while b"\r\n" not in self.buffer:
            if len(self.buffer) > BIGGEST_HEAD:
                raise ValueError("a line too long")
            self.more()
        data, self.buffer = self.buffer.split(b"\r\n", 1)
        return data

    def say(self, data):
        self.gateway.write(self.number, DATA, data)

    def refuse(self, code, text):
        body = ("github gateway: " + text + "\n").encode()
        self.say(("HTTP/1.1 " + str(code) + " Refused\r\nContent-Type: text/plain\r\nConnection: close\r\n"
                  "Content-Length: " + str(len(body)) + "\r\n\r\n").encode() + body)

    def read_head(self):
        while b"\r\n\r\n" not in self.buffer:
            if len(self.buffer) > BIGGEST_HEAD:
                raise ValueError("headers too long")
            self.more()
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        return method, path, headers

    def read_body(self, headers):
        """the body into a temporary file. git sends big pushes in chunks, GitHub wants to know the size first"""
        body = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        size = 0
        if headers.get("transfer-encoding", "").lower() == "chunked":
            while True:
                length = int(self.line().split(b";")[0], 16)
                if length == 0:
                    while self.line():                 # the trailers, if any
                        pass
                    break
                size += length
                if size > BIGGEST_BODY:
                    raise ValueError("too big")
                body.write(self.take(length))
                self.take(2)
        else:
            left = int(headers.get("content-length") or 0)
            if left > BIGGEST_BODY or left < 0:
                raise ValueError("too big")
            size = left
            while left:
                data = self.take(min(left, PIECE))
                left -= len(data)
                body.write(data)
        body.seek(0)
        return body, size

    def run(self):
        try:
            self.serve()
        except (ValueError, EOFError, queue.Empty, OSError, http.client.HTTPException) as e:
            try:
                self.refuse(502, type(e).__name__)
            except OSError:
                pass
        finally:
            self.gateway.finished(self.number)

    def serve(self):
        method, path, headers = self.read_head()
        sandbox = self.gateway.sandbox                 # the user's two switches: asked anew for each request
        if getattr(sandbox, "github_allowed", False) is not True:
            return self.refuse(503, "the user switched GitHub off. only the user can switch it on (/github on)")
        if not sandbox.network or sandbox.container != self.gateway.container:
            return self.refuse(503, "the sandbox's network is off. only the user can switch it on (/sandbox net on)")
        # a web page in a browser in this sandbox can reach this port too. it must not get to use the token
        if "origin" in headers or "sec-fetch-mode" in headers or "sec-fetch-site" in headers:
            return self.refuse(403, "not from a web page")
        if headers.get("host") not in ("127.0.0.1:" + str(PORT), "localhost:" + str(PORT)):
            return self.refuse(403, "wrong host")
        if method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
            return self.refuse(405, "that method")
        where = route(path)
        if not where:
            return self.refuse(403, "only /api/... and git paths, see the github skill")
        secret = token()
        if not secret:
            return self.refuse(503, "no GITHUB_API_KEY in .env on the user's computer")
        host, target, login = where
        body, size = self.read_body(headers)
        sent = {key: value for key, value in headers.items() if key in FORWARD}
        sent.setdefault("user-agent", "advanced-ai-harness")
        sent["Host"] = host
        sent["Authorization"] = ("Bearer " + secret if login == "Bearer"
                                 else "Basic " + secrets(secret)[1])     # set last: nothing from the sandbox stays
        if size or method in ("POST", "PUT", "PATCH"):
            sent["Content-Length"] = str(size)
        connection = upstream(host)
        try:
            connection.request(method, target, body=body if size else None, headers=sent)
            answer = connection.getresponse()          # a redirect is handed back as it is, never followed
            self.pass_back(answer, secret, method)
        finally:
            connection.close()
            body.close()

    def pass_back(self, answer, secret, method):
        head = ["HTTP/1.1 " + str(answer.status) + " " + re.sub(r"[^A-Za-z0-9 ]", "", answer.reason or "") ]
        for key, value in answer.getheaders():
            low = key.lower()
            if low in BACK or low.startswith(BACK_ALSO):
                head.append(key + ": " + value.replace("https://api.github.com/",
                                                       "http://127.0.0.1:" + str(PORT) + "/api/"))
        head += ["Connection: close", "", ""]
        self.say(scrub("\r\n".join(head).encode("latin-1", "replace"), secret))
        keep = max(len(shape) for shape in secrets(secret)) - 1   # a token cut in two by a piece is still found
        held = b""
        while method != "HEAD":
            data = answer.read(PIECE)
            if not data:
                break
            held = scrub(held + data, secret)
            if len(held) > keep:
                self.say(held[:-keep])
                held = held[-keep:]
        if held:
            self.say(scrub(held, secret))


class Gateway:
    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.container = container = sandbox.container
        command = sandbox.command
        self.guard = threading.Lock()
        self.open_connections = {}
        self.ready = threading.Event()
        self.process = subprocess.Popen([command, "exec", "-i", container, "python3", "-I", "-u", "-c", LISTENER,
                                         str(PORT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
        threading.Thread(target=self.read_frames, daemon=True).start()

    def alive(self):
        return self.process.poll() is None

    def start(self):
        """None if the listener runs in the sandbox, otherwise why not"""
        if self.ready.wait(15):
            return None
        self.close()
        said = self.process.stderr.read().decode("utf-8", "replace").strip()
        return "the listener did not start in the sandbox" + (": " + said[-300:] if said else "")

    def write(self, number, kind, data=b""):
        with self.guard:
            self.process.stdin.write(struct.pack(">IBI", number, kind, len(data)) + data)
            self.process.stdin.flush()

    def finished(self, number):
        with self.guard:
            self.open_connections.pop(number, None)
        try:
            self.write(number, CLOSE)
        except (OSError, ValueError):
            pass

    def read_frames(self):
        """everything that comes out of the sandbox. a frame that makes no sense ends the gateway, nothing else"""
        stream = self.process.stdout
        try:
            while True:
                head = stream.read(9)
                if len(head) < 9:
                    break
                number, kind, length = struct.unpack(">IBI", head)
                if length > BIGGEST_FRAME or kind not in (OPEN, DATA, CLOSE):
                    break
                data = stream.read(length) if length else b""
                if len(data) < length:
                    break
                if number == 0:
                    self.ready.set()
                elif kind == OPEN:
                    connection = Connection(self, number)
                    with self.guard:
                        self.open_connections[number] = connection
                    threading.Thread(target=connection.run, daemon=True).start()
                else:
                    with self.guard:
                        connection = self.open_connections.get(number)
                    if connection:
                        connection.incoming.put(data if kind == DATA else None)
        except (OSError, ValueError):
            pass
        self.close()

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        with self.guard:
            connections = list(self.open_connections.values())
        for connection in connections:
            connection.incoming.put(None)


running = {}                                           # container -> its Gateway
guard = threading.Lock()


def open_for(sandbox):
    """start the gateway for this sandbox, if it doesn't run yet. None if it runs, otherwise why not"""
    with guard:
        gateway = running.get(sandbox.container)
        if gateway and gateway.alive():
            return None
        gateway = Gateway(sandbox)
        problem = gateway.start()
        if problem:
            return problem
        running.clear()                                # one sandbox per instance: an older one is gone
        running[sandbox.container] = gateway
    return None


def close_for(sandbox):
    with guard:
        gateway = running.pop(sandbox.container, None)
    if gateway:
        gateway.close()
    return bool(gateway)

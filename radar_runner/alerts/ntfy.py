"""Publishing one alert to ntfy, with the standard library only.

The alert goes as JSON to the server's root URL (ntfy's "publish as JSON"):
topic, title, message, priority (1-5) and tags travel in the body, so no text
has to fit into an HTTP header. The token goes in `Authorization: Bearer`.

* HTTPS only, with the system's certificate store. Plain HTTP is refused
  except to this host itself (the tests' stand-in server), so the token never
  crosses the network in clear. A Pi whose clock has not been set yet fails
  certificate checks ("not yet valid"): the alert then waits like any other.
* http.client directly (what urllib uses underneath): no proxy from the
  environment and no redirects (a redirect would carry the token to another
  address), and the connection is ours, so it can be shut from outside.
* Each send runs in a thread of its own (`Send`). The caller never waits on
  the network: it looks at `done`, and `cancel()` shuts the socket, which ends
  a connect, a TLS handshake or a read at once. Only a DNS lookup cannot be
  ended (getaddrinfo); a cancelled send then finishes when the lookup gives
  up. A cancelled send is never reported as sent unless its whole answer had
  been read before the cancel.

The result says what to do with the alert: sent (delete it), retry (keep it,
back off; the link, the server or the token is the problem), or rejected (the
server refused this alert: 400, 413, 422).
"""
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import urllib.parse
from dataclasses import dataclass

SENT, RETRY, REJECTED, UNCONFIGURED = "sent", "retry", "rejected", "unconfigured"
REJECTING = (400, 413, 422)
USER_AGENT = "radar-runner-alerts"
MAX_REPLY = 4096
READ_TIMEOUT_S = 10.0       # each connect and read; the whole send is limited by the caller


@dataclass
class Result:
    kind: str
    status: int = None      # HTTP status, when there was one
    text: str = ""          # what happened, for the status file and the log; never the token
    retry_after_s: float = None


def server_problem(server):
    """"" if `server` is an address alerts may be sent to, else why not (without the address itself)."""
    if not server or server == "-":
        return "no ntfy server set (alerts.ntfy.server in /etc/radar-runner/secrets.yaml)"
    try:
        parts = urllib.parse.urlsplit(server)
        host = parts.hostname or ""
        parts.port         # raises on a bad port
    except ValueError:
        return "the ntfy server is not a valid URL"
    if parts.username or parts.password:
        return "the ntfy server URL must not contain a user name or password (use alerts.ntfy.token)"
    if parts.scheme == "https" and host:
        return ""
    if parts.scheme == "http" and host:
        try:
            if host == "localhost" or ipaddress.ip_address(host).is_loopback:
                return ""
        except ValueError:
            pass
        return "the ntfy server must use https (plain http only to this host itself)"
    return "the ntfy server must be an https:// URL"


def body_for(topic, alert):
    return json.dumps({"topic": topic, "title": alert["title"], "message": alert["message"],
                       "priority": int(alert["priority"]), "tags": list(alert.get("tags") or [])}).encode()


def _retry_after(headers):
    try:
        value = float(headers.get("Retry-After", ""))
        return value if value >= 0 else None
    except (TypeError, ValueError):
        return None


def _reply_text(raw):
    try:
        error = json.loads(raw).get("error")
        if isinstance(error, str):
            return error[:200]
    except (ValueError, AttributeError):
        pass
    return raw.decode("utf-8", "replace").strip()[:200]


class Send:
    """One alert on its way, in a daemon thread. `done` is set when `result` is there."""

    def __init__(self, server, topic, token, alert, read_timeout_s=READ_TIMEOUT_S):
        self.server, self.topic, self.token, self.alert = server, topic, token, alert
        self.read_timeout_s = read_timeout_s
        self.result = None
        self.done = threading.Event()
        self.cancelled = False
        self._complete = False      # the whole answer was read before any cancel
        self._conn = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name="alerts-send")

    def start(self):
        self._thread.start()
        return self

    def cancel(self):
        """End the send now, as far as that can be done (not a DNS lookup). A send whose answer was
        complete before the cancel keeps its result; any other ends as RETRY, never as SENT."""
        with self._lock:
            if self._complete:
                return
            self.cancelled = True
            conn = self._conn
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _run(self):
        try:
            self.result = self._send()
        except Exception as e:       # whatever happens, the caller gets a result
            self.result = Result(RETRY, text=f"{type(e).__name__}: {e}"[:300])
        finally:
            if self.cancelled:
                # A connection shut from outside can look like a short but complete answer.
                self.result = Result(RETRY, text="cancelled")
            if self.result is not None and self.token and self.token != "-" and self.token in self.result.text:
                self.result.text = self.result.text.replace(self.token, "[removed]")
            conn, self._conn = self._conn, None
            if conn is not None:
                conn.close()
            self.done.set()

    def _send(self):
        problem = server_problem(self.server)
        if problem:
            return Result(UNCONFIGURED, text=problem)
        if not self.topic or self.topic == "-":
            return Result(UNCONFIGURED, text="no ntfy topic set (alerts.ntfy.topic in /etc/radar-runner/secrets.yaml)")
        if self.token and any(not 32 < ord(c) < 127 for c in self.token):
            # It would end up in an error text; and it cannot go into a header anyway.
            return Result(UNCONFIGURED, text="the ntfy token has a space, a control or a non-ASCII character "
                                             "(alerts.ntfy.token in /etc/radar-runner/secrets.yaml)")
        try:
            body = body_for(self.topic, self.alert)
        except (TypeError, ValueError, KeyError) as e:
            return Result(REJECTED, text=f"cannot build the request: {type(e).__name__}")
        parts = urllib.parse.urlsplit(self.server)
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT, "Content-Length": str(len(body))}
        if self.token and self.token != "-":
            headers["Authorization"] = "Bearer " + self.token
        if parts.scheme == "https":
            conn = http.client.HTTPSConnection(parts.hostname, parts.port, timeout=self.read_timeout_s,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=self.read_timeout_s)
        with self._lock:
            if self.cancelled:
                return Result(RETRY, text="cancelled")
            self._conn = conn
        try:
            conn.connect()          # DNS lookup, TCP, TLS
            if self.cancelled:
                return Result(RETRY, text="cancelled")
            conn.request("POST", (parts.path or "").rstrip("/") + "/", body=body, headers=headers)
            response = conn.getresponse()
            status = response.status
            reply = response.read(MAX_REPLY)
            with self._lock:
                if self.cancelled:
                    return Result(RETRY, text="cancelled")
                self._complete = True
        except ssl.SSLCertVerificationError as e:
            return Result(RETRY, text=f"certificate not accepted: {e.verify_message} (is the clock set?)"[:300])
        except (socket.timeout, TimeoutError):
            return Result(RETRY, text=f"timed out ({self.read_timeout_s:g} s without data)")
        except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as e:
            return Result(RETRY, text=f"{type(e).__name__}: {getattr(e, 'strerror', None) or e}"[:300])
        if 200 <= status < 300:
            return Result(SENT, status, f"accepted ({status})")
        text = f"HTTP {status}" + (f": {_reply_text(reply)}" if reply else "")
        if status in REJECTING:
            return Result(REJECTED, status, text)
        if 300 <= status < 400:
            text = f"HTTP {status}: redirect refused (check alerts.ntfy.server)"
        elif status in (401, 403):
            text += " (check alerts.ntfy.token)"
        return Result(RETRY, status, text, _retry_after(response.headers) if status in (429, 503) else None)


def publish(server, topic, token, alert, timeout_s):
    """Send one alert and wait for it, for at most `timeout_s` in all. Never raises."""
    send = Send(server, topic, token, alert, min(timeout_s, READ_TIMEOUT_S)).start()
    if not send.done.wait(timeout_s):
        send.cancel()
        send.done.wait(1.0)
        if send.result is None or send.result.kind != SENT:
            return Result(RETRY, text=f"no complete answer within {timeout_s:g} s")
    return send.result

"""Publishing one alert to ntfy, with the standard library only.

The alert goes as JSON to the server's root URL (ntfy's "publish as JSON"):
topic, title, message, priority (1-5) and tags travel in the body, so no text
has to fit into an HTTP header. The token goes in `Authorization: Bearer`.

* HTTPS only, with the system's certificate store. Plain HTTP is refused
  except to this host itself (the tests' stand-in server), so the token never
  crosses the network in clear. A Pi whose clock has not been set yet fails
  certificate checks ("not yet valid"): the alert then waits like any other.
* No proxy from the environment, and no redirects: a redirect would carry the
  token to another address.
* The whole request is bounded by `timeout_s`, not only each read: a server
  that trickles bytes is cut off (SIGALRM, in the main thread; in another
  thread only the per-read timeout applies).

The result says what to do with the alert: sent (delete it), retry (keep it,
back off; the link, the server or the token is the problem), or rejected (the
server refuses this alert itself: 400, 413, 422).
"""
import contextlib
import http.client
import ipaddress
import json
import signal
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

SENT, RETRY, REJECTED, UNCONFIGURED = "sent", "retry", "rejected", "unconfigured"
REJECTING = (400, 413, 422)
USER_AGENT = "radar-runner-alerts"
MAX_REPLY = 4096


@dataclass
class Result:
    kind: str
    status: int = None      # HTTP status, when there was one
    text: str = ""          # what happened, for the status file and the log; never the token or topic
    retry_after_s: float = None


class _Deadline(Exception):
    pass


@contextlib.contextmanager
def deadline(seconds):
    """Raise _Deadline in the block after `seconds`, whatever it is waiting for (main thread only)."""
    if threading.current_thread() is not threading.main_thread() or seconds <= 0:
        yield
        return

    def expired(signum, frame):
        raise _Deadline()
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None         # urllib then raises HTTPError with the 3xx status


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                       urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def request_for(server, topic, token, alert):
    """The urllib request for one alert (a dict with title, message, priority, tags)."""
    body = json.dumps({"topic": topic, "title": alert["title"], "message": alert["message"],
                       "priority": int(alert["priority"]), "tags": list(alert.get("tags") or [])}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if token and token != "-":
        headers["Authorization"] = "Bearer " + token
    return urllib.request.Request(server.rstrip("/") + "/", data=body, headers=headers, method="POST")


def _retry_after(headers):
    try:
        value = float(headers.get("Retry-After", ""))
        return value if value >= 0 else None
    except (TypeError, ValueError):
        return None


def _reply_text(fp):
    try:
        raw = fp.read(MAX_REPLY)
    except Exception:
        return ""
    try:
        error = json.loads(raw).get("error")
        if isinstance(error, str):
            return error[:200]
    except (ValueError, AttributeError):
        pass
    return raw.decode("utf-8", "replace").strip()[:200]


def publish(server, topic, token, alert, timeout_s, opener=None):
    """Send one alert. Never raises."""
    problem = server_problem(server)
    if problem:
        return Result(UNCONFIGURED, text=problem)
    if not topic or topic == "-":
        return Result(UNCONFIGURED, text="no ntfy topic set (alerts.ntfy.topic in /etc/radar-runner/secrets.yaml)")
    try:
        req = request_for(server, topic, token, alert)
    except (TypeError, ValueError) as e:
        return Result(REJECTED, text=f"cannot build the request: {type(e).__name__}")
    response = None
    try:
        with deadline(timeout_s):
            response = (opener or _opener()).open(req, timeout=min(timeout_s, 10.0))
            status = response.status
            _reply_text(response)
        if 200 <= status < 300:
            return Result(SENT, status, f"accepted ({status})")
        return Result(RETRY, status, f"unexpected answer {status}")
    except urllib.error.HTTPError as e:
        text = f"HTTP {e.code}" + (f": {_reply_text(e)}" if e.fp else "")
        e.close()
        if e.code in REJECTING:
            return Result(REJECTED, e.code, text)
        if 300 <= e.code < 400:
            text = f"HTTP {e.code}: redirect refused (check alerts.ntfy.server)"
        elif e.code in (401, 403):
            text += " (check alerts.ntfy.token)"
        return Result(RETRY, e.code, text, _retry_after(e.headers) if e.code in (429, 503) else None)
    except _Deadline:
        return Result(RETRY, text=f"no complete answer within {timeout_s:g} s")
    except urllib.error.URLError as e:
        reason = e.reason
        text = f"{type(reason).__name__}: {getattr(reason, 'strerror', None) or reason}" \
            if isinstance(reason, BaseException) else str(reason)
        if isinstance(reason, ssl.SSLCertVerificationError):
            text += " (is the clock set?)"
        return Result(RETRY, text=text[:300])
    except (socket.timeout, TimeoutError):
        return Result(RETRY, text=f"timed out ({min(timeout_s, 10.0):g} s without data)")
    except (OSError, ssl.SSLError, ValueError, http.client.HTTPException) as e:
        return Result(RETRY, text=f"{type(e).__name__}: {e}"[:300])
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

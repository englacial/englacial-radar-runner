"""Sending one alert (radar_runner/alerts/ntfy.py) to a stand-in ntfy server on 127.0.0.1."""
import threading
import time

import pytest

from fake_ntfy import FakeNtfy, closed_port
from radar_runner.alerts import ntfy

ALERT = {"title": "unit: safety trip", "message": "amplifier temperature: 56 C ünïcode", "priority": 5,
         "tags": ["urgent"]}
TOKEN = "tk_testtokenabcdefghijklmnop12"


@pytest.fixture
def server():
    with FakeNtfy() as s:
        yield s


def send(server_url, timeout=5.0, alert=ALERT):
    return ntfy.publish(server_url, "radar-test-topic", TOKEN, alert, timeout)


def test_an_alert_is_posted_as_json_with_the_token_as_bearer(server):
    r = send(server.url)
    assert r.kind == ntfy.SENT and r.status == 200
    [got] = server.received
    assert got["path"] == "/"
    assert got["body"] == {"topic": "radar-test-topic", "title": ALERT["title"], "message": ALERT["message"],
                           "priority": 5, "tags": ["urgent"]}
    assert got["headers"]["authorization"] == "Bearer " + TOKEN
    assert got["headers"]["content-type"] == "application/json"
    # Nothing but the token is in a header; the topic and the text travel in the body only.
    headers = " ".join(got["headers"].values())
    assert "radar-test-topic" not in headers and "56 C" not in headers


def test_without_a_token_there_is_no_authorization_header(server):
    assert ntfy.publish(server.url, "t", "-", ALERT, 5.0).kind == ntfy.SENT
    assert "authorization" not in server.received[0]["headers"]


@pytest.mark.parametrize("status, kind", [(500, ntfy.RETRY), (502, ntfy.RETRY), (503, ntfy.RETRY),
                                          (429, ntfy.RETRY), (401, ntfy.RETRY), (403, ntfy.RETRY),
                                          (404, ntfy.RETRY), (408, ntfy.RETRY),
                                          (400, ntfy.REJECTED), (413, ntfy.REJECTED), (422, ntfy.REJECTED)])
def test_server_answers(server, status, kind):
    server.mode, server.status = "status", status
    r = send(server.url)
    assert r.kind == kind and r.status == status and f"HTTP {status}" in r.text
    assert "stand-in says" in r.text
    if status in (401, 403):
        assert "alerts.ntfy.token" in r.text
    assert TOKEN not in r.text and "radar-test-topic" not in r.text


def test_429_passes_on_retry_after(server):
    server.mode, server.status, server.retry_after = "status", 429, 120
    assert send(server.url).retry_after_s == 120.0


def test_a_refused_connection_is_a_retry():
    r = send(closed_port())
    assert r.kind == ntfy.RETRY and "Connection refused" in r.text


def test_a_server_that_never_answers_is_given_up_on_at_the_timeout(server):
    server.mode = "hang"
    t = time.monotonic()
    r = send(server.url, timeout=1.5)
    elapsed = time.monotonic() - t
    assert r.kind == ntfy.RETRY and 1.3 <= elapsed < 3.0, (r, elapsed)


def test_a_server_that_trickles_bytes_is_cut_off_at_the_total_timeout(server):
    # Each byte comes well within the per-read timeout; only the limit on the whole request ends it.
    server.mode = "slow"
    t = time.monotonic()
    r = send(server.url, timeout=2.0)
    elapsed = time.monotonic() - t
    assert r.kind == ntfy.RETRY and 1.8 <= elapsed < 4.0 and "within 2 s" in r.text, (r, elapsed)


def raw_server(answer):
    """A TCP server on 127.0.0.1 that runs `answer(conn)` for every connection; returns its URL."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(5)

    def serve():
        while True:
            try:
                conn, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=answer, args=(conn,), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{s.getsockname()[1]}", s


def test_an_error_answer_whose_body_trickles_is_cut_off_at_the_timeout():
    def trickle(conn):
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 4096\r\n\r\n")
        for _ in range(40):
            time.sleep(1.0)
            try:
                conn.sendall(b"x")
            except OSError:
                return
    url, sock = raw_server(trickle)
    t = time.monotonic()
    r = send(url, timeout=3.0)
    elapsed = time.monotonic() - t
    sock.close()
    assert r.kind == ntfy.RETRY and elapsed < 4.5, (r, elapsed)


def test_a_connection_cut_in_the_middle_of_the_headers_is_never_taken_as_sent():
    # Status 200 then the connection shut while the headers come: http.client would read it as a
    # complete answer. Cancelled sends are never "sent" (the alert would be deleted unsent).
    def half(conn):
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
        time.sleep(30)
    url, sock = raw_server(half)
    r = send(url, timeout=1.5)
    sock.close()
    assert r.kind == ntfy.RETRY


def test_a_send_is_cancelled_at_once(server):
    server.mode = "hang"
    s = ntfy.Send(server.url, "t", TOKEN, ALERT).start()
    time.sleep(0.5)
    t = time.monotonic()
    s.cancel()
    assert s.done.wait(2.0) and time.monotonic() - t < 1.0 and s.result.kind == ntfy.RETRY


def test_a_send_reading_an_answer_ended_by_the_close_is_cancelled_at_once():
    # http.client gives up its hold of the socket for such an answer: the cancel must still reach it.
    def drip(conn):
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n")
        for _ in range(100):
            time.sleep(0.2)
            try:
                conn.sendall(b"x")
            except OSError:
                return
    url, sock = raw_server(drip)
    s = ntfy.Send(url, "t", TOKEN, ALERT).start()
    time.sleep(1.0)
    t = time.monotonic()
    s.cancel()
    ended = s.done.wait(3.0)
    sock.close()
    assert ended and time.monotonic() - t < 1.5 and s.result.kind == ntfy.RETRY


def test_redirects_are_not_followed(server):
    # A redirect would carry the token to another address.
    server.mode = "redirect"
    r = send(server.url)
    assert r.kind == ntfy.RETRY and r.status == 302 and "redirect refused" in r.text
    assert server.gets == 0 and server.attempts == 1


def test_outside_the_main_thread_the_per_read_timeout_still_ends_a_hang(server):
    server.mode = "hang"
    out = {}
    thread = threading.Thread(target=lambda: out.update(r=send(server.url, timeout=1.0)))
    thread.start()
    thread.join(15)
    assert not thread.is_alive() and out["r"].kind == ntfy.RETRY


@pytest.mark.parametrize("url, ok", [
    ("https://ntfy.sh", True), ("https://ntfy.example.org/", True), ("https://ntfy.example.org:8443", True),
    ("http://127.0.0.1:8080", True), ("http://localhost:1", True), ("http://[::1]:80", True),
    ("http://ntfy.sh", False), ("http://10.0.0.1", False), ("ftp://ntfy.sh", False), ("ntfy.sh", False),
    ("-", False), ("", False), ("https://user:pw@ntfy.sh", False), ("https://ntfy.sh:99999", False),
])
def test_only_https_or_this_host(url, ok):
    problem = ntfy.server_problem(url)
    assert (problem == "") is ok, problem
    if not ok and "@" in url:
        assert "pw" not in problem


def test_an_unconfigured_server_or_topic_sends_nothing(server):
    assert ntfy.publish("-", "t", "-", ALERT, 1.0).kind == ntfy.UNCONFIGURED
    assert ntfy.publish("http://ntfy.sh", "t", "-", ALERT, 1.0).kind == ntfy.UNCONFIGURED
    assert ntfy.publish(server.url, "-", "-", ALERT, 1.0).kind == ntfy.UNCONFIGURED
    assert server.attempts == 0


@pytest.mark.parametrize("answer, sent", [
    (b"HTTP/1.1 200 OK\r\n", False),                                                   # status line, then closed
    (b"HTTP/1.1 200 OK\r\nContent-Type: applica", False),                              # cut in the headers
    (b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\n{\"id\": \"m1\"", False),          # short body
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\n{\"id\"\r\n", False),  # chunked, cut
    (b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n{\"id\": 12}", True),               # whole
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\n{\"a\"\r\n3\r\n: 1\r\n1\r\n}\r\n0\r\n\r\n", True),
    (b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n{\"id\": \"m1\"}", True),                # ended by the close
    (b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{\"id\": \"m1\"}", True),
    (b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n", False),      # no body: cannot tell from a cut
    (b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + b"x" * 100000, True),               # over-long: taken
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + b"10000\r\n" + b"x" * 65536 + b"\r\n"
     + b"10000\r\n" + b"x" * 65536 + b"\r\n0\r\n\r\n", True),
    (b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n" + b"x" * 100000, True),
    (b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n" + b"x" * 1000, False),           # over-long, cut short
])
def test_only_a_whole_answer_counts_as_sent(answer, sent):
    def reply(conn):
        import socket
        conn.recv(65536)
        conn.sendall(answer)
        conn.shutdown(socket.SHUT_WR)       # an orderly close: unread request bytes would make it a reset
        conn.settimeout(5.0)
        try:
            while conn.recv(65536):
                pass
        except OSError:
            pass
        conn.close()
    url, sock = raw_server(reply)
    r = send(url, timeout=3.0)
    sock.close()
    assert (r.kind == ntfy.SENT) is sent, r
    if not sent:
        assert r.kind == ntfy.RETRY

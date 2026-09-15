"""Opaque transfer, origin policy, protocol bounds and native worker lifetime."""

import hashlib
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

from siteops import _http_asset_worker as worker
from siteops import asset_transfer as transfer
from siteops.artifacts import ArtifactError
from siteops.cache_filesystem import check_private_node, make_private_directory
from siteops.workspace_source import MAX_SOURCE_ARTIFACT_BYTES, ArtifactIdentity

BODY = b"raise AssertionError('Downloaded content is data, not a local script.')\n"
IDENTITY = ArtifactIdentity("content.zip", len(BODY), hashlib.sha256(BODY).hexdigest())
ORIGIN = "https://source.example"
URL = ORIGIN + "/asset"


def request(**changes):
    return {"url": URL, "origins": [ORIGIN], "size": IDENTITY.size, "sha256": IDENTITY.sha256} | changes


def headers(*pairs):
    value = Message()
    for key, item in pairs:
        value[key] = item
    return value


class Response(io.BytesIO):
    def __init__(self, body=BODY, *, url=URL, status=200, fields=()):
        super().__init__(body)
        self.url = url
        self.status = status
        self.headers = headers(*fields)
        self.read_sizes = []

    def geturl(self):
        return self.url

    def read1(self, size):
        self.read_sizes.append(size)
        return super().read1(size)


@pytest.fixture
def private_parent(tmp_path):
    parent = tmp_path / "staging"
    make_private_directory(parent)
    return parent


@pytest.fixture
def opener(monkeypatch):
    result = Mock()
    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *handlers: result)
    return result


def download(parent, *, url=URL, origins=(ORIGIN,), **kwargs):
    return transfer.download_https_asset(
        url, IDENTITY, origins=origins, staging_parent=parent, **kwargs,
    )


@pytest.mark.parametrize("url", [
    "http://source.example/asset", "file:///asset", "https://user@source.example/asset",
    "https://source.example:0/asset", "https://source.example:/asset",
    "https://source.example:65536/asset", "https://source.example/asset#",
    "https://source.example\\other/asset", "https://source.example/has space",
    "https://source.example/\r\nheader", "https://source.example/\t",
    "https://source.example/\u00e9", "https://source.example.evil/asset",
    "https://source.example./asset", "https://source%2eexample/asset",
    "https://source.example:444/asset",
])
def test_invalid_url_stops_before_process_and_staging(private_parent, monkeypatch, url):
    run = Mock(side_effect=AssertionError("A rejected URL reached the worker."))
    monkeypatch.setattr(transfer, "_run_worker", run)
    with pytest.raises(transfer.AssetTransferError):
        with download(private_parent, url=url):
            pytest.fail("Rejected source was yielded.")
    run.assert_not_called()
    assert list(private_parent.iterdir()) == []


@pytest.mark.parametrize("changes", [
    {"size": True}, {"size": 0}, {"size": MAX_SOURCE_ARTIFACT_BYTES + 1},
    {"sha256": "A" * 64}, {"sha256": "0" * 63}, {"extra": 1},
    {"origins": []}, {"origins": [ORIGIN + "/path"]},
    {"origins": [ORIGIN + "?x=1"]}, {"origins": [ORIGIN, ORIGIN + ":443"]},
])
def test_worker_validates_its_input_independently(changes):
    with pytest.raises(worker.TransferFailure):
        worker.validate_request(request(**changes))


def test_port_and_escaped_path_controls():
    assert worker.validate_request(request(url=ORIGIN + ":443/a%20b?download=1")) == {
        ("source.example", 443),
    }
    assert worker.validate_request(request(
        url="https://[::1]:8443/a", origins=["https://[::1]:8443"],
    )) == {("::1", 8443)}
    assert MAX_SOURCE_ARTIFACT_BYTES == worker.MAX_ASSET_BYTES


@pytest.mark.parametrize("fields", [
    (), (("Content-Length", str(len(BODY))),),
    (("Content-Encoding", "Identity"),), (("Transfer-Encoding", "chunked"),),
])
def test_worker_streams_exact_opaque_bytes(opener, tmp_path, fields):
    response = Response(fields=fields)
    opener.open.return_value = response
    target = tmp_path / "asset.bin"
    assert worker.transfer(request(), str(target)) == {
        "status": "ok", "size": IDENTITY.size, "sha256": IDENTITY.sha256,
    }
    assert target.read_bytes() == BODY
    assert max(response.read_sizes) <= IDENTITY.size + 1
    assert response.closed
    call = opener.open.call_args
    assert call.kwargs == {"timeout": 15}
    assert call.args[0].header_items() == [
        ("Accept", "application/octet-stream"), ("Accept-encoding", "identity"),
        ("User-agent", "siteops-artifact-source"),
    ]
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("body", [BODY[:-1], BODY + b"x", b"x" * len(BODY)])
def test_worker_rejects_changed_bytes(opener, tmp_path, body):
    opener.open.return_value = Response(body)
    with pytest.raises(worker.TransferFailure, match="identity"):
        worker.transfer(request(), str(tmp_path / "asset"))


@pytest.mark.parametrize("fields", [
    (("Content-Length", "0"),), (("Content-Length", "9" * 5000),),
    (("Content-Length", str(len(BODY))), ("Content-Length", str(len(BODY)))),
    (("Content-Encoding", "gzip"),),
    (("Content-Encoding", "identity"), ("Content-Encoding", "gzip")),
    (("Transfer-Encoding", "gzip"),),
    (("Transfer-Encoding", "chunked"), ("Content-Length", str(len(BODY)))),
])
def test_response_guards_precede_body_reads(opener, tmp_path, fields):
    response = Response(fields=fields)
    response.read1 = Mock(side_effect=AssertionError("Invalid framing was consumed."))
    opener.open.return_value = response
    with pytest.raises(worker.TransferFailure):
        worker.transfer(request(), str(tmp_path / "asset"))
    response.read1.assert_not_called()
    assert response.closed
    assert not (tmp_path / "asset").exists()


@pytest.mark.parametrize("error,code", [
    (TimeoutError("private detail"), "timeout"),
    (ConnectionResetError("private detail"), "network"),
])
def test_read_failures_preserve_category_without_raw_values(opener, tmp_path, error, code):
    response = Response()
    response.read1 = Mock(side_effect=error)
    opener.open.return_value = response
    with pytest.raises(worker.TransferFailure) as caught:
        worker.transfer(request(), str(tmp_path / "asset"))
    assert caught.value.result == {"status": code}


def redirect(url, location):
    body = io.BytesIO(b"Untrusted redirect body")
    body.read = Mock(side_effect=AssertionError("Redirect bodies must not be drained."))
    error = urllib.error.HTTPError(url, 302, "private diagnostic", headers(("Location", location)), body)
    return error, body


def test_approved_redirect_closes_body_and_rebuilds_anonymous_headers(opener, tmp_path):
    other = "https://assets.example"
    error, body = redirect(URL, other + "/signed?opaque=value")
    response = Response(url=other + "/signed?opaque=value")
    opener.open.side_effect = [error, response]
    worker.transfer(request(origins=[ORIGIN, other]), str(tmp_path / "asset"))
    assert body.closed
    assert opener.open.call_count == 2
    for call in opener.open.call_args_list:
        assert {key.lower() for key, _ in call.args[0].header_items()} == {
            "accept", "accept-encoding", "user-agent",
        }


@pytest.mark.parametrize("location", [
    "http://source.example/asset", "https://unapproved.example/asset",
    "https://source.example:0/asset", "https://user@source.example/asset",
    "file:///private", "//unapproved.example/asset", "/line\nbreak", "/fragment#",
])
def test_disallowed_redirect_stops_before_following(opener, tmp_path, location):
    error, body = redirect(URL, location)
    opener.open.side_effect = error
    with pytest.raises(worker.TransferFailure):
        worker.transfer(request(), str(tmp_path / "asset"))
    assert opener.open.call_count == 1
    assert body.closed


def test_redirect_limit_and_relative_control(opener, tmp_path):
    observations = [redirect(URL, "/again") for _ in range(worker.MAX_REDIRECTS + 1)]
    opener.open.side_effect = [error for error, _ in observations]
    with pytest.raises(worker.TransferFailure, match="redirect"):
        worker.transfer(request(), str(tmp_path / "asset"))
    assert opener.open.call_count == worker.MAX_REDIRECTS + 1
    assert all(body.closed for _, body in observations)
    assert opener.open.call_args_list[-1].args[0].full_url == ORIGIN + "/again"


@pytest.mark.parametrize("status,fields,code", [
    (404, (), "http"), (429, (("Retry-After", "20"),), "rate-limit"),
    (403, (("X-RateLimit-Remaining", "0"),), "rate-limit"),
])
def test_http_failures_do_not_read_response_body(opener, tmp_path, status, fields, code):
    body = io.BytesIO(b"private response")
    body.read = Mock(side_effect=AssertionError("Error body read."))
    opener.open.side_effect = urllib.error.HTTPError(URL, status, "private", headers(*fields), body)
    with pytest.raises(worker.TransferFailure) as caught:
        worker.transfer(request(), str(tmp_path / "asset"))
    assert caught.value.result["status"] == code
    assert caught.value.result["httpStatus"] == status
    assert body.closed
    assert "private" not in json.dumps(caught.value.result)


def test_retry_after_handles_numeric_and_date_values(monkeypatch):
    monkeypatch.setattr(worker.time, "time", lambda: 0)
    assert worker._retry_after(headers(("Retry-After", "Thu, 01 Jan 1970 00:01:00 GMT"))) == 60
    assert worker._retry_after(headers(("Retry-After", "12"))) == 12
    assert worker._retry_after(headers(("Retry-After", "invalid"))) is None
    assert worker._retry_after(headers(("Retry-After", "1"), ("Retry-After", "2"))) is None


def test_worker_never_overwrites_existing_file(opener, tmp_path):
    target = tmp_path / "asset"
    target.write_bytes(b"operator file")
    opener.open.return_value = Response()
    with pytest.raises(worker.TransferFailure, match="io"):
        worker.transfer(request(), str(target))
    assert target.read_bytes() == b"operator file"


@pytest.mark.parametrize("raw", [
    b'{"url":1,"url":2}', b'{"size":NaN}', b"[]", b"\xff",
    b"[" * 2000, b"x" * (worker.MAX_REQUEST_BYTES + 1),
], ids=["duplicate", "nan", "array", "encoding", "nesting", "limit"])
def test_native_worker_rejects_invalid_protocol(private_parent, raw):
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(transfer._WORKER)],
        cwd=private_parent, input=raw, capture_output=True, timeout=10,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"status": "input"}
    assert result.stderr == b""
    assert list(private_parent.iterdir()) == []


@pytest.fixture
def https_source(monkeypatch):
    certificate = Path(__file__).parent / "fixtures" / "localhost-test.pem"
    responses = {"/asset": (200, [("Content-Length", str(len(BODY)))], BODY)}
    observed = []
    entered = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append((self.path, dict(self.headers)))
            if self.path == "/stall-headers":
                entered.set()
                release.wait(10)
                return
            status, fields, body = responses[self.path]
            self.send_response(status)
            for name, value in fields:
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(body)
                self.wfile.flush()
                if self.path == "/stall-body":
                    entered.set()
                    release.wait(10)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SSL_CERT_FILE", str(certificate))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    origin = f"https://127.0.0.1:{server.server_port}"
    try:
        yield origin, responses, observed, entered
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.fixture
def observed_processes(monkeypatch):
    native = subprocess.Popen
    processes = []
    calls = []

    def start(argv, **kwargs):
        calls.append((argv, kwargs))
        process = native(argv, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(transfer.subprocess, "Popen", start)
    yield processes, calls
    assert all(process.poll() is not None for process in processes)


def test_native_transfer_is_private_opaque_and_independent_of_workspace_imports(
    https_source, private_parent, observed_processes, monkeypatch, tmp_path,
):
    origin, _, observed, _ = https_source
    monkeypatch.setenv("GH_TOKEN", "test-source-token")
    monkeypatch.setenv("SSLKEYLOGFILE", str(tmp_path / "tls-keys"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    (tmp_path / "sitecustomize.py").write_text("raise AssertionError('workspace import')\n")
    sentinel = private_parent / "operator.txt"
    sentinel.write_bytes(b"preserve")
    with download(private_parent, url=origin + "/asset", origins=(origin,)) as asset:
        assert asset.name == "asset.bin"
        assert asset.read_bytes() == BODY
        check_private_node(asset.parent, directory=True)
        check_private_node(asset, directory=False)
        saved = asset
    assert not saved.parent.exists()
    assert list(private_parent.iterdir()) == [sentinel]
    assert not (tmp_path / "tls-keys").exists()
    argv, kwargs = observed_processes[1][0]
    assert argv == [sys.executable, "-I", "-S", str(transfer._WORKER)]
    assert kwargs["shell"] is False
    assert all(key.lower() not in {"gh_token", "pythonpath", "sslkeylogfile"} for key in kwargs["env"])
    assert len(observed) == 1
    assert "Authorization" not in observed[0][1]
    assert observed[0][1]["Accept-Encoding"] == "identity"


def test_native_redirect_and_chunked_delivery(https_source, private_parent):
    origin, responses, observed, _ = https_source
    responses["/start"] = (302, [("Location", "/chunked")], b"ignored")
    framed = f"{len(BODY):x}\r\n".encode() + BODY + b"\r\n0\r\n\r\n"
    responses["/chunked"] = (200, [("Transfer-Encoding", "chunked")], framed)
    with download(private_parent, url=origin + "/start", origins=(origin,)) as asset:
        assert asset.read_bytes() == BODY
    assert [path for path, _ in observed] == ["/start", "/chunked"]
    assert list(private_parent.iterdir()) == []


@pytest.mark.parametrize("path", ["/stall-headers", "/stall-body"])
def test_native_deadline_stops_worker_before_staging_cleanup(
    https_source, private_parent, observed_processes, path,
):
    origin, responses, _, entered = https_source
    responses["/stall-body"] = (200, [("Content-Length", str(len(BODY)))], BODY[:1])
    start = time.monotonic()
    with pytest.raises(transfer.AssetTransferError) as caught:
        with download(private_parent, url=origin + path, origins=(origin,), timeout=1.5):
            pytest.fail("A stalled transfer was yielded.")
    assert caught.value.code == "source.timeout"
    assert entered.is_set()
    assert time.monotonic() - start < 8
    assert observed_processes[0][0].poll() is not None
    assert list(private_parent.iterdir()) == []


def test_native_tls_verification_is_required(https_source, private_parent, monkeypatch, tmp_path):
    origin, _, _, _ = https_source
    empty = tmp_path / "empty-roots.pem"
    empty.write_text("")
    monkeypatch.setenv("SSL_CERT_FILE", str(empty))
    with pytest.raises(transfer.AssetTransferError, match="source could not"):
        with download(private_parent, url=origin + "/asset", origins=(origin,)):
            pytest.fail("Untrusted TLS was accepted.")
    assert list(private_parent.iterdir()) == []


@pytest.mark.parametrize("fields", [
    [("X-Large", "a" * 8192)],
    [(f"X-{number}", "value") for number in range(65)],
], ids=["line", "count"])
def test_native_headers_are_bounded(https_source, private_parent, fields):
    origin, responses, _, _ = https_source
    responses["/asset"] = (200, fields, BODY)
    with pytest.raises(transfer.AssetTransferError):
        with download(private_parent, url=origin + "/asset", origins=(origin,)):
            pytest.fail("Oversized headers were accepted.")
    assert list(private_parent.iterdir()) == []


def test_native_proxy_connection_has_absolute_deadline(private_parent, observed_processes, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(5)

        def accept():
            connection, _ = server.accept()
            with connection:
                entered.set()
                release.wait(8)

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{server.getsockname()[1]}")
        monkeypatch.setenv("https_proxy", os.environ["HTTPS_PROXY"])
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        try:
            with pytest.raises(transfer.AssetTransferError) as caught:
                with download(private_parent, timeout=1.5):
                    pytest.fail("Stalled proxy was accepted.")
            assert caught.value.code == "source.timeout"
            assert entered.is_set()
            assert list(private_parent.iterdir()) == []
        finally:
            release.set()
            thread.join(timeout=6)
            assert not thread.is_alive()


@pytest.mark.parametrize("body", [BODY[:-1], BODY + b"x", b"0" * len(BODY)])
def test_native_byte_identity_precedes_yield(https_source, private_parent, body):
    origin, responses, _, _ = https_source
    responses["/asset"] = (200, [], body)
    with pytest.raises(transfer.AssetTransferError) as caught:
        with download(private_parent, url=origin + "/asset", origins=(origin,)):
            pytest.fail("Changed bytes were yielded.")
    assert caught.value.code == "source.identity"
    assert list(private_parent.iterdir()) == []


def test_supervisor_rechecks_bytes_instead_of_trusting_worker_status(private_parent, monkeypatch):
    def run(operation, request_bytes, timeout):
        asset = operation / "asset.bin"
        asset.write_bytes(b"x" * len(BODY))
        asset.chmod(0o600)
        return 0, json.dumps({
            "status": "ok", "size": IDENTITY.size, "sha256": IDENTITY.sha256,
        }).encode()

    monkeypatch.setattr(transfer, "_run_worker", run)
    with pytest.raises(transfer.AssetTransferError) as caught:
        with download(private_parent):
            pytest.fail("A worker status authorized changed bytes.")
    assert caught.value.code == "source.identity"
    assert list(private_parent.iterdir()) == []


@pytest.mark.parametrize("raw", [
    b"{}", b"[]", b'{"status":"ok","status":"ok"}',
    b'{"status":"http","httpStatus":true}', b'{"status":"http","httpStatus":900}',
    b'{"status":"http","detail":"private"}', b'{"status":[]}',
])
def test_invalid_status_is_value_safe(raw):
    with pytest.raises(transfer.AssetTransferError) as caught:
        transfer._check_result(1, raw, IDENTITY)
    assert caught.value.code == "source.worker"
    assert "private" not in str(caught.value)


def test_numeric_retry_details_are_retained():
    with pytest.raises(transfer.AssetTransferError) as caught:
        transfer._check_result(1, json.dumps({
            "status": "rate-limit", "httpStatus": 429, "retryAfter": 30, "resetAt": 1234567890,
        }).encode(), IDENTITY)
    assert (caught.value.code, caught.value.http_status) == ("source.rate-limit", 429)
    assert (caught.value.retry_after, caught.value.reset_at) == (30, 1234567890)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_native_worker_output_is_bounded(private_parent, monkeypatch, tmp_path, observed_processes, stream):
    script = tmp_path / "verbose.py"
    script.write_text(
        f"import sys, time\nsys.{stream}.write('x' * 100000)\nsys.{stream}.flush()\ntime.sleep(10)\n",
    )
    monkeypatch.setattr(transfer, "_WORKER", script)
    with pytest.raises(transfer.AssetTransferError) as caught:
        with download(private_parent):
            pytest.fail("Excessive worker output was accepted.")
    assert caught.value.code == "source.worker"
    assert list(private_parent.iterdir()) == []


def test_unconfirmed_worker_exit_retains_only_its_operation(private_parent, monkeypatch, caplog):
    monkeypatch.setattr(transfer, "_run_worker", Mock(side_effect=transfer._WorkerStillRunning()))
    with pytest.raises(transfer.AssetTransferError):
        with download(private_parent):
            pytest.fail("A live worker result was accepted.")
    remaining = list(private_parent.iterdir())
    assert len(remaining) == 1
    assert remaining[0].name.startswith("transfer-")
    assert "worker exit could not be confirmed" in caplog.text


def test_caller_failure_keeps_its_meaning_and_cleans_staging(https_source, private_parent):
    origin, _, _, _ = https_source
    with pytest.raises(OSError, match="caller"):
        with download(private_parent, url=origin + "/asset", origins=(origin,)):
            raise OSError("caller")
    assert list(private_parent.iterdir()) == []


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), 301])
def test_invalid_deadline_creates_nothing(private_parent, timeout):
    with pytest.raises(transfer.AssetTransferError):
        with download(private_parent, timeout=timeout):
            pytest.fail("An invalid deadline was used.")
    assert list(private_parent.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_shared_staging_parent_is_not_repaired(private_parent):
    private_parent.chmod(0o755)
    with pytest.raises(ArtifactError):
        with download(private_parent):
            pytest.fail("Shared staging was accepted.")
    assert private_parent.stat().st_mode & 0o777 == 0o755
    assert list(private_parent.iterdir()) == []

"""Agent tests — no Shyni server, FFmpeg or GPU needed.

    python3 -m unittest discover -s . -p 'test_*.py'
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import shyni_transcode_agent as agent

JOB_ID = "0123456789abcdef0123456789abcdef"


class SpecTests(unittest.TestCase):
    def test_spec_keeps_known_fields_within_range(self):
        spec = agent.clean_spec({
            "mode": "video", "start_ms": -5, "max_height": 99999, "audio_channels": "6",
            "audio_title": "Kor\nean\x00 5.1", "hdr": "yes", "audio_compatible": True,
            "shell": "rm -rf /", "segment_format": "fmp4",
            "subtitle": {"source": "embedded", "stream_index": 3, "file_path": "/etc/passwd"},
        })
        self.assertEqual(spec["start_ms"], 0)
        self.assertIsNone(spec["max_height"])
        self.assertEqual(spec["audio_channels"], 6)
        self.assertEqual(spec["audio_title"], "Kor ean  5.1")
        self.assertFalse(spec["hdr"])
        self.assertTrue(spec["audio_compatible"])
        self.assertNotIn("shell", spec)
        self.assertEqual(spec["subtitle"], {
            "source": "embedded", "stream_index": 3, "lang_code": None, "title": None,
        })

    def test_unknown_modes_formats_and_subtitles_are_rejected(self):
        for raw in (
            {"mode": "shell"}, {"mode": "video", "segment_format": "webm"},
            {"mode": "music", "music_bitrate_kbps": 96},
            {"mode": "video", "subtitle": {"source": "file"}},
            {"mode": "video", "subtitle": {"source": "embedded"}}, None,
        ):
            with self.assertRaises(agent.AgentError):
                agent.clean_spec(raw)

    def test_source_must_be_public_http_or_the_server_itself(self):
        for url in ("file:///etc/passwd", "ftp://example.com/a", "http://127.0.0.1:8080/",
                    "http://192.168.1.1/admin", "http://169.254.169.254/latest", "http://[::1]/"):
            with self.assertRaises(agent.AgentError, msg=url):
                agent.clean_source({"kind": "url", "url": url}, "10.20.30.40")
        self.assertEqual(
            agent.clean_source({"kind": "url", "url": "http://10.20.30.40:8000/x",
                                "headers": {"User-Agent": "a", "Bad": "x\r\nInjected: 1"}}, "10.20.30.40"),
            {"kind": "url", "url": "http://10.20.30.40:8000/x", "headers": {"User-Agent": "a"}},
        )
        self.assertEqual(agent.clean_source({"kind": "server", "path": "/x"}, "h"), {"kind": "server"})
        with self.assertRaises(agent.AgentError):
            agent.clean_source({"kind": "path", "path": "/etc/passwd"}, "h")

    def test_commands_read_only_the_loopback_source_and_write_only_the_job_folder(self):
        source = "http://127.0.0.1:5/%s/nonce/media" % JOB_ID
        args = agent.build_hls_args(
            "/opt/ffmpeg", source, "video", out_dir="/work/job", max_height=480,
            video_encoder="h264_videotoolbox", start_ms=90500,
        )
        self.assertEqual(args[0], "/opt/ffmpeg")
        self.assertEqual(args[args.index("-i") + 1], source)
        self.assertEqual(args.count("-i"), 1)
        self.assertEqual(args[args.index("-ss") + 1], "90.500")
        self.assertIn("-allow_sw", args)
        self.assertTrue(args[-1].startswith("/work/job/"))
        self.assertIn("min(ih,480)", args[args.index("-vf") + 1])


class FakeApi:
    host = "127.0.0.1"

    def __init__(self):
        self.calls = []

    def request(self, method, path, *, body=None, headers=None, timeout=60.0):
        data = body.read() if hasattr(body, "read") else body
        self.calls.append((method, path.rsplit("/", 1)[-1], data))
        return 204, {}, b""

    def call(self, method, path, body=None, **kwargs):
        self.called = getattr(self, "called", []) + [(method, path, body)]
        return None

    def close(self):
        pass


class RejectingApi(FakeApi):
    def __init__(self, body):
        super().__init__()
        self.body = body

    def request(self, method, path, *, body=None, headers=None, timeout=60.0):
        super().request(method, path, body=body, headers=headers, timeout=timeout)
        return 413, {}, self.body


class UploadLimitTests(unittest.TestCase):
    def job(self, api, tmp):
        fake = SimpleNamespace(api=api, config=SimpleNamespace(tmp_dir=Path(tmp)))
        job = agent.Job(fake, {"id": JOB_ID, "kind": "hls", "spec": {"mode": "video"},
                               "source": {"kind": "server"}})
        job.dir.mkdir()
        (job.dir / "seg00000.ts").write_bytes(b"x" * 2000)
        return job

    def test_proxy_limit_is_explained_and_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = RejectingApi(b"<html><center><h1>413 Request Entity Too Large</h1></center>nginx</html>")
            job = self.job(api, tmp)
            with self.assertRaises(agent.UploadTooLarge) as error:
                job._upload("seg00000.ts")
            self.assertEqual(len(api.calls), 1)
            self.assertIn("client_max_body_size", str(error.exception))

    def test_server_refusal_keeps_the_server_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = self.job(RejectingApi(b'{"detail":"\ud30c\uc77c\uc774 \ub108\ubb34 \ud07d\ub2c8\ub2e4"}'), tmp)
            with self.assertRaises(agent.UploadTooLarge) as error:
                job._upload("seg00000.ts")
            self.assertIn("파일이 너무 큽니다", str(error.exception))
            self.assertNotIn("client_max_body_size", str(error.exception))


class UploadOrderTests(unittest.TestCase):
    def test_segments_go_up_before_the_playlist_that_lists_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = SimpleNamespace(api=FakeApi(), config=SimpleNamespace(tmp_dir=Path(tmp)))
            job = agent.Job(fake, {"id": JOB_ID, "kind": "hls", "spec": {"mode": "audio"},
                                   "source": {"kind": "server"}})
            job.dir.mkdir()
            (job.dir / "seg00000.ts").write_bytes(b"zero")
            (job.dir / "seg00001.ts").write_bytes(b"still being written")
            (job.dir / "index.m3u8").write_text("#EXTM3U\n#EXTINF:6.0,\nseg00000.ts\n")
            job._sync_outputs()
            self.assertEqual([(m, n) for m, n, _ in fake.api.calls],
                             [("PUT", "seg00000.ts"), ("PUT", "index.m3u8")])
            self.assertFalse((job.dir / "seg00000.ts").exists())     # uploaded pieces are removed
            self.assertTrue((job.dir / "seg00001.ts").exists())      # not listed yet: left alone

            job._sync_outputs()                                       # nothing changed
            self.assertEqual(len(fake.api.calls), 2)

            (job.dir / "index.m3u8").write_text(
                "#EXTM3U\n#EXTINF:6.0,\nseg00000.ts\n#EXTINF:6.0,\nseg00001.ts\n"
                "#EXTINF:1,\n../../etc/passwd\n#EXT-X-ENDLIST\n")
            job._sync_outputs()
            self.assertEqual([(m, n) for m, n, _ in fake.api.calls[2:]],
                             [("PUT", "seg00001.ts"), ("PUT", "index.m3u8")])
            self.assertEqual(fake.api.calls[2][2], b"still being written")

    def test_master_playlist_waits_for_the_media_playlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = SimpleNamespace(api=FakeApi(), config=SimpleNamespace(tmp_dir=Path(tmp)))
            job = agent.Job(fake, {"id": JOB_ID, "kind": "hls", "spec": {"mode": "video"},
                                   "source": {"kind": "server"}})
            job.dir.mkdir()
            (job.dir / "master.m3u8").write_text("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nindex.m3u8\n")
            job._sync_outputs()
            self.assertEqual(fake.api.calls, [])
            (job.dir / "init.mp4").write_bytes(b"init")
            (job.dir / "seg00000.m4s").write_bytes(b"m4s")
            (job.dir / "index.m3u8").write_text(
                '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:6.0,\nseg00000.m4s\n')
            job._sync_outputs()
            self.assertEqual([n for _, n, _ in fake.api.calls],
                             ["init.mp4", "seg00000.m4s", "index.m3u8", "master.m3u8"])

    def test_job_ids_and_kinds_are_validated(self):
        fake = SimpleNamespace(api=FakeApi(), config=SimpleNamespace(tmp_dir=Path("/tmp")))
        for command in (
            {"id": "../../x", "kind": "hls", "spec": {"mode": "audio"}, "source": {"kind": "server"}},
            {"id": JOB_ID, "kind": "exec", "spec": {"mode": "audio"}, "source": {"kind": "server"}},
            {"id": JOB_ID, "kind": "hls", "spec": {"mode": "audio"}},
        ):
            with self.assertRaises(agent.AgentError):
                agent.Job(fake, command)


class Upstream:
    """A cloud file server that needs a token, drops connections and supports Range."""

    def __init__(self, data):
        upstream = self
        self.data = data
        self.token = "old"
        self.seen = []
        self.agents = []
        self.drop_after = None

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                upstream.seen.append((self.headers.get("Authorization"), self.headers.get("Range")))
                upstream.agents.append(self.headers.get_all("User-Agent"))
                if self.headers.get("Authorization") != "Bearer " + upstream.token:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, end = 0, len(upstream.data) - 1
                ranged = self.headers.get("Range")
                if ranged:
                    first, _, last = ranged.split("=")[1].partition("-")
                    start = int(first)
                    end = int(last) if last else end
                body = upstream.data[start:end + 1]
                self.send_response(206 if ranged else 200)
                self.send_header("Content-Length", str(len(body)))
                if ranged:
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, len(upstream.data)))
                self.end_headers()
                if upstream.drop_after is not None:
                    cut, upstream.drop_after = upstream.drop_after, None
                    self.wfile.write(body[:cut])
                    self.wfile.flush()
                    self.connection.close()                 # the cloud drops the connection
                    return
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/file" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


class SourceProxyTests(unittest.TestCase):
    def setUp(self):
        self.upstream = Upstream(bytes(range(256)) * 4000)      # ~1 MB
        self.proxy = agent.SourceProxy(SimpleNamespace(base_url="http://127.0.0.1:1", token="runner"))
        self.job = SimpleNamespace(
            id=JOB_ID, nonce="n" * 32, stopped=threading.Event(), refreshed=0,
            source={"kind": "url", "url": self.upstream.url, "headers": {"Authorization": "Bearer old"}},
        )

        def refresh():
            self.job.refreshed += 1
            self.job.source = {"kind": "url", "url": self.upstream.url,
                               "headers": {"Authorization": "Bearer " + self.upstream.token}}

        self.job.refresh_source = refresh
        self.proxy.jobs[JOB_ID] = self.job
        agent.time.sleep, self._sleep = (lambda seconds: None), agent.time.sleep

    def tearDown(self):
        agent.time.sleep = self._sleep
        self.proxy.server.shutdown()
        self.upstream.server.shutdown()

    def fetch(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.port, timeout=10)
        conn.request("GET", urllib.parse.urlsplit(path).path, headers=headers or {})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, response.headers, body

    def test_range_requests_pass_through_with_the_job_credentials(self):
        status, headers, body = self.fetch(self.proxy.url_for(self.job, "media"), {"Range": "bytes=1000-1999"})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.upstream.data[1000:2000])
        self.assertEqual(headers["Content-Range"], "bytes 1000-1999/%d" % len(self.upstream.data))
        self.assertEqual(self.upstream.seen, [("Bearer old", "bytes=1000-1999")])

    def test_every_response_announces_that_the_connection_closes(self):
        """FFmpeg reuses a connection for its next seek unless told it will close; when the
        proxy closed silently, FFmpeg lost the index at the end of the file and read from byte 0."""
        _, headers, _ = self.fetch(self.proxy.url_for(self.job, "media"), {"Range": "bytes=0-99"})
        self.assertEqual(headers["Connection"], "close")
        status, headers, _ = self.fetch("/%s/%s/media" % (JOB_ID, "x" * 32))
        self.assertEqual((status, headers["Connection"]), (404, "close"))
        self.upstream.token = "gone"
        self.job.refresh_source = lambda: None
        status, headers, _ = self.fetch(self.proxy.url_for(self.job, "media"))
        self.assertEqual((status, headers["Connection"]), (401, "close"))

    def test_source_user_agent_replaces_the_agents_own(self):
        """A 115 signed URL only opens with the User-Agent it was issued for."""
        self.fetch(self.proxy.url_for(self.job, "media"), {"Range": "bytes=0-9"})
        self.assertEqual(self.upstream.agents[-1], [agent.USER_AGENT])
        self.job.source["headers"]["user-agent"] = "Mozilla/5.0 issued-for-this-url"
        self.fetch(self.proxy.url_for(self.job, "media"), {"Range": "bytes=0-9"})
        self.assertEqual(self.upstream.agents[-1], ["Mozilla/5.0 issued-for-this-url"])

    def test_only_the_job_nonce_opens_the_proxy(self):
        self.assertEqual(self.fetch("/%s/%s/media" % (JOB_ID, "x" * 32))[0], 404)
        self.assertEqual(self.fetch("/%s/%s/config.json" % (JOB_ID, self.job.nonce))[0], 404)
        self.assertEqual(self.fetch("/")[0], 404)
        self.assertEqual(self.upstream.seen, [])

    def test_expired_credentials_are_renewed_once(self):
        self.upstream.token = "new"
        status, _, body = self.fetch(self.proxy.url_for(self.job, "media"))
        self.assertEqual((status, body), (200, self.upstream.data))
        self.assertEqual(self.job.refreshed, 1)

    def test_a_dropped_connection_resumes_where_it_stopped(self):
        self.upstream.drop_after = 300000
        status, _, body = self.fetch(self.proxy.url_for(self.job, "media"))
        self.assertEqual(status, 200)
        self.assertEqual(body, self.upstream.data)
        self.assertEqual(self.upstream.seen[1][1], "bytes=300000-")


FAKE_RCLONE = '''#!%s
"""Stands in for rclone: `listremotes`, and `serve http <remote>:{id} --addr host:port`."""
import base64, http.server, os, sys, urllib.parse
if sys.argv[1] == "listremotes":
    print("gds:"); print("other:"); sys.exit(0)
assert sys.argv[1:3] == ["serve", "http"], sys.argv
target = sys.argv[3]
host, port = sys.argv[sys.argv.index("--addr") + 1].split(":")
root = os.environ["FAKE_RCLONE_ROOT"]
open(os.path.join(root, "starts.log"), "a").write(target + "\\n")
expected = "Basic " + base64.b64encode(("%%s:%%s" %% (os.environ["RCLONE_USER"], os.environ["RCLONE_PASS"])).encode()).decode()
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        path = os.path.join(root, "files", urllib.parse.unquote(self.path).lstrip("/"))
        if self.headers.get("Authorization") != expected:
            self.send_response(401); self.send_header("Content-Length", "0"); self.end_headers(); return
        if not os.path.isfile(path):
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        data = open(path, "rb").read()
        self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)
http.server.HTTPServer((host, int(port)), Handler).serve_forever()
''' % sys.executable


class RcloneSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.binary = root / "rclone"
        self.binary.write_text(FAKE_RCLONE)
        self.binary.chmod(0o755)
        (root / "files" / "영화" / "인터스텔라 (2014)").mkdir(parents=True)
        (root / "files" / "영화" / "인터스텔라 (2014)" / "Interstellar [x265].mkv").write_bytes(b"gds-bytes")
        os.environ["FAKE_RCLONE_ROOT"] = str(root)
        self.servers = agent.RcloneServers(str(self.binary), None, root / "logs")
        self.proxy = agent.SourceProxy(SimpleNamespace(base_url="http://127.0.0.1:1", token="runner"), self.servers)
        self.root = root

    def tearDown(self):
        self.servers.close()
        self.proxy.server.shutdown()
        os.environ.pop("FAKE_RCLONE_ROOT", None)
        self.tmp.cleanup()

    def job(self, path, **more):
        source = agent.clean_source(
            {"kind": "rclone", "remote": "gds", "folder_id": "1AbC_dEf-9", "path": path, **more}, "server", ["gds"])
        job = SimpleNamespace(id=JOB_ID, nonce="n" * 32, stopped=threading.Event(), source=source)
        self.proxy.jobs[JOB_ID] = job
        return job

    def fetch(self, job):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.port, timeout=40)
        conn.request("GET", "/%s/%s/media" % (job.id, job.nonce))
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, body

    def test_reads_the_same_path_through_the_runners_own_rclone(self):
        job = self.job("영화/인터스텔라 (2014)/Interstellar [x265].mkv")
        self.assertEqual(self.fetch(job), (200, b"gds-bytes"))
        self.assertEqual(self.fetch(job), (200, b"gds-bytes"))
        # One rclone per remote and root folder, kept running between jobs.
        self.assertEqual((self.root / "starts.log").read_text().splitlines(), ["gds:{1AbC_dEf-9}"])

    def test_a_missing_file_gets_one_fresh_listing(self):
        job = self.job("영화/new episode.mkv")
        self.assertEqual(self.fetch(job)[0], 404)
        self.assertEqual(len((self.root / "starts.log").read_text().splitlines()), 2)

    def test_a_remote_that_cannot_see_the_file_reads_through_the_server(self):
        """Same remote name, different contents (or the wrong rclone build): playback must not
        fail when the server can hand the file over."""
        server = Upstream(b"server-bytes")
        server.token = "runner"
        try:
            job = self.job("영화/not on this remote.mkv", server_fallback=True)
            job.api = SimpleNamespace(base_url=server.url.rsplit("/", 1)[0], token="runner")
            self.assertEqual(self.fetch(job), (200, b"server-bytes"))
            self.assertEqual(job.source, {"kind": "server"})
            # The next job on that remote skips rclone for a while instead of failing again first.
            starts = len((self.root / "starts.log").read_text().splitlines())
            later = self.job("영화/인터스텔라 (2014)/Interstellar [x265].mkv", server_fallback=True)
            later.api = job.api
            self.assertEqual(self.fetch(later), (200, b"server-bytes"))
            self.assertEqual(len((self.root / "starts.log").read_text().splitlines()), starts)
            # Meanwhile the remote is not advertised, so servers stop sending rclone sources.
            self.assertEqual(self.servers.unreadable_remotes(), {"gds"})
            # Without the server's offer the miss stays a miss.
            self.servers._unreadable.clear()
            self.assertEqual(self.servers.unreadable_remotes(), set())
            self.assertEqual(self.fetch(self.job("영화/not on this remote.mkv"))[0], 404)
        finally:
            server.server.shutdown()

    def test_only_remotes_the_owner_allowed_and_only_plain_paths(self):
        ok = {"kind": "rclone", "remote": "gds", "folder_id": "1AbC", "path": "a/b.mkv"}
        for change in ({"remote": "other"}, {"remote": "115_sftp"}, {"folder_id": "x} --config /etc"},
                       {"path": "../secret"}, {"path": "/abs"}, {"path": "a//b"}, {"path": "a/./b"}, {"path": ""}):
            with self.assertRaises(agent.AgentError, msg=change):
                agent.clean_source({**ok, **change}, "server", ["gds"])
        with self.assertRaises(agent.AgentError):
            agent.clean_source(ok, "server", [])            # a runner without rclone remotes

    def test_rclone_is_found_when_the_service_has_a_short_path(self):
        """launchd and systemd start the agent without /usr/local/bin on PATH."""
        saved = os.environ["PATH"], agent.RCLONE_SEARCH_DIRS
        os.environ["PATH"], agent.RCLONE_SEARCH_DIRS = "/nonexistent", ("/nowhere", str(self.root))
        try:
            self.assertEqual(agent.find_rclone(), str(self.binary))
            self.assertEqual(agent.find_rclone("rclone"), str(self.binary))
            self.assertIsNone(agent.find_rclone("rclone-missing"))
            self.assertIsNone(agent.find_rclone(str(self.root / "elsewhere" / "rclone")))
            config = SimpleNamespace(data={})
            agent.apply_rclone_options(config, SimpleNamespace(rclone=None, rclone_config=None, rclone_remote=["gds"]))
            self.assertEqual(config.data["rclone"], str(self.binary))      # kept as a full path
            self.assertEqual(agent.usable_rclone_remotes(config.data), ["gds"])
        finally:
            os.environ["PATH"], agent.RCLONE_SEARCH_DIRS = saved

    def test_install_checks_the_remote_exists_in_the_runners_config(self):
        config = SimpleNamespace(data={"rclone": str(self.binary)})
        args = SimpleNamespace(rclone=None, rclone_config=None, rclone_remote=["gds"])
        agent.apply_rclone_options(config, args)
        self.assertEqual(config.data["rclone_remotes"], ["gds"])
        self.assertEqual(agent.usable_rclone_remotes(config.data), ["gds"])
        for bad in (["nope"], ["-flag"], ["gds", "missing"]):
            with self.assertRaises(agent.AgentError):
                agent.apply_rclone_options(config, SimpleNamespace(rclone=None, rclone_config=None, rclone_remote=bad))
        agent.apply_rclone_options(config, SimpleNamespace(rclone=None, rclone_config=None, rclone_remote=[]))
        self.assertEqual(config.data["rclone_remotes"], [])


CAPS = {"video_encoder": "h264_videotoolbox", "hw_encoder": "h264_videotoolbox", "tonemap": True,
        "ffmpeg_version": "8.1"}


class FakeJob:
    def __init__(self, owner, command, link=None):
        if not agent._JOB_ID_RE.fullmatch(str(command.get("id") or "")):
            raise agent.AgentError("bad job id")
        self.id, self.link, self.stops = command["id"], link, 0

    def start(self):
        pass

    def stop(self):
        self.stops += 1


def job_id(number):
    return "%032x" % number


def start(number):
    return {"type": "start", "job": {"id": job_id(number)}}


class SharedSlotTests(unittest.TestCase):
    """One agent attached to several servers: the device counts its slots, the servers do not
    divide them up."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = agent.Config(Path(self.tmp.name))
        config.data.update({"ffmpeg": "/opt/ffmpeg", "slots": 2, "servers": [
            {"url": "https://one.example", "token": "a", "runner_id": 1, "name": "box"},
            {"url": "https://two.example", "token": "b", "runner_id": 7, "name": "box"},
        ]})
        self.saved = agent.detect_capabilities, agent.machine_info, agent.Job
        agent.detect_capabilities = lambda ffmpeg, preference="auto": dict(CAPS)
        agent.machine_info = lambda: {"os": "test"}
        agent.Job = FakeJob
        self.agent = agent.Agent(config)
        self.one, self.two = self.agent.links
        for link in self.agent.links:
            link.api = FakeApi()
            link.api.called = []

    def tearDown(self):
        self.agent.running = False
        agent.detect_capabilities, agent.machine_info, agent.Job = self.saved
        self.agent.proxy.server.shutdown()
        self.tmp.cleanup()

    def wait_for(self, check):
        deadline = agent.time.monotonic() + 5
        while not check() and agent.time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertTrue(check())

    def test_the_last_slot_goes_to_whichever_server_asks_first(self):
        self.agent.handle(self.one, start(1))
        self.agent.handle(self.two, start(2))
        self.agent.handle(self.one, start(3))                    # the device is full
        self.assertEqual(sorted(self.agent.jobs), [job_id(1), job_id(2)])
        # The refused server hears at once, so it can hand the job to another worker.
        refused = [call for call in self.one.api.called if call[1].endswith("/state")]
        self.assertEqual([(path, body["state"]) for _, path, body in refused],
                         [("/transcode-agent/jobs/%s/state" % job_id(3), "failed")])
        self.agent.job_finished(self.agent.jobs[job_id(2)])
        self.agent.handle(self.one, start(4))                    # a freed slot is anyone's
        self.assertEqual(sorted(self.agent.jobs), [job_id(1), job_id(4)])

    def test_each_server_is_told_what_the_others_are_using(self):
        self.agent.handle(self.one, start(1))
        mine, theirs = self.agent.report(self.one), self.agent.report(self.two)
        self.assertEqual((mine["jobs"], mine["busy_elsewhere"]), ([job_id(1)], 0))
        self.assertEqual((theirs["jobs"], theirs["busy_elsewhere"]), ([], 1))
        self.assertEqual((mine["recommended_slots"], theirs["recommended_slots"]), (2, 2))
        self.agent.job_finished(self.agent.jobs[job_id(1)])
        self.assertEqual(self.agent.report(self.two)["busy_elsewhere"], 0)

    def test_other_servers_hear_of_a_change_without_waiting_for_their_poll(self):
        self.agent.handle(self.one, start(1))
        self.wait_for(lambda: self.two.api.called)
        method, path, body = self.two.api.called[-1]
        self.assertEqual((path, body["wait"], body["busy_elsewhere"]), ("/transcode-agent/poll", 0, 1))
        self.assertEqual(self.one.api.called, [])                # the server that gave the job already knows
        self.agent.job_finished(self.agent.jobs[job_id(1)])
        self.wait_for(lambda: len(self.two.api.called) == 2)
        self.assertEqual(self.two.api.called[-1][2]["busy_elsewhere"], 0)

    def test_a_server_only_controls_its_own_jobs(self):
        self.agent.handle(self.one, start(1))
        job = self.agent.jobs[job_id(1)]
        self.agent.handle(self.two, {"type": "stop", "job_id": job_id(1)})
        self.assertEqual(job.stops, 0)
        self.agent.handle(self.one, {"type": "stop", "job_id": job_id(1)})
        self.assertEqual(job.stops, 1)

    def test_a_server_that_went_away_gives_its_slots_back(self):
        """A paused job uploads nothing, so it would never notice its server is gone."""
        class Down(FakeApi):
            def call(self, *args, **kwargs):
                raise agent.AgentError("server unreachable")

        self.agent.handle(self.one, start(1))
        self.agent.handle(self.two, start(2))
        mine, theirs = self.agent.jobs[job_id(1)], self.agent.jobs[job_id(2)]
        self.two.api = Down()
        self.agent._sleep = lambda seconds: setattr(self.agent, "running", False)
        self.agent._serve(self.two)                              # a short outage: nothing is touched
        self.assertEqual((mine.stops, theirs.stops), (0, 0))
        self.two.last_ok = agent.time.monotonic() - agent.SERVER_LOST_SEC - 1
        self.agent.running = True
        self.agent._serve(self.two)
        self.assertEqual((mine.stops, theirs.stops), (0, 1))

    def test_a_job_talks_to_the_server_that_gave_it(self):
        agent.Job = self.saved[2]
        job = agent.Job(self.agent, {"id": job_id(9), "kind": "hls", "spec": {"mode": "video"},
                                     "source": {"kind": "server"}}, self.two)
        self.assertIs(job.api, self.two.api)
        self.two.api.base_url, self.two.api.token = "https://two.example", "b"
        url, headers = self.agent.proxy._upstream(job, "media")
        self.assertEqual((url, headers["Authorization"]),
                         ("https://two.example/transcode-agent/jobs/%s/source" % job_id(9), "Bearer b"))


class RegisterApi:
    registered = []

    def __init__(self, base_url, token):
        self.base_url, self.token = base_url.rstrip("/"), token

    def call(self, method, path, body=None, **kwargs):
        RegisterApi.registered.append((self.base_url, body["recommended_slots"]))
        number = len(RegisterApi.registered)
        return {"runner_id": number, "token": "credential-%d" % number, "name": body["name"]}


class ServerListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.saved = agent.Api, agent.choose_ffmpeg, agent.machine_info, agent._restart_service
        agent.Api, RegisterApi.registered = RegisterApi, []
        agent.choose_ffmpeg = lambda config, explicit: ("/opt/ffmpeg", dict(CAPS))
        agent.machine_info = lambda: {"os": "test"}
        agent._restart_service = lambda: None
        self.stdout, self.stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = open(os.devnull, "w")

    def tearDown(self):
        sys.stdout.close()
        sys.stdout, sys.stderr = self.stdout, self.stderr
        agent.Api, agent.choose_ffmpeg, agent.machine_info, agent._restart_service = self.saved
        self.tmp.cleanup()

    def run_agent(self, *arguments):
        return agent.main([arguments[0], "--dir", str(self.home), *arguments[1:]])

    def servers(self):
        return [(entry["url"], entry["token"]) for entry in agent.Config(self.home).servers]

    def test_the_same_command_on_another_server_adds_it(self):
        install = ("install", "--token", "shr_once", "--no-service", "--name", "box")
        self.assertEqual(self.run_agent(*install, "--url", "https://one.example/", "--slots", "3"), 0)
        self.assertEqual(self.run_agent(*install, "--url", "https://two.example"), 0)
        self.assertEqual(self.servers(), [("https://one.example", "credential-1"),
                                          ("https://two.example", "credential-2")])
        # Both servers are told the device total, not a share of it.
        self.assertEqual(RegisterApi.registered, [("https://one.example", 3), ("https://two.example", 3)])
        self.assertEqual(self.run_agent(*install, "--url", "https://one.example"), 1)     # already there
        self.assertEqual(self.run_agent(*install, "--url", "https://one.example", "--force"), 0)
        self.assertEqual(self.servers(), [("https://one.example", "credential-3"),
                                          ("https://two.example", "credential-2")])

    def test_one_server_can_be_removed_but_not_the_last(self):
        install = ("install", "--token", "shr_once", "--no-service")
        self.run_agent(*install, "--url", "https://one.example")
        self.run_agent(*install, "--url", "https://two.example")
        self.assertEqual(self.run_agent("remove-server", "--url", "https://three.example"), 1)
        self.assertEqual(self.run_agent("remove-server", "--url", "https://one.example/"), 0)
        self.assertEqual(self.servers(), [("https://two.example", "credential-2")])
        self.assertEqual(self.run_agent("remove-server", "--url", "https://two.example"), 1)
        self.assertEqual(len(self.servers()), 1)

    def test_a_single_server_config_from_0_2_still_loads(self):
        (self.home / "config.json").write_text(json.dumps({
            "server_url": "https://one.example", "token": "old", "runner_id": 4, "name": "box",
            "ffmpeg": "/opt/ffmpeg", "tmp_dir": "/work",
        }))
        config = agent.Config(self.home)
        self.assertEqual(config.servers, [
            {"url": "https://one.example", "token": "old", "runner_id": 4, "name": "box"}])
        self.assertEqual((config.data["ffmpeg"], str(config.tmp_dir)), ("/opt/ffmpeg", "/work"))
        self.assertNotIn("token", config.data)


if __name__ == "__main__":
    unittest.main()

"""Agent tests — no Shyni server, FFmpeg or GPU needed.

    python3 -m unittest discover -s . -p 'test_*.py'
"""
import http.client
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

    def call(self, *args, **kwargs):
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
        self.drop_after = None

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                upstream.seen.append((self.headers.get("Authorization"), self.headers.get("Range")))
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


if __name__ == "__main__":
    unittest.main()

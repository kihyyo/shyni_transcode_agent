"""Agent tests — no Shyni server, FFmpeg or GPU needed.

    python3 -m unittest discover -s . -p 'test_*.py'
"""
import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import time
import threading
import unittest
from unittest import mock
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

    def test_output_codec_and_audio_bitrate_are_known_values_only(self):
        old_server = agent.clean_spec({"mode": "video"})          # a server from before 0.4
        self.assertEqual((old_server["output_codec"], old_server["audio_bitrate_kbps"]), ("h264", None))
        for codec in ("h264", "hevc", "av1"):
            self.assertEqual(agent.clean_spec({"mode": "video", "output_codec": codec})["output_codec"], codec)
        for value, expected in ((128, 128), ("96", 96), (32, 32), (640, 640), (31, None), (9999, None),
                                ("128k; rm -rf /", None), (True, None), (None, None)):
            spec = agent.clean_spec({"mode": "video", "audio_bitrate_kbps": value})
            self.assertEqual(spec["audio_bitrate_kbps"], expected, value)

    def test_unknown_modes_formats_and_subtitles_are_rejected(self):
        for raw in (
            {"mode": "shell"}, {"mode": "video", "segment_format": "webm"},
            {"mode": "music", "music_bitrate_kbps": 96},
            {"mode": "video", "output_codec": "vp9"}, {"mode": "video", "output_codec": "hevc_nvenc -y /etc/passwd"},
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

    def test_recommended_slots_follow_who_decodes(self):
        gpu_decode = {"hwaccel": "vaapi", "codecs": {"h264": [8], "hevc": [8, 10]}}
        for cores, gpu, cpu, software in ((2, 4, 2, 1), (4, 6, 4, 2), (6, 8, 6, 3), (16, 12, 12, 4)):
            with mock.patch.object(agent, "_performance_cores", return_value=cores), self.subTest(cores=cores):
                # 디코딩까지 GPU 면 코어 + 2, 디코딩을 CPU 가 하면(J5005 의 HEVC 10비트) 코어 수만큼.
                self.assertEqual(agent.recommended_slots({"hw_encoder": "h264_vaapi", "hw_decode": gpu_decode}), gpu)
                self.assertEqual(agent.recommended_slots({"hw_encoder": "h264_vaapi", "hw_decode": None}), cpu)
                self.assertEqual(agent.recommended_slots({"hw_encoder": "h264_nvenc", "hw_decode": {"codecs": {}}}), cpu)
                self.assertEqual(agent.recommended_slots({"hw_encoder": None, "video_encoder": "libx264"}), software)

    def test_systemd_unit_has_an_unquoted_working_directory(self):
        # A quoted WorkingDirectory is "not absolute" to systemd; the unit then never starts.
        text = agent.systemd_unit_text(
            ["/usr/bin/python3", "/root/shyni transcoder/agent/shyni_transcode_agent.py", "run", "--dir", "/root/50%"],
            Path("/root/50%"), system_wide=True)
        lines = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        self.assertEqual(lines["WorkingDirectory"], "/root/50%%")
        self.assertEqual(lines["ExecStart"], '"/usr/bin/python3" "/root/shyni transcoder/agent/shyni_transcode_agent.py" '
                                             '"run" "--dir" "/root/50%%"')
        self.assertEqual(lines["WantedBy"], "multi-user.target")
        self.assertEqual(dict(line.split("=", 1) for line in agent.systemd_unit_text(
            ["x"], Path("/home/u/a"), system_wide=False).splitlines() if "=" in line)["WantedBy"], "default.target")

    def test_update_lets_the_new_code_rewrite_the_linux_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "agent" / "shyni_transcode_agent.py"
            current.parent.mkdir()
            current.write_text("# old\n")
            home = Path(tmp).resolve()             # the installer writes the resolved folder
            unit = Path(tmp) / "shyni-transcode-agent.service"
            unit.write_text('WorkingDirectory="%s"\n' % home)

            def download(url, target):
                Path(target).write_text('import sys\nprint("shyni-transcode-agent 9.9.9")\n')

            calls = []
            with mock.patch.object(agent, "__file__", str(current)), \
                    mock.patch.object(agent, "_download", download), \
                    mock.patch.object(agent.platform, "system", return_value="Linux"), \
                    mock.patch.object(agent, "_systemd_unit_path", return_value=unit), \
                    mock.patch.object(agent.shutil, "which", return_value="/bin/systemctl"), \
                    mock.patch.object(agent.subprocess, "call", side_effect=lambda args: calls.append(args) or 0), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(agent.command_update(SimpleNamespace(dir=str(home))), 0)
            self.assertIn("9.9.9", current.read_text())
            # 옛 코드가 다시 시작만 하면 깨진 서비스 파일이 남는다 — 새 파일이 service install 로 다시 쓴다.
            self.assertEqual(calls, [[sys.executable, str(current.resolve()), "service", "install", "--dir", str(home)]])

    def test_video_output_is_always_8_bit_4_2_0(self):
        # 10비트 원본을 그대로 넘기면 h264_nvenc 는 시작하지 못하고(RTX 3060 Ti), libx264 는
        # High 10 을 낸다 — 출력 직전에 늘 8비트 4:2:0 으로 바꾼다.
        cases = (("libx264", "h264", "format=yuv420p"), ("h264_nvenc", "h264", "format=nv12"),
                 ("h264_qsv", "h264", "format=nv12"), ("h264_amf", "h264", "format=nv12"),
                 ("h264_videotoolbox", "h264", "format=nv12"), ("hevc_nvenc", "hevc", "format=nv12"),
                 ("av1_nvenc", "av1", "format=nv12"), ("h264_vaapi", "h264", "format=nv12,hwupload"))
        cuda = {"hwaccel": "cuda", "device": None, "pipeline": "download"}
        for encoder, codec, last in cases:
            for options in ({}, {"hdr": True}, {"hw_decode": cuda}, {"max_height": None}):
                args = agent.build_transcode_args(
                    "ffmpeg", "/media/a.mkv", "video", **{"max_height": 720, **options},
                    video_encoder=encoder, output_codec=codec, video_codec="hevc", vaapi_device="/dev/dri/renderD128")
                with self.subTest(encoder=encoder, options=options):
                    self.assertTrue(args[args.index("-vf") + 1].endswith(last))


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
            job = self.job(RejectingApi(rb'{"detail":"\ud30c\uc77c\uc774 \ub108\ubb34 \ud07d\ub2c8\ub2e4"}'), tmp)
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
                    if first:
                        start = int(first)
                        end = int(last) if last else end
                    else:                                   # suffix range: the last N bytes
                        start = max(0, len(upstream.data) - int(last))
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


class PrefetchTests(unittest.TestCase):
    """The head and tail of a source are fetched in parallel when a job starts and shared with
    later jobs on the same file: FFmpeg's open/probe/seek made six sequential 1-2 s requests."""

    def setUp(self):
        self.upstream = Upstream(bytes(range(256)) * 4000)      # ~1 MB
        self.upstream.token = "t"
        self.api = SimpleNamespace(base_url="http://server", token="runner", host="127.0.0.1")
        self.proxy = agent.SourceProxy(self.api)
        self._sizes = agent.SOURCE_HEAD_BYTES, agent.SOURCE_TAIL_BYTES
        agent.SOURCE_HEAD_BYTES, agent.SOURCE_TAIL_BYTES = 4096, 8192
        agent.time.sleep, self._sleep = (lambda seconds: None), agent.time.sleep

    def tearDown(self):
        agent.SOURCE_HEAD_BYTES, agent.SOURCE_TAIL_BYTES = self._sizes
        agent.time.sleep = self._sleep
        self.proxy.server.shutdown()
        self.upstream.server.shutdown()

    def job(self, job_id=JOB_ID, part_id=7):
        job = SimpleNamespace(
            id=job_id, nonce="n" * 32, stopped=threading.Event(), part_id=part_id, cache=None, api=self.api,
            source={"kind": "url", "url": self.upstream.url, "headers": {"Authorization": "Bearer t"}},
        )
        self.proxy.jobs[job_id] = job
        return job

    def fetch(self, job, ranged):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.port, timeout=10)
        conn.request("GET", urllib.parse.urlsplit(self.proxy.url_for(job, "media")).path,
                     headers={"Range": ranged})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, response.headers, body

    def wait_prefetched(self, job):
        self.assertTrue(job.cache.idle(5))

    def test_head_and_tail_are_served_from_the_prefetch(self):
        data, size = self.upstream.data, len(self.upstream.data)
        job = self.job()
        self.proxy.prefetch(job)
        self.wait_prefetched(job)
        self.assertEqual(sorted(r for _, r in self.upstream.seen), ["bytes=-8192", "bytes=0-4095"])
        status, headers, body = self.fetch(job, "bytes=100-999")
        self.assertEqual((status, body), (206, data[100:1000]))
        self.assertEqual(headers["Content-Range"], "bytes 100-999/%d" % size)
        status, headers, body = self.fetch(job, "bytes=%d-" % (size - 5000))
        self.assertEqual((status, body), (206, data[size - 5000:]))
        self.assertEqual(headers["Connection"], "close")
        self.assertEqual(len(self.upstream.seen), 2)              # nothing more from the cloud

    def test_reading_past_the_head_continues_from_the_source(self):
        job = self.job()
        self.proxy.prefetch(job)
        self.wait_prefetched(job)
        status, headers, body = self.fetch(job, "bytes=100-")
        self.assertEqual((status, body), (206, self.upstream.data[100:]))
        self.assertEqual(headers["Content-Length"], str(len(self.upstream.data) - 100))
        self.assertEqual(self.upstream.seen[-1][1], "bytes=4096-")
        # The middle of the file is not cached.
        status, _, body = self.fetch(job, "bytes=500000-500099")
        self.assertEqual(body, self.upstream.data[500000:500100])
        self.assertEqual(self.upstream.seen[-1][1], "bytes=500000-500099")

    def test_a_later_job_on_the_same_file_reuses_it(self):
        first = self.job()
        self.proxy.prefetch(first)
        self.wait_prefetched(first)
        second = self.job("b" * 32, part_id=7)
        self.proxy.prefetch(second)
        self.assertIs(second.cache, first.cache)
        self.assertEqual(self.fetch(second, "bytes=0-99")[2], self.upstream.data[:100])
        self.assertEqual(len(self.upstream.seen), 2)
        other = self.job("c" * 32, part_id=8)                       # another file
        self.proxy.prefetch(other)
        self.assertIsNot(other.cache, first.cache)

    def test_a_failed_prefetch_leaves_reading_to_the_source(self):
        self.upstream.token = "other"                                # the prefetch is refused
        job = self.job()
        self.proxy.prefetch(job)
        self.wait_prefetched(job)
        self.assertTrue(job.cache.failed)
        self.upstream.token = "t"
        status, _, body = self.fetch(job, "bytes=0-99")
        self.assertEqual((status, body), (206, self.upstream.data[:100]))
        self.proxy.prefetch(job)                                      # a new job tries again
        self.wait_prefetched(job)
        self.assertFalse(job.cache.failed)

    def test_a_warm_command_prefetches_before_any_job(self):
        link = SimpleNamespace(api=self.api)
        self.proxy.warm(link, {"part_id": 7, "source": {
            "kind": "url", "url": self.upstream.url, "headers": {"Authorization": "Bearer t"}}})
        job = self.job()
        self.proxy.prefetch(job)
        self.wait_prefetched(job)
        self.assertEqual(self.fetch(job, "bytes=0-99")[2], self.upstream.data[:100])
        self.assertEqual(len(self.upstream.seen), 2)                  # only the warm command's two
        self.proxy.warm(link, {"part_id": 9, "source": {"kind": "server"}})
        self.assertEqual(len(self.upstream.seen), 2)


FAKE_RCLONE = '''#!%s
"""Stands in for rclone: `listremotes`, and `rcd --rc-serve --rc-addr host:port` serving
`/[<remote>:{id}]/<path>` the way rclone's remote control does."""
import base64, http.server, os, re, sys, urllib.parse
if sys.argv[1] == "listremotes":
    print("gds:"); print("other:"); sys.exit(0)
assert sys.argv[1:3] == ["rcd", "--rc-serve"], sys.argv
host, port = sys.argv[sys.argv.index("--rc-addr") + 1].split(":")
root = os.environ["FAKE_RCLONE_ROOT"]
open(os.path.join(root, "starts.log"), "a").write(" ".join(sys.argv[1:]) + "\\n")
expected = "Basic " + base64.b64encode(("%%s:%%s" %% (os.environ["RCLONE_RC_USER"], os.environ["RCLONE_RC_PASS"])).encode()).decode()
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        match = re.match(r"^/\\[(.*?)\\](.*)$", urllib.parse.unquote(self.path))
        if self.headers.get("Authorization") != expected or not match:
            self.send_response(401); self.send_header("Content-Length", "0"); self.end_headers(); return
        open(os.path.join(root, "remotes.log"), "a").write(match.group(1) + "\\n")
        path = os.path.join(root, "files", match.group(2).lstrip("/"))
        if not os.path.isfile(path):
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        data = open(path, "rb").read()
        ranged = re.match(r"bytes=(\\d*)-(\\d*)$", self.headers.get("Range") or "")
        if not ranged:
            self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data); return
        open(os.path.join(root, "ranges.log"), "a").write(self.headers["Range"] + "\\n")
        first, last = ranged.groups()
        start = int(first) if first else max(0, len(data) - int(last))
        stop = int(last) + 1 if first and last else len(data)
        body = data[start:stop]
        slow = os.path.join(root, "slow")
        if self.headers["Range"] != "bytes=0-0" and os.path.exists(slow):
            import time; time.sleep(float(open(slow).read()))     # Google Drive took 20 s once
        self.send_response(206); self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Range", "bytes %%d-%%d/%%d" %% (start, start + len(body) - 1, len(data)))
        self.end_headers()
        cut = os.path.join(root, "cut_after")
        if start > 0 and os.path.exists(cut):          # the connection drops once, part way
            count = int(open(cut).read()); os.remove(cut)
            self.wfile.write(body[:count]); self.wfile.flush(); self.connection.close(); return
        self.wfile.write(body)
http.server.ThreadingHTTPServer((host, int(port)), Handler).serve_forever()
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
        # One rclone per remote and root folder, kept running between jobs; the root folder is
        # part of each request's address.
        self.assertEqual(len((self.root / "starts.log").read_text().splitlines()), 1)
        self.assertEqual(set((self.root / "remotes.log").read_text().splitlines()), {"gds:{1AbC_dEf-9}"})
        # The password reaches rclone through its environment, never its command line.
        self.assertNotIn("pass", (self.root / "starts.log").read_text())

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
            # Meanwhile the runner's rclone looked for the file (once more after a restart) and
            # gave up on it.
            for _ in range(100):
                if job.source == {"kind": "server"}:
                    break
                time.sleep(0.05)
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

    def test_install_command_names_the_servers_remotes_and_keeps_only_those_this_device_has(self):
        """The admin screen's install command carries every rclone remote of that server (0.6.1)."""
        def install(data, names, **options):
            config = SimpleNamespace(data=data)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                agent.apply_rclone_options(config, SimpleNamespace(
                    rclone=options.get("rclone"), rclone_config=None, rclone_remote=names), installing=True)
            return config.data, out.getvalue()

        data, out = install({"rclone": str(self.binary)}, ["gds", "115open"])
        self.assertEqual(data["rclone_remotes"], ["gds"])
        self.assertIn("115open", out)
        # A second server adds its remotes to the list; the first server's stay.
        data, _ = install({"rclone": str(self.binary), "rclone_remotes": ["gds"]}, ["other", "gds"])
        self.assertEqual(data["rclone_remotes"], ["gds", "other"])
        data, _ = install({"rclone": str(self.binary), "rclone_remotes": ["gds"]}, [])
        self.assertEqual(data["rclone_remotes"], ["gds"])
        # Without rclone the runner still installs and reads through the server.
        saved = os.environ["PATH"], agent.RCLONE_SEARCH_DIRS
        os.environ["PATH"], agent.RCLONE_SEARCH_DIRS = "/nonexistent", ()
        try:
            data, out = install({}, ["gds"])
        finally:
            os.environ["PATH"], agent.RCLONE_SEARCH_DIRS = saved
        self.assertNotIn("rclone_remotes", data)
        self.assertIn("rclone", out)
        with self.assertRaises(agent.AgentError):          # a malformed name is still refused
            install({"rclone": str(self.binary)}, ["-flag"])


def ebml(element: int, payload: bytes, size: bytes = None) -> bytes:
    """A Matroska element: its ID bytes, an 8-byte size and the payload (or a size of its own)."""
    width = (element.bit_length() + 7) // 8
    return element.to_bytes(width, "big") + (size if size is not None else b"\x01" + len(payload).to_bytes(7, "big")) + payload


def matroska(attachment_bytes: int, cluster_bytes: int, cues_at_end: int, tail_bytes: int):
    """A Matroska file shaped like a muxer writes it: SeekHead, Info, fonts (Attachments), then
    Clusters, with the Cues near the end. Returns (bytes, first cluster offset, cues offset)."""
    header = ebml(agent.MKV_EBML, b"\x42\x86\x81\x01")
    segment_start = len(header) + 4 + 8
    info = ebml(0x1549A966, b"\x2a\xd7\xb1\x83\x0f\x42\x40")
    attachments = ebml(0x1941A469, b"F" * attachment_bytes)
    cluster = ebml(agent.MKV_CLUSTER, b"C" * cluster_bytes)
    seekhead_size = len(ebml(agent.MKV_SEEKHEAD, ebml(agent.MKV_SEEK, ebml(0x53AB, b"\x1c\x53\xbb\x6b")
                                                                   + ebml(agent.MKV_SEEK_POSITION, b"\x00" * 8))))
    body_before_cues = seekhead_size + len(info) + len(attachments) + len(cluster) + cues_at_end
    seekhead = ebml(agent.MKV_SEEKHEAD, ebml(agent.MKV_SEEK, ebml(0x53AB, b"\x1c\x53\xbb\x6b")
                                             + ebml(agent.MKV_SEEK_POSITION, body_before_cues.to_bytes(8, "big"))))
    cues = ebml(0x1C53BB6B, b"Q" * 1024)
    data = (header + agent.MKV_SEGMENT.to_bytes(4, "big") + b"\x01" + b"\xff" * 7 + seekhead + info
            + attachments + cluster + b"P" * cues_at_end + cues + b"T" * tail_bytes)
    first_cluster = segment_start + seekhead_size + len(info) + len(attachments)
    return data, first_cluster, segment_start + body_before_cues


def mp4_box(kind: bytes, payload_bytes: int, declared: int = None) -> bytes:
    size = declared if declared is not None else 8 + payload_bytes
    return size.to_bytes(4, "big") + kind + b"\x00" * payload_bytes


class SourcePlanTests(unittest.TestCase):
    """FFmpeg reads the whole header, the first packets and the index while it opens a file.
    Fonts made a Matroska header 12.6 MB, and MP4 indexes (moov) were 3.6-6.3 MB at either end:
    each read outside the prefetched 1 MB head and 4 MB tail cost another 1.5-3 s (2026-11-18)."""

    MB = 1024 * 1024

    def test_matroska_fonts_are_read_through_to_the_first_packets(self):
        head, first_cluster, cues = matroska(12_600_000, 0, 5 * self.MB, 0)
        size = 4_307_550_930
        plan = agent.plan_source_regions(head[:64 * 1024], size)
        self.assertEqual(plan[0], (0, first_cluster + 4 * self.MB))     # from the sizes alone
        # The index the SeekHead points at, far from the header.
        self.assertEqual(plan[1:], [(cues, cues + 2 * self.MB)])

    def test_matroska_without_fonts_reads_from_the_cluster_it_sees(self):
        data, first_cluster, _ = matroska(100, 5000, 0, 0)
        plan = agent.plan_source_regions(data, 3_000_000_000)
        self.assertEqual(plan[0], (0, first_cluster + 4 * self.MB))

    def test_mp4_index_at_either_end(self):
        size = 4_233_946_657                        # moov after the media data
        head = mp4_box(b"ftyp", 24) + mp4_box(b"free", 0) + mp4_box(b"mdat", 64, declared=4_227_640_727)
        self.assertEqual(agent.plan_source_regions(head, size),
                         [(0, 48 + 4 * self.MB), (4_227_640_767, size)])
        size = 2_577_231_988                        # a 5.3 MB moov in front of it
        head = mp4_box(b"ftyp", 24) + mp4_box(b"moov", 64, declared=5_254_638)
        self.assertEqual(agent.plan_source_regions(head, size), [(0, 5_254_670 + 16 + 4 * self.MB)])
        # A 64-bit mdat size.
        head = mp4_box(b"ftyp", 24) + (1).to_bytes(4, "big") + b"mdat" + (6_000_000_000).to_bytes(8, "big")
        self.assertEqual(agent.plan_source_regions(head, 6_000_000_100),
                         [(0, 48 + 4 * self.MB), (6_000_000_032, 6_000_000_100)])

    def test_unknown_or_damaged_files_plan_nothing(self):
        self.assertEqual(agent.plan_source_regions(b"\x47" * 4096, 10 ** 9), [])     # MPEG-TS
        self.assertEqual(agent.plan_source_regions(b"\x1a\x45\xdf\xa3\x00", 10 ** 9), [])
        self.assertEqual(agent.plan_source_regions(mp4_box(b"ftyp", 4, declared=3), 10 ** 9), [])
        # Nothing over the region limit.
        head = mp4_box(b"ftyp", 24) + mp4_box(b"moov", 64, declared=900 * self.MB)
        self.assertEqual(agent.plan_source_regions(head, 10 ** 10), [(0, agent.SOURCE_REGION_MAX_BYTES)])

    def test_the_planned_ranges_answer_ffmpegs_opening_reads(self):
        data, first_cluster, cues = matroska(300_000, 50_000, 600_000, 20_000)
        upstream = Upstream(data)
        upstream.token = "t"
        api = SimpleNamespace(base_url="http://server", token="runner", host="127.0.0.1")
        proxy = agent.SourceProxy(api)
        saved = (agent.SOURCE_HEAD_BYTES, agent.SOURCE_TAIL_BYTES, agent.SOURCE_PACKET_BYTES,
                 agent.SOURCE_PLAN_BYTES, agent.SOURCE_INDEX_GUESS_BYTES)
        (agent.SOURCE_HEAD_BYTES, agent.SOURCE_TAIL_BYTES, agent.SOURCE_PACKET_BYTES,
         agent.SOURCE_PLAN_BYTES, agent.SOURCE_INDEX_GUESS_BYTES) = 4096, 8192, 16384, 1024, 4096
        try:
            job = SimpleNamespace(id=JOB_ID, nonce="n" * 32, stopped=threading.Event(), part_id=3, cache=None,
                                  api=api, source={"kind": "url", "url": upstream.url,
                                                   "headers": {"Authorization": "Bearer t"}})
            proxy.jobs[JOB_ID] = job
            proxy.prefetch(job)
            for _ in range(100):
                if job.cache.planned and job.cache.idle(5):
                    break
                time.sleep(0.02)
            self.assertEqual(sorted(r for _, r in upstream.seen), sorted([
                "bytes=0-4095", "bytes=-8192", "bytes=4096-%d" % (first_cluster + 16384 - 1),
                "bytes=%d-%d" % (cues, cues + 4096 - 1)]))
            asked = len(upstream.seen)
            for start, end in ((0, first_cluster + 16000), (cues, cues + 1000), (len(data) - 5000, len(data) - 1)):
                conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
                conn.request("GET", "/%s/%s/media" % (JOB_ID, job.nonce), headers={"Range": "bytes=%d-%d" % (start, end)})
                response = conn.getresponse()
                self.assertEqual((response.status, response.read()), (206, data[start:end + 1]))
                conn.close()
            self.assertEqual(len(upstream.seen), asked)        # FFmpeg's opening reads stay local
        finally:
            (agent.SOURCE_HEAD_BYTES, agent.SOURCE_TAIL_BYTES, agent.SOURCE_PACKET_BYTES,
             agent.SOURCE_PLAN_BYTES, agent.SOURCE_INDEX_GUESS_BYTES) = saved
            proxy.server.shutdown()
            upstream.server.shutdown()


class SlowServer:
    """The Shyni server's /source for a job: needs the runner token, sends slowly (a far server)."""

    def __init__(self, data, pause=0.05, block=64 * 1024):
        server = self
        self.data, self.ranges, self.status = data, [], None

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                server.ranges.append(self.headers.get("Range"))
                if self.headers.get("Authorization") != "Bearer runner" or server.status:
                    self.send_response(server.status or 401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                first, _, last = (self.headers.get("Range") or "bytes=0-").split("=")[1].partition("-")
                start = int(first) if first else len(server.data) - int(last)
                stop = int(last) + 1 if first and last else len(server.data)
                body = server.data[start:stop]
                self.send_response(206)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, stop - 1, len(server.data)))
                self.end_headers()
                try:
                    for index in range(0, len(body), block):
                        self.wfile.write(body[index:index + block])
                        self.wfile.flush()
                        time.sleep(pause)
                except OSError:
                    pass                                      # the runner switched to its rclone

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


class ServerFirstTests(unittest.TestCase):
    """Until the runner's rclone has found the file's folder (4-8 s for a new folder), or when it
    fails, the server that gave the job sends the bytes; reading moves to rclone at a byte boundary
    as soon as it can, so a far server does not set the pace for the whole film."""

    PATH = "영화/인터스텔라 (2014)/Interstellar.mkv"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        binary = self.root / "rclone"
        binary.write_text(FAKE_RCLONE)
        binary.chmod(0o755)
        self.data = bytes((index * 7 + index // 251) % 256 for index in range(2 * 1024 * 1024))
        target = self.root / "files" / self.PATH
        target.parent.mkdir(parents=True)
        target.write_bytes(self.data)
        os.environ["FAKE_RCLONE_ROOT"] = str(self.root)
        self.servers = agent.RcloneServers(str(binary), None, self.root / "logs")
        self.far = SlowServer(self.data)
        self.proxy = agent.SourceProxy(SimpleNamespace(base_url=self.far.base_url, token="runner"), self.servers)
        self.saved = agent.HANDOVER_MIN_BYTES, agent.HANDOVER_LEAD_SEC
        agent.HANDOVER_MIN_BYTES, agent.HANDOVER_LEAD_SEC = 128 * 1024, 0.5

    def tearDown(self):
        agent.HANDOVER_MIN_BYTES, agent.HANDOVER_LEAD_SEC = self.saved
        self.servers.close()
        self.proxy.server.shutdown()
        self.far.server.shutdown()
        os.environ.pop("FAKE_RCLONE_ROOT", None)
        self.tmp.cleanup()

    def job(self, server_fallback=True):
        source = agent.clean_source({"kind": "rclone", "remote": "gds", "folder_id": "1AbC", "path": self.PATH,
                                     "server_fallback": server_fallback}, "server", ["gds"])
        job = SimpleNamespace(id=JOB_ID, nonce="n" * 32, stopped=threading.Event(), source=source,
                              api=SimpleNamespace(base_url=self.far.base_url, token="runner"))
        self.proxy.jobs[JOB_ID] = job
        return job

    def read(self, job, ranged="bytes=0-"):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.port, timeout=30)
        conn.request("GET", "/%s/%s/media" % (job.id, job.nonce), headers={"Range": ranged})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, body

    def rclone_ranges(self):
        path = self.root / "ranges.log"
        return path.read_text().splitlines() if path.exists() else []

    def test_the_server_starts_and_rclone_takes_over_at_a_byte_boundary(self):
        job = self.job()
        self.assertEqual(self.read(job), (206, self.data))
        self.assertEqual(self.far.ranges, ["bytes=0-"])                 # began on the server at once
        handover = [line for line in self.rclone_ranges() if line != "bytes=0-0"]
        self.assertIn("bytes=0-0", self.rclone_ranges())                 # rclone looked the file up meanwhile
        self.assertEqual(len(handover), 1)
        start = int(handover[0].split("=")[1].rstrip("-"))
        self.assertTrue(0 < start < len(self.data), handover)
        # The next read on that folder goes to rclone directly.
        self.assertEqual(self.read(job, "bytes=1000-1999"), (206, self.data[1000:2000]))
        self.assertEqual(self.far.ranges, ["bytes=0-"])

    def test_without_the_servers_offer_rclone_reads_it_alone(self):
        job = self.job(server_fallback=False)
        self.assertEqual(self.read(job), (206, self.data))
        self.assertEqual(self.far.ranges, [])
        self.assertEqual(self.rclone_ranges(), ["bytes=0-"])

    def test_a_dropped_rclone_read_continues_from_the_server(self):
        job = self.job()
        self.assertEqual(self.read(job, "bytes=0-99"), (206, self.data[:100]))
        for _ in range(100):                                            # rclone has found the file
            if self.servers.ready(job.source):
                break
            time.sleep(0.05)
        (self.root / "cut_after").write_text("300000")
        self.far.ranges.clear()
        self.assertEqual(self.read(job, "bytes=500000-"), (206, self.data[500000:]))
        self.assertEqual(self.far.ranges[0], "bytes=800000-")

    def test_a_slow_rclone_answer_is_raced_by_the_server(self):
        job = self.job()
        self.assertEqual(self.read(job, "bytes=0-99"), (206, self.data[:100]))
        for _ in range(100):
            if self.servers.ready(job.source):
                break
            time.sleep(0.05)
        (self.root / "slow").write_text("5")
        saved, agent.RCLONE_HEDGE_SEC = agent.RCLONE_HEDGE_SEC, 0.3
        try:
            began = time.monotonic()
            self.assertEqual(self.read(job, "bytes=1000-1999"), (206, self.data[1000:2000]))
            self.assertLess(time.monotonic() - began, 3)
            self.assertEqual(self.far.ranges[-1], "bytes=1000-1999")
        finally:
            agent.RCLONE_HEDGE_SEC = saved
            (self.root / "slow").unlink()

    def test_before_rclone_is_ready_the_prefetch_asks_both(self):
        job = self.job()
        job.part_id, job.cache = 5, None
        saved = agent.SOURCE_HEAD_BYTES, agent.SOURCE_TAIL_BYTES
        agent.SOURCE_HEAD_BYTES, agent.SOURCE_TAIL_BYTES = 64 * 1024, 32 * 1024
        try:
            self.proxy.prefetch(job)
            self.assertTrue(job.cache.idle(10))
        finally:
            agent.SOURCE_HEAD_BYTES, agent.SOURCE_TAIL_BYTES = saved
        self.assertIn("bytes=0-65535", self.far.ranges)
        for _ in range(100):                     # the slower one still asked (and found the folder)
            if "bytes=0-65535" in self.rclone_ranges() and self.servers.ready(job.source):
                break
            time.sleep(0.05)
        self.assertIn("bytes=0-65535", self.rclone_ranges())
        self.assertTrue(self.servers.ready(job.source))
        head = job.cache.region_at(0)
        self.assertEqual(bytes(head.data), self.data[:65536])
        self.assertEqual(self.read(job, "bytes=100-199"), (206, self.data[100:200]))

    def test_a_server_that_cannot_send_it_leaves_it_to_rclone(self):
        self.far.status = 404                       # an older server, or the job already ended there
        job = self.job()
        self.assertEqual(self.read(job), (206, self.data))
        self.assertTrue(job.server_failed)
        self.assertIn("bytes=0-", self.rclone_ranges())


class SubtitlePrefetchTests(unittest.TestCase):
    """FFmpeg opens the external subtitle only after reading the source up to the start position,
    so its request to the server sat on the path to the first segment. The agent fetches it as the
    job starts and serves FFmpeg from memory."""

    def setUp(self):
        self.text = ("1\n00:00:01,000 --> 00:00:02,000\n안녕\n\n" * 50).encode()
        self.status = 200
        self.hits = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                test.hits.append((self.path, self.headers.get("Range")))
                ok = self.headers.get("Authorization") == "Bearer runner" and test.status == 200
                body = test.text if ok else b""
                self.send_response(200 if ok else test.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        api = SimpleNamespace(base_url="http://127.0.0.1:%d" % self.server.server_address[1], token="runner")
        self.proxy = agent.SourceProxy(api)
        self.job = SimpleNamespace(id=JOB_ID, nonce="n" * 32, stopped=threading.Event(), api=api,
                                   source={"kind": "server"})
        self.proxy.jobs[JOB_ID] = self.job

    def tearDown(self):
        self.proxy.server.shutdown()
        self.server.shutdown()

    def read(self, ranged=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.port, timeout=10)
        conn.request("GET", "/%s/%s/subtitle" % (JOB_ID, self.job.nonce), headers={"Range": ranged} if ranged else {})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, body

    def test_the_subtitle_is_fetched_once_at_job_start_and_served_locally(self):
        self.proxy.prefetch_subtitle(self.job)
        self.assertTrue(self.job.subtitle_ready.wait(5))
        self.assertEqual(self.read("bytes=0-"), (206, self.text))
        self.assertEqual(self.read("bytes=10-19"), (206, self.text[10:20]))
        self.assertEqual(self.read(), (200, self.text))
        self.assertEqual(len(self.hits), 1)

    def test_a_failed_prefetch_leaves_it_to_the_server(self):
        self.status = 404
        self.proxy.prefetch_subtitle(self.job)
        self.assertTrue(self.job.subtitle_ready.wait(5))
        self.assertEqual(self.read("bytes=0-")[0], 404)
        self.assertEqual(len(self.hits), 2)                  # the prefetch, then FFmpeg's own request


CAPS = {"video_encoder": "h264_videotoolbox", "hw_encoder": "h264_videotoolbox", "tonemap": True,
        "ffmpeg_version": "8.1"}


M1_CAPS = {
    "video_encoder": "h264_videotoolbox", "hw_encoder": "h264_videotoolbox", "tonemap": True,
    "video_encoders": {"h264": "h264_videotoolbox", "hevc": "hevc_videotoolbox"},
    "hls_fmp4_subtitles": True, "vaapi_device": None, "vaapi_driver": None, "hw_decode": None,
}


def fake_agent(api, tmp, caps):
    return SimpleNamespace(
        api=api, config=SimpleNamespace(tmp_dir=Path(tmp)), caps=caps, ffmpeg="/opt/ffmpeg",
        proxy=SimpleNamespace(jobs={}, url_for=lambda job, what: "http://127.0.0.1:5/%s/n/%s" % (job.id, what)),
        job_finished=lambda job: None,
    )


class OutputCodecTests(unittest.TestCase):
    def job(self, tmp, caps, **spec):
        return agent.Job(fake_agent(FakeApi(), tmp, caps), {
            "id": JOB_ID, "kind": "hls", "spec": {"mode": "video", "max_height": 480, **spec},
            "source": {"kind": "server"}, "has_subtitle_file": True,
        })

    def test_the_servers_codec_is_encoded_with_this_machines_encoder_for_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = self.job(tmp, M1_CAPS, output_codec="hevc", audio_bitrate_kbps=128,
                           audio_output_max_channels=2, audio_channels=6,
                           subtitle={"source": "external"})
            job._choose_encoder()
            args = job._arguments()
            self.assertEqual((job.output_codec, job.encoder), ("hevc", "hevc_videotoolbox"))
            self.assertEqual(args[args.index("-c:v") + 1], "hevc_videotoolbox")
            self.assertEqual(args[args.index("-b:v") + 1], "1000k")
            self.assertEqual(args[args.index("-tag:v") + 1], "hvc1")
            self.assertEqual(args[args.index("-b:a"):args.index("-b:a") + 4], ["-b:a", "128k", "-ac", "2"])
            # HEVC is never MPEG-TS, although the server default (and a subtitle rendition) is.
            self.assertEqual(args[args.index("-hls_segment_type") + 1], "fmp4")
            self.assertEqual(args[args.index("-var_stream_map") + 1], "v:0,a:0,s:0,sgroup:subs")

    def test_a_codec_this_machine_cannot_encode_falls_back_to_h264(self):
        with tempfile.TemporaryDirectory() as tmp:
            for caps, wanted in ((M1_CAPS, "av1"), ({**M1_CAPS, "video_encoders": {}}, "hevc"),
                                 ({"video_encoder": "libx264", "hw_encoder": None}, "hevc")):
                job = self.job(tmp, caps, output_codec=wanted, subtitle={"source": "external"})
                job._choose_encoder()
                args = job._arguments()
                self.assertEqual((job.output_codec, job.encoder), ("h264", caps["video_encoder"]))
                self.assertEqual(args[args.index("-c:v") + 1], caps["video_encoder"])
                self.assertEqual(args[args.index("-b:v") + 1], "1500k")
                self.assertNotIn("-tag:v", args)
                self.assertEqual(args[args.index("-hls_segment_type") + 1], "mpegts")

    def test_capabilities_report_the_codecs_and_the_fmp4_subtitle_check(self):
        public = agent.public_capabilities(M1_CAPS, ["gds"])
        self.assertEqual(public["video_encoders"], {"h264": "h264_videotoolbox", "hevc": "hevc_videotoolbox"})
        self.assertIs(public["hls_fmp4_subtitles"], True)
        self.assertEqual((public["video_encoder"], public["hw_encoder"]), ("h264_videotoolbox",) * 2)
        software = agent.public_capabilities({"video_encoder": "libx264", "video_encoders": {"hevc": "libx265"}})
        self.assertEqual((software["video_encoders"], software["hls_fmp4_subtitles"]), ({"h264": "libx264"}, False))
        self.assertEqual(agent.describe_output_codecs(M1_CAPS), "H.264, H.265")
        self.assertIn("H.264)", agent.describe_output_codecs({**M1_CAPS, "hls_fmp4_subtitles": False}))

    def test_detection_probes_the_same_family_and_keeps_what_passes(self):
        listed = " ".join(" %s " % name for name in (
            "libx264", "h264_nvenc", "hevc_nvenc", "av1_nvenc", "hevc_vaapi"))
        probed = []

        def run(ffmpeg, args, timeout=15.0, env=None):
            if "-version" in args:
                return 0, "ffmpeg version 8.1"
            return (0, listed) if "-encoders" in args else (0, " zscale  tonemap ")

        def smoke(ffmpeg, encoder, device=None, driver=None):
            probed.append(encoder)
            return encoder != "av1_nvenc"             # this card has no AV1 encoder

        originals = {name: getattr(agent, name) for name in (
            "run_ffmpeg", "_smoke_test_encoder", "probe_hw_decode", "probe_hls_fmp4_subtitles",
            "_ordered_hw_encoder_candidates")}
        agent.run_ffmpeg, agent._smoke_test_encoder = run, smoke
        agent.probe_hw_decode = lambda ffmpeg, caps: None
        agent.probe_hls_fmp4_subtitles = lambda ffmpeg, caps, timeout=15.0: "hevc" in caps["video_encoders"]
        agent._ordered_hw_encoder_candidates = lambda preference: ("h264_nvenc",)
        try:
            caps = agent.detect_capabilities("/opt/ffmpeg")
            agent._ordered_hw_encoder_candidates = lambda preference: ()
            software = agent.detect_capabilities("/opt/ffmpeg")
        finally:
            for name, original in originals.items():
                setattr(agent, name, original)
        self.assertEqual(probed, ["h264_nvenc", "hevc_nvenc", "av1_nvenc"])      # never hevc_vaapi
        self.assertEqual(caps["video_encoders"], {"h264": "h264_nvenc", "hevc": "hevc_nvenc"})
        self.assertIs(caps["hls_fmp4_subtitles"], True)
        self.assertEqual((caps["video_encoder"], caps["hw_encoder"]), ("h264_nvenc", "h264_nvenc"))
        self.assertEqual(software["video_encoders"], {"h264": "libx264"})
        self.assertIs(software["hls_fmp4_subtitles"], False)


class RunningReportTests(unittest.TestCase):
    """The server gives up on a job that is not reported running within a few seconds. A remote
    source can take longer than that to produce the first output, so the report must not wait."""

    def run_job(self, caps, kind="hls"):
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeApi()
            job = agent.Job(fake_agent(api, tmp, caps), {
                "id": JOB_ID, "kind": kind, "spec": {"mode": "video", "video_codec": "h264"},
                "source": {"kind": "server"},
            })
            events = []
            output_ready = threading.Event()

            def states():
                return [json.loads(body)["state"] for _method, name, body in api.calls if name == "state"]

            def start_ffmpeg(encoder):
                events.append(("ffmpeg", states()))
                job.proc = SimpleNamespace(
                    poll=lambda: 0 if output_ready.is_set() else None, wait=lambda: 0, returncode=0,
                    kill=lambda: None)

            def wait_for_output(*_args):
                # ffmpeg is still opening the source: nothing has been written yet.
                events.append(("waiting", states()))
                output_ready.set()
                return b""

            job._start_ffmpeg = start_ffmpeg
            job._first_output = wait_for_output
            job._sync_outputs = wait_for_output
            job._pump_pipe = wait_for_output
            job.run()
            return events, states()

    def test_running_is_reported_before_the_wait_for_the_first_output(self):
        decoding = {**M1_CAPS, "hw_decode": {"hwaccel": "videotoolbox", "codecs": {"h264": [8]}}}
        for caps, kind in ((decoding, "hls"), (decoding, "pipe"), (M1_CAPS, "hls"), (M1_CAPS, "pipe")):
            events, states = self.run_job(caps, kind)
            self.assertEqual(events[0], ("ffmpeg", []), (kind, events))         # nothing before ffmpeg starts
            # The first wait for output already sees the report, and only that report.
            self.assertEqual(events[1], ("waiting", ["running"]), (kind, events))
            self.assertEqual(states, ["running", "finished"])


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

    def test_a_server_that_deleted_this_runner_is_forgotten_and_the_last_one_ends_the_agent(self):
        class Removed(FakeApi):
            def __init__(self, token):
                super().__init__()
                self.token = token

            def call(self, *args, **kwargs):
                raise agent.RunnerRemoved("등록되지 않은 러너입니다")

        home = self.agent.config.home
        self.agent.config.save()
        self.agent.handle(self.two, start(2))
        theirs = self.agent.jobs[job_id(2)]
        removed_autostart = []
        with mock.patch.object(agent, "remove_own_autostart", side_effect=removed_autostart.append):
            self.two.api = Removed("b")
            self.agent._serve(self.two)                          # returns: that server is done with us
            self.assertEqual(theirs.stops, 1)
            self.assertEqual([entry["url"] for entry in agent.Config(home).servers], ["https://one.example"])
            self.assertEqual(self.agent.links, [self.one])
            self.assertTrue(self.agent.running)
            self.assertEqual(removed_autostart, [])
            self.one.api = Removed("a")
            self.agent._serve(self.one)                          # the last server: nothing left to serve
            self.assertEqual(agent.Config(home).servers, [])
            self.assertFalse(self.agent.running)
            self.assertEqual(removed_autostart, [home])

    def test_a_reinstalled_folder_is_not_forgotten_for_its_old_registration(self):
        """install replaced the token while the old agent still ran; the server dropping the old
        runner must not take the new registration with it."""
        home = self.agent.config.home
        self.two.api.token = "b"
        self.agent.config.data["servers"][1]["token"] = "b-new"
        self.agent.config.save()
        with mock.patch.object(agent, "remove_own_autostart") as autostart:
            self.agent.forget(self.two)                          # still holds the old token "b"
        self.assertEqual([entry["token"] for entry in agent.Config(home).servers], ["a", "b-new"])
        self.assertTrue(self.agent.running)
        autostart.assert_not_called()

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
        url, headers = self.agent.proxy._upstream(job, self.agent.proxy._via(job, "media"))
        self.assertEqual((url, headers["Authorization"]),
                         ("https://two.example/transcode-agent/jobs/%s/source" % job_id(9), "Bearer b"))


class RegisterApi:
    registered = []
    replaces = []

    def __init__(self, base_url, token):
        self.base_url, self.token = base_url.rstrip("/"), token

    def call(self, method, path, body=None, **kwargs):
        RegisterApi.registered.append((self.base_url, body["recommended_slots"]))
        RegisterApi.replaces.append(body.get("replaces"))
        number = len(RegisterApi.registered)
        return {"runner_id": number, "token": "credential-%d" % number, "name": body["name"]}


class ServerListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.saved = agent.Api, agent.choose_ffmpeg, agent.machine_info, agent._restart_service
        agent.Api, RegisterApi.registered, RegisterApi.replaces = RegisterApi, [], []
        agent.choose_ffmpeg = lambda config, explicit: ("/opt/ffmpeg", dict(CAPS))
        agent.machine_info = lambda: {"os": "test"}
        agent._restart_service = lambda *_args: None
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
        # Running the command again on a server it is already on registers anew (no --force since
        # 0.6.1) and asks that server to drop the earlier runner, so it is not listed twice.
        self.assertEqual(self.run_agent(*install, "--url", "https://one.example"), 0)
        self.assertEqual(self.servers(), [("https://one.example", "credential-3"),
                                          ("https://two.example", "credential-2")])
        self.assertEqual(RegisterApi.replaces, [None, None, "credential-1"])
        self.assertEqual(self.run_agent(*install, "--url", "https://one.example", "--force"), 0)
        self.assertEqual(RegisterApi.replaces[-1], "credential-3")

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


class RunnerRemovedTests(unittest.TestCase):
    def test_only_the_servers_own_signal_means_the_runner_was_deleted(self):
        """A 401 from a proxy in front of the server must not make the agent forget the server."""
        api = agent.Api("https://one.example", "t")
        for marked, expected in ((True, agent.RunnerRemoved), (False, agent.Unauthorized)):
            headers = http.client.HTTPMessage()
            if marked:
                headers[agent.RUNNER_STATE_HEADER] = "unknown"
            api.request = lambda *args, **kwargs: (401, headers, b'{"detail": "x"}')
            with self.assertRaises(agent.Unauthorized) as caught:
                api.call("POST", "/transcode-agent/poll", {})
            self.assertIs(type(caught.exception), expected)

    def test_the_agent_removes_only_its_own_folders_autostart(self):
        with tempfile.TemporaryDirectory() as folder:
            home, other = Path(folder) / "mine", Path(folder) / "mine-old"        # a name that starts the same
            with mock.patch.object(agent.platform, "system", return_value="Windows"), \
                    mock.patch.object(agent, "_windows_autostart") as autostart:
                for registered, removed in ((home, True), (other, False)):
                    command = agent.subprocess.list2cmdline(["pythonw.exe", "agent.py", "run", "--dir", str(registered)])
                    with mock.patch.object(agent, "windows_autostart_command", return_value=command):
                        autostart.reset_mock()
                        agent.remove_own_autostart(home)
                        self.assertEqual(autostart.call_args_list, [mock.call(None)] if removed else [])
            plist = Path(folder) / "agent.plist"
            with mock.patch.object(agent.platform, "system", return_value="Darwin"), \
                    mock.patch.object(agent, "_launch_agent_path", return_value=plist), \
                    mock.patch.object(agent.subprocess, "call") as call, \
                    mock.patch.object(agent.os, "getuid", return_value=501, create=True):
                plist.write_text("<string>%s</string>" % agent._xml(str(other)))
                agent.remove_own_autostart(home)
                self.assertTrue(plist.exists())
                call.assert_not_called()
                plist.write_text("<string>%s</string>" % agent._xml(str(home)))
                agent.remove_own_autostart(home)
                self.assertFalse(plist.exists())           # gone before launchd could restart it
                self.assertEqual(call.call_args[0][0][:2], ["launchctl", "bootout"])


class WindowsSupportTests(unittest.TestCase):
    """Windows 경로는 이 OS 의 API 를 바꿔 끼워 리눅스·macOS 에서도 시험한다."""

    def test_encoders_follow_the_gpus_found_in_the_registry(self):
        cases = (
            ([("nvidia", "NVIDIA GeForce RTX 4070"), ("intel", "Intel(R) UHD Graphics 770")],
             ("h264_nvenc", "h264_qsv")),
            ([("intel", "Intel(R) UHD Graphics"), ("amd", "AMD Radeon RX 7800 XT")], ("h264_amf", "h264_qsv")),
            ([("intel", "Intel(R) Arc(TM) A380")], ("h264_qsv",)),
            ([("", "Microsoft Basic Display Adapter")], ("h264_nvenc", "h264_amf", "h264_qsv")),
            ([], ("h264_nvenc", "h264_amf", "h264_qsv")),
        )
        with mock.patch.object(agent.platform, "system", return_value="Windows"):
            for gpus, expected in cases:
                with mock.patch.object(agent, "windows_gpus", return_value=gpus):
                    self.assertEqual(agent._ordered_hw_encoder_candidates("auto"), expected)
                    # CUDA 디코딩은 NVIDIA 가 있거나 GPU 를 읽지 못했을 때만 시험한다.
                    self.assertEqual(bool(agent._hw_decode_candidates({})), expected[0] == "h264_nvenc")
                    self.assertEqual(agent._ordered_hw_encoder_candidates("h264_amf"), ("h264_amf",))

    def test_amf_output_is_8_bit_with_a_bitrate_ceiling(self):
        for encoder, codec in (("h264_amf", "h264"), ("hevc_amf", "hevc"), ("av1_amf", "av1")):
            args = agent.build_transcode_args(
                "ffmpeg", "http://127.0.0.1:1/media", "video", max_height=720, hdr=False,
                video_encoder=encoder, output_codec=codec)
            self.assertIn("format=nv12", args[args.index("-vf") + 1])
            self.assertEqual(args[args.index("-c:v") + 1:args.index("-c:v") + 6],
                             [encoder, "-quality", "balanced", "-rc", "vbr_peak"])
            self.assertEqual(args[args.index("-maxrate") + 1], args[args.index("-b:v") + 1])
        self.assertEqual(agent.ENCODER_ALIASES["amf"], "h264_amf")

    def test_the_subtitle_probe_runs_in_its_output_folder(self):
        """Windows ffmpeg writes the fMP4 init file into the current folder, not next to the playlist."""
        seen = []

        def run(ffmpeg, args, timeout=15.0, env=None, cwd=None):
            if "rawvideo" in args:
                Path(args[-1]).write_bytes(b"nut")
                return 0, ""
            seen.append((Path(args[-1]).parent, cwd))
            out = Path(cwd or os.getcwd())                      # where this ffmpeg would put init.mp4
            (out / "init.mp4").write_bytes(b"init")
            (Path(args[-1]).parent / "index.m3u8").write_text("#EXTM3U\n")
            (Path(args[-1]).parent / "master.m3u8").write_text("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nindex.m3u8\n")
            return 0, ""

        caps = {"video_encoder": "h264_qsv", "video_encoders": {"h264": "h264_qsv", "hevc": "hevc_qsv"}}
        with mock.patch.object(agent, "run_ffmpeg", side_effect=run):
            self.assertTrue(agent.probe_hls_fmp4_subtitles("ffmpeg", caps))
        [(playlist_folder, cwd)] = seen
        self.assertEqual(str(playlist_folder), cwd)

    def test_intel_decodes_scales_and_encodes_on_the_gpu_with_qsv(self):
        qsv_caps = {"hw_encoder": "h264_qsv", "video_encoder": "h264_qsv"}
        with mock.patch.object(agent.platform, "system", return_value="Windows"):
            with mock.patch.object(agent, "windows_gpus", return_value=[("intel", "Intel(R) UHD Graphics 630")]):
                # D3D11 위의 QSV 하나. 내려받는 방식(d3d11va)과 모니터가 꺼지면 안 되는 DXVA2 는 쓰지 않는다.
                self.assertEqual(agent._hw_decode_candidates(qsv_caps), [("qsv", "d3d11va", None)])
                self.assertEqual(agent._hw_decode_candidates({"hw_encoder": None}), [])
            with mock.patch.object(agent, "windows_gpus", return_value=[("nvidia", "RTX"), ("intel", "UHD 770")]):
                self.assertEqual(agent._hw_decode_candidates({"hw_encoder": "h264_nvenc"}), [("cuda", None, None)])
        self.assertEqual(agent.hw_decode_pipeline(qsv_caps, "qsv", "d3d11va"), "gpu")
        self.assertEqual(agent.hw_decode_pipeline({"hw_encoder": "libx264"}, "qsv", "d3d11va"), "")

        check = agent.hw_decode_check_args("qsv", "d3d11va", 10, "/tmp/hevc-10.mkv")
        self.assertEqual(check[check.index("-init_hw_device") + 1], "qsv=hw,child_device_type=d3d11va")
        self.assertEqual(check[check.index("-hwaccel_output_format") + 1], "qsv")
        # 축소 출력까지 만들어 본다 — D3D11 에서 디코딩만 되고 scale_qsv 가 실패하는 기기를 거른다.
        self.assertEqual(check[check.index("-vf") + 1], "scale_qsv=w=320:h=180:format=nv12,hwdownload,format=nv12")

        decode = agent.hw_decode_for({"hw_decode": {"hwaccel": "qsv", "device": "d3d11va", "pipeline": "gpu",
                                                    "codecs": {"hevc": [8, 10]}}}, "hevc", 10)
        self.assertEqual(decode, {"hwaccel": "qsv", "device": "d3d11va", "pipeline": "gpu"})
        for encoder, codec in (("h264_qsv", "h264"), ("hevc_qsv", "hevc")):
            args = agent.build_transcode_args("ffmpeg", "http://127.0.0.1:1/media", "video", max_height=720,
                                              video_encoder=encoder, output_codec=codec, hw_decode=decode)
            source = args.index("-i")
            self.assertEqual(args[1:source][3:], [
                "-init_hw_device", "qsv=hw,child_device_type=d3d11va", "-filter_hw_device", "hw",
                "-hwaccel", "qsv", "-hwaccel_device", "hw", "-hwaccel_output_format", "qsv"])
            [video_filter] = [args[i + 1] for i, value in enumerate(args) if value == "-vf"]
            self.assertTrue(video_filter.startswith("scale_qsv=") and video_filter.endswith(":format=nv12"))
            self.assertNotIn("hwupload", video_filter)
            self.assertEqual(args[args.index("-c:v") + 1], encoder)
        unscaled = agent.build_transcode_args("ffmpeg", "http://127.0.0.1:1/media", "video",
                                              video_encoder="h264_qsv", hw_decode=decode)
        self.assertEqual(unscaled[unscaled.index("-vf") + 1], "scale_qsv=format=nv12")
        # HDR 은 톤매핑(CPU 필터)이 필요해 S/W 로 푼다.
        self.assertIsNone(agent.hw_decode_for({"hw_decode": {"hwaccel": "qsv", "device": "d3d11va", "pipeline": "gpu",
                                                             "codecs": {"hevc": [10]}}}, "hevc", 10, hdr=True))

    def test_nvidia_decodes_scales_and_encodes_on_the_gpu_with_cuda(self):
        nvenc = {"hw_encoder": "h264_nvenc", "video_encoder": "h264_nvenc"}
        filters = " ... scale_cuda        V->V       GPU accelerated video resizer\n"
        self.assertEqual(agent.hw_decode_pipeline(nvenc, "cuda", None, filters), "gpu")
        # scale_cuda 가 없는 ffmpeg, NVENC 가 아닌 인코더는 지금처럼 내려받는다.
        self.assertEqual(agent.hw_decode_pipeline(nvenc, "cuda", None, " scale "), "download")
        self.assertEqual(agent.hw_decode_pipeline({"hw_encoder": "h264_vaapi"}, "cuda", None, filters), "download")

        check = agent.hw_decode_check_args("cuda", None, 10, "/tmp/hevc-10.mkv", "gpu")
        self.assertEqual(check[check.index("-hwaccel_output_format") + 1], "cuda")
        self.assertEqual(check[check.index("-vf") + 1], "scale_cuda=w=320:h=180:format=nv12,hwdownload,format=nv12")
        download = agent.hw_decode_check_args("cuda", None, 10, "/tmp/hevc-10.mkv")
        self.assertEqual(download[download.index("-vf") + 1], "hwdownload,format=p010le")

        caps = {"hw_decode": {"hwaccel": "cuda", "device": None, "pipeline": "gpu", "codecs": {"hevc": [8, 10]}}}
        decode = agent.hw_decode_for(caps, "hevc", 10)
        self.assertEqual(decode, {"hwaccel": "cuda", "device": None, "pipeline": "gpu"})
        for encoder, codec in (("h264_nvenc", "h264"), ("hevc_nvenc", "hevc")):
            args = agent.build_transcode_args("ffmpeg", "http://127.0.0.1:1/media", "video", max_height=720,
                                              start_ms=5000, video_encoder=encoder, output_codec=codec, hw_decode=decode)
            source = args.index("-i")
            self.assertEqual(args[4:source], ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-ss", "5.000"])
            [video_filter] = [args[i + 1] for i, value in enumerate(args) if value == "-vf"]
            self.assertEqual(video_filter, "scale_cuda=w='min(iw,1280)':h='min(ih,720)':"
                                           "force_original_aspect_ratio=decrease:force_divisible_by=2:format=nv12")
            self.assertEqual(args[args.index("-c:v") + 1], encoder)
        unscaled = agent.build_transcode_args("ffmpeg", "http://127.0.0.1:1/media", "video",
                                              video_encoder="h264_nvenc", hw_decode=decode)
        self.assertEqual(unscaled[unscaled.index("-vf") + 1], "scale_cuda=format=nv12")
        # HDR 은 톤매핑(CPU 필터)이 필요해 S/W 로 푼다.
        self.assertIsNone(agent.hw_decode_for(caps, "hevc", 10, hdr=True))

    def test_nvidia_probe_checks_the_gpu_scaler_and_keeps_frames_on_the_gpu(self):
        commands, measured = [], []

        def fake_run(ffmpeg, args, timeout=15.0, env=None, cwd=None):
            commands.append(args)
            if args[:2] == ["-hide_banner", "-encoders"]:
                return 0, " V..... libx264  V..... libx265 "
            if args[:2] == ["-hide_banner", "-filters"]:
                return 0, filters_out
            if "-hwaccel" not in args:                      # 시험 영상 만들기
                Path(args[-1]).write_bytes(b"sample")
            return 0, ""

        def fake_measure(args, env, timeout=30.0):
            measured.append(args)
            return (0, 1.0, 0.2) if "-hwaccel" in args else (0, 1.0, 1.0)

        caps = {"video_encoder": "h264_nvenc", "hw_encoder": "h264_nvenc"}
        with mock.patch.object(agent, "run_ffmpeg", fake_run), \
                mock.patch.object(agent, "_measure_ffmpeg", fake_measure), \
                mock.patch.object(agent.platform, "system", return_value="Windows"), \
                mock.patch.object(agent, "windows_gpus", return_value=[("nvidia", "NVIDIA GeForce RTX 3060 Ti")]):
            for filters_out, pipeline in ((" T.. scale_cuda  V->V  GPU accelerated video resizer ", "gpu"),
                                          (" ..C scale  V->V  Scale the input video size ", "download")):
                commands.clear(), measured.clear()
                decode = agent.probe_hw_decode("ffmpeg", caps)
                self.assertEqual(decode["pipeline"], pipeline)
                self.assertEqual(decode["codecs"], {"h264": [8], "hevc": [8, 10]})
                checks = [args for args in commands if "-hwaccel" in args]
                self.assertTrue(checks and all(("scale_cuda=" in " ".join(args)) == (pipeline == "gpu") for args in checks))
                self.assertEqual(("scale_cuda=" in " ".join(measured[1])), pipeline == "gpu")   # 실제 480p 명령

    def test_qsv_scaling_keeps_the_picture_shape(self):
        """scale_qsv 에는 force_original_aspect_ratio 가 없어 크기를 식으로 준다 — 같은 상자에 맞춘다."""
        decode = {"hwaccel": "qsv", "device": "d3d11va", "pipeline": "gpu"}
        args = agent.build_transcode_args("ffmpeg", "http://127.0.0.1:1/media", "video", max_height=720,
                                          video_encoder="h264_qsv", hw_decode=decode)
        options = dict(part.split("=", 1) for part in args[args.index("-vf") + 1][len("scale_qsv="):].split(":"))

        def evaluate(expression, iw, ih):
            return eval(expression.strip("'"), {"min": min, "trunc": int, "iw": iw, "ih": ih})

        for (iw, ih), expected in (((1920, 1080), (1280, 720)), ((1920, 800), (1280, 532)),
                                   ((1440, 1080), (960, 720)), ((1280, 720), (1280, 720)),
                                   ((720, 480), (720, 480)), ((1920, 1088), (1270, 720))):
            self.assertEqual((evaluate(options["w"], iw, ih), evaluate(options["h"], iw, ih)), expected, (iw, ih))

    def test_pause_freezes_ffmpeg_once_per_change(self):
        calls = []
        job = SimpleNamespace(proc=SimpleNamespace(pid=4242, poll=lambda: None), _suspended=False)
        with mock.patch.object(agent, "WINDOWS", True), \
                mock.patch.object(agent, "windows_suspend", side_effect=lambda pid, on: calls.append((pid, on)) or True):
            for paused in (True, True, False, False, True):
                agent.Job.pause(job, paused)
        self.assertEqual(calls, [(4242, True), (4242, False), (4242, True)])
        self.assertTrue(job._suspended)

    def test_cpu_time_is_measured_through_the_process_handle(self):
        closed = []
        api = {"kernel32": SimpleNamespace(CloseHandle=closed.append)}
        with mock.patch.object(agent, "WINDOWS", True), \
                mock.patch.object(agent, "_windows_open_process", return_value=77), \
                mock.patch.object(agent, "windows_cpu_seconds", return_value=1.25), \
                mock.patch.object(agent, "_windows_api", return_value=api):
            code, wall, cpu = agent._measure_ffmpeg([sys.executable, "-c", "pass"], None)
        self.assertEqual((code, cpu, closed), (0, 1.25, [77]))
        self.assertGreater(wall, 0)

    def test_the_service_runs_windowless_from_the_agent_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            python = Path(tmp, "python.exe")
            python.write_text("")
            with mock.patch.object(agent.sys, "executable", str(python)):
                home = Path(tmp, "runner")
                self.assertEqual(agent.windows_service_command(home)[0], str(python))
                Path(tmp, "pythonw.exe").write_text("")
                command = agent.windows_service_command(home)
        self.assertEqual(command[0], str(Path(tmp, "pythonw.exe")))
        self.assertEqual(command[2:], ["run", "--dir", str(home), "--background"])
        self.assertTrue(command[1].endswith("shyni_transcode_agent.py"))

    def test_only_one_agent_runs_per_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertFalse(agent.agent_running(home))
            first = agent.acquire_instance_lock(home)
            self.assertIsNotNone(first)
            started = time.monotonic()
            self.assertIsNone(agent.acquire_instance_lock(home, wait=0.6))
            self.assertGreaterEqual(time.monotonic() - started, 0.5)
            self.assertTrue(agent.agent_running(home))
            agent.release_instance_lock(first)
            self.assertFalse(agent.agent_running(home))

    @unittest.skipIf(os.name == "nt", "POSIX flock")
    def test_a_folder_that_cannot_be_locked_still_runs_the_agent(self):
        # NFS without a lock daemon and some FUSE mounts refuse flock outright. That is not another
        # agent holding the folder, so the agent must start rather than wait and give up.
        import errno
        import fcntl
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                fcntl, "flock", side_effect=OSError(errno.ENOLCK, "No locks available")):
            started = time.monotonic()
            handle = agent.acquire_instance_lock(Path(tmp), wait=5)
            self.assertIsNotNone(handle)
            self.assertLess(time.monotonic() - started, 1)
            agent.release_instance_lock(handle)

    def test_other_commands_restart_or_stop_the_service_through_request_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.json").write_text("{}")
            runner = SimpleNamespace(running=True, restart_requested=False)
            (home / agent.RESTART_REQUEST).write_text("")
            agent._watch_requests(runner, home)
            self.assertTrue(runner.restart_requested)
            self.assertFalse((home / agent.RESTART_REQUEST).exists())
            runner = SimpleNamespace(running=True, restart_requested=False)
            (home / "config.json").unlink()          # uninstall 이 등록 정보를 지웠다
            agent._watch_requests(runner, home)
            self.assertFalse(runner.running or runner.restart_requested)

    def test_background_output_goes_to_a_rotated_log(self):
        saved = sys.stdout, sys.stderr
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / agent.AGENT_LOG).write_text("x" * 64)
            try:
                with mock.patch.object(agent, "AGENT_LOG_ROTATE_BYTES", 10):
                    agent._redirect_output_to_log(home)
                agent.log("에이전트 시작")
            finally:
                sys.stdout.close()
                sys.stdout, sys.stderr = saved
            self.assertEqual((home / (agent.AGENT_LOG + ".1")).read_text(), "x" * 64)
            self.assertIn("에이전트 시작", (home / agent.AGENT_LOG).read_text(encoding="utf-8"))

    def test_service_check_reads_only_what_the_new_agent_wrote(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, agent.AGENT_LOG)
            path.write_text("old run\n", encoding="utf-8")
            offset = path.stat().st_size
            with open(path, "a", encoding="utf-8") as out:
                out.write("에이전트 %s 시작: 서버=…\n" % agent.__version__)
            self.assertEqual(agent._read_log_from(path, offset), "에이전트 %s 시작: 서버=…\n" % agent.__version__)
            path.write_text("new\n", encoding="utf-8")       # 로그를 새로 시작해 offset 보다 짧다
            self.assertEqual(agent._read_log_from(path, offset), "new\n")


class InstallScriptTests(unittest.TestCase):
    """install.sh 가 OS 를 보고 Python 과 경로를 고르는지 — uname·cygpath·python 을 바꿔 끼워 본다."""

    def _run(self, uname: str):
        import shutil
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp, "bin")
            fake.mkdir()
            record = Path(tmp, "args.txt")
            scripts = {
                "uname": "#!/bin/sh\necho %s\n" % uname,
                "cygpath": "#!/bin/sh\nprintf 'W:%s\\n' \"$2\"\n",
                # Windows 용 Python 인지 묻는 검사를 통과하고, 실행되면 받은 인자를 적는다.
                "python": "#!/bin/sh\n[ \"$1\" = -c ] && exit 0\nprintf '%%s\\n' \"$@\" > %s\n" % record,
            }
            for name, body in scripts.items():
                Path(fake, name).write_text(body)
                Path(fake, name).chmod(0o755)
            script_dir = Path(tmp, "repo")
            script_dir.mkdir()
            here = Path(__file__).resolve().parent
            for name in ("install.sh", "shyni_transcode_agent.py"):
                shutil.copy(here / name, script_dir / name)
            home = Path(tmp, "home")
            env = {**os.environ, "PATH": "%s:%s" % (fake, os.environ["PATH"]), "HOME": str(home)}
            env.pop("PYTHON", None)
            if uname.startswith("Linux"):
                env["PYTHON"] = str(fake / "python")
            subprocess.run(["sh", str(script_dir / "install.sh"), "--url", "https://shyni.example",
                            "--token", "shr_x", "--tmp", "/d/scratch"], env=env, check=True, timeout=30)
            self.assertTrue((home / "shyni-transcoder" / "agent" / "shyni_transcode_agent.py").is_file())
            return record.read_text().splitlines(), home

    def test_git_bash_on_windows_hands_windows_paths_to_windows_python(self):
        args, home = self._run("MINGW64_NT-10.0-22631")
        folder = home / "shyni-transcoder"
        self.assertEqual(args, [
            "W:%s" % (folder / "agent" / "shyni_transcode_agent.py"), "install", "--dir", "W:%s" % folder,
            "--url", "https://shyni.example", "--token", "shr_x", "--tmp", "W:/d/scratch",
        ])

    def test_linux_passes_paths_through(self):
        args, home = self._run("Linux")
        folder = home / "shyni-transcoder"
        self.assertEqual(args, [
            str(folder / "agent" / "shyni_transcode_agent.py"), "install", "--dir", str(folder),
            "--url", "https://shyni.example", "--token", "shr_x", "--tmp", "/d/scratch",
        ])


if __name__ == "__main__":
    unittest.main()

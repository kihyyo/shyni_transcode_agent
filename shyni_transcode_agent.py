#!/usr/bin/env python3
"""Shyni 트랜스코딩 에이전트 — H/W 인코더가 있는 기기를 Shyni 서버의 원격 작업자로 만든다.

GitHub self-hosted runner 처럼 쓴다. Shyni 관리자 화면(설정 → 관리 → 트랜스코더)이 주는
명령을 이 기기에서 한 번 실행하면 등록되고, 그 뒤로는 에이전트가 서버에 **먼저** 연결해 변환
작업을 기다린다. 이 기기는 포트를 열지 않는다.

    shyni_transcode_agent.py install --url https://shyni.example --token shr_… [--dir DIR]
    shyni_transcode_agent.py run [--dir DIR]
    shyni_transcode_agent.py status | update | uninstall [--dir DIR]
    shyni_transcode_agent.py configure --rclone-remote NAME [--dir DIR]
    shyni_transcode_agent.py service install|uninstall [--dir DIR]

보안 경계
- 서버는 ffmpeg 명령을 보내지 않는다. 변환 사양(해상도·오디오·자막 등)만 보내고, 명령은
  이 에이전트가 자기 인코더에 맞춰 만든다. 입력은 http(s) 원본뿐이고 출력은 작업 폴더 안이다.
- ffmpeg 는 원본을 이 에이전트의 loopback 프록시(127.0.0.1)로 읽는다. 클라우드 자격은
  프록시가 붙이므로 ffmpeg 명령줄에 남지 않고, 만료되면 프록시가 서버에서 새로 받는다.
- rclone 리모트는 이 기기의 주인이 설치할 때 이름을 적어 준 것만 쓴다. 서버는 그 이름과
  경로만 보내고, 자격은 이 기기의 rclone 설정에 있는 것을 쓴다.
- 에이전트는 스스로 갱신되지 않는다. 새 버전은 이 기기에서 `update` 를 실행해 받는다.

파일 하나, 표준 라이브러리만 쓴다(Python 3.9+).
"""

from __future__ import annotations

import argparse
import base64
import glob
import hashlib
import http.client
import ipaddress
import json
import os
import platform
import re
import secrets
import select
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__version__ = "0.2.0"
PROTOCOL_VERSION = 1
# 이 파일이 공개된 곳. `update` 가 여기서 새 버전을 받는다.
SOURCE_URL = os.environ.get(
    "SHYNI_AGENT_SOURCE_URL",
    "https://raw.githubusercontent.com/kihyyo/shyni_transcode_agent/main/shyni_transcode_agent.py",
)
POLL_WAIT_SEC = 25
USER_AGENT = "shyni-transcode-agent/" + __version__
SERVICE_LABEL = "com.shyni.transcode-agent"
SYSTEMD_UNIT = "shyni-transcode-agent.service"
MIN_FREE_BYTES = 500 * 1024 * 1024
PIPE_CHUNK_BYTES = 256 * 1024

# 이 기기의 ffmpeg 로 H/W 인코딩이 안 될 때 받는 빌드 — Jellyfin 프로젝트의 공식 휴대용 빌드
# (VideoToolbox/NVENC/QSV/VAAPI 와 톤매핑 필터 포함). 버전과 체크섬을 고정한다.
FFMPEG_RELEASE = "v8.1.3-2"
FFMPEG_BASE_URL = "https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/" + FFMPEG_RELEASE
FFMPEG_BUILDS = {
    ("Darwin", "arm64"): (
        "jellyfin-ffmpeg_8.1.3-2_portable_macarm64-gpl.tar.xz",
        "d898cc819e7c4e9219d6410ddbdaada0838f14bec6ff89e283639915394cb43f",
    ),
    ("Darwin", "x86_64"): (
        "jellyfin-ffmpeg_8.1.3-2_portable_mac64-gpl.tar.xz",
        "d394180f1155f84857544a48a99869f5741ccc50c5a1f73d2797429a2b326448",
    ),
    ("Linux", "x86_64"): (
        "jellyfin-ffmpeg_8.1.3-2_portable_linux64-gpl.tar.xz",
        "1df84d04c4322902dba81c290508cdf3d89524d210b9fe03154bea6c4e3bfbb6",
    ),
    ("Linux", "aarch64"): (
        "jellyfin-ffmpeg_8.1.3-2_portable_linuxarm64-gpl.tar.xz",
        "6522585818059cc886363fb1dee86ac9b8579fec3b914337fb92974ef9eb9884",
    ),
    ("Windows", "AMD64"): (
        "jellyfin-ffmpeg_8.1.3-2_portable_win64-clang-gpl.zip",
        "b65dd5011a80b5abec08bd559489cf188217238c8e38b5bf553504c974f5694d",
    ),
}


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + message, flush=True)


class AgentError(Exception):
    pass


class Unauthorized(AgentError):
    pass


class JobGone(AgentError):
    """서버가 그 작업을 더는 모른다(끝났거나 취소됨)."""


class UploadTooLarge(AgentError):
    """업로드가 너무 커서 거절됐다(413). 다시 보내도 같으므로 재시도하지 않는다."""


PROXY_UPLOAD_HINT = (
    "서버 앞의 리버스 프록시가 업로드를 막았습니다(HTTP 413). nginx 라면 /transcode-agent/ "
    "위치에 client_max_body_size 64m; 를 넣으세요(README의 '리버스 프록시' 참고)."
)


# ══ 변환 파이프라인 ═══════════════════════════════════════════════════
# Shyni 서버의 app/services/transcoder.py 와 같은 ffmpeg 명령을 만들어야 한다 — 같은 원본이
# 서버에서 변환되든 이 기기에서 변환되든 재생기가 받는 결과가 같도록. 서버 저장소의
# tests/test_transcode_agent_parity.py 가 두 구현의 출력을 맞대어 검사한다.
MODE_REMUX = "remux"    # 비디오/오디오 copy — 컨테이너만 교체
MODE_AUDIO = "audio"    # 비디오 copy + 오디오 AAC
MODE_VIDEO = "video"    # 비디오 재인코딩(다운스케일/톤매핑)
MODE_MUSIC = "music"    # 오디오 전용 파일의 음질(kbps) 변환
VALID_MODES = {MODE_REMUX, MODE_AUDIO, MODE_VIDEO, MODE_MUSIC}
MUSIC_BITRATE_KBPS = (320, 256, 192, 128)
VIDEO_BITRATE_LADDER = [(2160, "16M"), (1440, "10M"), (1080, "6M"), (720, "3M"), (480, "1500k")]
VIDEO_RESOLUTION_WIDTHS = {360: 640, 480: 854, 576: 1024, 720: 1280, 1080: 1920, 1440: 2560, 2160: 3840}
TONEMAP_CHAIN = (
    "zscale=transfer=linear:npl=100,format=gbrpf32le,"
    "zscale=primaries=bt709,tonemap=tonemap=hable:desat=0,"
    "zscale=transfer=bt709:matrix=bt709:range=tv,format=yuv420p"
)
HW_ENCODER_CANDIDATES = ("h264_nvenc", "h264_vaapi", "h264_qsv", "h264_videotoolbox")
KNOWN_ENCODERS = (*HW_ENCODER_CANDIDATES, "libx264")
ENCODER_ALIASES = {
    "auto": "auto", "software": "libx264", "sw": "libx264", "libx264": "libx264",
    "nvenc": "h264_nvenc", "vaapi": "h264_vaapi", "qsv": "h264_qsv",
    "videotoolbox": "h264_videotoolbox",
}
DEFAULT_VAAPI_DEVICE = "/dev/dri/renderD128"
HW_PROBE_TIMEOUT_SEC = 20
FMP4_MOVFLAGS = "+frag_keyframe+empty_moov+default_base_moof"
HLS_SEGMENT_SEC = 6
HLS_SEGMENT_FMP4 = "fmp4"
HLS_SEGMENT_MPEGTS = "mpegts"
VALID_HLS_SEGMENT_FORMATS = {HLS_SEGMENT_FMP4, HLS_SEGMENT_MPEGTS}


def run_ffmpeg(ffmpeg: str, args: list, timeout: float = 15.0, env=None) -> tuple:
    try:
        done = subprocess.run(
            [ffmpeg, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    return done.returncode, done.stdout.decode(errors="replace")


def _accessible_device(path: str) -> bool:
    try:
        return stat.S_ISCHR(os.stat(path).st_mode) and os.access(path, os.R_OK | os.W_OK)
    except OSError:
        return False


def _nvidia_runtime_present() -> bool:
    return any(_accessible_device(path) for path in glob.glob("/dev/nvidia[0-9]*")) or (
        _accessible_device("/dev/dxg") and os.path.exists("/usr/lib/wsl/lib/libnvidia-encode.so.1")
    )


def _vaapi_devices() -> tuple:
    configured = os.environ.get("VAAPI_DEVICE")
    paths = [configured] if configured else [DEFAULT_VAAPI_DEVICE, *sorted(glob.glob("/dev/dri/renderD*"))]
    return tuple(path for path in dict.fromkeys(paths) if _accessible_device(path))


def _vaapi_drivers(device: str) -> tuple:
    override = os.environ.get("LIBVA_DRIVER_NAME")
    if override:
        return (override,)
    try:
        vendor = Path("/sys/class/drm", os.path.basename(device), "device/vendor").read_text().strip().lower()
    except OSError:
        vendor = ""
    # libva 기본값 iHD 는 구형 Intel GPU 에서 실패할 수 있다 — i965 도 시험한다.
    return (None, "iHD", "i965") if vendor == "0x8086" else (None,)


def encoder_environment(caps: dict):
    driver = caps.get("vaapi_driver")
    return {**os.environ, "LIBVA_DRIVER_NAME": driver} if driver else None


def _ordered_hw_encoder_candidates(preference: str) -> tuple:
    """이 기기에 실제로 보이는 장치 순서대로 시험할 인코더. 지정한 인코더도 실측을 통과해야 쓴다."""
    if preference == "libx264":
        return ()
    if preference != "auto":
        return (preference,)
    system = platform.system()
    if system == "Darwin":
        return ("h264_videotoolbox",)
    if system == "Windows":
        return ("h264_nvenc", "h264_qsv")
    if system != "Linux":
        return ()
    ordered: list = []
    if _nvidia_runtime_present():
        ordered.append("h264_nvenc")
    if _vaapi_devices():
        ordered.extend(("h264_vaapi", "h264_qsv"))
    return tuple(ordered)


def _fmp4_has_avc_config(path: str) -> bool:
    """fMP4 초기화 구간(moov)에 디코더 설정(avcC: SPS/PPS)이 실제로 들어 있는지 확인."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return False
    offset = 0
    while offset + 8 <= len(data):
        size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        if size == 1 and offset + 16 <= len(data):
            size = int.from_bytes(data[offset + 8:offset + 16], "big")
        elif size == 0:
            size = len(data) - offset
        if size < 8:
            return False
        if kind == b"moov":
            moov = data[offset:offset + size]
            at = moov.find(b"avcC")
            if at < 4:
                return False
            body = moov[at + 4:at - 4 + int.from_bytes(moov[at - 4:at], "big")]
            return len(body) >= 7 and body[0] == 1 and body[5] & 0x1F >= 1
        offset += size
    return False


def _smoke_test_encoder(ffmpeg: str, encoder: str, vaapi_device=None, vaapi_driver=None) -> bool:
    """작은 시험 인코딩으로 이 인코더가 재생 가능한 fMP4 를 실제로 만드는지 확인한다.

    ffmpeg 의 인코더 목록은 빌드에 포함된 래퍼일 뿐이라, 드라이버·장치가 없어도 목록에는
    나온다. 종료 코드만으로도 부족하다(일부 VAAPI 드라이버는 성공하고도 avcC 를 비운다).
    """
    args = ["-v", "warning", "-f", "lavfi", "-i", "nullsrc=s=256x144:d=0.3:r=10"]
    if encoder == "h264_vaapi":
        device = vaapi_device or DEFAULT_VAAPI_DEVICE
        if not _accessible_device(device):
            return False
        args = ["-v", "warning", "-vaapi_device", device] + args[2:]
        args += ["-vf", "format=nv12,hwupload"]
    else:
        args += ["-vf", "format=nv12"]
    args += ["-c:v", encoder]
    if encoder == "h264_videotoolbox":
        # VideoToolbox 의 소프트웨어 구현은 H/W 로 치지 않는다.
        args += ["-allow_sw", "0", "-realtime", "1", "-b:v", "1M"]
    with tempfile.TemporaryDirectory(prefix="shyni-encoder-probe-") as out_dir:
        output_path = os.path.join(out_dir, "probe.mp4")
        args += ["-frames:v", "3", "-movflags", FMP4_MOVFLAGS, "-f", "mp4", "-y", output_path]
        code, output = run_ffmpeg(ffmpeg, args, env=encoder_environment({"vaapi_driver": vaapi_driver}))
        passed = code == 0 and _fmp4_has_avc_config(output_path)
    if not passed:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        detail = " | ".join(dict.fromkeys(lines[:2] + lines[-2:]))[:400] or "exit=%s" % code
        log("인코더 시험 실패: %s: %s" % (encoder, detail))
    return passed


def detect_capabilities(ffmpeg: str, preference: str = "auto") -> dict:
    """이 ffmpeg 로 쓸 수 있는 인코더를 실측한다.

    {"video_encoder", "hw_encoder", "tonemap", "vaapi_device", "vaapi_driver", "ffmpeg_version"}.
    video_encoder 가 None 이면 이 ffmpeg 로는 비디오 변환을 못 한다.
    """
    caps = {
        "video_encoder": None, "hw_encoder": None, "tonemap": False,
        "vaapi_device": None, "vaapi_driver": None, "ffmpeg_version": "",
    }
    try:
        code, version_out = run_ffmpeg(ffmpeg, ["-version"], timeout=10)
        words = version_out.split()
        caps["ffmpeg_version"] = words[2] if code == 0 and len(words) > 2 else ""
        code, encoders_out = run_ffmpeg(ffmpeg, ["-hide_banner", "-encoders"])
        available = {
            name for name in KNOWN_ENCODERS if code == 0 and (" %s " % name) in encoders_out
        }
        if "libx264" in available:
            caps["video_encoder"] = "libx264"
        deadline = time.monotonic() + HW_PROBE_TIMEOUT_SEC
        for encoder in _ordered_hw_encoder_candidates(preference):
            if encoder not in available or caps["hw_encoder"]:
                continue
            attempts = (
                [(device, driver) for device in _vaapi_devices() for driver in _vaapi_drivers(device)]
                if encoder == "h264_vaapi" else [(None, None)]
            )
            for device, driver in attempts:
                if time.monotonic() > deadline:
                    break
                if _smoke_test_encoder(ffmpeg, encoder, device, driver):
                    caps.update(video_encoder=encoder, hw_encoder=encoder,
                                vaapi_device=device, vaapi_driver=driver)
                    break
        code, filters_out = run_ffmpeg(ffmpeg, ["-hide_banner", "-filters"])
        caps["tonemap"] = code == 0 and " zscale " in filters_out and " tonemap " in filters_out
    except OSError:
        pass        # ffmpeg 를 실행할 수 없다 — 전부 불가
    return caps


def _performance_cores() -> int:
    """변환에 쓸 만한 코어 수. Apple Silicon 은 효율 코어를 빼고 고성능 코어만 센다."""
    if platform.system() == "Darwin":
        out = _command_output(["sysctl", "-n", "hw.perflevel0.physicalcpu"], timeout=3)
        if out.isdigit() and int(out) > 0:
            return int(out)
    return max(1, (os.cpu_count() or 1) // 2)


def recommended_slots(caps: dict) -> int:
    """이 기기에서 동시에 돌릴 만한 변환 수.

    H/W 인코더는 인코딩이 가볍고 디코드·스케일만 CPU 몫이라 코어 수에 2를 더한다
    (M1 = 고성능 4코어 → 6). S/W(libx264)는 1080p 실시간 한 편에 물리 2코어쯤 쓴다고 본다.
    """
    cores = _performance_cores()
    if caps.get("hw_encoder"):
        return max(2, min(cores + 2, 12))
    return max(1, min(cores // 2, 4))


def _video_bitrate_for(height) -> str:
    wanted = height or 1080
    for tier, rate in VIDEO_BITRATE_LADDER:
        if wanted >= tier:
            return rate
    return VIDEO_BITRATE_LADDER[-1][1]


def aac_output_channels(source_channels, max_output_channels=None) -> int:
    source = source_channels if source_channels and source_channels > 0 else 2
    client_limit = max_output_channels if max_output_channels is not None and max_output_channels > 0 else 6
    return max(1, min(source, 6, client_limit))


def resolve_hls_segment_format(segment_format: str = HLS_SEGMENT_MPEGTS, subtitle: bool = False) -> str:
    normalized = str(segment_format or "").strip().lower()
    if normalized not in VALID_HLS_SEGMENT_FORMATS:
        raise ValueError("segment_format must be one of %s" % sorted(VALID_HLS_SEGMENT_FORMATS))
    return HLS_SEGMENT_MPEGTS if subtitle else normalized


def build_transcode_args(
    ffmpeg: str, source: str, mode: str, *,
    start_ms: int = 0, audio_channels=None, audio_output_max_channels=None,
    audio_compatible: bool = False, max_height=None, hdr: bool = False,
    dolby_vision: bool = False, video_codec=None, video_encoder: str = "libx264",
    vaapi_device=None, audio_stream_index=None, audio_language=None, audio_title=None,
) -> list:
    """progressive fMP4(표준 출력) 변환 명령. 업스케일은 하지 않고, HDR 은 SDR 로 톤매핑한다."""
    if mode not in VALID_MODES:
        raise ValueError("unknown transcode mode: %s" % mode)
    args = [ffmpeg, "-v", "error", "-nostdin"]
    if mode == MODE_VIDEO and video_encoder == "h264_vaapi" and vaapi_device:
        args += ["-vaapi_device", vaapi_device]
    if start_ms and start_ms > 0:
        args += ["-ss", "%.3f" % (start_ms / 1000)]
    audio_map = "0:%s?" % audio_stream_index if audio_stream_index is not None else "0:a:0?"
    args += ["-i", source, "-map", "0:v:0", "-map", audio_map]

    if mode == MODE_VIDEO:
        filters: list = []
        if max_height:
            max_width = VIDEO_RESOLUTION_WIDTHS.get(max_height, max_height * 16 // 9)
            filters.append(
                "scale=w='min(iw,%d)':h='min(ih,%d)':"
                "force_original_aspect_ratio=decrease:force_divisible_by=2" % (max_width, max_height)
            )
        if hdr:
            filters.append(TONEMAP_CHAIN)
        if video_encoder == "h264_vaapi":
            filters.append("format=nv12,hwupload")
        elif video_encoder == "h264_videotoolbox":
            filters.append("format=nv12")
        if filters:
            args += ["-vf", ",".join(filters)]
        bitrate = _video_bitrate_for(max_height)
        args += ["-c:v", video_encoder]
        if video_encoder == "libx264":
            args += ["-preset", "superfast", "-crf", "23"]
        elif video_encoder == "h264_nvenc":
            args += ["-preset", "p4", "-cq", "23"]
        elif video_encoder == "h264_qsv":
            args += ["-global_quality", "23"]
        elif video_encoder == "h264_vaapi":
            args += ["-qp", "23"]
        elif video_encoder == "h264_videotoolbox":
            args += ["-allow_sw", "0", "-realtime", "1"]
        args += ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate]
    else:
        args += ["-c:v", "copy"]
        if dolby_vision and mode in (MODE_REMUX, MODE_AUDIO):
            args += ["-strict", "unofficial"]
            if str(video_codec or "").strip().lower() in {"hevc", "h265", "hev1", "hvc1"}:
                args += ["-tag:v", "hvc1"]

    audio_copy = mode == MODE_REMUX or (mode == MODE_VIDEO and audio_compatible)
    if audio_copy:
        args += ["-c:a", "copy"]
    else:
        channels = aac_output_channels(audio_channels, audio_output_max_channels)
        args += [
            "-c:a", "aac", "-profile:a", "aac_low",
            "-b:a", "384k" if channels > 2 else "192k", "-ac", str(channels),
        ]
    if audio_language:
        args += ["-metadata:s:a:0", "language=%s" % audio_language]
    if audio_title:
        args += ["-metadata:s:a:0", "title=%s" % audio_title]
    args += [
        "-sn", "-dn",
        "-movflags", FMP4_MOVFLAGS + ("+delay_moov" if audio_copy else ""),
        "-max_muxing_queue_size", "1024",
        "-f", "mp4", "pipe:1",
    ]
    return args


def build_music_transcode_args(
    ffmpeg: str, source: str, bitrate_kbps: int, start_ms: int = 0, sample_rate_hz=None,
) -> list:
    if bitrate_kbps not in MUSIC_BITRATE_KBPS:
        raise ValueError("bitrate_kbps must be one of %s" % (MUSIC_BITRATE_KBPS,))
    output_sample_rate = max(8000, min(sample_rate_hz or 48000, 48000))
    args = [ffmpeg, "-v", "error", "-nostdin"]
    if start_ms and start_ms > 0:
        args += ["-ss", "%.3f" % (start_ms / 1000)]
    args += [
        "-i", source,
        "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-c:a", "aac", "-profile:a", "aac_low",
        "-b:a", "%dk" % bitrate_kbps, "-ac", "2",
        "-ar", str(output_sample_rate),
        "-movflags", FMP4_MOVFLAGS,
        "-frag_duration", "2000000",
        "-f", "mp4", "pipe:1",
    ]
    return args


def build_music_hls_args(
    ffmpeg: str, source: str, *, out_dir: str, start_ms: int = 0, audio_channels=None,
    audio_output_max_channels=None, audio_stream_index=None, audio_language=None,
    audio_title=None, bitrate_kbps: int = 192, sample_rate_hz=None,
    segment_format: str = HLS_SEGMENT_MPEGTS,
) -> list:
    """음악용 오디오 전용 AAC-LC HLS. 앨범 표지(첨부 비디오 스트림)를 시계로 쓰지 않게
    오디오 한 스트림만 고른다."""
    if bitrate_kbps not in MUSIC_BITRATE_KBPS:
        raise ValueError("bitrate_kbps must be one of %s" % (MUSIC_BITRATE_KBPS,))
    output_sample_rate = max(8000, min(sample_rate_hz or 48000, 48000))
    actual_segment_format = resolve_hls_segment_format(segment_format)
    args = [ffmpeg, "-v", "error", "-nostdin"]
    if start_ms and start_ms > 0:
        args += ["-ss", "%.3f" % (start_ms / 1000)]
    audio_map = "0:%s" % audio_stream_index if audio_stream_index is not None else "0:a:0"
    client_limit = max(1, min(audio_output_max_channels or 2, 2))
    channels = aac_output_channels(audio_channels, client_limit)
    args += [
        "-i", source,
        "-map", audio_map,
        "-vn", "-sn", "-dn",
        "-c:a", "aac", "-profile:a", "aac_low",
        "-b:a", "%dk" % bitrate_kbps,
        "-ac", str(channels),
        "-ar", str(output_sample_rate),
    ]
    if audio_language:
        args += ["-metadata:s:a:0", "language=%s" % audio_language]
    if audio_title:
        args += ["-metadata:s:a:0", "title=%s" % audio_title]
    args += [
        "-avoid_negative_ts", "make_zero",
        "-max_muxing_queue_size", "1024",
        "-f", "hls",
        "-hls_time", str(HLS_SEGMENT_SEC),
        "-hls_playlist_type", "event",
    ]
    if actual_segment_format == HLS_SEGMENT_MPEGTS:
        args += [
            "-muxpreload", "0",
            "-muxdelay", "0",
            "-hls_segment_filename", os.path.join(out_dir, "seg%05d.ts"),
            os.path.join(out_dir, "index.m3u8"),
        ]
    else:
        args += [
            "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", os.path.join(out_dir, "seg%05d.m4s"),
            os.path.join(out_dir, "index.m3u8"),
        ]
    return args


def build_hls_args(
    ffmpeg: str, source: str, mode: str, *, out_dir: str,
    start_ms: int = 0, audio_channels=None, audio_output_max_channels=None,
    audio_compatible: bool = False, max_height=None, hdr: bool = False,
    dolby_vision: bool = False, video_codec=None, video_encoder: str = "libx264",
    vaapi_device=None, audio_stream_index=None, audio_language=None, audio_title=None,
    subtitle=None, music_bitrate_kbps: int = 192, music_sample_rate_hz=None,
    segment_format: str = HLS_SEGMENT_MPEGTS,
) -> list:
    """HLS(event playlist) 변환 명령. 코덱·필터는 progressive 와 같고 출력 형식만 다르다."""
    if mode == MODE_MUSIC:
        return build_music_hls_args(
            ffmpeg, source, out_dir=out_dir, start_ms=start_ms, audio_channels=audio_channels,
            audio_output_max_channels=audio_output_max_channels,
            audio_stream_index=audio_stream_index, audio_language=audio_language,
            audio_title=audio_title, bitrate_kbps=music_bitrate_kbps,
            sample_rate_hz=music_sample_rate_hz, segment_format=segment_format,
        )
    actual_segment_format = resolve_hls_segment_format(segment_format, subtitle=bool(subtitle))
    base = build_transcode_args(
        ffmpeg, source, mode,
        start_ms=start_ms, audio_channels=audio_channels,
        audio_output_max_channels=audio_output_max_channels,
        audio_compatible=audio_compatible, max_height=max_height, hdr=hdr,
        dolby_vision=dolby_vision, video_codec=video_codec,
        video_encoder=video_encoder, vaapi_device=vaapi_device,
        audio_stream_index=audio_stream_index,
        audio_language=audio_language, audio_title=audio_title,
    )
    args = base[:base.index("-movflags")]
    if subtitle:
        # 자막을 같은 ffmpeg 세션의 WebVTT rendition 으로 넣어 A/V 와 한 시간축을 쓰게 한다.
        args = [value for value in args if value != "-sn"]
        first_map = args.index("-map")
        if subtitle.get("source") == "external":
            subtitle_input: list = []
            if start_ms and start_ms > 0:
                subtitle_input += ["-ss", "%.3f" % (start_ms / 1000)]
            subtitle_input += ["-i", str(subtitle["file_path"])]
            args[first_map:first_map] = subtitle_input
            subtitle_map = "1:0?"
        else:
            subtitle_map = "0:%d?" % int(subtitle["stream_index"])
        args += ["-map", subtitle_map, "-c:s", "webvtt"]
        if subtitle.get("lang_code"):
            args += ["-metadata:s:s:0", "language=%s" % subtitle["lang_code"]]
        if subtitle.get("title"):
            args += ["-metadata:s:s:0", "title=%s" % subtitle["title"]]
    if mode == MODE_VIDEO:
        args += ["-force_key_frames", "expr:gte(t,n_forced*%d)" % HLS_SEGMENT_SEC]
    args += [
        "-avoid_negative_ts", "make_zero",
        "-max_muxing_queue_size", "1024",
        "-f", "hls",
        "-hls_time", str(HLS_SEGMENT_SEC),
        "-hls_playlist_type", "event",
    ]
    if subtitle:
        args += [
            "-muxpreload", "0",
            "-muxdelay", "0",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", os.path.join(out_dir, "seg%05d.ts"),
            "-var_stream_map", "v:0,a:0,s:0,sgroup:subs",
            "-master_pl_name", "master.m3u8",
            os.path.join(out_dir, "index.m3u8"),
        ]
    elif actual_segment_format == HLS_SEGMENT_MPEGTS:
        args += [
            "-muxpreload", "0",
            "-muxdelay", "0",
            "-hls_segment_filename", os.path.join(out_dir, "seg%05d.ts"),
            os.path.join(out_dir, "index.m3u8"),
        ]
    else:
        args += [
            "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", os.path.join(out_dir, "seg%05d.m4s"),
            os.path.join(out_dir, "index.m3u8"),
        ]
    return args


# ══ 설정 ══════════════════════════════════════════════════════════════
def default_home() -> Path:
    here = Path(__file__).resolve().parent
    return here.parent if here.name == "agent" else Path.home() / "shyni-transcoder"


class Config:
    def __init__(self, home: Path):
        self.home = home
        self.path = home / "config.json"
        self.data: dict = {}
        if self.path.is_file():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        # 러너 자격이 들어 있다 — 만들 때부터 소유자만 읽게 한다.
        descriptor = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as out:
            json.dump(self.data, out, ensure_ascii=False, indent=2)
        os.replace(str(temp), str(self.path))

    @property
    def tmp_dir(self) -> Path:
        return Path(self.data.get("tmp_dir") or self.home / "tmp")

    @property
    def encoder(self) -> str:
        return ENCODER_ALIASES.get(str(self.data.get("encoder") or "auto"), "auto")


# ══ 서버 API ══════════════════════════════════════════════════════════
class Api:
    """서버로 나가는 요청. 스레드마다 연결 하나를 재사용한다(세그먼트 업로드가 잦다)."""

    def __init__(self, base_url: str, token):
        parsed = urllib.parse.urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise AgentError("서버 주소는 http:// 또는 https:// 로 시작해야 합니다: " + base_url)
        self.base_url = base_url.rstrip("/")
        self.scheme, self.host, self.port = parsed.scheme, parsed.hostname, parsed.port
        self.prefix = parsed.path.rstrip("/")
        self.token = token
        self._local = threading.local()

    def _connect(self, timeout: float):
        if self.scheme == "https":
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=timeout, context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def request(self, method: str, path: str, *, body=None, headers=None, timeout: float = 60.0):
        """(status, headers, bytes). 재사용한 연결이 끊겨 있었으면 새 연결로 한 번 더 보낸다."""
        send_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.token:
            send_headers["Authorization"] = "Bearer " + self.token
        send_headers.update(headers or {})
        last_error = None
        for attempt in range(2):
            conn = getattr(self._local, "conn", None)
            if conn is None:
                conn = self._local.conn = self._connect(timeout)
            try:
                conn.timeout = timeout
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
                if hasattr(body, "seek"):
                    body.seek(0)
                conn.request(method, self.prefix + path, body=body, headers=send_headers)
                response = conn.getresponse()
                data = response.read()
                if response.getheader("connection", "").lower() == "close":
                    self.close()
                return response.status, response.headers, data
            except (OSError, http.client.HTTPException) as exc:
                self.close()
                last_error = exc
                if attempt or isinstance(exc, socket.timeout):
                    break
        raise AgentError("서버 연결 실패: %s: %s" % (type(last_error).__name__, last_error))

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def call(self, method: str, path: str, payload=None, *, timeout: float = 60.0):
        body = headers = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers = {"Content-Type": "application/json"}
        status, _, data = self.request(method, path, body=body, headers=headers, timeout=timeout)
        return self.decode(status, data)

    @staticmethod
    def decode(status: int, data: bytes):
        if status == 401:
            raise Unauthorized(_detail(data) or "인증 실패")
        if status == 410:
            raise JobGone(_detail(data) or "작업 없음")
        if status == 413:
            # Shyni 가 거절했으면 JSON 설명이 오고, 앞단 프록시가 막았으면 HTML 오류 페이지가 온다.
            detail = _json_detail(data)
            raise UploadTooLarge("서버가 업로드를 거절했습니다(413): " + detail if detail else PROXY_UPLOAD_HINT)
        if status >= 400:
            raise AgentError("서버 오류 %d: %s" % (status, _detail(data)))
        return json.loads(data) if data else None


def _json_detail(data: bytes) -> str:
    try:
        return str(json.loads(data).get("detail") or "")[:300]
    except (ValueError, AttributeError):
        return ""


def _detail(data: bytes) -> str:
    return _json_detail(data) or data.decode(errors="replace")[:300]


# ══ 이 기기 정보 ══════════════════════════════════════════════════════
def _command_output(args: list, timeout: float = 5.0) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def machine_info() -> dict:
    system = platform.system()
    info = {
        "os": {"Darwin": "macOS"}.get(system, system),
        "os_version": platform.mac_ver()[0] if system == "Darwin" else platform.release(),
        "arch": platform.machine(),
        "hostname": socket.gethostname().split(".")[0],
        "python": platform.python_version(),
    }
    if system == "Darwin":
        info["cpu"] = _command_output(["sysctl", "-n", "machdep.cpu.brand_string"])
        info["gpu"] = info["cpu"] if platform.machine() == "arm64" else ""
    elif system == "Linux":
        try:
            for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
                if line.lower().startswith(("model name", "hardware")):
                    info["cpu"] = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
        nvidia = _command_output(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
        info["gpu"] = nvidia.splitlines()[0] if nvidia else ""
    else:
        info["cpu"] = platform.processor()
    return {key: value for key, value in info.items() if value}


# ══ ffmpeg 준비 ═══════════════════════════════════════════════════════
def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, target: Path, redirects: int = 5) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise AgentError("https 가 아닌 주소에서는 받지 않습니다: " + url)
    conn = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=60,
                                       context=ssl.create_default_context())
    try:
        conn.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""),
                     headers={"User-Agent": USER_AGENT})
        response = conn.getresponse()
        if response.status in (301, 302, 303, 307, 308) and redirects > 0:
            location = urllib.parse.urljoin(url, response.getheader("location") or "")
            response.read()
            return _download(location, target, redirects - 1)
        if response.status != 200:
            raise AgentError("HTTP %d: %s" % (response.status, url))
        with open(target, "wb") as out:
            shutil.copyfileobj(response, out, 1024 * 1024)
    finally:
        conn.close()


def download_ffmpeg(home: Path):
    """이 기기 종류에 맞는 ffmpeg 를 <폴더>/ffmpeg 에 받는다. 받을 수 없으면 None."""
    build = FFMPEG_BUILDS.get((platform.system(), platform.machine()))
    if build is None:
        log("이 플랫폼(%s %s)용 자동 설치 빌드가 없습니다." % (platform.system(), platform.machine()))
        return None
    name, expected = build
    target_dir = home / "ffmpeg"
    binary = target_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    marker = target_dir / "release.txt"
    if binary.is_file() and marker.is_file() and marker.read_text().strip() == name:
        return str(binary)
    target_dir.mkdir(parents=True, exist_ok=True)
    archive = target_dir / name
    url = FFMPEG_BASE_URL + "/" + name
    log("이 기기에 맞는 ffmpeg 를 받는 중: " + url)
    try:
        _download(url, archive)
    except (OSError, AgentError, http.client.HTTPException) as exc:
        log("ffmpeg 내려받기 실패: %s" % exc)
        return None
    actual = _sha256_file(archive)
    if actual != expected:
        archive.unlink()
        raise AgentError("받은 ffmpeg 의 체크섬이 다릅니다 — 설치를 중단합니다 (%s)" % actual)
    wanted = ("ffmpeg.exe", "ffprobe.exe") if name.endswith(".zip") else ("ffmpeg", "ffprobe")
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                if Path(member).name in wanted:
                    (target_dir / Path(member).name).write_bytes(bundle.read(member))
    else:
        with tarfile.open(archive) as bundle:
            for member in bundle.getmembers():
                if member.isfile() and Path(member.name).name in wanted:
                    with bundle.extractfile(member) as source:
                        (target_dir / Path(member.name).name).write_bytes(source.read())
    archive.unlink()
    if not binary.is_file():
        raise AgentError("받은 묶음에 ffmpeg 실행 파일이 없습니다")
    for tool in wanted:
        if (target_dir / tool).is_file():
            (target_dir / tool).chmod(0o755)
    marker.write_text(name + "\n")
    return str(binary)


def choose_ffmpeg(config: Config, explicit) -> tuple:
    """H/W 인코딩이 되는 ffmpeg 를 고른다: 지정한 것 → 이 기기에 이미 있는 것 → 내려받은 것.

    이미 있는 ffmpeg 로 H/W 가 되면 아무것도 받지 않는다. 안 되면 이 기기 종류에 맞는
    빌드를 받아 다시 시험하고, 그래도 H/W 가 안 되면 동작하는 것(S/W)을 쓴다.
    """
    preference = config.encoder
    if explicit:
        caps = detect_capabilities(explicit, preference)
        if not caps.get("video_encoder"):
            raise AgentError("지정한 ffmpeg 를 쓸 수 없습니다: " + explicit)
        return explicit, caps

    def candidates():
        yield config.data.get("ffmpeg")
        yield shutil.which("ffmpeg")
        if preference != "libx264":
            yield download_ffmpeg(config.home)   # 앞의 것으로 H/W 가 안 될 때만 여기까지 온다

    working: list = []
    for path in candidates():
        if not path or not os.path.isfile(path) or any(path == done for done, _ in working):
            continue
        caps = detect_capabilities(path, preference)
        if not caps.get("video_encoder"):
            log("이 ffmpeg 는 쓸 수 없습니다: " + path)
            continue
        if caps.get("hw_encoder") or preference == "libx264":
            return path, caps
        working.append((path, caps))
    if not working:
        path = download_ffmpeg(config.home)
        caps = detect_capabilities(path, preference) if path else {}
        if not caps.get("video_encoder"):
            raise AgentError("쓸 수 있는 ffmpeg 가 없습니다. --ffmpeg 로 경로를 지정하세요.")
        return path, caps
    return working[0]


def hardware_hint(caps: dict, preference: str) -> str:
    if caps.get("hw_encoder") or preference == "libx264":
        return ""
    system = platform.system()
    if system == "Linux":
        return ("H/W 인코더를 찾지 못해 S/W 로 등록합니다. NVIDIA 는 드라이버가, Intel/AMD 는 "
                "/dev/dri 접근 권한(render 그룹)과 VAAPI 드라이버가 이 기기에 있어야 합니다. "
                "갖춘 뒤 에이전트를 다시 시작하면 다시 시험합니다.")
    if system == "Darwin":
        return "VideoToolbox H/W 인코딩이 통과하지 못해 S/W 로 등록합니다(가상 머신이거나 오래된 Mac)."
    return "H/W 인코더를 찾지 못해 S/W 로 등록합니다. GPU 드라이버를 확인하세요."


# ══ 받은 작업 사양 검증 ═══════════════════════════════════════════════
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_JOB_ID_RE = re.compile(r"[0-9a-f]{32}")


def _int(value, low: int, high: int, default=None):
    if value is None or isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if low <= number <= high else default


def _short(value, limit: int):
    if value is None:
        return None
    text = _CONTROL_RE.sub(" ", str(value)).strip()[:limit]
    return text or None


def clean_spec(raw: dict) -> dict:
    """서버가 보낸 사양에서 아는 항목만, 범위 안의 값으로 받는다."""
    if not isinstance(raw, dict):
        raise AgentError("변환 사양이 없습니다")
    mode = raw.get("mode")
    if mode not in VALID_MODES:
        raise AgentError("모르는 변환 모드입니다")
    segment_format = raw.get("segment_format") or HLS_SEGMENT_MPEGTS
    if segment_format not in VALID_HLS_SEGMENT_FORMATS:
        raise AgentError("모르는 세그먼트 형식입니다")
    bitrate = _int(raw.get("music_bitrate_kbps"), 1, 100000, 192)
    if bitrate not in MUSIC_BITRATE_KBPS:
        raise AgentError("모르는 음악 비트레이트입니다")
    subtitle = raw.get("subtitle")
    if subtitle is not None:
        if not isinstance(subtitle, dict) or subtitle.get("source") not in ("external", "embedded"):
            raise AgentError("자막 사양이 올바르지 않습니다")
        subtitle = {
            "source": subtitle["source"],
            "stream_index": _int(subtitle.get("stream_index"), 0, 1023),
            "lang_code": _short(subtitle.get("lang_code"), 32),
            "title": _short(subtitle.get("title"), 200),
        }
        if subtitle["source"] == "embedded" and subtitle["stream_index"] is None:
            raise AgentError("내부 자막의 스트림 번호가 없습니다")
    return {
        "mode": mode,
        "start_ms": _int(raw.get("start_ms"), 0, 10 ** 9, 0),
        "audio_channels": _int(raw.get("audio_channels"), 1, 64),
        "audio_output_max_channels": _int(raw.get("audio_output_max_channels"), 1, 64),
        "audio_compatible": raw.get("audio_compatible") is True,
        "max_height": _int(raw.get("max_height"), 144, 4320),
        "hdr": raw.get("hdr") is True,
        "dolby_vision": raw.get("dolby_vision") is True,
        "video_codec": _short(raw.get("video_codec"), 32),
        "audio_stream_index": _int(raw.get("audio_stream_index"), 0, 1023),
        "audio_language": _short(raw.get("audio_language"), 32),
        "audio_title": _short(raw.get("audio_title"), 200),
        "music_bitrate_kbps": bitrate,
        "music_sample_rate_hz": _int(raw.get("music_sample_rate_hz"), 8000, 768000),
        "segment_format": segment_format,
        "subtitle": subtitle,
    }


def clean_source(raw: dict, server_host: str, rclone_remotes=()) -> dict:
    """원본은 셋 중 하나다: 서버가 내주는 파일, http(s) 주소, 이 기기의 rclone 리모트 안의 경로.

    http(s) 주소는 서버 자신이 아닌 내부망·loopback 이면 거절한다 — 서버가 이 기기를 발판으로
    같은 망의 다른 장비에 요청을 보내게 두지 않는다. rclone 은 설치할 때 허용한 리모트만 받는다."""
    if not isinstance(raw, dict):
        raise AgentError("원본 정보가 없습니다")
    if raw.get("kind") == "server":
        return {"kind": "server"}
    if raw.get("kind") == "rclone":
        remote = str(raw.get("remote") or "")
        if remote not in rclone_remotes:
            raise AgentError("이 기기에 허용되지 않은 rclone 리모트입니다: %s" % remote[:64])
        folder_id = str(raw.get("folder_id") or "")
        if folder_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", folder_id):
            raise AgentError("rclone 폴더 ID 형식이 올바르지 않습니다")
        path = str(raw.get("path") or "")
        parts = path.split("/")
        if len(path) > 4096 or any(part in ("", ".", "..") or _CONTROL_RE.search(part) for part in parts):
            raise AgentError("rclone 원본 경로가 올바르지 않습니다")
        return {"kind": "rclone", "remote": remote, "folder_id": folder_id, "path": path}
    if raw.get("kind") != "url":
        raise AgentError("모르는 원본 종류입니다")
    url = str(raw.get("url") or "")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise AgentError("원본 주소가 http(s) 가 아닙니다")
    if parsed.hostname != server_host:
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, None)}
        except OSError as exc:
            raise AgentError("원본 주소를 찾을 수 없습니다: %s" % exc)
        for address in addresses:
            if not ipaddress.ip_address(address.split("%")[0]).is_global:
                raise AgentError("원본 주소가 공인 주소가 아닙니다")
    headers = {}
    for key, value in list((raw.get("headers") or {}).items())[:20]:
        key, value = str(key).strip(), str(value).strip()
        if key and not _CONTROL_RE.search(key) and not _CONTROL_RE.search(value):
            headers[key] = value
    return {"kind": "url", "url": url, "headers": headers}


# ══ rclone 리모트로 원본 읽기 ═════════════════════════════════════════
_REMOTE_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+@ -]{0,63}")


def rclone_list_remotes(binary: str, config_path) -> list:
    args = [binary, "listremotes"] + (["--config", config_path] if config_path else [])
    return [line.strip()[:-1] for line in _command_output(args, timeout=20).splitlines()
            if line.strip().endswith(":")]


def usable_rclone_remotes(data: dict) -> list:
    """설치할 때 허용한 리모트 가운데 지금 이 기기의 rclone 설정에 실제로 있는 것."""
    wanted = [name for name in data.get("rclone_remotes") or [] if _REMOTE_NAME_RE.fullmatch(str(name))]
    if not wanted:
        return []
    binary = data.get("rclone") or "rclone"
    if not (shutil.which(binary) or os.path.isfile(binary)):
        log("rclone 을 찾을 수 없어 rclone 리모트를 쓰지 않습니다: %s" % binary)
        return []
    present = set(rclone_list_remotes(binary, data.get("rclone_config")))
    missing = [name for name in wanted if name not in present]
    if missing:
        log("rclone 설정에 없는 리모트는 쓰지 않습니다: %s" % ", ".join(missing))
    return [name for name in wanted if name in present]


class RcloneServers:
    """이 기기의 rclone 으로 원본을 읽는다. 리모트(와 루트 폴더)마다 `rclone serve http` 를
    127.0.0.1 에 하나 띄워 두고 계속 쓴다 — 폴더 목록이 rclone 안에 남아 다음 작업이 빠르다.

    자격은 이 기기의 rclone 설정에 있다. 구글 공유 드라이브처럼 조회용 토큰과 읽기용 자격이
    다른 리모트도 rclone 이 알아서 처리하므로, 서버는 리모트 이름과 경로만 알려 주면 된다.
    """

    def __init__(self, binary: str, config_path, log_dir: Path):
        self.binary, self.config_path, self.log_dir = binary, config_path, log_dir
        self._servers: dict = {}
        self._lock = threading.Lock()

    def upstream(self, source: dict, fresh: bool = False) -> tuple:
        key = (source["remote"], source["folder_id"])
        with self._lock:
            server = self._servers.get(key)
            if server is not None and (fresh or server["proc"].poll() is not None):
                self._stop(server)
                server = None
            if server is None:
                server = self._servers[key] = self._start(*key)
        url = "http://127.0.0.1:%d/%s" % (server["port"], urllib.parse.quote(source["path"]))
        return url, {"Authorization": server["authorization"]}

    def _start(self, remote: str, folder_id: str) -> dict:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        password = secrets.token_hex(16)
        target = "%s:{%s}" % (remote, folder_id) if folder_id else remote + ":"
        args = [self.binary, "serve", "http", target, "--addr", "127.0.0.1:%d" % port,
                "--read-only", "--vfs-cache-mode", "off", "--dir-cache-time", "30m", "--poll-interval", "0"]
        if self.config_path:
            args += ["--config", self.config_path]
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / ("rclone-%s.log" % re.sub(r"[^A-Za-z0-9_.-]", "_", remote))
        with open(log_path, "wb") as errors:
            # 접속 암호는 환경변수로 넘겨 명령줄(ps)에 남기지 않는다.
            proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                env={**os.environ, "RCLONE_USER": "shyni", "RCLONE_PASS": password},
            )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                log("rclone serve http 시작: %s (포트 %d)" % (target, port))
                token = "%s:%s" % ("shyni", password)
                return {"proc": proc, "port": port,
                        "authorization": "Basic " + base64.b64encode(token.encode()).decode()}
            except OSError:
                time.sleep(0.2)
        try:
            proc.kill()
        except OSError:
            pass
        try:
            detail = log_path.read_text(errors="replace").strip().splitlines()[-1][:300]
        except (OSError, IndexError):
            detail = ""
        raise AgentError("rclone 을 시작하지 못했습니다(%s): %s" % (target, detail))

    @staticmethod
    def _stop(server: dict) -> None:
        try:
            server["proc"].terminate()
            server["proc"].wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def close(self) -> None:
        with self._lock:
            for server in self._servers.values():
                self._stop(server)
            self._servers.clear()


# ══ 원본 loopback 프록시 ══════════════════════════════════════════════
class SourceProxy:
    """ffmpeg ↔ 원본 사이의 127.0.0.1 프록시.

    ffmpeg 는 http://127.0.0.1:포트/작업/난수/media 만 안다. 프록시가 실제 주소와 자격을 붙여
    Range 요청을 넘기고, 도중에 끊기면 그 자리부터 이어 받고, 자격이 만료되면(401/403)
    서버에서 새로 받아 다시 요청한다. 긴 영화도 한 시간짜리 토큰에 묶이지 않는다.
    """

    def __init__(self, api: Api, rclone=None):
        self.api = api
        self.rclone = rclone
        self.jobs: dict = {}
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                proxy.handle(self, body=True)

            def do_HEAD(self) -> None:
                proxy.handle(self, body=False)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, name="source-proxy", daemon=True).start()

    def url_for(self, job, what: str) -> str:
        return "http://127.0.0.1:%d/%s/%s/%s" % (self.port, job.id, job.nonce, what)

    def _upstream(self, job, what: str, fresh: bool = False) -> tuple:
        authorization = {"Authorization": "Bearer " + self.api.token}
        if what == "subtitle":
            return self.api.base_url + "/transcode-agent/jobs/%s/subtitle" % job.id, authorization
        source = job.source
        if source["kind"] == "server":
            return self.api.base_url + "/transcode-agent/jobs/%s/source" % job.id, authorization
        if source["kind"] == "rclone":
            if self.rclone is None:
                raise AgentError("이 기기는 rclone 원본을 쓰도록 설정되지 않았습니다")
            return self.rclone.upstream(source, fresh)
        return source["url"], dict(source["headers"])

    def _open(self, url: str, headers: dict, redirects: int = 5):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(
                parsed.hostname, parsed.port, timeout=60, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=60)
        send = dict(headers)
        if not any(key.lower() == "user-agent" for key in send):
            # 115 처럼 발급할 때의 User-Agent 로만 열리는 주소가 있다 — 원본이 준 것을 그대로 쓴다.
            send["User-Agent"] = USER_AGENT
        conn.request("GET", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
                     headers=send)
        response = conn.getresponse()
        if response.status in (301, 302, 303, 307, 308) and redirects > 0:
            location = urllib.parse.urljoin(url, response.getheader("location") or "")
            response.read()
            conn.close()
            if urllib.parse.urlsplit(location).hostname != parsed.hostname:
                # 다른 호스트로 넘어가면 자격 헤더는 가져가지 않는다.
                headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
            return self._open(location, headers, redirects - 1)
        return conn, response

    def handle(self, request: BaseHTTPRequestHandler, body: bool) -> None:
        parts = request.path.strip("/").split("/")
        job = self.jobs.get(parts[0]) if len(parts) == 3 else None
        if job is None or parts[1] != job.nonce or parts[2] not in ("media", "subtitle"):
            request.send_error(404)
            return
        what = parts[2]
        range_header = request.headers.get("Range")
        match = re.match(r"bytes=(\d+)-(\d*)$", range_header or "")
        offset = int(match.group(1)) if match else 0
        end = match.group(2) if match else ""
        sent_headers = False
        refreshed = False
        failures = 0
        # 응답마다 연결을 닫고, 닫는다고 알린다. 알리지 않으면 ffmpeg 가 같은 연결로 다음 요청을
        # 보내려다(탐색) 실패하고, 파일 끝의 색인을 못 읽어 처음부터 훑게 된다.
        request.close_connection = True
        while True:
            conn = None
            try:
                url, headers = self._upstream(job, what, fresh=refreshed)
                if range_header or sent_headers:
                    headers["Range"] = "bytes=%d-%s" % (offset, end)
                conn, response = self._open(url, headers)
                renewable = what == "media" and not refreshed and not sent_headers
                if renewable and job.source["kind"] == "url" and response.status in (401, 403):
                    refreshed = True
                    response.read()
                    job.refresh_source()
                    continue
                if renewable and job.source["kind"] == "rclone" and response.status == 404:
                    # 방금 추가된 파일은 rclone 이 기억하는 폴더 목록에 없을 수 있다 — 새로 띄워 다시 본다.
                    refreshed = True
                    response.read()
                    continue
                if response.status >= 400 or (sent_headers and response.status != 206):
                    if not sent_headers:
                        request.send_response(response.status)
                        request.send_header("Content-Length", "0")
                        request.send_header("Connection", "close")
                        request.end_headers()
                    return
                if not sent_headers:
                    request.send_response(response.status)
                    for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                        value = response.getheader(name)
                        if value:
                            request.send_header(name, value)
                    request.send_header("Connection", "close")
                    request.end_headers()
                    sent_headers = True
                if not body:
                    return
                length = response.getheader("Content-Length")
                remaining = int(length) if length and length.isdigit() else None
                while True:
                    block = response.read(256 * 1024)
                    if not block:
                        if remaining:
                            # 약속한 길이보다 일찍 끊겼다 — 받은 자리부터 이어 받는다.
                            raise OSError("원본 연결이 %d바이트를 남기고 끊겼습니다" % remaining)
                        return
                    try:
                        request.wfile.write(block)
                    except OSError:
                        return          # ffmpeg 가 연결을 닫았다(탐색·종료)
                    offset += len(block)
                    if remaining is not None:
                        remaining -= len(block)
                    failures = 0
            except (OSError, http.client.HTTPException, AgentError) as exc:
                failures += 1
                if failures > 5 or job.stopped.is_set():
                    if not sent_headers:
                        try:
                            request.send_error(502)
                        except OSError:
                            pass
                    return
                log("job %s 원본 읽기 재시도(%d): %s" % (job.id[:8], failures, exc))
                time.sleep(min(2 ** failures, 15))
            finally:
                if conn is not None:
                    conn.close()


# ══ 작업 ══════════════════════════════════════════════════════════════
class Job(threading.Thread):
    def __init__(self, agent, command: dict):
        super().__init__(name="job-" + str(command.get("id"))[:8], daemon=True)
        self.agent = agent
        self.id = str(command.get("id") or "")
        if not _JOB_ID_RE.fullmatch(self.id):
            raise AgentError("작업 ID 형식이 올바르지 않습니다")
        self.kind = command.get("kind")
        if self.kind not in ("hls", "pipe"):
            raise AgentError("모르는 작업 종류입니다")
        self.spec = clean_spec(command.get("spec"))
        self.source = clean_source(
            command.get("source"), agent.api.host, getattr(agent, "rclone_remotes", ()))
        self.has_subtitle_file = command.get("has_subtitle_file") is True
        self.nonce = hashlib.sha256(os.urandom(32)).hexdigest()[:32]
        self.dir = agent.config.tmp_dir / ("job_" + self.id)
        self.proc = None
        self.stopped = threading.Event()
        self._uploaded: set = set()
        self._playlists: dict = {}
        self._source_lock = threading.Lock()

    # 서버가 보낸 명령
    def stop(self) -> None:
        self.stopped.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            self._signal(getattr(signal, "SIGCONT", None))
            try:
                proc.kill()
            except OSError:
                pass

    def pause(self, paused: bool) -> None:
        """재생 위치보다 너무 앞서 변환했으면 서버가 멈추라고 한다(0 CPU). 다시 깨우면 이어 만든다."""
        self._signal(getattr(signal, "SIGSTOP" if paused else "SIGCONT", None))

    def _signal(self, number) -> bool:
        proc = self.proc
        if number is None or proc is None or proc.poll() is not None:
            return False
        try:
            proc.send_signal(number)
            return True
        except OSError:
            return False

    def refresh_source(self) -> None:
        with self._source_lock:
            fresh = self.agent.api.call("POST", "/transcode-agent/jobs/%s/source/refresh" % self.id)
            self.source = clean_source({"kind": "url", **(fresh or {})}, self.agent.api.host)
            log("job %s 원본 자격 갱신" % self.id[:8])

    # 실행
    def _arguments(self) -> list:
        agent = self.agent
        caps = agent.caps
        spec = self.spec
        source_url = agent.proxy.url_for(self, "media")
        common = dict(
            start_ms=spec["start_ms"], audio_channels=spec["audio_channels"],
            audio_output_max_channels=spec["audio_output_max_channels"],
            audio_compatible=spec["audio_compatible"], max_height=spec["max_height"],
            hdr=spec["hdr"], dolby_vision=spec["dolby_vision"], video_codec=spec["video_codec"],
            video_encoder=caps.get("video_encoder") or "libx264",
            vaapi_device=caps.get("vaapi_device"),
            audio_stream_index=spec["audio_stream_index"],
            audio_language=spec["audio_language"], audio_title=spec["audio_title"],
        )
        if self.kind == "hls":
            subtitle = spec["subtitle"]
            if subtitle and subtitle["source"] == "external":
                if not self.has_subtitle_file:
                    raise AgentError("외부 자막 파일이 없는 작업입니다")
                subtitle = {**subtitle, "file_path": agent.proxy.url_for(self, "subtitle")}
            return build_hls_args(
                agent.ffmpeg, source_url, spec["mode"], out_dir=str(self.dir),
                subtitle=subtitle, music_bitrate_kbps=spec["music_bitrate_kbps"],
                music_sample_rate_hz=spec["music_sample_rate_hz"],
                segment_format=spec["segment_format"], **common,
            )
        if spec["mode"] == MODE_MUSIC:
            return build_music_transcode_args(
                agent.ffmpeg, source_url, spec["music_bitrate_kbps"],
                start_ms=spec["start_ms"], sample_rate_hz=spec["music_sample_rate_hz"],
            )
        return build_transcode_args(agent.ffmpeg, source_url, spec["mode"], **common)

    def run(self) -> None:
        agent = self.agent
        error = ""
        returncode = 1
        try:
            video = self.spec["mode"] == MODE_VIDEO
            if video and not agent.caps.get("video_encoder"):
                raise AgentError("이 기기에 비디오 인코더가 없습니다")
            if video and self.spec["hdr"] and not agent.caps.get("tonemap"):
                raise AgentError("이 기기의 ffmpeg 는 HDR 톤매핑을 지원하지 않습니다")
            self.dir.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(str(self.dir)).free < MIN_FREE_BYTES:
                raise AgentError("임시 폴더의 여유 공간이 부족합니다")
            agent.proxy.jobs[self.id] = self
            args = self._arguments()
            encoder = agent.caps.get("video_encoder") if video else None
            log("job %s 시작 kind=%s mode=%s encoder=%s height=%s start_ms=%s source=%s" % (
                self.id[:8], self.kind, self.spec["mode"], encoder,
                self.spec["max_height"], self.spec["start_ms"], self.source["kind"]))
            with open(self.dir / "ffmpeg.log", "wb") as errors:
                self.proc = subprocess.Popen(
                    args, cwd=str(self.dir), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE if self.kind == "pipe" else subprocess.DEVNULL,
                    stderr=errors, env=encoder_environment(agent.caps),
                )
            # 에이전트가 강제 종료되면 ffmpeg 가 남는다(멈춰 둔 것은 멈춘 채로). 다음 시작 때
            # 정리할 수 있게 PID 를 적어 둔다.
            (self.dir / "ffmpeg.pid").write_text(str(self.proc.pid))
            if self.stopped.is_set():
                self.stop()
            self._report("running", encoder=encoder)
            if self.kind == "pipe":
                self._pump_pipe()
            else:
                while self.proc.poll() is None and not self.stopped.is_set():
                    self._sync_outputs()
                    time.sleep(0.25)
            returncode = self.proc.wait()
            if self.kind == "hls" and returncode == 0 and not self.stopped.is_set():
                self._sync_outputs()         # 마지막 세그먼트와 ENDLIST
            if returncode != 0 and not self.stopped.is_set():
                error = self._ffmpeg_error() or "ffmpeg 실패"
        except JobGone:
            self.stopped.set()
        except Exception as exc:          # 작업 하나의 실패가 에이전트를 멈추면 안 된다
            error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            cancelled = self.stopped.is_set()     # 서버가 멈춘 작업은 결과를 보고하지 않는다
            self.stop()
            agent.proxy.jobs.pop(self.id, None)
            if not cancelled:
                try:
                    if error:
                        self._report("failed", returncode=returncode, error=error)
                    else:
                        self._report("finished")
                except AgentError:
                    pass
            shutil.rmtree(str(self.dir), ignore_errors=True)
            agent.jobs.pop(self.id, None)
            agent.api.close()
            log("job %s 종료 rc=%s%s%s" % (
                self.id[:8], returncode, " (서버가 중지)" if cancelled else "",
                " err=" + error if error else ""))

    def _ffmpeg_error(self) -> str:
        try:
            lines = (self.dir / "ffmpeg.log").read_text(errors="replace").strip().splitlines()
        except OSError:
            return ""
        return " | ".join(lines[-4:])[-600:]

    def _send(self, method: str, path: str, *, body, headers: dict, timeout: float, attempts: int = 5):
        """일시적인 연결 오류는 다시 시도한다. 서버가 작업을 모르면(410) 바로 그만둔다."""
        for attempt in range(attempts):
            try:
                status, _, reply = self.agent.api.request(
                    method, path, body=body, headers=headers, timeout=timeout)
                return Api.decode(status, reply)
            except (JobGone, Unauthorized, UploadTooLarge):
                raise
            except AgentError:
                if attempt == attempts - 1 or self.stopped.is_set():
                    raise
                time.sleep(1 + attempt)

    def _report(self, state: str, **fields) -> None:
        self._send(
            "POST", "/transcode-agent/jobs/%s/state" % self.id,
            body=json.dumps({"state": state, **fields}).encode(),
            headers={"Content-Type": "application/json"}, timeout=60, attempts=4)

    def _upload(self, name: str, data=None) -> None:
        path = "/transcode-agent/jobs/%s/files/%s" % (self.id, name)
        if data is not None:
            self._send("PUT", path, body=data, headers={"Content-Length": str(len(data))}, timeout=120)
            return
        local = self.dir / name
        with open(local, "rb") as body:
            self._send("PUT", path, body=body,
                       headers={"Content-Length": str(local.stat().st_size)}, timeout=300)

    def _sync_outputs(self) -> None:
        """ffmpeg 가 다 쓴 조각만 올린다. playlist 에 올라온 조각은 완성된 것이므로 조각을 먼저,
        그 조각을 가리키는 playlist 를 나중에 올린다 — 재생기가 없는 조각을 요청하지 않는다."""
        for name in ("index.m3u8", "index_vtt.m3u8"):
            try:
                text = (self.dir / name).read_bytes()
            except OSError:
                continue
            if self._playlists.get(name) == text:
                continue
            for line in text.decode(errors="replace").splitlines():
                line = line.strip()
                found = re.search(r'URI="([^"]+)"', line) if line.startswith("#") else None
                reference = found.group(1) if found else (line if line and not line.startswith("#") else None)
                if not reference or reference in self._uploaded:
                    continue
                if not re.fullmatch(r"[A-Za-z0-9_.]+", reference) or not (self.dir / reference).is_file():
                    continue
                self._upload(reference)
                self._uploaded.add(reference)
                if reference != "init.mp4":
                    # 서버가 사본을 가졌다 — 이 기기의 임시 폴더에는 아직 안 올린 조각만 남긴다.
                    try:
                        (self.dir / reference).unlink()
                    except OSError:
                        pass
            self._upload(name, text)
            self._playlists[name] = text
        master = self.dir / "master.m3u8"
        if "index.m3u8" in self._playlists and master.is_file():
            text = master.read_bytes()
            if self._playlists.get("master.m3u8") != text:
                self._upload("master.m3u8", text)
                self._playlists["master.m3u8"] = text

    def _pump_pipe(self) -> None:
        """progressive 출력: ffmpeg 표준 출력을 순서 번호를 붙여 서버로 보낸다. 서버는 클라이언트가
        받아 가는 만큼만 응답하므로, 여기서 기다리는 동안 ffmpeg 도 파이프에 막혀 기다린다."""
        descriptor = self.proc.stdout.fileno()
        can_select = os.name != "nt"
        sequence = 0
        while not self.stopped.is_set():
            block = os.read(descriptor, 65536)
            if not block:
                return
            chunks = [block]
            size = len(block)
            while can_select and size < PIPE_CHUNK_BYTES:
                if not select.select([descriptor], [], [], 0.05)[0]:
                    break
                block = os.read(descriptor, 65536)
                if not block:
                    break
                chunks.append(block)
                size += len(block)
            self._send(
                "POST", "/transcode-agent/jobs/%s/chunks/%d" % (self.id, sequence),
                body=b"".join(chunks), headers={"Content-Type": "application/octet-stream"},
                timeout=120)
            sequence += 1


# ══ 에이전트 ══════════════════════════════════════════════════════════
class Agent:
    def __init__(self, config: Config):
        self.config = config
        data = config.data
        self.api = Api(data["server_url"], data["token"])
        self.ffmpeg = data["ffmpeg"]
        self.caps = detect_capabilities(self.ffmpeg, config.encoder)
        self.info = machine_info()
        self.jobs: dict = {}
        self.rclone_remotes = usable_rclone_remotes(data)
        self.rclone = RcloneServers(
            data.get("rclone") or "rclone", data.get("rclone_config"), config.tmp_dir,
        ) if self.rclone_remotes else None
        self.proxy = SourceProxy(self.api, self.rclone)
        self.running = True
        self._told_protocol = False
        self._told_local_network = False

    def recommended_slots(self) -> int:
        limit = self.config.data.get("slots")
        recommended = recommended_slots(self.caps)
        return min(recommended, int(limit)) if limit else recommended

    def report(self) -> dict:
        tmp_dir = self.config.tmp_dir
        try:
            free = shutil.disk_usage(str(tmp_dir)).free
        except OSError:
            free = None
        return {
            "protocol": PROTOCOL_VERSION, "agent_version": __version__,
            "platform": self.info,
            "capabilities": public_capabilities(self.caps, self.rclone_remotes),
            "recommended_slots": self.recommended_slots(),
            "temp_dir": str(tmp_dir), "temp_free_bytes": free,
            "jobs": list(self.jobs), "wait": POLL_WAIT_SEC,
        }

    def handle(self, command: dict) -> None:
        kind = command.get("type")
        if kind == "start":
            payload = command.get("job") if isinstance(command.get("job"), dict) else {}
            try:
                limit = self.config.data.get("slots")
                if limit and len(self.jobs) >= int(limit):
                    # 설치할 때 정한 이 기기의 상한(--slots)은 서버 설정보다 우선한다.
                    raise AgentError("이 기기의 동시 작업 상한(%s)을 넘었습니다" % limit)
                job = Job(self, payload)
            except AgentError as exc:
                log("작업을 받을 수 없습니다: %s" % exc)
                job_id = str(payload.get("id") or "")
                if _JOB_ID_RE.fullmatch(job_id):
                    try:
                        self.api.call("POST", "/transcode-agent/jobs/%s/state" % job_id,
                                      {"state": "failed", "error": str(exc)})
                    except AgentError:
                        pass
                return
            self.jobs[job.id] = job
            job.start()
            return
        job = self.jobs.get(str(command.get("job_id")))
        if job is None:
            return
        if kind == "stop":
            job.stop()
        elif kind == "pause":
            job.pause(True)
        elif kind == "resume":
            job.pause(False)

    def run(self) -> int:
        if not self.caps.get("video_encoder"):
            raise AgentError("ffmpeg 를 쓸 수 없습니다: %s (install 을 다시 실행하세요)" % self.ffmpeg)
        tmp_dir = self.config.tmp_dir
        tmp_dir.mkdir(parents=True, exist_ok=True)
        for leftover in tmp_dir.glob("job_*"):      # 이전 실행이 남긴 작업 폴더와 ffmpeg
            kill_leftover_ffmpeg(leftover)
            shutil.rmtree(str(leftover), ignore_errors=True)
        log("에이전트 %s 시작: 서버=%s 인코더=%s(%s) 톤매핑=%s 추천 슬롯=%d 임시 폴더=%s" % (
            __version__, self.api.base_url, self.caps.get("video_encoder"),
            "H/W" if self.caps.get("hw_encoder") else "S/W", bool(self.caps.get("tonemap")),
            self.recommended_slots(), tmp_dir))
        if self.rclone_remotes:
            log("rclone 으로 직접 읽을 리모트: %s" % ", ".join(self.rclone_remotes))
        if self.api.scheme == "http":
            log("주의: 서버 주소가 http 입니다. 같은 내부망이 아니면 https 주소로 등록하세요.")
        try:
            self._serve()
        except KeyboardInterrupt:
            pass
        log("에이전트 종료 중")
        for job in list(self.jobs.values()):
            job.stop()
        for job in list(self.jobs.values()):
            job.join(timeout=5)
        if self.rclone is not None:
            self.rclone.close()
        return 0

    def _serve(self) -> None:
        delay = 2
        while self.running:
            try:
                reply = self.api.call("POST", "/transcode-agent/poll", self.report(),
                                      timeout=POLL_WAIT_SEC + 20)
                delay = 2
            except Unauthorized:
                log("서버가 이 러너를 모릅니다(등록 해제됨). 다시 install 하세요. 1분 뒤 재시도합니다.")
                self._sleep(60)
                continue
            except AgentError as exc:
                log("%s — %d초 뒤 다시 연결합니다" % (exc, delay))
                if ("No route to host" in str(exc) and platform.system() == "Darwin"
                        and not self._told_local_network):
                    self._told_local_network = True
                    log("내부망 서버에 닿지 못합니다. macOS 가 서비스의 로컬 네트워크 접근을 막았을 수 "
                        "있습니다: 시스템 설정 → 개인정보 보호 및 보안 → 로컬 네트워크에서 Python 을 켜세요.")
                self._sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if not reply:
                continue
            if reply.get("protocol") != PROTOCOL_VERSION:
                if not self._told_protocol:
                    self._told_protocol = True
                    log("서버와 에이전트의 프로토콜 버전이 다릅니다(서버 %s, 에이전트 %s). "
                        "이 기기에서 update 를 실행하거나 서버를 올리세요."
                        % (reply.get("protocol"), PROTOCOL_VERSION))
                self._sleep(60)
                continue
            for command in reply.get("commands") or []:
                if isinstance(command, dict):
                    self.handle(command)

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while self.running and time.monotonic() < deadline:
            time.sleep(0.5)

    def shutdown(self, *_args) -> None:
        # 주 스레드는 대개 poll 응답을 기다리며 막혀 있다 — 예외로 바로 빠져나온다.
        self.running = False
        raise KeyboardInterrupt


def kill_leftover_ffmpeg(job_dir: Path) -> None:
    """이전 실행이 남긴 작업의 ffmpeg 를 끝낸다. 그 PID 가 정말 그 작업의 ffmpeg 일 때만
    (명령줄에 작업 폴더가 있을 때만) 건드린다 — PID 는 다른 프로세스가 다시 쓸 수 있다."""
    if os.name == "nt":
        return
    try:
        pid = int((job_dir / "ffmpeg.pid").read_text().strip())
    except (OSError, ValueError):
        return
    if str(job_dir) not in _command_output(["ps", "-p", str(pid), "-o", "command="]):
        return
    for number in (signal.SIGCONT, signal.SIGKILL):
        try:
            os.kill(pid, number)
        except OSError:
            return
    log("이전 실행이 남긴 ffmpeg 정리: pid=%d" % pid)


def public_capabilities(caps: dict, rclone_remotes=()) -> dict:
    return {
        "video_encoder": caps.get("video_encoder"), "hw_encoder": caps.get("hw_encoder"),
        "tonemap": bool(caps.get("tonemap")), "ffmpeg_version": caps.get("ffmpeg_version"),
        # 이 기기의 rclone 으로 직접 읽을 수 있는 리모트 — 서버가 같은 이름의 원본을 맡길 때 쓴다.
        "rclone_remotes": list(rclone_remotes),
    }


# ══ 명령 ══════════════════════════════════════════════════════════════
def _describe(caps: dict, ffmpeg: str, slots: int, tmp_dir: Path, rclone_remotes=()) -> None:
    print("  인코더   : %s (%s)" % (caps.get("video_encoder"), "H/W" if caps.get("hw_encoder") else "S/W"))
    print("  톤매핑   : %s" % ("가능" if caps.get("tonemap") else "불가"))
    print("  ffmpeg   : %s (%s)" % (ffmpeg, caps.get("ffmpeg_version")))
    print("  추천 슬롯: %d" % slots)
    print("  임시 폴더: %s" % tmp_dir)
    print("  rclone   : %s" % (", ".join(rclone_remotes) if rclone_remotes else "쓰지 않음(원본은 서버나 클라우드 주소로 읽음)"))


def apply_rclone_options(config: Config, args) -> None:
    """--rclone-remote 로 적은 리모트를 이 기기의 rclone 설정에서 확인하고 허용 목록에 올린다."""
    if getattr(args, "rclone", None):
        config.data["rclone"] = args.rclone
    if getattr(args, "rclone_config", None):
        config.data["rclone_config"] = str(Path(args.rclone_config).expanduser().resolve())
    remotes = getattr(args, "rclone_remote", None)
    if remotes is None:
        return
    remotes = [name for name in dict.fromkeys(remotes) if name]
    for name in remotes:
        if not _REMOTE_NAME_RE.fullmatch(name):
            raise AgentError("rclone 리모트 이름이 올바르지 않습니다: %s" % name)
    if remotes:
        binary = config.data.get("rclone") or "rclone"
        if not (shutil.which(binary) or os.path.isfile(binary)):
            raise AgentError("rclone 을 찾을 수 없습니다: %s (--rclone 로 경로를 지정하세요)" % binary)
        present = rclone_list_remotes(binary, config.data.get("rclone_config"))
        missing = [name for name in remotes if name not in present]
        if missing:
            raise AgentError("이 기기의 rclone 설정에 없는 리모트입니다: %s (있는 것: %s)" % (
                ", ".join(missing), ", ".join(present) or "없음"))
    config.data["rclone_remotes"] = remotes


def command_install(args) -> int:
    home = Path(args.dir).expanduser().resolve()
    config = Config(home)
    if config.data.get("token") and not args.force:
        raise AgentError("이미 등록된 폴더입니다: %s (다시 등록하려면 --force)" % home)
    if not args.url or not args.token:
        raise AgentError("--url 과 --token 이 필요합니다")
    encoder = str(args.encoder or "auto").lower()
    if encoder not in ENCODER_ALIASES:
        raise AgentError("--encoder 는 %s 중 하나여야 합니다" % ", ".join(sorted(ENCODER_ALIASES)))
    api = Api(args.url, args.token)
    tmp_dir = Path(args.tmp).expanduser().resolve() if args.tmp else home / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    config.data.update({"server_url": api.base_url, "tmp_dir": str(tmp_dir), "encoder": encoder})
    if args.slots:
        config.data["slots"] = max(1, int(args.slots))
    apply_rclone_options(config, args)
    rclone_remotes = usable_rclone_remotes(config.data)
    ffmpeg, caps = choose_ffmpeg(config, args.ffmpeg)
    slots = recommended_slots(caps)
    if args.slots:
        slots = min(slots, max(1, int(args.slots)))
    try:
        free = shutil.disk_usage(str(tmp_dir)).free
    except OSError:
        free = None
    registered = api.call("POST", "/transcode-agent/register", {
        "name": args.name or socket.gethostname().split(".")[0],
        "protocol": PROTOCOL_VERSION, "agent_version": __version__,
        "platform": machine_info(), "capabilities": public_capabilities(caps, rclone_remotes),
        "recommended_slots": slots, "temp_dir": str(tmp_dir), "temp_free_bytes": free,
    })
    config.data.update({
        "runner_id": registered["runner_id"], "token": registered["token"],
        "name": registered["name"], "ffmpeg": ffmpeg,
    })
    config.save()
    print()
    print("등록 완료: %s (러너 #%s)" % (registered["name"], registered["runner_id"]))
    _describe(caps, ffmpeg, slots, tmp_dir, rclone_remotes)
    hint = hardware_hint(caps, config.encoder)
    if hint:
        print("  참고     : " + hint)
    if args.no_service:
        print("\n실행: %s %s run --dir %s" % (sys.executable, Path(__file__).resolve(), home))
        return 0
    if _external_volume(home) or _external_volume(config.tmp_dir):
        print("\n참고: 외장 디스크 폴더는 macOS 가 서비스(launchd)의 접근을 막습니다. "
              "전체 디스크 접근 권한에 %s 를 추가해야 서비스로 돕니다." % sys.executable)
    return command_service(argparse.Namespace(dir=str(home), action="install"))


def _require_config(args) -> Config:
    config = Config(Path(args.dir).expanduser().resolve())
    if not config.data.get("token"):
        raise AgentError("등록되지 않은 폴더입니다: %s (먼저 install)" % config.home)
    return config


def command_run(args) -> int:
    agent = Agent(_require_config(args))
    signal.signal(signal.SIGINT, agent.shutdown)
    signal.signal(signal.SIGTERM, agent.shutdown)
    return agent.run()


def command_status(args) -> int:
    config = _require_config(args)
    caps = detect_capabilities(config.data["ffmpeg"], config.encoder)
    print("에이전트 %s" % __version__)
    print("  서버     : %s" % config.data["server_url"])
    print("  러너     : %s (#%s)" % (config.data.get("name"), config.data.get("runner_id")))
    _describe(caps, config.data["ffmpeg"], recommended_slots(caps), config.tmp_dir,
              usable_rclone_remotes(config.data))
    return 0


def command_configure(args) -> int:
    """등록은 그대로 두고 이 기기의 설정만 바꾼다(rclone 리모트·동시 작업 상한)."""
    config = _require_config(args)
    apply_rclone_options(config, args)
    if args.slots is not None:
        if args.slots > 0:
            config.data["slots"] = int(args.slots)
        else:
            config.data.pop("slots", None)
    config.save()
    print("저장했습니다.")
    print("  rclone   : %s" % (", ".join(usable_rclone_remotes(config.data)) or "쓰지 않음"))
    print("  작업 상한: %s" % (config.data.get("slots") or "추천값"))
    _restart_service()
    return 0


def command_update(args) -> int:
    """공개 저장소에서 새 에이전트 파일을 받아 바꿔 끼운다. 받은 파일이 실행되는지 먼저 확인한다."""
    current = Path(__file__).resolve()
    staged = current.with_suffix(".new")
    _download(SOURCE_URL, staged)
    version = _command_output([sys.executable, str(staged), "--version"], timeout=20)
    if not version.startswith("shyni-transcode-agent "):
        staged.unlink()
        raise AgentError("받은 파일이 에이전트가 아닙니다: " + SOURCE_URL)
    os.replace(str(staged), str(current))
    print("%s → %s" % (__version__, version.split()[-1]))
    _restart_service()
    return 0


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / (SERVICE_LABEL + ".plist")


def _systemd_unit_path() -> Path:
    if os.geteuid() == 0:
        return Path("/etc/systemd/system") / SYSTEMD_UNIT
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def _systemctl(*arguments) -> int:
    scope = [] if os.geteuid() == 0 else ["--user"]
    return subprocess.call(["systemctl", *scope, *arguments])


def _restart_service() -> None:
    system = platform.system()
    if system == "Darwin" and _launch_agent_path().is_file():
        subprocess.call(["launchctl", "kickstart", "-k", "gui/%d/%s" % (os.getuid(), SERVICE_LABEL)])
    elif system == "Linux" and _systemd_unit_path().is_file():
        _systemctl("restart", SYSTEMD_UNIT)
    else:
        print("실행 중인 에이전트를 다시 시작하세요.")


def _macos_log_path() -> Path:
    # 로그는 늘 내장 디스크에 둔다. launchd 는 외장 디스크의 로그 파일을 열지 못하면 아무것도
    # 남기지 않고 실행 자체를 포기한다(EX_CONFIG) — 그러면 왜 안 도는지 알 길이 없다.
    return Path.home() / "Library" / "Logs" / "shyni-transcode-agent.log"


def _external_volume(path: Path) -> bool:
    return platform.system() == "Darwin" and str(path).startswith("/Volumes/")


def _permission_hint(path) -> str:
    return (
        "macOS 가 이 폴더 접근을 막았습니다: %s\n"
        "  launchd 서비스로 도는 프로그램은 외장 디스크(/Volumes/…)에 접근할 수 없습니다. 둘 중 하나를 하세요.\n"
        "  1) 시스템 설정 → 개인정보 보호 및 보안 → 전체 디스크 접근 권한에 %s 를 추가\n"
        "  2) 에이전트 폴더(--dir)와 임시 폴더(--tmp)를 내장 디스크로 옮기기"
    ) % (path, sys.executable)


def _macos_service_check(home: Path, log_path: Path) -> None:
    """서비스를 띄운 뒤 몇 초 기다려 실제로 도는지 확인하고, 안 돌면 이유를 알려 준다."""
    time.sleep(4)
    state = _command_output(["launchctl", "print", "gui/%d/%s" % (os.getuid(), SERVICE_LABEL)])
    try:
        tail = log_path.read_text(errors="replace")[-2000:]
    except OSError:
        tail = ""
    running = "state = running" in state
    if running and "에이전트 " in tail and "시작" in tail:
        print("서비스 동작 확인됨")
    elif "Operation not permitted" in tail or "접근을 막았습니다" in tail:
        print("\n서비스가 폴더에 접근하지 못했습니다:\n" + _permission_hint(home))
    elif running and not tail.strip() and _external_volume(home):
        # 앱 묶음인 Python 은 외장 디스크에 처음 접근할 때 macOS 가 묻는 창을 띄우고 기다린다.
        print("\nmacOS 가 'Python' 의 외장 디스크 접근을 묻는 창을 띄웠을 수 있습니다. 허용하면 바로 "
              "시작합니다. 거부했다면:\n" + _permission_hint(home))
    else:
        print("서비스가 아직 돌지 않습니다. 로그를 확인하세요: %s" % log_path)


def command_service(args) -> int:
    home = Path(args.dir).expanduser().resolve()
    command = [sys.executable, str(Path(__file__).resolve()), "run", "--dir", str(home)]
    system = platform.system()
    if system == "Darwin":
        path = _launch_agent_path()
        target = "gui/%d" % os.getuid()
        subprocess.call(["launchctl", "bootout", target + "/" + SERVICE_LABEL],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if args.action == "uninstall":
            if path.is_file():
                path.unlink()
            print("서비스 제거됨")
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        arguments = "".join("    <string>%s</string>\n" % _xml(item) for item in command)
        log_file = _macos_log_path()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_path = _xml(str(log_file))
        path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            "  <key>Label</key><string>%s</string>\n"
            "  <key>ProgramArguments</key><array>\n%s  </array>\n"
            "  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n"
            "  <key>ThrottleInterval</key><integer>30</integer>\n"
            "  <key>ProcessType</key><string>Interactive</string>\n"
            "  <key>StandardOutPath</key><string>%s</string>\n"
            "  <key>StandardErrorPath</key><string>%s</string>\n"
            "</dict></plist>\n" % (SERVICE_LABEL, arguments, log_path, log_path),
            encoding="utf-8",
        )
        code = subprocess.call(["launchctl", "bootstrap", target, str(path)])
        if code == 0:
            print("서비스 등록됨(로그인하면 자동 시작): %s" % path)
            print("로그: %s" % log_file)
            _macos_service_check(home, log_file)
        else:
            print("launchctl 등록 실패 — 직접 실행: " + " ".join(command))
        return code
    if system == "Linux" and shutil.which("systemctl"):
        path = _systemd_unit_path()
        if args.action == "uninstall":
            _systemctl("disable", "--now", SYSTEMD_UNIT)
            if path.is_file():
                path.unlink()
            _systemctl("daemon-reload")
            print("서비스 제거됨")
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[Unit]\nDescription=Shyni transcode agent\nAfter=network-online.target\n"
            "Wants=network-online.target\n\n[Service]\nExecStart=%s\nRestart=always\nRestartSec=5\n"
            "WorkingDirectory=%s\n\n[Install]\nWantedBy=%s\n" % (
                " ".join(_quote(item) for item in command), _quote(str(home)),
                "multi-user.target" if os.geteuid() == 0 else "default.target"),
            encoding="utf-8",
        )
        _systemctl("daemon-reload")
        code = _systemctl("enable", "--now", SYSTEMD_UNIT)
        if code == 0:
            print("서비스 등록됨: %s" % path)
            if os.geteuid() != 0:
                print("로그아웃 뒤에도 돌리려면: loginctl enable-linger %s" % os.environ.get("USER", ""))
        else:
            print("systemctl 등록 실패 — 직접 실행: " + " ".join(command))
        return code
    if args.action == "install":
        print("이 플랫폼은 서비스 자동 등록을 지원하지 않습니다. 직접 실행하세요:\n  " + " ".join(command))
    return 0


def _xml(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _quote(value: str) -> str:
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def command_uninstall(args) -> int:
    home = Path(args.dir).expanduser().resolve()
    command_service(argparse.Namespace(dir=str(home), action="uninstall"))
    config = Config(home)
    if config.path.is_file():
        config.path.unlink()
    print("이 기기의 등록 정보를 지웠습니다. 서버의 러너 목록에서도 삭제하세요.")
    print("남은 파일(에이전트·ffmpeg·임시 폴더)은 직접 지우면 됩니다: %s" % home)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Shyni 트랜스코딩 에이전트")
    parser.add_argument("--version", action="version", version="shyni-transcode-agent " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def with_dir(name: str, help_text: str):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--dir", default=str(default_home()), help="에이전트 폴더")
        return item

    def add_rclone_options(item) -> None:
        item.add_argument("--rclone-remote", action="append", metavar="NAME",
                          help="이 기기의 rclone 으로 직접 읽을 리모트 이름(여러 번 쓸 수 있음). "
                               "서버의 원본 리모트와 같은 이름이어야 한다")
        item.add_argument("--rclone", metavar="PATH", help="rclone 실행 파일(기본: PATH 의 rclone)")
        item.add_argument("--rclone-config", metavar="PATH", help="rclone 설정 파일(기본: rclone 기본 위치)")

    install = with_dir("install", "이 기기를 서버에 등록")
    install.add_argument("--url", help="Shyni 서버 주소")
    install.add_argument("--token", help="관리자 화면에서 발급한 등록 토큰")
    install.add_argument("--name", help="러너 이름(기본: 호스트 이름)")
    install.add_argument("--tmp", help="변환 임시 폴더(기본: <폴더>/tmp)")
    install.add_argument("--ffmpeg", help="쓸 ffmpeg 경로(기본: 자동 선택·설치)")
    install.add_argument("--encoder", help="auto(기본) | software | nvenc | vaapi | qsv | videotoolbox")
    install.add_argument("--slots", type=int, help="이 기기가 받을 동시 작업 상한")
    add_rclone_options(install)
    install.add_argument("--no-service", action="store_true", help="서비스로 등록하지 않음")
    install.add_argument("--force", action="store_true", help="이미 등록된 폴더에 다시 등록")
    install.set_defaults(handler=command_install)
    with_dir("run", "작업을 받아 실행").set_defaults(handler=command_run)
    with_dir("status", "이 기기의 인코더·설정 표시").set_defaults(handler=command_status)
    with_dir("update", "공개 저장소에서 새 에이전트 받기").set_defaults(handler=command_update)
    configure = with_dir("configure", "이 기기의 설정 바꾸기(rclone 리모트·작업 상한)")
    add_rclone_options(configure)
    configure.add_argument("--slots", type=int, help="동시 작업 상한(0 이면 추천값으로)")
    configure.set_defaults(handler=command_configure)
    with_dir("uninstall", "서비스와 등록 정보 제거").set_defaults(handler=command_uninstall)
    service = with_dir("service", "자동 시작 서비스 등록/제거")
    service.add_argument("action", choices=("install", "uninstall"))
    service.set_defaults(handler=command_service)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except AgentError as exc:
        print("오류: %s" % exc, file=sys.stderr)
        return 1
    except PermissionError as exc:
        print(time.strftime("%Y-%m-%d %H:%M:%S ") + _permission_hint(exc.filename or args.dir),
              file=sys.stderr, flush=True)
        if os.environ.get("XPC_SERVICE_NAME") == SERVICE_LABEL:
            time.sleep(300)    # launchd 가 곧바로 다시 띄워 로그를 채우지 않게
        return 1


if __name__ == "__main__":
    sys.exit(main())

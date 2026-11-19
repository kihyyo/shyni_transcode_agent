#!/usr/bin/env python3
"""Shyni 트랜스코딩 에이전트 — H/W 인코더가 있는 기기를 Shyni 서버의 원격 작업자로 만든다.

GitHub self-hosted runner 처럼 쓴다. Shyni 관리자 화면(설정 → 관리 → 트랜스코더)이 주는
명령을 이 기기에서 한 번 실행하면 등록되고, 그 뒤로는 에이전트가 서버에 **먼저** 연결해 변환
작업을 기다린다. 이 기기는 포트를 열지 않는다.

서버 여러 대에 같이 붙을 수 있다(서버마다 그 서버의 명령을 한 번씩 실행). 동시 작업 수는 이
에이전트가 센다 — 총 슬롯을 서버들이 나눠 갖지 않고, 그때그때 필요한 서버가 쓴다.

    shyni_transcode_agent.py install --url https://shyni.example --token shr_… [--dir DIR]
    shyni_transcode_agent.py run [--dir DIR]
    shyni_transcode_agent.py status | update | uninstall [--dir DIR]
    shyni_transcode_agent.py configure --rclone-remote NAME [--dir DIR]
    shyni_transcode_agent.py remove-server --url URL [--dir DIR]
    shyni_transcode_agent.py service install|uninstall [--dir DIR]

보안 경계
- 서버는 ffmpeg 명령을 보내지 않는다. 변환 사양(해상도·오디오·자막 등)만 보내고, 명령은
  이 에이전트가 자기 인코더에 맞춰 만든다. 입력은 http(s) 원본뿐이고 출력은 작업 폴더 안이다.
- ffmpeg 는 원본을 이 에이전트의 loopback 프록시(127.0.0.1)로 읽는다. 클라우드 자격은
  프록시가 붙이므로 ffmpeg 명령줄에 남지 않고, 만료되면 프록시가 서버에서 새로 받는다.
- rclone 리모트는 설치 명령(`--rclone-remote`)에 적힌 이름 가운데 이 기기의 rclone 설정에 있는
  것만 쓴다(서버 관리자 화면의 설치 명령에는 그 서버의 리모트 이름이 들어 있다). 서버는 그 이름과
  경로만 보내고, 자격은 이 기기의 rclone 설정에 있는 것을 쓴다.
- 에이전트는 스스로 갱신되지 않는다. 새 버전은 이 기기에서 `update` 를 실행해 받는다.

macOS·Linux·Windows 에서 같은 파일이 돈다. 설치는 이 기기의 OS 와 GPU 를 보고 인코더를 실측해
고르고, 로그인하면 자동으로 시작하게 등록한다(macOS LaunchAgent, Linux systemd, Windows 시작
프로그램).

파일 하나, 표준 라이브러리만 쓴다(Python 3.9+).
"""

from __future__ import annotations

import argparse
import base64
import errno
import glob
import hashlib
import http.client
import ipaddress
import json
import os
import platform
import queue
import re
import secrets
import select
import shutil
import signal
import socket
import ssl
import stat
import struct
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

__version__ = "0.6.1"
PROTOCOL_VERSION = 1
# 이 파일이 공개된 곳. `update` 가 여기서 새 버전을 받는다.
SOURCE_URL = os.environ.get(
    "SHYNI_AGENT_SOURCE_URL",
    "https://raw.githubusercontent.com/kihyyo/shyni_transcode_agent/main/shyni_transcode_agent.py",
)
POLL_WAIT_SEC = 25
# 서버가 이만큼 응답하지 않으면 그 서버가 맡긴 작업을 멈춘다. 서버도 러너의 poll 이 같은 시간
# 끊기면 작업을 실패로 끝내므로, 그 뒤의 결과는 받을 곳이 없다 — 슬롯만 잡고 있게 된다.
SERVER_LOST_SEC = 45
# 서버가 다시 켜지면 곧바로 붙는다. 상한이 30초일 때 서버를 재시작하면 30초 가까이 러너 없이 재생이 서버
# 자체 변환으로 갔다(2026-11-18 로그). 거부된 연결은 비용이 없다. 기록은 처음과 1분마다만 남긴다.
RECONNECT_MAX_SEC = 5
RECONNECT_LOG_EVERY_SEC = 60
USER_AGENT = "shyni-transcode-agent/" + __version__
SERVICE_LABEL = "com.shyni.transcode-agent"
SYSTEMD_UNIT = "shyni-transcode-agent.service"
# Windows 는 현재 사용자의 시작 프로그램(HKCU …\Run)에 등록한다. 관리자 권한이 필요 없고, 로그인해
# 있는 동안 돈다 — macOS LaunchAgent·Linux systemd --user 와 같은 범위다.
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_RUN_VALUE = "ShyniTranscodeAgent"
# 서비스(Windows)로 도는 에이전트에게 다른 명령(configure·update·uninstall)이 남기는 요청 파일.
RESTART_REQUEST = "restart.request"
STOP_REQUEST = "stop.request"
AGENT_LOG = "agent.log"
RUNNER_STATE_HEADER = "X-Shyni-Runner"
AGENT_LOG_ROTATE_BYTES = 10 * 1024 * 1024
WINDOWS = os.name == "nt"
# 콘솔 없이 도는 에이전트(Windows 의 pythonw)가 ffmpeg·rclone 을 띄울 때마다 콘솔 창이 번쩍이지
# 않게 한다(CREATE_NO_WINDOW). 다른 OS 에는 없는 옵션이다.
POPEN_FLAGS = {"creationflags": 0x08000000} if WINDOWS else {}
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


class RunnerRemoved(Unauthorized):
    """서버가 이 러너를 모른다고 분명히 알렸다(관리자가 러너 목록에서 지웠다). 앞단 프록시 등이 낸 401 과
    구별하려고 서버가 `X-Shyni-Runner: unknown` 머리를 붙인다(서버가 그 머리를 안 보내면 그냥 401)."""


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
HW_ENCODER_CANDIDATES = ("h264_nvenc", "h264_vaapi", "h264_qsv", "h264_amf", "h264_videotoolbox")
KNOWN_ENCODERS = (*HW_ENCODER_CANDIDATES, "libx264")
ENCODER_ALIASES = {
    "auto": "auto", "software": "libx264", "sw": "libx264", "libx264": "libx264",
    "nvenc": "h264_nvenc", "vaapi": "h264_vaapi", "qsv": "h264_qsv", "amf": "h264_amf",
    "videotoolbox": "h264_videotoolbox",
}
# Windows: 이 기기에 있는 GPU 제조사(레지스트리의 디스플레이 어댑터)에 맞는 인코더를 먼저 시험한다.
# 외장 그래픽(NVIDIA·AMD)이 내장(Intel)보다 앞이다.
WINDOWS_GPU_VENDOR_IDS = {"10de": "nvidia", "1002": "amd", "8086": "intel"}
WINDOWS_VENDOR_ENCODERS = (("nvidia", "h264_nvenc"), ("amd", "h264_amf"), ("intel", "h264_qsv"))
WINDOWS_DISPLAY_CLASS_KEY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
DEFAULT_VAAPI_DEVICE = "/dev/dri/renderD128"
HW_PROBE_TIMEOUT_SEC = 20
# 비디오 변환의 출력 코덱, 선호 순. H.264 가 기본이고, HEVC·AV1 은 이 기기의 H.264 H/W 인코더와 같은
# 계열의 인코더가 실측을 통과했을 때만 낸다(S/W HEVC·AV1 은 쓰지 않는다). 어느 코덱으로 낼지는
# 서버가 재생 기기를 보고 작업마다 정한다.
OUTPUT_CODECS = ("av1", "hevc", "h264")
HW_CODEC_ENCODERS = {
    "h264_nvenc": {"hevc": "hevc_nvenc", "av1": "av1_nvenc"},
    "h264_vaapi": {"hevc": "hevc_vaapi", "av1": "av1_vaapi"},
    "h264_qsv": {"hevc": "hevc_qsv", "av1": "av1_qsv"},
    "h264_amf": {"hevc": "hevc_amf", "av1": "av1_amf"},
    "h264_videotoolbox": {"hevc": "hevc_videotoolbox"},
}
# 같은 화질을 내는 비트레이트의 H.264 대비 비율(분자, 분모): HEVC 2/3, AV1 3/5.
VIDEO_BITRATE_SCALE = {"h264": (1, 1), "hevc": (2, 3), "av1": (3, 5)}
# fMP4 초기화 구간에서 그 코덱의 디코더 설정이 들어 있는 상자.
CODEC_CONFIG_BOXES = {"h264": b"avcC", "hevc": b"hvcC", "av1": b"av1C"}
# 하드웨어 디코딩: 코덱·비트 깊이별로 이 기기가 실제로 풀어 본 것만 하드웨어로 푼다. 못 푸는
# 형식에 -hwaccel 을 걸면 ffmpeg 은 소프트웨어로 넘어가지 않고 실패한다(M1 의 AV1). 서버의
# transcoder.py 와 같은 값이다(test_transcode_agent_parity).
HW_DECODE_PROBES = (
    ("h264", 8), ("hevc", 8), ("hevc", 10), ("vp9", 8), ("vp9", 10), ("av1", 8), ("av1", 10),
)
HW_DECODE_SAMPLE_ENCODERS = {
    "h264": (("libx264", ("-preset", "ultrafast")),),
    "hevc": (("libx265", ("-preset", "ultrafast", "-x265-params", "log-level=error")),),
    "vp9": (("libvpx-vp9", ("-deadline", "realtime", "-cpu-used", "8")),),
    "av1": (("libsvtav1", ("-preset", "12")), ("libaom-av1", ("-cpu-used", "8", "-usage", "realtime"))),
}
HW_DECODE_OUTPUT_FORMATS = {
    "videotoolbox": "videotoolbox_vld", "cuda": "cuda", "vaapi": "vaapi", "d3d11va": "d3d11",
}
HW_DECODE_PROBE_TIMEOUT_SEC = 60
# 실제 변환으로 시험해 소프트웨어 디코딩보다 CPU 를 이만큼 덜 쓰고, 이보다 느리지 않을 때만 쓴다.
# 프레임을 GPU 에서 내려받는 비용이 기기마다 크게 달라서다(M1 은 이득, Intel 내장 GPU 는 손해).
HW_DECODE_MAX_CPU_RATIO = 0.7
HW_DECODE_MAX_WALL_RATIO = 2.0
_VIDEO_CODEC_ALIASES = {
    "h265": "hevc", "hev1": "hevc", "hvc1": "hevc", "x265": "hevc",
    "avc": "h264", "avc1": "h264", "x264": "h264", "av01": "av1", "vp09": "vp9",
}
FMP4_MOVFLAGS = "+frag_keyframe+empty_moov+default_base_moof"
HLS_SEGMENT_SEC = 6
HLS_SEGMENT_FMP4 = "fmp4"
HLS_SEGMENT_MPEGTS = "mpegts"
VALID_HLS_SEGMENT_FORMATS = {HLS_SEGMENT_FMP4, HLS_SEGMENT_MPEGTS}


def encoder_codec(encoder) -> str:
    """인코더 이름 → 출력 코덱(hevc_nvenc → hevc, libx264 → h264)."""
    prefix = str(encoder or "").partition("_")[0]
    return prefix if prefix in ("hevc", "av1") else "h264"


def output_encoders(caps: dict) -> dict:
    """이 기기가 낼 수 있는 {출력 코덱: 인코더}. 아는 코덱·아는 인코더만 남긴다."""
    found = {}
    if caps.get("video_encoder"):
        found["h264"] = caps["video_encoder"]
    reported = caps.get("video_encoders")
    for codec in ("hevc", "av1"):
        encoder = reported.get(codec) if isinstance(reported, dict) else None
        if encoder and any(encoder == family.get(codec) for family in HW_CODEC_ENCODERS.values()):
            found[codec] = encoder
    return found if "h264" in found else {}


def run_ffmpeg(ffmpeg: str, args: list, timeout: float = 15.0, env=None) -> tuple:
    try:
        done = subprocess.run(
            [ffmpeg, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, timeout=timeout, env=env, **POPEN_FLAGS,
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
    driver = caps.get("vaapi_driver") or (caps.get("hw_decode") or {}).get("driver")
    return {**os.environ, "LIBVA_DRIVER_NAME": driver} if driver else None


# ══ Windows ═══════════════════════════════════════════════════════════
# POSIX 의 SIGSTOP·자식 정리·/dev 장치 대신 쓰는 것들. ctypes·winreg 는 Windows 에서만 불러온다.
def windows_gpus() -> list:
    """이 Windows 기기의 디스플레이 어댑터 [(제조사, 이름)]. 제조사는 nvidia·amd·intel 또는 ""."""
    if not WINDOWS:
        return []
    import winreg
    found = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, WINDOWS_DISPLAY_CLASS_KEY)
    except OSError:
        return []
    with root:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            if not name.isdigit():          # 0000, 0001 … 만 어댑터다(Properties 등은 건너뜀)
                continue
            try:
                with winreg.OpenKey(root, name) as key:
                    description = str(winreg.QueryValueEx(key, "DriverDesc")[0])
                    try:
                        device_id = str(winreg.QueryValueEx(key, "MatchingDeviceId")[0]).lower()
                    except OSError:
                        device_id = ""
            except OSError:
                continue
            match = re.search(r"ven_([0-9a-f]{4})", device_id)
            found.append((WINDOWS_GPU_VENDOR_IDS.get(match.group(1), "") if match else "", description))
    return found


def _windows_gpu_vendors() -> tuple:
    """H/W 인코더를 시험할 GPU 제조사, 외장 그래픽부터. 읽지 못하면 빈 튜플(전부 시험)."""
    present = {vendor for vendor, _name in windows_gpus()}
    return tuple(vendor for vendor, _encoder in WINDOWS_VENDOR_ENCODERS if vendor in present)


_WINDOWS_API: dict = {}
_WINDOWS_API_LOCK = threading.Lock()


def _windows_api() -> dict:
    """kernel32·ntdll 함수. 처음 부를 때 64비트 HANDLE 이 잘리지 않게 시그니처를 정해 둔다."""
    with _WINDOWS_API_LOCK:
        if not _WINDOWS_API:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            ntdll = ctypes.WinDLL("ntdll")
            handle = wintypes.HANDLE
            kernel32.OpenProcess.restype = handle
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.CloseHandle.argtypes = (handle,)
            kernel32.CreateJobObjectW.restype = handle
            kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.SetInformationJobObject.argtypes = (handle, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = (handle, handle)
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.GetProcessTimes.argtypes = (handle,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
            ntdll.NtSuspendProcess.argtypes = (handle,)
            ntdll.NtResumeProcess.argtypes = (handle,)
            _WINDOWS_API.update(ctypes=ctypes, wintypes=wintypes, kernel32=kernel32, ntdll=ntdll, job=None)
        return _WINDOWS_API


def _windows_open_process(pid: int, access: int):
    api = _windows_api()
    handle = api["kernel32"].OpenProcess(access, False, pid)
    if not handle:
        raise OSError("OpenProcess(pid=%d) 실패: %d" % (pid, api["ctypes"].get_last_error()))
    return handle


def windows_suspend(pid: int, suspend: bool) -> bool:
    """프로세스 전체를 멈추거나(0 CPU) 깨운다 — POSIX 의 SIGSTOP/SIGCONT. 서버의 hls_sessions 와 같다."""
    try:
        api = _windows_api()
        handle = _windows_open_process(pid, 0x0800)            # PROCESS_SUSPEND_RESUME
    except OSError:
        return False
    try:
        call = api["ntdll"].NtSuspendProcess if suspend else api["ntdll"].NtResumeProcess
        return call(handle) == 0
    finally:
        api["kernel32"].CloseHandle(handle)


def windows_end_with_agent(proc) -> None:
    """이 자식 프로세스가 에이전트와 함께 끝나게 한다.

    Windows 는 부모가 끝나도 자식이 남는다(멈춰 둔 ffmpeg 는 멈춘 채로). 자식을 '닫히면 안의
    프로세스를 끝내는' 작업 개체에 넣어 두면, 에이전트가 어떻게 끝나든 OS 가 그 개체를 닫으며
    자식도 끝낸다. 실패해도 작업은 그대로 돈다.
    """
    if not WINDOWS:
        return
    try:
        api = _windows_api()
        ctypes, wintypes, kernel32 = api["ctypes"], api["wintypes"], api["kernel32"]
        with _WINDOWS_API_LOCK:
            if api["job"] is None:
                class IoCounters(ctypes.Structure):
                    _fields_ = [(name, ctypes.c_ulonglong) for name in (
                        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

                class BasicLimits(ctypes.Structure):
                    _fields_ = [
                        ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD),
                    ]

                class ExtendedLimits(ctypes.Structure):
                    _fields_ = [
                        ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
                    ]

                job = kernel32.CreateJobObjectW(None, None)
                if not job:
                    raise OSError("CreateJobObject 실패: %d" % ctypes.get_last_error())
                limits = ExtendedLimits()
                limits.BasicLimitInformation.LimitFlags = 0x2000        # KILL_ON_JOB_CLOSE
                if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                    raise OSError("SetInformationJobObject 실패: %d" % ctypes.get_last_error())
                api["job"] = job
        handle = _windows_open_process(proc.pid, 0x0101)          # PROCESS_SET_QUOTA | TERMINATE
        try:
            if not kernel32.AssignProcessToJobObject(api["job"], handle):
                raise OSError("AssignProcessToJobObject 실패: %d" % ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(handle)
    except OSError as exc:
        log("자식 프로세스를 에이전트와 함께 끝나게 묶지 못했습니다(작업은 계속): %s" % exc)


def windows_cpu_seconds(handle) -> float:
    """그 프로세스가 쓴 CPU 시간(사용자 + 커널). 끝난 프로세스도 핸들이 열려 있으면 읽힌다."""
    api = _windows_api()
    times = [api["wintypes"].FILETIME() for _ in range(4)]   # 생성·종료·커널·사용자
    if not api["kernel32"].GetProcessTimes(handle, *(api["ctypes"].byref(item) for item in times)):
        return 0.0
    return sum(item.dwHighDateTime << 32 | item.dwLowDateTime for item in times[2:]) / 1e7


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
        vendors = _windows_gpu_vendors()
        return tuple(encoder for vendor, encoder in WINDOWS_VENDOR_ENCODERS if not vendors or vendor in vendors)
    if system != "Linux":
        return ()
    ordered: list = []
    if _nvidia_runtime_present():
        ordered.append("h264_nvenc")
    if _vaapi_devices():
        ordered.extend(("h264_vaapi", "h264_qsv"))
    return tuple(ordered)


def _fmp4_has_codec_config(path: str, codec: str = "h264") -> bool:
    """fMP4 초기화 구간(moov)에 그 코덱의 디코더 설정이 실제로 들어 있는지 확인.

    H.264 는 avcC(SPS/PPS), HEVC 는 hvcC(VPS/SPS/PPS), AV1 은 av1C(sequence header)."""
    box = CODEC_CONFIG_BOXES[codec]
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
            at = moov.find(box)
            if at < 4:
                return False
            # global header 가 없으면 ffmpeg 는 내용 없는 8바이트 상자를 쓰기도 한다.
            body = moov[at + 4:at - 4 + int.from_bytes(moov[at - 4:at], "big")]
            if codec == "hevc":
                return len(body) >= 23 and body[0] == 1 and body[22] >= 1
            if codec == "av1":
                return len(body) > 4 and body[0] == 0x81
            return len(body) >= 7 and body[0] == 1 and body[5] & 0x1F >= 1
        offset += size
    return False


def _smoke_test_encoder(ffmpeg: str, encoder: str, vaapi_device=None, vaapi_driver=None) -> bool:
    """작은 시험 인코딩으로 이 인코더가 재생 가능한 fMP4 를 실제로 만드는지 확인한다.

    ffmpeg 의 인코더 목록은 빌드에 포함된 래퍼일 뿐이라, 드라이버·장치가 없어도 목록에는
    나온다. 종료 코드만으로도 부족하다(일부 VAAPI 드라이버는 성공하고도 avcC 를 비운다).
    HEVC·AV1 인코더는 같은 방식으로 hvcC·av1C 를 확인한다.
    """
    codec = encoder_codec(encoder)
    args = ["-v", "warning", "-f", "lavfi", "-i", "nullsrc=s=256x144:d=0.3:r=10"]
    if encoder.endswith("_vaapi"):
        device = vaapi_device or DEFAULT_VAAPI_DEVICE
        if not _accessible_device(device):
            return False
        args = ["-v", "warning", "-vaapi_device", device] + args[2:]
        args += ["-vf", "format=nv12,hwupload"]
    else:
        args += ["-vf", "format=nv12"]
    args += ["-c:v", encoder]
    if encoder.endswith("_videotoolbox"):
        # VideoToolbox 의 소프트웨어 구현은 H/W 로 치지 않는다.
        args += ["-allow_sw", "0", "-realtime", "1", "-b:v", "1M"]
    if codec == "hevc":
        args += ["-tag:v", "hvc1"]
    with tempfile.TemporaryDirectory(prefix="shyni-encoder-probe-") as out_dir:
        output_path = os.path.join(out_dir, "probe.mp4")
        args += ["-frames:v", "3", "-movflags", FMP4_MOVFLAGS, "-f", "mp4", "-y", output_path]
        code, output = run_ffmpeg(ffmpeg, args, env=encoder_environment({"vaapi_driver": vaapi_driver}))
        passed = code == 0 and _fmp4_has_codec_config(output_path, codec)
    if not passed:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        detail = " | ".join(dict.fromkeys(lines[:2] + lines[-2:]))[:400] or "exit=%s" % code
        log("인코더 시험 실패: %s: %s" % (encoder, detail))
    return passed


def hw_decode_for(caps: dict, video_codec, bit_depth=None, hdr: bool = False):
    """이 기기가 이 원본을 하드웨어로 풀 수 있으면 {"hwaccel", "device"}, 아니면 None.

    비트 깊이를 모르면 HDR 은 10비트, 아니면 8비트로 본다. 틀려서 실패하면 작업이
    소프트웨어 디코딩으로 한 번 다시 시작한다(Job.run).
    """
    decode = caps.get("hw_decode") or {}
    codec = str(video_codec or "").strip().lower()
    codec = _VIDEO_CODEC_ALIASES.get(codec, codec)
    try:
        depth = int(bit_depth) if bit_depth else (10 if hdr else 8)
    except (TypeError, ValueError):
        depth = 10 if hdr else 8
    if not decode.get("hwaccel") or depth not in (decode.get("codecs") or {}).get(codec, ()):
        return None
    pipeline = decode.get("pipeline") or "download"
    if pipeline == "gpu" and hdr:
        return None          # GPU 안에서 끝내는 방식은 톤매핑(CPU 필터)을 못 한다
    return {"hwaccel": decode["hwaccel"], "device": decode.get("device"), "pipeline": pipeline}


def _hw_decode_candidates(caps: dict) -> list:
    """시험할 (하드웨어 디코더, 장치, VAAPI 드라이버). 인코더가 쓰는 장치를 먼저 본다."""
    system = platform.system()
    if system == "Darwin":
        return [("videotoolbox", None, None)]
    if system == "Windows":
        # d3d11va 는 프레임을 내려받아야 해서 Intel·AMD 내장 GPU 에서 느리다. NVIDIA 만 쓴다.
        vendors = _windows_gpu_vendors()
        return [("cuda", None, None)] if not vendors or "nvidia" in vendors else []
    if system != "Linux":
        return []
    found = []
    if _nvidia_runtime_present():
        found.append(("cuda", None, None))
    devices = list(_vaapi_devices())
    if caps.get("vaapi_device") in devices:
        devices.remove(caps["vaapi_device"])
        devices.insert(0, caps["vaapi_device"])
    for device in devices:
        drivers = (caps["vaapi_driver"],) if caps.get("vaapi_driver") else _vaapi_drivers(device)
        found.extend(("vaapi", device, driver) for driver in drivers)
    if caps.get("hw_encoder") == "h264_vaapi":
        found.sort(key=lambda item: item[0] != "vaapi")
    return found


def hw_decode_sample_args(codec: str, depth: int, encoders_out: str, path: str):
    for encoder, extra in HW_DECODE_SAMPLE_ENCODERS[codec]:
        if (" %s " % encoder) in encoders_out:
            return [
                "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24",
                "-frames:v", "6", "-pix_fmt", "yuv420p10le" if depth == 10 else "yuv420p",
                "-c:v", encoder, *extra, "-y", path,
            ]
    return None


def hw_decode_check_args(hwaccel: str, device, depth: int, path: str) -> list:
    """프레임을 GPU 메모리로 받아 hwdownload 로 내려받는다 — 소프트웨어로 풀었으면 여기서 실패한다."""
    args = ["-v", "error"]
    if device:
        args += ["-hwaccel_device", device]
    return args + [
        "-hwaccel", hwaccel, "-hwaccel_output_format", HW_DECODE_OUTPUT_FORMATS[hwaccel],
        "-i", path, "-vf", "hwdownload,format=" + ("p010le" if depth == 10 else "nv12"),
        "-f", "null", "-",
    ]


def hw_decode_pipeline(caps: dict, hwaccel: str, device) -> str:
    """VAAPI 는 같은 장치의 VAAPI 인코더가 있으면 GPU 안에서 끝낸다(내려받기는 너무 느리다)."""
    if hwaccel == "vaapi":
        return "gpu" if caps.get("hw_encoder") == "h264_vaapi" and caps.get("vaapi_device") == device else ""
    return "download"


def hw_decode_benchmark_sample_args(path: str) -> list:
    return [
        "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=24,noise=alls=8:allf=t",
        "-frames:v", "120", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
        "-b:v", "8M", "-y", path,
    ]


def hw_decode_pays_off(software: tuple, hardware: tuple) -> bool:
    """(종료 코드, 걸린 시간, CPU 시간) 두 측정에서 하드웨어 쪽이 확실히 나은지."""
    sw_rc, sw_wall, sw_cpu = software
    hw_rc, hw_wall, hw_cpu = hardware
    if hw_rc != 0:
        return False
    if sw_rc != 0 or sw_cpu <= 0:
        return True
    return hw_cpu <= sw_cpu * HW_DECODE_MAX_CPU_RATIO and hw_wall <= sw_wall * HW_DECODE_MAX_WALL_RATIO


def _measure_ffmpeg(args: list, env, timeout: float = 30.0) -> tuple:
    """ffmpeg 한 번의 (종료 코드, 걸린 시간, CPU 시간). CPU 를 재지 못하는 OS 에서는 0 이다."""
    started = time.monotonic()
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env, **POPEN_FLAGS)
    if WINDOWS:
        return _wait_measuring_windows(proc, started, timeout)
    if not hasattr(os, "wait4"):
        try:
            return proc.wait(timeout=timeout), time.monotonic() - started, 0.0
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return -1, timeout, 0.0
    while True:
        pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
        if pid:
            proc.returncode = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") \
                else (status >> 8 if status & 0x7F == 0 else -(status & 0x7F))
            return proc.returncode, time.monotonic() - started, usage.ru_utime + usage.ru_stime
        if time.monotonic() - started > timeout:
            proc.kill()
            proc.wait()
            return -1, timeout, 0.0
        time.sleep(0.02)


def _wait_measuring_windows(proc, started: float, timeout: float) -> tuple:
    """Windows: 끝나기 전에 핸들을 열어 두고, 끝난 뒤 GetProcessTimes 로 CPU 시간을 읽는다."""
    try:
        handle = _windows_open_process(proc.pid, 0x1000)        # PROCESS_QUERY_LIMITED_INFORMATION
    except OSError:
        handle = None
    try:
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return -1, timeout, 0.0
        return code, time.monotonic() - started, windows_cpu_seconds(handle) if handle else 0.0
    finally:
        if handle:
            _windows_api()["kernel32"].CloseHandle(handle)


def probe_hw_decode(ffmpeg: str, caps: dict):
    """이 기기가 하드웨어로 풀 수 있는 코덱. 하나도 못 풀거나 이득이 없으면 None(늘 소프트웨어)."""
    deadline = time.monotonic() + HW_DECODE_PROBE_TIMEOUT_SEC
    code, encoders_out = run_ffmpeg(ffmpeg, ["-hide_banner", "-encoders"])
    if code != 0 or not caps.get("video_encoder"):
        return None
    with tempfile.TemporaryDirectory(prefix="shyni-decode-probe-") as tmp:
        samples = {}
        for codec, depth in HW_DECODE_PROBES:
            path = os.path.join(tmp, "%s-%d.mkv" % (codec, depth))
            args = hw_decode_sample_args(codec, depth, encoders_out, path)
            if args and time.monotonic() < deadline and run_ffmpeg(ffmpeg, args, timeout=20)[0] == 0 \
                    and os.path.isfile(path):
                samples[(codec, depth)] = path
        bench = os.path.join(tmp, "bench.mkv")
        if run_ffmpeg(ffmpeg, hw_decode_benchmark_sample_args(bench), timeout=30)[0] != 0:
            return None
        for hwaccel, device, driver in _hw_decode_candidates(caps):
            pipeline = hw_decode_pipeline(caps, hwaccel, device)
            if not pipeline or time.monotonic() > deadline:
                continue
            env = encoder_environment({"vaapi_driver": driver})
            codecs = {}
            for (codec, depth), path in samples.items():
                if run_ffmpeg(ffmpeg, hw_decode_check_args(hwaccel, device, depth, path), env=env)[0] == 0:
                    codecs.setdefault(codec, []).append(depth)
            if "h264" not in codecs:
                continue
            # 실제 변환과 같은 명령으로 480p 를 만들어 보고, 이득이 있을 때만 쓴다.
            run_env = encoder_environment({**caps, "hw_decode": {"driver": driver}})
            common = dict(max_height=480, audio_compatible=True, video_encoder=caps["video_encoder"],
                          vaapi_device=caps.get("vaapi_device"))
            software = _measure_ffmpeg(build_transcode_args(ffmpeg, bench, MODE_VIDEO, **common), run_env)
            hardware = _measure_ffmpeg(build_transcode_args(
                ffmpeg, bench, MODE_VIDEO,
                hw_decode={"hwaccel": hwaccel, "device": device, "pipeline": pipeline}, **common), run_env)
            if hw_decode_pays_off(software, hardware):
                return {"hwaccel": hwaccel, "device": device, "driver": driver,
                        "pipeline": pipeline, "codecs": codecs}
            log("하드웨어 디코딩(%s) 이득 없음 — 소프트웨어 %.1f초/CPU %.1f초, 하드웨어 %.1f초/CPU %.1f초" % (
                hwaccel, software[1], software[2], hardware[1], hardware[2]))
    return None


def describe_hw_decode(caps: dict) -> str:
    decode = caps.get("hw_decode") or {}
    if not decode.get("codecs"):
        return "S/W"
    parts = [
        codec + ("" if depths == [8] else "(" + "/".join("%dbit" % d for d in depths) + ")")
        for codec, depths in decode["codecs"].items()
    ]
    return "%s/%s: " % (decode["hwaccel"], decode.get("pipeline") or "download") + ", ".join(parts)


def hls_probe_sample_args(path: str) -> list:
    """fMP4 HLS + 자막 시험의 원본: 외부 인코더가 필요 없는 무압축 1초(영상 + 소리)."""
    return [
        "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=256x144:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "1", "-c:v", "rawvideo", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", "-f", "nut", "-y", path,
    ]


HLS_PROBE_SUBTITLE = "1\n00:00:00,000 --> 00:00:01,000\nprobe\n"


def hls_fmp4_subtitle_output_ok(out_dir: str) -> bool:
    """init.mp4 가 실제로 있고, master 가 비디오 재생목록(EXT-X-STREAM-INF)을 가리키는지."""
    try:
        if os.path.getsize(os.path.join(out_dir, "init.mp4")) <= 0:
            return False
        with open(os.path.join(out_dir, "master.m3u8"), encoding="utf-8") as playlist:
            lines = [line.strip() for line in playlist if line.strip()]
    except OSError:
        return False
    return os.path.isfile(os.path.join(out_dir, "index.m3u8")) and any(
        line.startswith("#EXT-X-STREAM-INF:") and lines[index + 1] == "index.m3u8"
        for index, line in enumerate(lines[:-1])
    )


def probe_hls_fmp4_subtitles(ffmpeg: str, caps: dict, timeout: float = 15.0) -> bool:
    """fMP4 HLS 에 WebVTT 자막 rendition 을 같이 실어도 재생 가능한 출력이 나오는지.

    HEVC·AV1 은 MPEG-TS 로 보내지 않으므로 자막 rendition 이 있어도 fMP4 를 쓴다. ffmpeg 6.1 은
    이 조합에서 playlist 에 init.mp4 를 적고도 그 파일을 만들지 않았다(8.1 은 만든다). 실제 변환과
    같은 명령(build_hls_args)으로 1초를 만들어 확인한다. HEVC·AV1 인코더가 없으면 이 조합을 쓸
    일이 없어 시험하지 않는다(False).
    """
    encoders = caps.get("video_encoders") or {}
    codec = next((name for name in OUTPUT_CODECS if name != "h264" and name in encoders), None)
    if codec is None:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="shyni-hls-probe-") as tmp:
            sample = os.path.join(tmp, "sample.nut")
            subtitle = os.path.join(tmp, "sample.srt")
            out_dir = os.path.join(tmp, "out")
            os.mkdir(out_dir)
            Path(subtitle).write_text(HLS_PROBE_SUBTITLE, encoding="utf-8")
            if run_ffmpeg(ffmpeg, hls_probe_sample_args(sample), timeout=timeout)[0] != 0:
                return False
            args = build_hls_args(
                ffmpeg, sample, MODE_VIDEO, out_dir=out_dir, video_encoder=encoders[codec],
                output_codec=codec, vaapi_device=caps.get("vaapi_device"),
                subtitle={"source": "external", "file_path": subtitle},
            )
            code, output = run_ffmpeg(ffmpeg, args[1:], timeout=timeout, env=encoder_environment(caps))
            passed = code == 0 and hls_fmp4_subtitle_output_ok(out_dir)
    except Exception as exc:        # 시험 실패는 "못 쓴다"일 뿐이다 — 능력 판정을 멈추지 않는다
        log("fMP4 HLS 자막 시험 실패: %s" % type(exc).__name__)
        return False
    if not passed:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        log("이 ffmpeg 는 fMP4 HLS 에 자막을 같이 싣지 못합니다(자막이 있는 재생은 H.264 로 변환): %s" % (
            " | ".join(dict.fromkeys(lines[-2:]))[:300] or "exit=%s, init.mp4 또는 STREAM-INF 없음" % code))
    return passed


def detect_capabilities(ffmpeg: str, preference: str = "auto") -> dict:
    """이 ffmpeg 로 쓸 수 있는 인코더를 실측한다.

    {"video_encoder", "hw_encoder", "video_encoders", "hls_fmp4_subtitles", "tonemap",
    "vaapi_device", "vaapi_driver", "ffmpeg_version", "hw_decode"}.
    video_encoder 가 None 이면 이 ffmpeg 로는 비디오 변환을 못 한다. video_encoder/hw_encoder 는
    H.264 인코더다. video_encoders 는 출력 코덱별 인코더로, "h264" 는 video_encoder 와 같고
    "hevc"·"av1" 은 실측을 통과한 같은 계열의 H/W 인코더가 있을 때만 있다.
    """
    caps = {
        "video_encoder": None, "hw_encoder": None, "tonemap": False,
        "vaapi_device": None, "vaapi_driver": None, "ffmpeg_version": "", "hw_decode": None,
        "video_encoders": {}, "hls_fmp4_subtitles": False,
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
        # 고른 H.264 H/W 인코더와 같은 계열(같은 장치·드라이버)의 HEVC·AV1 인코더.
        codec_encoders = {}
        for codec, encoder in HW_CODEC_ENCODERS.get(caps["hw_encoder"] or "", {}).items():
            if (" %s " % encoder) not in encoders_out or time.monotonic() > deadline:
                continue
            if _smoke_test_encoder(ffmpeg, encoder, caps["vaapi_device"], caps["vaapi_driver"]):
                codec_encoders[codec] = encoder
        if caps["video_encoder"]:
            caps["video_encoders"] = {"h264": caps["video_encoder"], **codec_encoders}
        remaining = deadline - time.monotonic()
        if remaining > 1:
            caps["hls_fmp4_subtitles"] = probe_hls_fmp4_subtitles(ffmpeg, caps, timeout=min(15.0, remaining))
        code, filters_out = run_ffmpeg(ffmpeg, ["-hide_banner", "-filters"])
        caps["tonemap"] = code == 0 and " zscale " in filters_out and " tonemap " in filters_out
        caps["hw_decode"] = probe_hw_decode(ffmpeg, caps)
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


def _h264_bitrate_for(height) -> str:
    wanted = height or 1080
    for tier, rate in VIDEO_BITRATE_LADDER:
        if wanted >= tier:
            return rate
    return VIDEO_BITRATE_LADDER[-1][1]


def video_bitrate_kbps(height, codec: str = "h264") -> int:
    """목표 해상도·출력 코덱의 비디오 비트레이트(kbps). HEVC·AV1 은 같은 화질을 더 낮게 낸다."""
    value = _h264_bitrate_for(height).lower()
    if value.endswith("m"):
        kbps = int(value[:-1]) * 1000
    elif value.endswith("k"):
        kbps = int(value[:-1])
    else:
        kbps = int(value) // 1000
    numerator, denominator = VIDEO_BITRATE_SCALE[codec]
    return (kbps * numerator + denominator // 2) // denominator


def _video_bitrate_for(height, codec: str = "h264") -> str:
    """ffmpeg 에 넘길 비트레이트 표기. H.264 는 사다리의 표기 그대로 쓴다."""
    if codec == "h264":
        return _h264_bitrate_for(height)
    return "%dk" % video_bitrate_kbps(height, codec)


def aac_output_channels(source_channels, max_output_channels=None) -> int:
    source = source_channels if source_channels and source_channels > 0 else 2
    client_limit = max_output_channels if max_output_channels is not None and max_output_channels > 0 else 6
    return max(1, min(source, 6, client_limit))


def resolve_hls_segment_format(
    segment_format: str = HLS_SEGMENT_MPEGTS, subtitle: bool = False, output_codec: str = "h264",
) -> str:
    """HEVC·AV1 로 인코딩한 출력은 늘 fMP4 다(MPEG-TS 의 HEVC 는 Apple 재생기가 받지 않고 AV1 은
    실을 수 없다). H.264 는 자막 rendition 이 있으면 MPEG-TS, 없으면 요청한 형식이다."""
    normalized = str(segment_format or "").strip().lower()
    if normalized not in VALID_HLS_SEGMENT_FORMATS:
        raise ValueError("segment_format must be one of %s" % sorted(VALID_HLS_SEGMENT_FORMATS))
    if output_codec != "h264":
        return HLS_SEGMENT_FMP4
    return HLS_SEGMENT_MPEGTS if subtitle else normalized


def build_transcode_args(
    ffmpeg: str, source: str, mode: str, *,
    start_ms: int = 0, audio_channels=None, audio_output_max_channels=None,
    audio_compatible: bool = False, max_height=None, hdr: bool = False,
    dolby_vision: bool = False, video_codec=None, video_encoder: str = "libx264",
    vaapi_device=None, audio_stream_index=None, audio_language=None, audio_title=None,
    hw_decode=None, output_codec: str = "h264", audio_bitrate_kbps=None,
) -> list:
    """progressive fMP4(표준 출력) 변환 명령. 업스케일은 하지 않고, HDR 은 SDR 로 톤매핑한다.

    output_codec 은 출력 코덱(h264|hevc|av1), video_encoder 는 그 코덱을 내는 이 기기의 인코더다.
    audio_bitrate_kbps 를 주면 AAC 로 변환할 때 기본값(384k/192k) 대신 그 비트레이트를 쓴다."""
    if mode not in VALID_MODES:
        raise ValueError("unknown transcode mode: %s" % mode)
    if output_codec not in OUTPUT_CODECS:
        raise ValueError("unknown output codec: %s" % output_codec)
    vaapi_encoder = video_encoder.endswith("_vaapi")
    args = [ffmpeg, "-v", "error", "-nostdin"]
    if mode == MODE_VIDEO and vaapi_encoder and vaapi_device:
        args += ["-vaapi_device", vaapi_device]
    gpu_frames = mode == MODE_VIDEO and bool(hw_decode) and hw_decode.get("pipeline") == "gpu"
    if mode == MODE_VIDEO and hw_decode:
        # "download": 프레임을 내려받아 필터·인코더가 그대로 동작한다.
        # "gpu"(VAAPI): 프레임을 GPU 에 둔 채 scale_vaapi 로 줄여 같은 장치에서 인코딩한다.
        if hw_decode.get("device"):
            args += ["-hwaccel_device", hw_decode["device"]]
        args += ["-hwaccel", hw_decode["hwaccel"]]
        if gpu_frames:
            args += ["-hwaccel_output_format", HW_DECODE_OUTPUT_FORMATS[hw_decode["hwaccel"]]]
    if start_ms and start_ms > 0:
        args += ["-ss", "%.3f" % (start_ms / 1000)]
    audio_map = "0:%s?" % audio_stream_index if audio_stream_index is not None else "0:a:0?"
    args += ["-i", source, "-map", "0:v:0", "-map", audio_map]

    if mode == MODE_VIDEO and gpu_frames:
        # 디코딩·축소·인코딩을 모두 GPU 에서 한다. format=nv12 가 10비트를 8비트로 바꾼다.
        scale = "format=nv12"
        if max_height:
            max_width = VIDEO_RESOLUTION_WIDTHS.get(max_height, max_height * 16 // 9)
            scale = ("w='min(iw,%d)':h='min(ih,%d)':"
                     "force_original_aspect_ratio=decrease:force_divisible_by=2:format=nv12" % (max_width, max_height))
        args += ["-vf", "scale_vaapi=" + scale]
    if mode == MODE_VIDEO:
        filters: list = []
        if max_height and not gpu_frames:
            max_width = VIDEO_RESOLUTION_WIDTHS.get(max_height, max_height * 16 // 9)
            filters.append(
                "scale=w='min(iw,%d)':h='min(ih,%d)':"
                "force_original_aspect_ratio=decrease:force_divisible_by=2" % (max_width, max_height)
            )
        if hdr and not gpu_frames:
            filters.append(TONEMAP_CHAIN)
        if gpu_frames:
            pass                                  # 프레임이 이미 GPU 의 nv12 다
        elif vaapi_encoder:
            filters.append("format=nv12,hwupload")
        elif video_encoder.endswith(("_videotoolbox", "_amf")) or output_codec != "h264":
            # HEVC·AV1 은 어느 인코더든 8비트 4:2:0 으로 고정한다(10비트 원본이 Main 10 으로
            # 나가지 않게 — 서버는 재생 기기가 그 코덱을 푼다는 것만 안다). AMF 는 10비트(P010)
            # 프레임을 받으면 H.264 도 실패하므로 같이 8비트로 넘긴다.
            filters.append("format=nv12")
        if filters:
            args += ["-vf", ",".join(filters)]
        bitrate = _video_bitrate_for(max_height, output_codec)
        args += ["-c:v", video_encoder]
        if video_encoder == "libx264":
            args += ["-preset", "superfast", "-crf", "23"]
        elif video_encoder == "h264_nvenc":
            args += ["-preset", "p4", "-cq", "23"]
        elif video_encoder == "h264_qsv":
            args += ["-global_quality", "23"]
        elif video_encoder == "h264_vaapi":
            args += ["-qp", "23"]
        elif video_encoder.endswith("_videotoolbox"):
            args += ["-allow_sw", "0", "-realtime", "1"]
        elif video_encoder.endswith("_nvenc"):
            # HEVC·AV1: 품질 고정(-cq) 없이 비트레이트 모드 — 낮춘 비트레이트가 그대로 적용된다.
            # QSV·VAAPI 의 HEVC·AV1 도 같은 이유로 품질 옵션을 주지 않는다.
            args += ["-preset", "p4"]
        elif video_encoder.endswith("_amf"):
            # AMD AMF: 품질 고정 모드의 이름·범위가 드라이버·코덱마다 달라, 비트레이트 상한
            # 모드(vbr_peak)로 둔다. 아래 -b:v·-maxrate 가 그대로 상한이 된다.
            args += ["-quality", "balanced", "-rc", "vbr_peak"]
        args += ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate]
        if output_codec == "hevc":
            args += ["-tag:v", "hvc1"]        # Apple 재생기는 hvc1 sample entry 만 재생한다
    else:
        args += ["-c:v", "copy"]
        if dolby_vision and mode in (MODE_REMUX, MODE_AUDIO):
            args += ["-strict", "unofficial"]
            if str(video_codec or "").strip().lower() in {"hevc", "h265", "hev1", "hvc1"}:
                args += ["-tag:v", "hvc1"]

    audio_copy = mode == MODE_REMUX or (mode == MODE_VIDEO and audio_compatible)
    if audio_copy:
        args += ["-c:a", "copy"]
        if mode == MODE_VIDEO and start_ms and start_ms > 0:
            # 입력 -ss 는 그 앞 키프레임으로 뛴다. 다시 인코딩하는 영상은 디코더가 요청 시각 전
            # 프레임을 버리지만, 복사하는 오디오는 디코딩하지 않아 키프레임부터 그대로 실린다.
            # 그러면 -avoid_negative_ts make_zero 가 그 오디오를 0초로 당겨, 출력의 0초가 요청
            # 위치보다 키프레임 간격만큼(인터스텔라 6.1초) 앞이 되고 첫 조각엔 영상 없이 오디오만
            # 들어간다. 서버·재생기는 0초를 start_ms 로 보므로 위치·자막·재생기의 시간축 맞춤이
            # 그만큼 어긋났다(2026-11-18). 요청 시각 전 오디오 패킷을 버린다 — 남는 패킷은 원래
            # 타임스탬프 그대로라 영상과 맞고, 첫 패킷이 길어야 한 프레임(AAC 21ms) 늦게 시작한다.
            args += ["-copypriorss:a", "0"]
    else:
        channels = aac_output_channels(audio_channels, audio_output_max_channels)
        bitrate = "%dk" % int(audio_bitrate_kbps) if audio_bitrate_kbps else "384k" if channels > 2 else "192k"
        args += [
            "-c:a", "aac", "-profile:a", "aac_low",
            "-b:a", bitrate, "-ac", str(channels),
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
    segment_format: str = HLS_SEGMENT_MPEGTS, hw_decode=None,
    output_codec: str = "h264", audio_bitrate_kbps=None,
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
    actual_segment_format = resolve_hls_segment_format(
        segment_format, subtitle=bool(subtitle),
        output_codec=output_codec if mode == MODE_VIDEO else "h264",
    )
    base = build_transcode_args(
        ffmpeg, source, mode,
        start_ms=start_ms, audio_channels=audio_channels,
        audio_output_max_channels=audio_output_max_channels,
        audio_compatible=audio_compatible, max_height=max_height, hdr=hdr,
        dolby_vision=dolby_vision, video_codec=video_codec,
        video_encoder=video_encoder, vaapi_device=vaapi_device,
        audio_stream_index=audio_stream_index,
        audio_language=audio_language, audio_title=audio_title,
        hw_decode=hw_decode, output_codec=output_codec,
        audio_bitrate_kbps=audio_bitrate_kbps,
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
        if mode == MODE_VIDEO and start_ms and start_ms > 0:
            # 자막 입력도 -ss 로 열면 요청 시각 "앞에서 시작한" 마지막 대사부터 나온다(이미 끝난
            # 대사여도). 그 대사가 음수 시각이라 -avoid_negative_ts make_zero 가 영상·음성까지 그만큼
            # 뒤로 밀었다 — 대사 사이가 길면 수십 초. 출력의 0초가 start_ms 가 아니게 되고 첫 조각
            # 길이(EXTINF)도 0.5초 등으로 깨져 재생기마다 영상·음성·자막이 어긋났다(2026-11-18
            # 인터스텔라 12.4초). 출력 쪽 -ss 0 은 0초 전 자막을 버린다(요청 시각에 걸쳐 있던 대사
            # 하나도 함께 빠진다). 영상은 이미 0초부터라 그대로다. 영상을 복사하는 remux/audio 는
            # 키프레임 전 패킷을 버리면 안 되므로 비디오 변환에만 쓴다.
            args += ["-ss", "0"]
    if mode == MODE_VIDEO:
        args += ["-force_key_frames", "expr:gte(t,n_forced*%d)" % HLS_SEGMENT_SEC]
    args += [
        "-avoid_negative_ts", "make_zero",
        "-max_muxing_queue_size", "1024",
        "-f", "hls",
        "-hls_time", str(HLS_SEGMENT_SEC),
        "-hls_playlist_type", "event",
    ]
    if subtitle and actual_segment_format == HLS_SEGMENT_FMP4:
        # HEVC·AV1 출력만 여기로 온다. 서버는 이 조합이 실측을 통과한 기기(hls_fmp4_subtitles)에만
        # 맡긴다 — ffmpeg 6.1 은 playlist 에 init.mp4 를 적고도 그 파일을 만들지 않는다.
        args += [
            "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", os.path.join(out_dir, "seg%05d.m4s"),
            "-var_stream_map", "v:0,a:0,s:0,sgroup:subs",
            "-master_pl_name", "master.m3u8",
            os.path.join(out_dir, "index.m3u8"),
        ]
    elif subtitle:
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
        if "servers" not in self.data and self.data.get("token"):
            # 0.2 까지는 서버 하나만 적었다 — 서버 목록으로 옮긴다.
            self.data["servers"] = [{
                "url": self.data.pop("server_url"), "token": self.data.pop("token"),
                "runner_id": self.data.pop("runner_id", None), "name": self.data.pop("name", None),
            }]

    @property
    def servers(self) -> list:
        return self.data.setdefault("servers", [])

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
        status, response_headers, data = self.request(method, path, body=body, headers=headers, timeout=timeout)
        if status == 401 and str(response_headers.get(RUNNER_STATE_HEADER) or "").lower() == "unknown":
            raise RunnerRemoved(_detail(data) or "등록되지 않은 러너입니다")
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
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, **POPEN_FLAGS).stdout.strip()
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
    elif system == "Windows":
        info["os_version"] = platform.version()           # 10.0.22631 — release 는 11 도 "10" 이다
        info["cpu"] = _windows_cpu_name() or platform.processor()
        info["gpu"] = ", ".join(dict.fromkeys(name for _vendor, name in windows_gpus()))
    else:
        info["cpu"] = platform.processor()
    return {key: value for key, value in info.items() if value}


def _windows_cpu_name() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except OSError:
        return ""


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
    if system == "Windows":
        names = ", ".join(name for _vendor, name in windows_gpus()) or "찾지 못함"
        return ("H/W 인코더 시험을 통과하지 못해 S/W 로 등록합니다(GPU: %s). NVIDIA 는 그래픽 드라이버, "
                "AMD 는 Adrenalin 드라이버, Intel 은 그래픽 드라이버가 최신이어야 합니다. 원격 데스크톱 세션"
                "에서는 GPU 를 못 쓸 수 있습니다. 갖춘 뒤 에이전트를 다시 시작하면 다시 시험합니다." % names)
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
    output_codec = raw.get("output_codec") or "h264"     # 0.4 이전 서버는 보내지 않는다
    if output_codec not in OUTPUT_CODECS:
        raise AgentError("모르는 출력 코덱입니다")
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
        "video_bit_depth": _int(raw.get("video_bit_depth"), 1, 16),
        "audio_stream_index": _int(raw.get("audio_stream_index"), 0, 1023),
        "audio_language": _short(raw.get("audio_language"), 32),
        "audio_title": _short(raw.get("audio_title"), 200),
        "music_bitrate_kbps": bitrate,
        "music_sample_rate_hz": _int(raw.get("music_sample_rate_hz"), 8000, 768000),
        "segment_format": segment_format,
        "subtitle": subtitle,
        "output_codec": output_codec,
        # AAC 로 변환할 때의 비트레이트(낮은 화질 변환의 AAC 2.0 128k). 없으면 채널 수에 따른 기본값.
        "audio_bitrate_kbps": _int(raw.get("audio_bitrate_kbps"), 32, 640),
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
        return {"kind": "rclone", "remote": remote, "folder_id": folder_id, "path": path,
                "server_fallback": raw.get("server_fallback") is True}
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


RCLONE_SEARCH_DIRS = ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/snap/bin",
                      str(Path.home() / "bin"), str(Path.home() / ".local" / "bin"))


def find_rclone(binary=None):
    """rclone 실행 파일의 전체 경로. 서비스(launchd·systemd)는 PATH 가 짧아 터미널에서는 되던
    `rclone` 을 못 찾는다 — 흔한 설치 위치도 본다."""
    binary = binary or "rclone"
    found = shutil.which(binary)
    if found:
        return os.path.abspath(found)
    if os.path.dirname(binary):
        return None
    for folder in RCLONE_SEARCH_DIRS:
        candidate = os.path.join(folder, binary)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def rclone_list_remotes(binary: str, config_path) -> list:
    args = [binary, "listremotes"] + (["--config", config_path] if config_path else [])
    return [line.strip()[:-1] for line in _command_output(args, timeout=20).splitlines()
            if line.strip().endswith(":")]


def usable_rclone_remotes(data: dict) -> list:
    """설치할 때 허용한 리모트 가운데 지금 이 기기의 rclone 설정에 실제로 있는 것."""
    wanted = [name for name in data.get("rclone_remotes") or [] if _REMOTE_NAME_RE.fullmatch(str(name))]
    if not wanted:
        return []
    binary = find_rclone(data.get("rclone"))
    if not binary:
        log("rclone 을 찾을 수 없어 rclone 리모트를 쓰지 않습니다: %s" % (data.get("rclone") or "rclone"))
        return []
    present = set(rclone_list_remotes(binary, data.get("rclone_config")))
    missing = [name for name in wanted if name not in present]
    if missing:
        log("rclone 설정에 없는 리모트는 쓰지 않습니다: %s" % ", ".join(missing))
    return [name for name in wanted if name in present]


class RcloneServers:
    """이 기기의 rclone 으로 원본을 읽는다. 리모트(와 루트 폴더)마다 `rclone rcd --rc-serve` 를
    127.0.0.1 에 하나 띄워 두고 계속 쓴다. `/[리모트:{폴더}]/경로` 로 파일을 바로 연다.

    `serve http` 는 VFS 라 경로의 폴더마다 목록을 통째로 받았다. 영화 3,865개 폴더는 9쪽을 차례로
    받느라 13초, 처음 여는 파일이 20초 걸렸다. `--fast-list` 는 재귀 목록에만 쓰여 효과가 없었다(실측
    14~18초 그대로). rcd 는 경로를 폴더 이름 조회(한 번에 0.4초)로 찾고 찾은 폴더를 기억해, 처음
    여는 파일이 5초, 같은 rclone 의 다른 파일이 2.7초다(2026-11-18). 파일은 매번 이름으로 다시
    찾으므로 새로 생긴 파일도 바로 보인다.

    자격은 이 기기의 rclone 설정에 있다. 구글 공유 드라이브처럼 조회용 토큰과 읽기용 자격이
    다른 리모트도 rclone 이 알아서 처리하므로, 서버는 리모트 이름과 경로만 알려 주면 된다.
    rcd 는 접속 암호를 아는 쪽에 rclone 명령을 모두 열어 준다. 암호는 그때그때 만들어 환경변수로만
    넘기고(ps 에 안 보임) ffmpeg 에도 주지 않는다 — 그 암호를 엿볼 수 있는 같은 사용자의 프로세스는
    어차피 rclone 설정을 직접 읽을 수 있다.
    """

    UNREADABLE_SEC = 3600     # 못 읽은 리모트는 이만큼은 다시 시도하지 않고 서버에서 받는다

    def __init__(self, binary: str, config_path, log_dir: Path):
        self.binary, self.config_path, self.log_dir = binary, config_path, log_dir
        self._servers: dict = {}
        self._unreadable: dict = {}
        self._lock = threading.Lock()

    def mark_unreadable(self, source: dict) -> None:
        self._unreadable[(source["remote"], source["folder_id"])] = time.monotonic()

    def unreadable(self, source: dict) -> bool:
        marked = self._unreadable.get((source["remote"], source["folder_id"]))
        return marked is not None and time.monotonic() - marked < self.UNREADABLE_SEC

    def unreadable_remotes(self) -> set:
        """방금 읽지 못한 리모트 이름. 그동안은 서버에 "읽을 수 있다"고 알리지 않는다."""
        now = time.monotonic()
        return {remote for (remote, _), marked in list(self._unreadable.items())
                if now - marked < self.UNREADABLE_SEC}

    def _server_for(self, source: dict):
        server = self._servers.get((source["remote"], source["folder_id"]))
        return server if server is not None and server["proc"].poll() is None else None

    def ready(self, source: dict) -> bool:
        """이 rclone 이 그 파일의 폴더를 이미 찾았다 — 경로를 다시 찾지 않고 바로 연다."""
        server = self._server_for(source)
        return server is not None and source["path"].rpartition("/")[0] in server["ready"]

    def mark_ready(self, source: dict) -> None:
        server = self._server_for(source)
        if server is not None:
            server["ready"].add(source["path"].rpartition("/")[0])

    def forget(self, source: dict) -> None:
        server = self._server_for(source)
        if server is not None:
            server["ready"].discard(source["path"].rpartition("/")[0])

    def upstream(self, source: dict, fresh: bool = False) -> tuple:
        key = (source["remote"], source["folder_id"])
        with self._lock:
            server = self._servers.get(key)
            if server is not None and (fresh or server["proc"].poll() is not None):
                self._stop(server)
                server = None
            if server is None:
                server = self._servers[key] = self._start(*key)
        url = "http://127.0.0.1:%d/%s/%s" % (
            server["port"], urllib.parse.quote("[%s]" % server["target"], safe=""),
            urllib.parse.quote(source["path"]))
        return url, {"Authorization": server["authorization"]}

    def _start(self, remote: str, folder_id: str) -> dict:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        password = secrets.token_hex(16)
        target = "%s:{%s}" % (remote, folder_id) if folder_id else remote + ":"
        args = [self.binary, "rcd", "--rc-serve", "--rc-addr", "127.0.0.1:%d" % port,
                "--fs-cache-expire-duration", RCLONE_FS_CACHE_TIME]
        if self.config_path:
            args += ["--config", self.config_path]
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / ("rclone-%s.log" % re.sub(r"[^A-Za-z0-9_.-]", "_", remote))
        with open(log_path, "wb") as errors:
            # 접속 암호는 환경변수로 넘겨 명령줄(ps)에 남기지 않는다.
            proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                env={**os.environ, "RCLONE_RC_USER": "shyni", "RCLONE_RC_PASS": password}, **POPEN_FLAGS,
            )
        windows_end_with_agent(proc)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                log("rclone 시작: %s (포트 %d)" % (target, port))
                token = "%s:%s" % ("shyni", password)
                return {"proc": proc, "port": port, "target": target, "ready": set(),
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
# rclone 이 찾아 둔 폴더(경로 → 폴더 ID)를 오래 기억한다. 기본(5분)이면 잠깐 쉬고 나서 인증부터
# 다시 하고 폴더를 다시 찾는다. 폴더를 지우고 같은 이름으로 다시 만들어 ID 가 바뀌면 404 가 나고,
# 그때 rclone 을 새로 띄워 다시 찾는다(SourceProxy._open_source).
RCLONE_FS_CACHE_TIME = "1000h"
# ffmpeg 는 원본을 열 때 앞(헤더)·뒤(색인)·첫 패킷을 오가며 요청을 3~6번 차례로 보낸다. 원격 원본은
# 요청마다 첫 바이트까지 1.5~3초라 여는 데만 7~17초가 걸렸다(2026-11-18 실측: 글꼴이 든 MKV 는 머리가
# 12.6MB, 색인이 끝에 있는 MP4 는 moov 가 6.3MB). 작업을 받자마자 앞·뒤를 동시에 받고, 앞의 첫 64KB 로
# MKV·MP4 구조를 읽어 ffmpeg 가 읽을 범위(머리 전체와 그 뒤 첫 패킷, 색인)를 함께 받아 둔다. 같은 파일의
# 다음 작업(탐색·이어보기로 새 세션)도 그대로 쓴다.
# 앞은 처음부터 5MB 를 받는다 — 보통 파일은 헤더와 그 뒤 첫 패킷이 다 들어가, 구조를 읽고 나서 한 번 더
# 요청하지 않아도 된다(그 왕복이 1.5~3초).
SOURCE_HEAD_BYTES = 5 * 1024 * 1024
SOURCE_TAIL_BYTES = 4 * 1024 * 1024
SOURCE_PACKET_BYTES = 4 * 1024 * 1024     # 헤더 뒤에서 ffmpeg 가 스트림 정보를 읽으려고 읽는 첫 패킷(실측 1~2MB)
SOURCE_PLAN_BYTES = 64 * 1024             # 구조를 읽는 데 쓰는 앞부분
SOURCE_INDEX_GUESS_BYTES = 2 * 1024 * 1024    # 크기를 모르는 MKV 색인(Cues·Tags)을 이만큼 받아 둔다
SOURCE_REGION_MAX_BYTES = 64 * 1024 * 1024
SOURCE_CACHE_ENTRIES = 6
SOURCE_CACHE_BYTES = 256 * 1024 * 1024
SOURCE_CACHE_TTL_SEC = 2 * 3600
SOURCE_PREFETCH_WAIT_SEC = 30.0
# 이 기기의 rclone 이 그 경로를 아직 못 찾았으면(처음 띄움·새 폴더: 4~8초) 서버가 내주는 것으로 읽고,
# rclone 이 찾으면 같은 바이트에서 rclone 으로 넘어간다. 서버에서 읽는 속도로 이만큼(초) 앞에 rclone 을
# 미리 열어 두었다가 그 바이트에 닿으면 갈아탄다 — 서버가 느린 인터넷 너머여도 멈추지 않는다.
HANDOVER_LEAD_SEC = 2.5
HANDOVER_MIN_BYTES = 2 * 1024 * 1024
HANDOVER_MAX_BYTES = 64 * 1024 * 1024
HANDOVER_MAX_SKIP_BYTES = 32 * 1024 * 1024
HANDOVER_RETRY_SEC = 5.0
RCLONE_PROBE_RETRY_SEC = 10.0
# 외부 자막은 작업을 받자마자 서버에서 받아 둔다. ffmpeg 는 원본을 열고 목표 위치까지 읽은 뒤에야 자막을
# 열어서, 그 요청(서버가 마운트에서 처음 읽으면 1~2초)이 첫 조각까지의 길에 그대로 들어갔다.
SUBTITLE_PREFETCH_MAX_BYTES = 20 * 1024 * 1024
# 이 기기의 rclone 이 이만큼 첫 바이트를 주지 않으면 서버에도 같은 요청을 보내 첫 바이트가 먼저 오는
# 쪽을 쓴다(실측: 구글 드라이브가 한 요청에 20초를 끈 적이 있다. 경로를 아는 파일도 첫 바이트가 1.4~2.1초,
# 탐색 다섯 번 중 두 번이 3초를 넘겼다). 받는 도중 이만큼 멈춰도 서버에서 이어 받는다.
RCLONE_HEDGE_SEC = 2.0
RCLONE_STALL_SEC = 20.0

MKV_EBML, MKV_SEGMENT, MKV_SEEKHEAD, MKV_SEEK, MKV_SEEK_POSITION, MKV_CLUSTER = (
    0x1A45DFA3, 0x18538067, 0x114D9B74, 0x4DBB, 0x53AC, 0x1F43B675)
MP4_TOP_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"pdin", b"meta",
                 b"styp", b"sidx", b"moof"}


def _ebml_vint(data, pos: int, element_id: bool):
    """EBML 가변 길이 정수. 요소 ID 는 표지 비트를 그대로 두고, 크기는 떼며 '모름'(전부 1)은 None."""
    first = data[pos]
    length, mask = 1, 0x80
    while not first & mask:
        mask >>= 1
        length += 1
        if length > 8:
            raise ValueError("EBML 길이가 올바르지 않습니다")
    if pos + length > len(data):
        raise IndexError(pos)
    value = first if element_id else first & (mask - 1)
    for byte in data[pos + 1:pos + length]:
        value = value << 8 | byte
    if not element_id and value == (1 << (7 * length)) - 1:
        value = None
    return value, pos + length


def _matroska_seek_positions(data, pos: int, end: int) -> list:
    """SeekHead 가 알려 준 요소 위치(Segment 데이터 기준)."""
    positions = []
    while pos < end:
        element, pos = _ebml_vint(data, pos, True)
        length, pos = _ebml_vint(data, pos, False)
        if length is None:
            break
        if element == MKV_SEEK:
            inner, stop = pos, pos + length
            while inner < stop:
                child, inner = _ebml_vint(data, inner, True)
                size, inner = _ebml_vint(data, inner, False)
                if size is None:
                    break
                if child == MKV_SEEK_POSITION:
                    positions.append(int.from_bytes(bytes(data[inner:inner + size]), "big"))
                inner += size
        pos += length
    return positions


def _matroska_regions(head, size: int) -> list:
    element, pos = _ebml_vint(head, 0, True)
    length, pos = _ebml_vint(head, pos, False)
    if element != MKV_EBML or length is None:
        return []
    element, pos = _ebml_vint(head, pos + length, True)
    if element != MKV_SEGMENT:
        return []
    _, pos = _ebml_vint(head, pos, False)
    segment = pos
    elsewhere = []
    # 첫 Cluster 까지가 ffmpeg 가 차례로 읽는 머리다. 글꼴(Attachments)처럼 큰 요소는 크기만 보고 건너뛴다.
    while pos < len(head):
        start = pos
        try:
            element, pos = _ebml_vint(head, pos, True)
            length, pos = _ebml_vint(head, pos, False)
        except IndexError:
            pos = start
            break
        if element == MKV_CLUSTER:
            pos = start
            break
        if length is None:
            return []
        if element == MKV_SEEKHEAD and pos + length <= len(head):
            elsewhere += [segment + position for position in _matroska_seek_positions(head, pos, pos + length)]
        pos += length
    head_end = min(size, pos + SOURCE_PACKET_BYTES)
    regions = [(0, head_end)]
    # 머리 밖에 있는 요소(대개 파일 끝의 Cues·Tags)도 ffmpeg 가 연 직후 읽는다.
    for position in sorted(set(elsewhere))[:4]:
        if head_end <= position < size:
            regions.append((position, min(size, position + SOURCE_INDEX_GUESS_BYTES)))
    return regions


def _mp4_regions(head, size: int) -> list:
    pos, moov_seen = 0, False
    while pos + 8 <= len(head):
        length, kind = struct.unpack(">I4s", bytes(head[pos:pos + 8]))
        header = 8
        if length == 1:
            if pos + 16 > len(head):
                break
            length, header = struct.unpack(">Q", bytes(head[pos + 8:pos + 16]))[0], 16
        elif length == 0:
            length = size - pos
        if length < header:
            return []
        if kind == b"mdat":
            regions = [(0, min(size, pos + header + SOURCE_PACKET_BYTES))]
            if not moov_seen and pos + length < size:
                regions.append((pos + length, size))      # 색인(moov)이 미디어 데이터 뒤에 있다
            return regions
        moov_seen = moov_seen or kind == b"moov"
        pos += length
    # 상자가 읽은 앞부분을 넘어간다: 미디어 데이터 앞의 큰 moov. 그 뒤 상자 머리와 첫 패킷까지.
    return [(0, min(size, pos + 16 + SOURCE_PACKET_BYTES))]


def plan_source_regions(head, size: int) -> list:
    """원본의 앞부분으로 ffmpeg 가 열면서 읽을 바이트 범위 [(시작, 끝)) 를 정한다. 모르는 형식이면 []."""
    try:
        if bytes(head[:4]) == b"\x1a\x45\xdf\xa3":
            regions = _matroska_regions(head, size)
        elif bytes(head[4:8]) in MP4_TOP_BOXES:
            regions = _mp4_regions(head, size)
        else:
            return []
    except (IndexError, ValueError, struct.error):
        return []
    return [(start, min(end, start + SOURCE_REGION_MAX_BYTES)) for start, end in regions if start < end]


class CachedRegion:
    """원본의 한 구간 [start, end). 받는 대로 쌓고, 이 구간을 읽으려는 요청에는 받은 만큼씩 바로 내준다.
    여러 곳(서버·rclone)에서 같이 받으면 먼저 바이트를 낸 쪽이 채우고 나머지는 그만둔다."""

    def __init__(self, start=None, end=None, suffix=None):
        self.start, self.end, self.suffix = start, end, suffix    # suffix: 끝에서 몇 바이트(크기를 알 때 정해짐)
        self.data = bytearray()
        self.owner = None
        self.fetchers = 0
        self.done = False
        self.cond = threading.Condition()

    @property
    def span(self) -> int:
        return self.end - self.start if self.start is not None else self.suffix

    def covers(self, offset: int) -> bool:
        return self.start is not None and self.start <= offset < self.end

    def append(self, owner, block) -> bool:
        with self.cond:
            if self.done or self.owner not in (None, owner):
                return False
            self.owner = owner
            self.data += block
            self.cond.notify_all()
            return True

    def finish(self, owner) -> None:
        with self.cond:
            self.fetchers -= 1
            if self.owner is owner or (self.owner is None and self.fetchers <= 0):
                self.done = True
            self.cond.notify_all()

    def read(self, offset: int, deadline: float):
        """offset 부터 지금 받아 둔 바이트(최대 1MB). 더 받을 것이 없거나 기다려도 안 오면 None."""
        with self.cond:
            while True:
                if self.start is not None and offset < self.start + len(self.data):
                    index = offset - self.start
                    return bytes(self.data[index:index + 1024 * 1024])
                left = deadline - time.monotonic()
                if self.done or left <= 0:
                    return None
                self.cond.wait(left)


class CachedSource:
    """한 원본에서 받아 둔 구간들. 같은 파일의 작업끼리 나눠 쓴다."""

    def __init__(self):
        self.size = None
        self.content_type = None
        self.regions: list = []
        self.jobs: list = []                  # 이 파일을 읽는 작업(최근 것이 뒤)
        self.planned = False
        self.active = 0                       # 받는 중인 것
        self.sized = threading.Event()        # 크기를 알았다(또는 처음 받기가 모두 실패했다)
        self.used = time.monotonic()
        self.lock = threading.Lock()

    @property
    def failed(self) -> bool:
        return self.sized.is_set() and self.size is None

    @property
    def nbytes(self) -> int:
        return sum(region.span for region in self.regions)

    def idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        for region in list(self.regions):
            with region.cond:
                while not region.done and time.monotonic() < deadline:
                    region.cond.wait(max(0.0, deadline - time.monotonic()))
                if not region.done:
                    return False
        return True

    def add(self, start: int, end: int):
        """[start, end) 중 아직 없는 앞쪽을 새 구간으로. 다 있으면 None."""
        with self.lock:
            for region in sorted((r for r in self.regions if r.start is not None), key=lambda r: r.start):
                if region.start <= start < region.end:
                    start = region.end
                elif start < region.start < end:
                    end = region.start
            if start >= end:
                return None
            region = CachedRegion(start, end)
            self.regions.append(region)
            return region

    def add_suffix(self, length: int):
        region = CachedRegion(suffix=length)
        with self.lock:
            self.regions.append(region)
        return region

    def set_size(self, total: int, content_type) -> bool:
        """처음 받은 응답이 알려 준 파일 크기. 다른 크기를 말하는 응답(다른 파일)은 False."""
        with self.lock:
            if self.size is not None:
                return self.size == total
            self.size = total
            self.content_type = content_type
            for region in self.regions:
                if region.suffix is not None and region.start is None:
                    region.start, region.end = max(0, total - region.suffix), total
            self.sized.set()
            return True

    def started(self, region) -> None:
        with self.lock:
            self.active += 1
        with region.cond:
            region.fetchers += 1

    def ended(self) -> None:
        with self.lock:
            self.active -= 1
            if self.active <= 0 and self.size is None:
                self.sized.set()              # 아무것도 못 받았다 — 기다리는 요청은 원본에서 직접 읽는다

    def region_at(self, offset: int):
        for region in list(self.regions):
            if region.covers(offset):
                return region
        return None

    def pending(self) -> list:
        return [region for region in list(self.regions) if not region.done and region.owner is None]


class _Stream:
    """원본에서 받는 응답 하나. 서버 → rclone 으로 갈아타면 연결이 바뀐다."""

    def __init__(self, conn, response, via: str, offset: int = 0, total=None):
        self.conn, self.response, self.via = conn, response, via
        self.offset, self.total = offset, total

    def switch(self, conn, response, via: str) -> None:
        old = self.conn
        self.conn, self.response, self.via = conn, response, via
        old.close()

    def close(self) -> None:
        self.conn.close()


def _response_total(response):
    match = re.match(r"bytes \d+-\d+/(\d+)$", response.getheader("Content-Range") or "")
    if match:
        return int(match.group(1))
    length = response.getheader("Content-Length")
    return int(length) if response.status == 200 and length and length.isdigit() else None


class _Handover:
    """서버가 내주는 응답을 읽는 동안 이 기기의 rclone 을 target 바이트부터 미리 연다."""

    def __init__(self, proxy, job, target: int, last, total: int):
        self.target = target
        self.done = threading.Event()
        self._result = None
        self._cancelled = False
        self._lock = threading.Lock()
        threading.Thread(target=self._open, args=(proxy, job, last, total), name="handover", daemon=True).start()

    def _open(self, proxy, job, last, total: int) -> None:
        conn = None
        try:
            source = job.source
            if source.get("kind") != "rclone":
                return
            url, headers = proxy.rclone.upstream(source)
            headers["Range"] = "bytes=%d-%s" % (self.target, "" if last is None else last)
            conn, response = proxy._open(url, headers, timeout=RCLONE_STALL_SEC)
            match = re.match(r"bytes (\d+)-\d+/(\d+)$", response.getheader("Content-Range") or "")
            if response.status == 206 and match and int(match.group(1)) == self.target:
                if int(match.group(2)) != total:
                    proxy._fall_back_to_server(job, "이 기기의 rclone 이 본 파일 크기가 서버와 다릅니다")
                    return
                with self._lock:
                    if not self._cancelled:
                        self._result, conn = (conn, response), None
        except (OSError, http.client.HTTPException, AgentError, KeyError):
            pass
        finally:
            if conn is not None:
                conn.close()
            self.done.set()

    def take(self, offset: int):
        """None = 아직 여는 중, False = 못 열었다, 아니면 offset 에 맞춘 (conn, response, 남은 길이)."""
        if not self.done.is_set():
            return None
        with self._lock:
            result, self._result = self._result, None
        if result is None:
            return False
        conn, response = result
        skip = offset - self.target            # 서버에서 target 을 넘겨 더 받았다 — 그만큼 버린다
        try:
            if skip > HANDOVER_MAX_SKIP_BYTES:
                raise OSError("갈아탈 자리를 너무 지났습니다")
            while skip > 0:
                block = response.read(min(skip, PIPE_CHUNK_BYTES))
                if not block:
                    raise OSError("rclone 연결이 끊겼습니다")
                skip -= len(block)
        except (OSError, http.client.HTTPException):
            conn.close()
            return False
        length = response.getheader("Content-Length")
        remaining = int(length) - (offset - self.target) if length and length.isdigit() else None
        return conn, response, remaining

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            result, self._result = self._result, None
        if result is not None:
            result[0].close()


class SourceProxy:
    """ffmpeg ↔ 원본 사이의 127.0.0.1 프록시.

    ffmpeg 는 http://127.0.0.1:포트/작업/난수/media 만 안다. 프록시가 실제 주소와 자격을 붙여
    Range 요청을 넘기고, 도중에 끊기면 그 자리부터 이어 받고, 자격이 만료되면(401/403)
    서버에서 새로 받아 다시 요청한다. 긴 영화도 한 시간짜리 토큰에 묶이지 않는다.

    rclone 원본은 이 기기의 rclone 으로 읽는다. 서버도 그 파일을 내줄 수 있으면(server_fallback),
    rclone 이 그 경로를 찾기 전(처음 띄움·새 폴더)과 rclone 이 실패했을 때는 서버에서 받고, rclone 이
    준비되면 같은 바이트에서 rclone 으로 넘어간다.
    """

    def __init__(self, api: Api, rclone=None):
        self.api = api
        self.rclone = rclone
        self.jobs: dict = {}
        self._cache: dict = {}
        self._cache_lock = threading.Lock()
        self._probes: dict = {}
        self._probe_lock = threading.Lock()
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

    # 원본 미리 받기
    @staticmethod
    def _cache_key(job):
        """같은 파일을 가리키는 작업끼리 나눠 쓰는 열쇠. 서버가 알려 준 파트 번호가 있으면 그것,
        없으면 rclone 경로. 매번 서명이 바뀌는 주소는 그 작업 안에서만 쓴다."""
        part_id = getattr(job, "part_id", None)
        api = getattr(job, "api", None)
        if part_id:
            return ("part", getattr(api, "base_url", ""), part_id)
        source = job.source
        if source.get("kind") == "rclone":
            return ("rclone", source["remote"], source.get("folder_id"), source["path"])
        return ("job", job.id)

    def prefetch(self, job) -> None:
        """원본의 앞·뒤를 동시에 받기 시작한다(이미 받아 뒀거나 받는 중이면 그것을 쓴다)."""
        if job.source.get("kind") == "server" and getattr(job, "nonce", None) is None:
            return                                   # 작업이 아닌 미리 받기는 서버 중계를 못 쓴다
        key = self._cache_key(job)
        now = time.monotonic()
        with self._cache_lock:
            for stale in [k for k, e in self._cache.items() if now - e.used > SOURCE_CACHE_TTL_SEC]:
                del self._cache[stale]
            entry = self._cache.get(key)
            new = entry is None or entry.failed
            if new:
                self._cache.pop(key, None)
                while self._cache and (len(self._cache) >= SOURCE_CACHE_ENTRIES or sum(
                        e.nbytes for e in self._cache.values()) > SOURCE_CACHE_BYTES - SOURCE_REGION_MAX_BYTES):
                    del self._cache[min(self._cache, key=lambda k: self._cache[k].used)]
                entry = self._cache[key] = CachedSource()
            entry.used = now
            entry.jobs = (entry.jobs + [job])[-4:]
        job.cache = entry
        if new:
            for region in (entry.add(0, SOURCE_HEAD_BYTES), entry.add_suffix(SOURCE_TAIL_BYTES)):
                self._fetch(job, entry, region)
        elif self._server_first(job):
            # 미리 받기(warm)가 아직 rclone 을 기다리는 구간은 서버에서도 받는다 — 먼저 오는 쪽을 쓴다.
            for region in entry.pending():
                self._start_fetch(job, entry, region, "server")

    def _fetch(self, job, entry: CachedSource, region: CachedRegion) -> None:
        """구간을 받기 시작한다. rclone 이 아직 그 경로를 못 찾았으면 서버와 rclone 에서 같이 받아
        먼저 오는 쪽을 쓴다(서버 마운트도 처음 읽는 곳은 첫 바이트까지 1.5~3초다)."""
        if self._server_first(job):
            self._start_fetch(job, entry, region, "server")
            self._start_fetch(job, entry, region, "rclone")
        else:
            self._start_fetch(job, entry, region)

    def prefetch_subtitle(self, job) -> None:
        """작업의 외부 자막을 지금 받아 둔다(작은 파일). 못 받으면 ffmpeg 가 서버에서 직접 받는다."""
        job.subtitle_data = None
        job.subtitle_ready = threading.Event()

        def fetch() -> None:
            conn = None
            try:
                url, headers = self._upstream(job, "subtitle")
                conn, response = self._open(url, headers)
                length = response.getheader("Content-Length")
                if (response.status == 200 and length and length.isdigit()
                        and int(length) <= SUBTITLE_PREFETCH_MAX_BYTES):
                    data = response.read(int(length))
                    if len(data) == int(length):
                        job.subtitle_data = data
            except (OSError, http.client.HTTPException):
                pass
            finally:
                if conn is not None:
                    conn.close()
                job.subtitle_ready.set()

        threading.Thread(target=fetch, name="subtitle-prefetch", daemon=True).start()

    def _serve_subtitle(self, request, job, offset: int, end: str, ranged: bool, body: bool) -> bool:
        """받아 둔 자막에서 응답한다. 받아 두지 못했으면 False(서버에서 직접 받는다)."""
        ready = getattr(job, "subtitle_ready", None)
        if ready is None or not ready.wait(SOURCE_PREFETCH_WAIT_SEC) or job.subtitle_data is None:
            return False
        data = job.subtitle_data
        last = min(int(end), len(data) - 1) if end else len(data) - 1
        if offset >= len(data) or offset > last:
            request.send_response(416)
            request.send_header("Content-Range", "bytes */%d" % len(data))
            request.send_header("Content-Length", "0")
            request.send_header("Connection", "close")
            request.end_headers()
            return True
        request.send_response(206 if ranged else 200)
        request.send_header("Content-Type", "application/octet-stream")
        request.send_header("Content-Length", str(last - offset + 1))
        if ranged:
            request.send_header("Content-Range", "bytes %d-%d/%d" % (offset, last, len(data)))
        request.send_header("Accept-Ranges", "bytes")
        request.send_header("Connection", "close")
        request.end_headers()
        if body:
            try:
                request.wfile.write(data[offset:last + 1])
            except OSError:
                pass
        return True

    def _start_fetch(self, job, entry: CachedSource, region: CachedRegion, only=None) -> None:
        entry.started(region)
        threading.Thread(target=self._fetch_region, args=(job, entry, region, only),
                         name="prefetch", daemon=True).start()

    def _fetch_region(self, job, entry: CachedSource, region: CachedRegion, only=None) -> None:
        me = object()
        began = time.monotonic()
        via = None
        try:
            for attempt in range(3):
                if region.done or region.owner not in (None, me) or job.stopped.is_set():
                    return
                stream = None
                try:
                    if region.start is None:
                        begin, range_value = None, "bytes=-%d" % region.suffix
                    else:
                        begin = region.start + (len(region.data) if region.owner is me else 0)
                        range_value = "bytes=%d-%d" % (begin, region.end - 1)
                    # 자격 갱신·rclone 재시작은 ffmpeg 의 요청에 맡긴다(미리 받기는 덤이다).
                    conn, response, via = self._open_source(job, "media", range_value, renew=False, only=only)
                    stream = _Stream(conn, response, via)
                    match = re.match(r"bytes (\d+)-(\d+)/(\d+)$", response.getheader("Content-Range") or "")
                    if response.status != 206 or not match:
                        return
                    start, last, total = (int(value) for value in match.groups())
                    if not entry.set_size(total, response.getheader("Content-Type")):
                        if via == "rclone":
                            self._fall_back_to_server(job, "이 기기의 rclone 이 본 파일 크기가 다릅니다")
                        return
                    if start != (region.start if begin is None else begin):
                        return
                    stream.offset, stream.total = start, total

                    def write(block) -> bool:
                        if not region.append(me, block):
                            return False                 # 다른 쪽이 먼저 채우고 있다
                        if region.start == 0 and not entry.planned and len(region.data) >= SOURCE_PLAN_BYTES:
                            self._plan(job, entry, bytes(region.data))
                        return True

                    self._pump(job, stream, last, write)
                    return
                except (OSError, http.client.HTTPException, AgentError):
                    if attempt < 2:
                        time.sleep(1 + attempt)
                finally:
                    if stream is not None:
                        stream.close()
        finally:
            region.finish(me)
            entry.ended()
            if region.owner is me:
                if region.start == 0 and not entry.planned:
                    self._plan(job, entry, bytes(region.data))
                log("job %s 원본 %s %dKB 미리 받음 %.1f초%s" % (
                    job.id[:8], "앞" if region.start == 0 else "뒤" if region.suffix else
                    "%.1fMB 지점" % (region.start / 1048576), len(region.data) // 1024,
                    time.monotonic() - began, " (서버)" if via == "server" else ""))

    def _plan(self, job, entry: CachedSource, head: bytes) -> None:
        """앞부분으로 구조를 읽어 ffmpeg 가 읽을 나머지 범위도 받기 시작한다."""
        with entry.lock:
            if entry.planned or entry.size is None:
                return
            entry.planned = True
        reader = next((j for j in reversed(entry.jobs)
                       if getattr(j, "nonce", None) and not j.stopped.is_set()), job)
        for start, end in plan_source_regions(head, entry.size):
            if entry.nbytes + (end - start) > SOURCE_CACHE_BYTES // 2:
                break
            region = entry.add(start, end)
            if region is not None:
                self._fetch(reader, entry, region)

    def warm(self, link, command: dict, rclone_remotes=()) -> None:
        """곧 변환할 듯한 원본을 미리 받아 둔다(서버의 warm 명령). rclone 이 경로도 미리 찾는다."""
        try:
            source = clean_source(command.get("source"), link.api.host, rclone_remotes)
        except AgentError:
            return
        if source["kind"] == "server":
            return
        source["server_fallback"] = False
        part_id = _int(command.get("part_id"), 1, 2 ** 62)
        job = type("WarmJob", (), {})()
        job.id, job.api, job.source, job.part_id, job.cache = "warm" + "0" * 4, link.api, source, part_id, None
        job.stopped = threading.Event()
        self.prefetch(job)

    def _serve_cached(self, request, job, offset: int, end: str, body: bool):
        """받아 둔 구간에서 응답한다. None = 해당 없음, True = 다 보냄, 숫자 = 그 자리부터 원본에서."""
        entry = getattr(job, "cache", None)
        if entry is None:
            return None
        deadline = time.monotonic() + SOURCE_PREFETCH_WAIT_SEC
        entry.sized.wait(SOURCE_PREFETCH_WAIT_SEC)
        total = entry.size
        if total is None or entry.region_at(offset) is None:
            return None
        last = min(int(end), total - 1) if end else total - 1
        if offset > last:
            return None
        chunk = entry.region_at(offset).read(offset, deadline)
        if chunk is None:
            return None
        entry.used = time.monotonic()
        request.send_response(206)
        request.send_header("Content-Type", entry.content_type or "application/octet-stream")
        request.send_header("Content-Length", str(last - offset + 1))
        request.send_header("Content-Range", "bytes %d-%d/%d" % (offset, last, total))
        request.send_header("Accept-Ranges", "bytes")
        request.send_header("Connection", "close")
        request.end_headers()
        if not body:
            return True
        while chunk is not None:
            chunk = chunk[:last - offset + 1]
            try:
                request.wfile.write(chunk)
            except OSError:
                return True
            offset += len(chunk)
            if offset > last:
                return True
            region = entry.region_at(offset)      # 받아 둔 구간이 이어지면 계속 거기서
            chunk = region.read(offset, deadline) if region is not None else None
        return offset

    # 원본 고르기와 열기
    def _server_first(self, job) -> bool:
        """rclone 원본을 서버가 대신 내줄 때: rclone 이 그 경로를 아직 못 찾았다."""
        source = job.source
        return (source.get("kind") == "rclone" and bool(source.get("server_fallback"))
                and getattr(job, "nonce", None) is not None and not getattr(job, "server_failed", False)
                and self.rclone is not None and not self.rclone.ready(source))

    def _server_can_serve(self, job) -> bool:
        source = job.source
        return (source.get("kind") == "rclone" and bool(source.get("server_fallback"))
                and getattr(job, "nonce", None) is not None and not getattr(job, "server_failed", False))

    def _via(self, job, what: str) -> str:
        if what == "subtitle":
            return "subtitle"
        source = job.source
        if source["kind"] == "rclone":
            if self.rclone is None:
                raise AgentError("이 기기는 rclone 원본을 쓰도록 설정되지 않았습니다")
            if self.rclone.unreadable(source) and self._fall_back_to_server(job, "조금 전에도 읽지 못했습니다"):
                return "server"
            return "server" if self._server_first(job) else "rclone"
        return source["kind"]

    def _upstream(self, job, via: str, fresh: bool = False) -> tuple:
        api = getattr(job, "api", None) or self.api       # 그 작업을 준 서버
        authorization = {"Authorization": "Bearer " + api.token}
        if via == "subtitle":
            return api.base_url + "/transcode-agent/jobs/%s/subtitle" % job.id, authorization
        if via == "server":
            return api.base_url + "/transcode-agent/jobs/%s/source" % job.id, authorization
        if via == "rclone":
            return self.rclone.upstream(job.source, fresh)
        return job.source["url"], dict(job.source["headers"])

    def _fall_back_to_server(self, job, why: str) -> bool:
        """이 기기의 rclone 으로 못 읽는 원본을 서버가 내주는 것으로 바꿔 읽는다(서버가 내줄 수
        있다고 알린 작업만). 이름만 같고 내용이 다른 리모트, 맞지 않는 rclone 빌드에서 그렇다."""
        source = job.source
        if source.get("kind") != "rclone" or not source.get("server_fallback"):
            return False
        if self.rclone is not None:
            self.rclone.mark_unreadable(source)
        log("job %s 이 기기의 rclone 리모트 '%s' 로 원본을 읽지 못해 서버에서 받습니다(%s). "
            "서버의 리모트와 같은 것인지, rclone 빌드가 맞는지 확인하세요." % (job.id[:8], source["remote"], why))
        job.source = {"kind": "server"}
        return True

    def _server_failed(self, job, via: str, why: str) -> bool:
        """서버가 rclone 원본을 대신 내주다 실패하면 이 작업은 rclone 으로만 읽는다."""
        if via != "server" or not self._server_can_serve(job):
            return False
        job.server_failed = True
        log("job %s 서버에서 원본을 받지 못해 이 기기의 rclone 으로 읽습니다(%s)" % (job.id[:8], why))
        return True

    def _open_source(self, job, what: str, range_value, renew: bool = True, only=None) -> tuple:
        """원본을 열어 (conn, response, via) 를 돌려준다. via: rclone | server | url | subtitle.
        자격 만료(401/403)는 한 번 갱신하고, rclone 의 404 는 rclone 을 새로 띄워 한 번 더 보고, 그래도
        못 읽으면 서버가 내주는 것으로 바꾼다. 서버가 못 내주면 rclone 으로 돌아간다."""
        refreshed = fresh = False
        while True:
            try:
                via = self._via(job, what)
                if only is not None and via in ("server", "rclone") and job.source.get("kind") == "rclone":
                    if only == "server" and not self._server_can_serve(job):
                        raise AgentError("서버가 이 원본을 내주지 않습니다")
                    via = only
                if via == "server" and job.source.get("kind") == "rclone" and only is None:
                    self._probe_rclone(job)                   # 그사이 rclone 이 경로를 찾게 한다
                url, headers = self._upstream(job, via, fresh)
            except AgentError as exc:
                if what == "media" and self._fall_back_to_server(job, str(exc)):
                    continue                                  # rclone 을 띄우지 못했다
                raise
            fresh = False
            if range_value:
                headers["Range"] = range_value
            try:
                if via == "rclone" and what == "media" and only is None and self._server_can_serve(job):
                    via, conn, response = self._open_hedged(job, url, headers, range_value)
                else:
                    conn, response = self._open(url, headers, timeout=RCLONE_STALL_SEC if via == "rclone" else 60)
            except (OSError, http.client.HTTPException) as exc:
                if self._server_failed(job, via, str(exc)):
                    continue
                raise
            status = response.status
            retry = False
            if what == "media" and renew and not refreshed and via == "url" and status in (401, 403):
                refreshed = retry = True
            elif what == "media" and renew and not refreshed and via == "rclone" and status == 404:
                # rclone 이 기억하는 폴더 ID 가 낡았을 수 있다(폴더를 다시 만듦) — 새로 띄워 다시 본다.
                refreshed = fresh = retry = True
            elif what == "media" and via == "rclone" and status in (401, 403, 404):
                retry = self._fall_back_to_server(job, "HTTP %d" % status)
            elif what == "media" and status >= 400:
                retry = self._server_failed(job, via, "HTTP %d" % status)
            if retry:
                response.read()
                conn.close()
                if via == "url":
                    job.refresh_source()
                continue
            if via == "rclone" and status in (200, 206):
                self.rclone.mark_ready(job.source)
            if via == "server" and job.source.get("kind") == "rclone" and not getattr(job, "server_noted", False):
                job.server_noted = True
                log("job %s rclone 이 경로를 찾는 동안 서버에서 원본을 받습니다" % job.id[:8])
            return conn, response, via

    def _open_hedged(self, job, url: str, headers: dict, range_value) -> tuple:
        """이 기기의 rclone 으로 연다. RCLONE_HEDGE_SEC 안에 응답이 없으면 서버에도 같은 요청을 보내고
        먼저 제대로 응답한 쪽을 쓴다. 반환 (via, conn, response)."""
        results: queue.Queue = queue.Queue()

        def attempt(via: str, target: str, sent: dict) -> None:
            conn = None
            try:
                conn, response = self._open(target, sent, timeout=RCLONE_STALL_SEC if via == "rclone" else 60)
                if response.status < 400:
                    # 서버는 응답 머리를 바로 보내고 데이터는 원본을 연 뒤에 보낸다 — 첫 바이트로 비교한다.
                    response.peek(1)
                results.put((via, conn, response))
            except (OSError, http.client.HTTPException) as exc:
                if conn is not None:
                    conn.close()
                results.put((via, None, exc))

        threading.Thread(target=attempt, args=("rclone", url, headers), daemon=True).start()
        try:
            return self._hedge_result(results.get(timeout=RCLONE_HEDGE_SEC))
        except queue.Empty:
            pass
        server_url, server_headers = self._upstream(job, "server")
        if range_value:
            server_headers["Range"] = range_value
        threading.Thread(target=attempt, args=("server", server_url, server_headers), daemon=True).start()
        first = results.get()
        if first[1] is not None and first[2].status < 400:
            log("job %s rclone 이 %.0f초 안에 응답하지 않아 %s 응답을 씁니다" % (
                job.id[:8], RCLONE_HEDGE_SEC, "서버" if first[0] == "server" else "늦게 온 rclone"))

            def close_other() -> None:
                other = results.get()
                if other[1] is not None:
                    other[1].close()

            threading.Thread(target=close_other, daemon=True).start()
            return first
        second = results.get()                       # 먼저 온 쪽이 실패했다 — 나머지를 본다
        usable = second[1] is not None and second[2].status < 400
        keep, drop = (second, first) if usable or first[1] is None else (first, second)
        if drop[1] is not None:
            drop[1].close()
        return self._hedge_result(keep)

    @staticmethod
    def _hedge_result(result) -> tuple:
        via, conn, value = result
        if conn is None:
            raise value
        return via, conn, value

    def _probe_rclone(self, job) -> None:
        """이 기기의 rclone 이 그 파일을 찾게 한다(한 경로에 하나씩). 찾으면 그때부터 rclone 으로 읽는다."""
        source = dict(job.source)
        key = (source["remote"], source["folder_id"], source["path"])
        with self._probe_lock:
            last = self._probes.get(key)
            if last is True or (last is not None and time.monotonic() - last < RCLONE_PROBE_RETRY_SEC):
                return
            self._probes[key] = True
        threading.Thread(target=self._run_probe, args=(job, source, key), name="rclone-probe", daemon=True).start()

    def _run_probe(self, job, source: dict, key) -> None:
        began = time.monotonic()
        try:
            for fresh in (False, True):
                conn = None
                try:
                    url, headers = self.rclone.upstream(source, fresh)
                    headers["Range"] = "bytes=0-0"
                    conn, response = self._open(url, headers, timeout=RCLONE_STALL_SEC)
                    response.read()
                    status = response.status
                finally:
                    if conn is not None:
                        conn.close()
                if status in (200, 206):
                    cache = getattr(job, "cache", None)
                    total = _response_total(response)
                    if cache is not None and cache.size is not None and total != cache.size:
                        self._fall_back_to_server(job, "이 기기의 rclone 이 본 파일 크기가 서버와 다릅니다")
                        return
                    self.rclone.mark_ready(source)
                    log("job %s rclone 이 원본 경로를 찾음 %.1f초" % (job.id[:8], time.monotonic() - began))
                    return
                if status != 404:
                    break                   # 404 는 rclone 을 새로 띄워 한 번 더 본다(폴더를 다시 만듦)
            if status in (401, 403, 404):
                self._fall_back_to_server(job, "HTTP %d" % status)
        except AgentError as exc:
            self._fall_back_to_server(job, str(exc))
        except (OSError, http.client.HTTPException):
            pass
        finally:
            with self._probe_lock:
                self._probes[key] = time.monotonic()

    def _open(self, url: str, headers: dict, redirects: int = 5, timeout: float = 60):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(
                parsed.hostname, parsed.port, timeout=timeout, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
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
            return self._open(location, headers, redirects - 1, timeout)
        return conn, response

    def _rclone_ready(self, job) -> bool:
        source = job.source
        return (source.get("kind") == "rclone" and self.rclone is not None
                and self.rclone.ready(source) and not self.rclone.unreadable(source))

    def _pump(self, job, stream: _Stream, last, write) -> bool:
        """stream 의 응답을 write 로 옮긴다(last 바이트까지, None 이면 끝까지). True = 다 옮김,
        False = 받는 쪽이 그만둠. 원본이 끊기면 OSError(stream.offset 은 옮긴 데까지).

        서버가 rclone 원본을 대신 내주는 중에 rclone 이 준비되면, 지금 받는 속도로 몇 초 앞에 rclone 을
        미리 열고 서버에서 그 바이트까지 받은 뒤 rclone 으로 갈아탄다."""
        length = stream.response.getheader("Content-Length")
        remaining = int(length) if length and length.isdigit() else None
        handover, retry_at = None, 0.0
        began, first = time.monotonic(), stream.offset
        try:
            while True:
                want = PIPE_CHUNK_BYTES
                if handover is not None and handover.target > stream.offset:
                    want = min(want, handover.target - stream.offset)
                block = stream.response.read(want)
                if not block:
                    if remaining:
                        # 약속한 길이보다 일찍 끊겼다 — 받은 자리부터 이어 받는다.
                        raise OSError("원본 연결이 %d바이트를 남기고 끊겼습니다" % remaining)
                    return True
                if not write(block):
                    return False
                stream.offset += len(block)
                if remaining is not None:
                    remaining -= len(block)
                if (handover is None and stream.via == "server" and stream.total is not None
                        and time.monotonic() >= retry_at and self._rclone_ready(job)):
                    rate = (stream.offset - first) / max(0.25, time.monotonic() - began)
                    lead = int(min(HANDOVER_MAX_BYTES, max(HANDOVER_MIN_BYTES, rate * HANDOVER_LEAD_SEC)))
                    target = stream.offset + lead
                    if (last if last is not None else stream.total - 1) >= target + HANDOVER_MIN_BYTES:
                        handover = _Handover(self, job, target, last, stream.total)
                    else:
                        retry_at = float("inf")           # 남은 것이 적다 — 서버에서 마저 받는다
                if handover is not None and stream.offset >= handover.target:
                    taken = handover.take(stream.offset)
                    if taken is None:
                        continue                          # rclone 이 아직 열리는 중 — 서버에서 계속 받는다
                    handover = None
                    if taken is False:
                        retry_at = time.monotonic() + HANDOVER_RETRY_SEC
                        continue
                    conn, response, remaining = taken
                    log("job %s 원본을 %.0fMB 지점부터 rclone 으로 받습니다(서버에서 %.0fMB 받음)" % (
                        job.id[:8], stream.offset / 1048576, (stream.offset - first) / 1048576))
                    stream.switch(conn, response, "rclone")
        finally:
            if handover is not None:
                handover.cancel()

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
        failures = 0
        if what == "subtitle" and (match or not range_header):
            if self._serve_subtitle(request, job, offset, end, bool(match), body):
                request.close_connection = True
                return
        if what == "media" and match:
            served = self._serve_cached(request, job, offset, end, body)
            if served is True:
                request.close_connection = True
                return
            if served is not None:
                offset, sent_headers = served, True     # 받아 둔 끝부터 원본에서 이어 보낸다
        # 응답마다 연결을 닫고, 닫는다고 알린다. 알리지 않으면 ffmpeg 가 같은 연결로 다음 요청을
        # 보내려다(탐색) 실패하고, 파일 끝의 색인을 못 읽어 처음부터 훑게 된다.
        request.close_connection = True

        def write(block) -> bool:
            try:
                request.wfile.write(block)
                return True
            except OSError:
                return False        # ffmpeg 가 연결을 닫았다(탐색·종료)

        while True:
            stream = None
            try:
                ranged = bool(range_header or sent_headers)
                conn, response, via = self._open_source(
                    job, what, "bytes=%d-%s" % (offset, end) if ranged else None, renew=not sent_headers)
                stream = _Stream(conn, response, via, offset, _response_total(response))
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
                self._pump(job, stream, int(end) if end else None, write)
                return
            except (OSError, http.client.HTTPException, AgentError) as exc:
                if stream is not None:
                    offset = stream.offset
                    if stream.via == "rclone" and what == "media" and self._server_can_serve(job) and failures < 5:
                        # 이 기기의 rclone 이 끊겼다 — 서버에서 바로 이어 받고, rclone 은 다시 준비시킨다.
                        failures += 1
                        self.rclone.forget(job.source)
                        log("job %s rclone 읽기 실패, 서버에서 이어 받습니다: %s" % (job.id[:8], exc))
                        continue
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
                if stream is not None:
                    stream.close()


# ══ 작업 ══════════════════════════════════════════════════════════════
class Job(threading.Thread):
    def __init__(self, agent, command: dict, link=None):
        super().__init__(name="job-" + str(command.get("id"))[:8], daemon=True)
        self.agent = agent
        self.link = link                                  # 이 작업을 준 서버
        self.api = link.api if link is not None else agent.api
        self.id = str(command.get("id") or "")
        if not _JOB_ID_RE.fullmatch(self.id):
            raise AgentError("작업 ID 형식이 올바르지 않습니다")
        self.kind = command.get("kind")
        if self.kind not in ("hls", "pipe"):
            raise AgentError("모르는 작업 종류입니다")
        self.spec = clean_spec(command.get("spec"))
        self.source = clean_source(
            command.get("source"), self.api.host, getattr(agent, "rclone_remotes", ()))
        self.has_subtitle_file = command.get("has_subtitle_file") is True
        self.part_id = _int(command.get("part_id"), 1, 2 ** 62)     # 같은 파일의 작업끼리 미리 받은 것을 나눈다
        self.cache = None
        self.nonce = hashlib.sha256(os.urandom(32)).hexdigest()[:32]
        self.dir = agent.config.tmp_dir / ("job_" + self.id)
        self.proc = None
        self._suspended = False   # Windows: NtSuspendProcess 는 횟수를 세므로 상태가 바뀔 때만 부른다
        self.hw_decode = None     # 이 작업에서 쓰는 하드웨어 디코더(없으면 소프트웨어)
        self.output_codec = "h264"   # 이 작업의 출력 코덱과 인코더(_choose_encoder)
        self.encoder = None
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
        """재생 위치보다 너무 앞서 변환했으면 서버가 멈추라고 한다(0 CPU). 다시 깨우면 이어 만든다.

        Windows 에는 SIGSTOP 이 없다. 그대로 두면 ffmpeg 가 파일 끝까지 변환해 서버로 올리므로
        프로세스 전체를 NtSuspendProcess 로 멈춘다(서버가 자기 ffmpeg 를 멈추는 방식과 같다)."""
        if WINDOWS:
            proc = self.proc
            if proc is not None and proc.poll() is None and paused != self._suspended:
                if windows_suspend(proc.pid, paused):
                    self._suspended = paused
            return
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
            fresh = self.api.call("POST", "/transcode-agent/jobs/%s/source/refresh" % self.id)
            self.source = clean_source({"kind": "url", **(fresh or {})}, self.api.host)
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
            video_encoder=self.encoder or caps.get("video_encoder") or "libx264",
            vaapi_device=caps.get("vaapi_device"),
            audio_stream_index=spec["audio_stream_index"],
            audio_language=spec["audio_language"], audio_title=spec["audio_title"],
            hw_decode=self.hw_decode, output_codec=self.output_codec,
            audio_bitrate_kbps=spec["audio_bitrate_kbps"],
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

    def _choose_encoder(self) -> None:
        """서버가 고른 출력 코덱의 인코더를 쓴다. 이 기기에 그 인코더가 (더는) 없으면 H.264 로 낸다
        — 어느 재생 기기나 풀 수 있다."""
        encoders = output_encoders(self.agent.caps)
        wanted = self.spec["output_codec"]
        self.output_codec = wanted if wanted in encoders else "h264"
        self.encoder = encoders.get(self.output_codec)
        if wanted != self.output_codec:
            log("job %s 이 기기에 %s 인코더가 없어 H.264 로 변환합니다" % (self.id[:8], wanted))

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
            try:
                agent.proxy.prefetch(self)      # ffmpeg 가 헤더를 읽기 전에 앞·뒤를 동시에 받기 시작한다
                subtitle = self.spec.get("subtitle")
                if self.has_subtitle_file and subtitle and subtitle["source"] == "external":
                    agent.proxy.prefetch_subtitle(self)
            except Exception as exc:            # 미리 받기는 덤이다 — 못 해도 ffmpeg 가 직접 읽는다
                log("job %s 원본 미리 받기를 시작하지 못했습니다: %s" % (self.id[:8], exc))
            if video:
                self._choose_encoder()
            encoder = self.encoder
            self.hw_decode = hw_decode_for(
                agent.caps, self.spec["video_codec"], self.spec["video_bit_depth"], self.spec["hdr"],
            ) if video else None
            self._start_ffmpeg(encoder)
            # ffmpeg 를 띄우자마자 알린다. 서버는 몇 초 안에 이 보고가 없으면 러너가 꺼졌다고 보고
            # 다른 작업자에게 넘긴다. 첫 출력(원격 원본은 10초를 넘기기도 한다)을 기다린 뒤에 알리면
            # 멀쩡한 작업이 버려진다.
            self._report("running", encoder=encoder)
            first = b""
            if self.hw_decode:
                first = self._first_output()
                if first is None:
                    # 하드웨어 디코딩이 첫 출력도 못 내고 끝났다(손상된 파일·드라이버 문제).
                    log("job %s 하드웨어 디코딩 실패(%s) — 소프트웨어 디코딩으로 다시 시작: %s" % (
                        self.id[:8], self.hw_decode["hwaccel"], self._ffmpeg_error()))
                    self.hw_decode = None
                    self._start_ffmpeg(encoder)
                    first = b""
            if self.kind == "pipe":
                self._pump_pipe(first)
            else:
                started = time.monotonic()
                while self.proc.poll() is None and not self.stopped.is_set():
                    self._sync_outputs()
                    # 처음 몇 조각은 나오는 즉시 올린다 — 서버는 조각 둘을 받아야 재생기에 목록을 준다.
                    time.sleep(0.1 if time.monotonic() - started < 30 else 0.25)
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
            self.api.close()
            agent.job_finished(self)
            log("job %s 종료 rc=%s%s%s" % (
                self.id[:8], returncode, " (서버가 중지)" if cancelled else "",
                " err=" + error if error else ""))

    def _start_ffmpeg(self, encoder) -> None:
        agent = self.agent
        args = self._arguments()
        log("job %s 시작 kind=%s mode=%s encoder=%s decode=%s height=%s start_ms=%s source=%s" % (
            self.id[:8], self.kind, self.spec["mode"], encoder,
            (self.hw_decode or {}).get("hwaccel", "sw") if encoder else "-",
            self.spec["max_height"], self.spec["start_ms"], self.source["kind"]))
        with open(self.dir / "ffmpeg.log", "wb") as errors:
            self.proc = subprocess.Popen(
                args, cwd=str(self.dir), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if self.kind == "pipe" else subprocess.DEVNULL,
                stderr=errors, env=encoder_environment(agent.caps), **POPEN_FLAGS,
            )
        self._suspended = False
        # 에이전트가 강제 종료되면 ffmpeg 가 남는다(멈춰 둔 것은 멈춘 채로). Windows 는 에이전트와
        # 함께 끝나게 묶고, POSIX 는 다음 시작 때 정리할 수 있게 PID 를 적어 둔다.
        windows_end_with_agent(self.proc)
        (self.dir / "ffmpeg.pid").write_text(str(self.proc.pid))
        if self.stopped.is_set():
            self.stop()

    def _first_output(self):
        """ffmpeg 의 첫 출력까지 기다린다. 출력 없이 실패로 끝났으면 None.

        pipe 는 읽은 첫 블록을 돌려준다(_pump_pipe 가 그것부터 보낸다). HLS 는 첫 playlist·조각이
        생기면 빈 바이트를 돌려준다. 서버가 작업을 멈춘 경우는 실패로 보지 않는다.
        """
        proc = self.proc
        if self.kind == "pipe":
            block = os.read(proc.stdout.fileno(), 65536)
            if block or self.stopped.is_set() or proc.wait() == 0:
                return block
            return None
        while proc.poll() is None and not self.stopped.is_set():
            if any(path.suffix in (".m3u8", ".ts", ".m4s", ".mp4") for path in self.dir.iterdir()):
                return b""
            time.sleep(0.1)
        if self.stopped.is_set() or proc.returncode == 0:
            return b""
        if any(path.suffix in (".m3u8", ".ts", ".m4s", ".mp4") for path in self.dir.iterdir()):
            return b""
        return None

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
                status, _, reply = self.api.request(
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

    def _pump_pipe(self, first: bytes = b"") -> None:
        """progressive 출력: ffmpeg 표준 출력을 순서 번호를 붙여 서버로 보낸다. 서버는 클라이언트가
        받아 가는 만큼만 응답하므로, 여기서 기다리는 동안 ffmpeg 도 파이프에 막혀 기다린다."""
        descriptor = self.proc.stdout.fileno()
        can_select = os.name != "nt"
        sequence = 0
        pending = first
        while not self.stopped.is_set():
            block = pending or os.read(descriptor, 65536)
            pending = b""
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
class ServerLink:
    """이 러너가 붙어 있는 Shyni 서버 하나. 서버마다 따로 등록하고 따로 작업을 기다린다."""

    def __init__(self, entry: dict):
        self.api = Api(entry["url"], entry["token"])
        self.label = urllib.parse.urlsplit(self.api.base_url).netloc
        self.told_protocol = False
        self.told_local_network = False
        self.last_ok = time.monotonic()                   # 마지막으로 poll 이 성공한 때


class Agent:
    def __init__(self, config: Config):
        self.config = config
        data = config.data
        self.links = [ServerLink(entry) for entry in config.servers]
        self.ffmpeg = data["ffmpeg"]
        self.caps = detect_capabilities(self.ffmpeg, config.encoder)
        self.info = machine_info()
        self.jobs: dict = {}
        self.lock = threading.Lock()
        self.rclone_remotes = usable_rclone_remotes(data)
        self.rclone = RcloneServers(
            find_rclone(data.get("rclone")), data.get("rclone_config"), config.tmp_dir,
        ) if self.rclone_remotes else None
        self.proxy = SourceProxy(None, self.rclone)
        self.running = True
        self.restart_requested = False     # 서비스(Windows)로 돌 때 다른 명령이 다시 시작을 요청함

    def total_slots(self) -> int:
        """이 기기가 동시에 돌릴 작업 수 — 붙어 있는 모든 서버를 합친 한도다."""
        limit = self.config.data.get("slots")
        return max(1, int(limit)) if limit else recommended_slots(self.caps)

    def report(self, link, wait: float = POLL_WAIT_SEC) -> dict:
        tmp_dir = self.config.tmp_dir
        try:
            free = shutil.disk_usage(str(tmp_dir)).free
        except OSError:
            free = None
        with self.lock:
            mine = [job.id for job in self.jobs.values() if job.link is link]
            elsewhere = len(self.jobs) - len(mine)
        remotes = self.rclone_remotes
        if self.rclone is not None:
            failed = self.rclone.unreadable_remotes()
            remotes = [name for name in remotes if name not in failed]
        return {
            "protocol": PROTOCOL_VERSION, "agent_version": __version__,
            "platform": self.info,
            "capabilities": public_capabilities(self.caps, remotes),
            # 총 슬롯과, 그중 다른 서버의 작업이 쓰고 있는 수. 서버는 남은 만큼만 맡긴다.
            "recommended_slots": self.total_slots(), "busy_elsewhere": elsewhere,
            "temp_dir": str(tmp_dir), "temp_free_bytes": free,
            "jobs": mine, "wait": wait,
        }

    def handle(self, link, command: dict) -> None:
        kind = command.get("type")
        if kind == "warm":
            self.proxy.warm(link, command, getattr(self, "rclone_remotes", ()))
            return
        if kind == "start":
            payload = command.get("job") if isinstance(command.get("job"), dict) else {}
            try:
                with self.lock:
                    # 두 서버가 마지막 슬롯을 동시에 주면 늦은 쪽을 거절한다 — 그 서버가 다른
                    # 작업자를 고른다. 한도는 여기서만 확실히 지켜진다.
                    if len(self.jobs) >= self.total_slots():
                        raise AgentError("이 기기의 동시 작업 한도(%d)가 찼습니다" % self.total_slots())
                    job = Job(self, payload, link)
                    self.jobs[job.id] = job
            except AgentError as exc:
                log("%s 의 작업을 받을 수 없습니다: %s" % (link.label, exc))
                job_id = str(payload.get("id") or "")
                if _JOB_ID_RE.fullmatch(job_id):
                    try:
                        link.api.call("POST", "/transcode-agent/jobs/%s/state" % job_id,
                                      {"state": "failed", "error": str(exc)})
                    except AgentError:
                        pass
                return
            job.start()
            self.tell_others(link)
            return
        job = self.jobs.get(str(command.get("job_id")))
        if job is None or job.link is not link:
            return
        if kind == "stop":
            job.stop()
        elif kind == "pause":
            job.pause(True)
        elif kind == "resume":
            job.pause(False)

    def stop_jobs(self, link, why: str) -> None:
        """한 서버가 맡긴 작업을 모두 멈춘다. 멈춰 둔(pause) 작업은 올릴 것이 없어 서버가 사라진
        것을 스스로 알지 못한다 — 그대로 두면 다른 서버가 쓸 슬롯을 계속 잡는다."""
        with self.lock:
            jobs = [job for job in self.jobs.values() if job.link is link]
        if jobs:
            log("%s: %s — 그 서버의 작업 %d개를 멈춥니다" % (link.label, why, len(jobs)))
        for job in jobs:
            job.stop()

    def job_finished(self, job) -> None:
        with self.lock:
            self.jobs.pop(job.id, None)
        self.tell_others(job.link)

    def tell_others(self, origin) -> None:
        """작업 수가 바뀌면 다른 서버들에 바로 알린다 — 걸려 있는 poll 이 끝나길 기다리지 않는다."""
        for link in self.links:
            if link is not origin and self.running:
                threading.Thread(target=self._push_status, args=(link,), daemon=True).start()

    def _push_status(self, link) -> None:
        try:
            reply = link.api.call("POST", "/transcode-agent/poll", self.report(link, wait=0), timeout=30)
        except AgentError:
            return
        finally:
            link.api.close()
        self._apply(link, reply)

    def _apply(self, link, reply) -> bool:
        if not reply:
            return True
        if reply.get("protocol") != PROTOCOL_VERSION:
            if not link.told_protocol:
                link.told_protocol = True
                log("%s: 서버와 에이전트의 프로토콜 버전이 다릅니다(서버 %s, 에이전트 %s). "
                    "이 기기에서 update 를 실행하거나 서버를 올리세요."
                    % (link.label, reply.get("protocol"), PROTOCOL_VERSION))
            return False
        for command in reply.get("commands") or []:
            if isinstance(command, dict):
                self.handle(link, command)
        return True

    def run(self) -> int:
        if not self.caps.get("video_encoder"):
            raise AgentError("ffmpeg 를 쓸 수 없습니다: %s (install 을 다시 실행하세요)" % self.ffmpeg)
        tmp_dir = self.config.tmp_dir
        tmp_dir.mkdir(parents=True, exist_ok=True)
        for leftover in tmp_dir.glob("job_*"):      # 이전 실행이 남긴 작업 폴더와 ffmpeg
            kill_leftover_ffmpeg(leftover)
            shutil.rmtree(str(leftover), ignore_errors=True)
        log("에이전트 %s 시작: 서버=%s 인코더=%s(%s) 코덱=%s 디코딩=%s 톤매핑=%s 총 슬롯=%d 임시 폴더=%s" % (
            __version__, ", ".join(link.api.base_url for link in self.links),
            self.caps.get("video_encoder"),
            "H/W" if self.caps.get("hw_encoder") else "S/W", describe_output_codecs(self.caps),
            describe_hw_decode(self.caps),
            bool(self.caps.get("tonemap")), self.total_slots(), tmp_dir))
        if self.rclone_remotes:
            log("rclone 으로 직접 읽을 리모트: %s" % ", ".join(self.rclone_remotes))
        if any(link.api.scheme == "http" for link in self.links):
            log("주의: 서버 주소가 http 입니다. 같은 내부망이 아니면 https 주소로 등록하세요.")
        for link in self.links:
            threading.Thread(target=self._serve, args=(link,), name="poll-" + link.label, daemon=True).start()
        try:
            while self.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        self.running = False
        log("에이전트 종료 중")
        for job in list(self.jobs.values()):
            job.stop()
        for job in list(self.jobs.values()):
            job.join(timeout=5)
        if self.rclone is not None:
            self.rclone.close()
        return 0

    def _serve(self, link) -> None:
        delay = 2
        while self.running:
            try:
                reply = link.api.call("POST", "/transcode-agent/poll", self.report(link),
                                      timeout=POLL_WAIT_SEC + 20)
                delay = 2
                link.last_ok = time.monotonic()
            except RunnerRemoved:
                self.forget(link)
                return
            except Unauthorized:
                log("%s 가 이 러너를 모릅니다(등록 해제됨). 그 서버의 설치 명령을 다시 실행하거나 "
                    "remove-server 로 지우세요. 1분 뒤 재시도합니다." % link.label)
                self.stop_jobs(link, "등록이 해제됐습니다")
                self._sleep(60)
                continue
            except AgentError as exc:
                now = time.monotonic()
                if delay == 2 or now - getattr(link, "failure_logged", 0.0) >= RECONNECT_LOG_EVERY_SEC:
                    link.failure_logged = now
                    log("%s: %s — %d초 뒤 다시 연결합니다" % (link.label, exc, delay))
                if time.monotonic() - link.last_ok > SERVER_LOST_SEC:
                    self.stop_jobs(link, "서버 응답이 %d초 넘게 없습니다" % SERVER_LOST_SEC)
                if ("No route to host" in str(exc) and platform.system() == "Darwin"
                        and not link.told_local_network):
                    link.told_local_network = True
                    log("내부망 서버에 닿지 못합니다. macOS 가 서비스의 로컬 네트워크 접근을 막았을 수 "
                        "있습니다: 시스템 설정 → 개인정보 보호 및 보안 → 로컬 네트워크에서 Python 을 켜세요.")
                self._sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_SEC)
                continue
            if not self._apply(link, reply):
                self._sleep(60)

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while self.running and time.monotonic() < deadline:
            time.sleep(0.5)

    def forget(self, link) -> None:
        """관리자가 그 서버의 러너 목록에서 이 러너를 지웠다. 이 기기에서도 그 서버 등록을 지우고, 붙은
        서버가 남지 않으면 자동 시작을 해제하고 끝낸다. 그래서 같은 설치 명령을 다시 실행하면 처음처럼
        등록된다. 폴더(받아 둔 ffmpeg 등)는 남긴다 — 다시 설치할 때 그대로 쓴다."""
        self.stop_jobs(link, "등록이 해제됐습니다")
        with self.lock:
            # 그사이 configure·install 이 바꾼 설정을 덮지 않도록 파일을 다시 읽어 그 등록만 뺀다.
            # 다시 설치해 토큰이 바뀌었으면 지울 것이 없다.
            config = Config(self.config.home)
            kept = [entry for entry in config.servers if entry.get("token") != link.api.token]
            removed = len(kept) != len(config.servers)
            if removed:
                config.data["servers"] = kept
                config.save()
            self.config.data["servers"] = list(kept)
            self.links = [item for item in self.links if item is not link]
        if not removed:
            return
        log("%s 가 이 러너를 지웠습니다. 이 기기에서도 그 서버 등록을 지웠습니다." % link.label)
        if kept:
            return
        log("붙은 서버가 없어 자동 시작을 해제하고 끝냅니다. 폴더 %s 는 남겨 둡니다(다시 설치하면 받아 둔 "
            "ffmpeg 를 그대로 씀, 필요 없으면 지워도 됨)." % self.config.home)
        self.running = False
        remove_own_autostart(self.config.home)

    def shutdown(self, *_args) -> None:
        self.running = False


def kill_leftover_ffmpeg(job_dir: Path) -> None:
    """이전 실행이 남긴 작업의 ffmpeg 를 끝낸다. 그 PID 가 정말 그 작업의 ffmpeg 일 때만
    (명령줄에 작업 폴더가 있을 때만) 건드린다 — PID 는 다른 프로세스가 다시 쓸 수 있다.
    Windows 는 ffmpeg 를 에이전트와 함께 끝나게 묶어 두므로(windows_end_with_agent) 남지 않는다."""
    if WINDOWS:
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


CODEC_LABELS = {"h264": "H.264", "hevc": "H.265", "av1": "AV1"}


def describe_output_codecs(caps: dict) -> str:
    encoders = output_encoders(caps)
    names = [CODEC_LABELS[codec] for codec in reversed(OUTPUT_CODECS) if codec in encoders]
    if len(names) > 1 and not caps.get("hls_fmp4_subtitles"):
        return ", ".join(names) + " (자막을 같이 싣는 재생은 H.264)"
    return ", ".join(names) or "-"


def public_capabilities(caps: dict, rclone_remotes=()) -> dict:
    decode = caps.get("hw_decode") or {}
    return {
        "video_encoder": caps.get("video_encoder"), "hw_encoder": caps.get("hw_encoder"),
        # 출력 코덱별 인코더. 이 가운데 어느 코덱을 쓸지는 서버의 관리 화면과 재생 기기가 정한다.
        "video_encoders": output_encoders(caps),
        # fMP4 HLS 에 자막 rendition 을 같이 실을 수 있는지 — HEVC·AV1 + 자막에 필요하다.
        "hls_fmp4_subtitles": bool(caps.get("hls_fmp4_subtitles")),
        "tonemap": bool(caps.get("tonemap")), "ffmpeg_version": caps.get("ffmpeg_version"),
        # 하드웨어로 풀 수 있는 원본 코덱 — 서버가 그 원본을 풀 수 있는 러너를 먼저 고른다.
        "hw_decode": {"hwaccel": decode["hwaccel"], "codecs": decode["codecs"]} if decode.get("codecs") else None,
        # 이 기기의 rclone 으로 직접 읽을 수 있는 리모트 — 서버가 같은 이름의 원본을 맡길 때 쓴다.
        "rclone_remotes": list(rclone_remotes),
    }


# ══ 명령 ══════════════════════════════════════════════════════════════
def _describe(caps: dict, ffmpeg: str, slots: int, tmp_dir: Path, rclone_remotes=()) -> None:
    print("  인코더   : %s (%s)" % (caps.get("video_encoder"), "H/W" if caps.get("hw_encoder") else "S/W"))
    print("  출력 코덱: %s" % describe_output_codecs(caps))
    print("  톤매핑   : %s" % ("가능" if caps.get("tonemap") else "불가"))
    print("  디코딩   : %s" % describe_hw_decode(caps))
    print("  ffmpeg   : %s (%s)" % (ffmpeg, caps.get("ffmpeg_version")))
    print("  총 슬롯  : %d" % slots)
    print("  임시 폴더: %s" % tmp_dir)
    print("  rclone   : %s" % (", ".join(rclone_remotes) if rclone_remotes else "쓰지 않음(원본은 서버나 클라우드 주소로 읽음)"))


def apply_rclone_options(config: Config, args, installing: bool = False) -> None:
    """--rclone-remote 로 적은 리모트를 이 기기의 rclone 설정에서 확인하고 허용 목록에 올린다.

    configure 는 목록을 적은 것으로 바꾸고, 없는 리모트·rclone 은 오류다. install 은 서버 관리자 화면의
    설치 명령이 그 서버의 리모트 이름을 모두 적어 오므로(0.6.1+) 이 기기에 있는 것만 지금 목록에 더하고
    나머지는 알리고 건너뛴다 — rclone 이 없는 기기도 그대로 설치되고, 다른 서버를 더해도 앞 서버의
    리모트가 지워지지 않는다."""
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
    missing = []
    if remotes:
        binary = find_rclone(config.data.get("rclone"))
        if not binary:
            if installing:
                print("참고: rclone 을 찾지 못해 rclone 리모트(%s)를 쓰지 않습니다. 원본은 서버가 내줍니다. "
                      "rclone 이 다른 곳에 있으면 --rclone <경로> 를 붙여 다시 실행하세요." % ", ".join(remotes))
                return
            raise AgentError("rclone 을 찾을 수 없습니다: %s (--rclone 로 경로를 지정하세요)"
                             % (config.data.get("rclone") or "rclone"))
        config.data["rclone"] = binary       # 서비스로 돌 때도 찾도록 전체 경로를 적어 둔다
        present = rclone_list_remotes(binary, config.data.get("rclone_config"))
        missing = [name for name in remotes if name not in present]
        if missing and not installing:
            raise AgentError("이 기기의 rclone 설정에 없는 리모트입니다: %s (있는 것: %s)" % (
                ", ".join(missing), ", ".join(present) or "없음"))
        if missing:
            print("참고: 이 기기의 rclone 설정에 없는 리모트는 쓰지 않습니다: %s" % ", ".join(missing))
    if installing:
        remotes = list(dict.fromkeys(list(config.data.get("rclone_remotes") or [])
                                     + [name for name in remotes if name not in missing]))
    config.data["rclone_remotes"] = remotes


def command_install(args) -> int:
    """이 기기를 서버에 등록한다. 이미 다른 서버에 등록된 폴더면 그 서버를 **추가**한다."""
    home = Path(args.dir).expanduser().resolve()
    config = Config(home)
    if not args.url or not args.token:
        raise AgentError("--url 과 --token 이 필요합니다")
    api = Api(args.url, args.token)
    # 이미 이 서버에 등록된 폴더면 그 등록을 바꾼다(0.6.1 부터 --force 불필요). 관리자가 러너를 지운 뒤
    # 다시 설치하는 경우이거나, 아직 서버에 있으면 서버가 이전 러너를 지워(replaces) 둘로 남지 않는다.
    existing = next((entry for entry in config.servers if entry.get("url") == api.base_url), None)
    adding = existing is None and bool(config.servers)
    if args.encoder or not config.data.get("encoder"):
        encoder = str(args.encoder or "auto").lower()
        if encoder not in ENCODER_ALIASES:
            raise AgentError("--encoder 는 %s 중 하나여야 합니다" % ", ".join(sorted(ENCODER_ALIASES)))
        config.data["encoder"] = encoder
    if args.tmp or not config.data.get("tmp_dir"):
        config.data["tmp_dir"] = str(Path(args.tmp).expanduser().resolve() if args.tmp else home / "tmp")
    tmp_dir = config.tmp_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    if args.slots:
        config.data["slots"] = max(1, int(args.slots))
    apply_rclone_options(config, args, installing=True)
    rclone_remotes = usable_rclone_remotes(config.data)
    ffmpeg, caps = choose_ffmpeg(config, args.ffmpeg)
    config.data["ffmpeg"] = ffmpeg
    slots = max(1, int(config.data["slots"])) if config.data.get("slots") else recommended_slots(caps)
    try:
        free = shutil.disk_usage(str(tmp_dir)).free
    except OSError:
        free = None
    registered = api.call("POST", "/transcode-agent/register", {
        "name": args.name or (config.servers[0].get("name") if config.servers else None)
                or socket.gethostname().split(".")[0],
        "protocol": PROTOCOL_VERSION, "agent_version": __version__,
        "platform": machine_info(), "capabilities": public_capabilities(caps, rclone_remotes),
        "recommended_slots": slots, "temp_dir": str(tmp_dir), "temp_free_bytes": free,
        **({"replaces": existing.get("token")} if existing is not None and existing.get("token") else {}),
    })
    entry = {"url": api.base_url, "token": registered["token"],
             "runner_id": registered["runner_id"], "name": registered["name"]}
    if existing is not None:
        existing.clear()
        existing.update(entry)
    else:
        config.servers.append(entry)
    config.save()
    print()
    print("등록 완료: %s (러너 #%s) → %s" % (registered["name"], registered["runner_id"], api.base_url))
    if registered.get("replaced"):
        print("  이 폴더의 이전 등록(러너 #%s)은 서버에서 지웠습니다." % registered["replaced"])
    if adding:
        print("  이 기기가 붙은 서버 %d곳이 총 슬롯 %d개를 같이 씁니다." % (len(config.servers), slots))
    _describe(caps, ffmpeg, slots, tmp_dir, rclone_remotes)
    hint = hardware_hint(caps, config.encoder)
    if hint:
        print("  참고     : " + hint)
    if args.no_service:
        print("\n실행: %s %s run --dir %s" % (sys.executable, Path(__file__).resolve(), home))
        if adding:
            print("실행 중인 에이전트가 있으면 다시 시작해야 새 서버에 붙습니다.")
        return 0
    if _external_volume(home) or _external_volume(config.tmp_dir):
        print("\n참고: 외장 디스크 폴더는 macOS 가 서비스(launchd)의 접근을 막습니다. "
              "전체 디스크 접근 권한에 %s 를 추가해야 서비스로 돕니다." % sys.executable)
    return command_service(argparse.Namespace(dir=str(home), action="install"))


def _require_config(args) -> Config:
    config = Config(Path(args.dir).expanduser().resolve())
    if not config.servers:
        raise AgentError("등록되지 않은 폴더입니다: %s (먼저 install)" % config.home)
    return config


def command_run(args) -> int:
    config = _require_config(args)
    background = bool(getattr(args, "background", False))
    if background:
        _redirect_output_to_log(config.home)
    # 서비스가 재시작하는 동안 앞 프로세스가 잠금을 놓기를 기다린다.
    lock = acquire_instance_lock(config.home, wait=60 if background else 30)
    if lock is None:
        message = "이 폴더의 에이전트가 이미 돌고 있습니다: %s" % config.home
        if background:
            log(message + " — 이 실행은 끝냅니다")
            return 0
        raise AgentError(message + " (서비스로 돌고 있으면 status 로 확인하세요. 직접 실행하려면 먼저 "
                         "service uninstall)")
    try:
        agent = Agent(config)
        signal.signal(signal.SIGINT, agent.shutdown)
        signal.signal(signal.SIGTERM, agent.shutdown)
        if background:
            for name in (RESTART_REQUEST, STOP_REQUEST):        # 이전 실행에 남은 요청은 지난 일이다
                _take_request(config.home / name)
            threading.Thread(target=_watch_requests, args=(agent, config.home), name="requests",
                             daemon=True).start()
        code = agent.run()
    finally:
        release_instance_lock(lock)
    if agent.restart_requested:
        start_background_agent(config.home)
    return code


# ══ 한 폴더에 에이전트 하나 · Windows 서비스 ══════════════════════════════
def acquire_instance_lock(home: Path, wait: float = 0.0):
    """이 폴더의 에이전트가 둘 돌지 않게 잠근다(같은 러너 자격으로 둘이 붙으면 서버가 작업을 나눠 줄
    수 없다). 잠금은 프로세스가 어떻게 끝나든 OS 가 푼다. wait 초 안에 잠그지 못하면 None."""
    path = home / "agent.lock"
    deadline = time.monotonic() + wait
    while True:
        handle = open(path, "a+b")
        try:
            if WINDOWS:
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError as error:
            if not WINDOWS and error.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                # 잠금을 지원하지 않는 파일 시스템(잠금 없는 NFS 등)이다. 다른 에이전트가 잡고 있는 것이
                # 아니므로 잠그지 않고 돈다 — 여기서 "이미 돌고 있음"으로 끝내면 에이전트가 아예 못 뜬다.
                return handle
            handle.close()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.5)


def release_instance_lock(handle) -> None:
    try:
        if WINDOWS:
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    handle.close()


def agent_running(home: Path) -> bool:
    lock = acquire_instance_lock(home)
    if lock is None:
        return True
    release_instance_lock(lock)
    return False


def _take_request(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _watch_requests(agent, home: Path) -> None:
    """서비스(Windows)로 도는 에이전트: configure·update·uninstall 같은 명령이 남긴 요청을 따른다.
    Windows 에는 systemctl restart 같은 서비스 관리자가 없어 이 파일들로 주고받는다."""
    config_path = home / "config.json"
    while agent.running:
        time.sleep(2)
        if _take_request(home / STOP_REQUEST) or not config_path.is_file():
            log("중지 요청을 받아 에이전트를 끝냅니다")
            agent.running = False
        elif _take_request(home / RESTART_REQUEST):
            log("설정이 바뀌어 에이전트를 다시 시작합니다")
            agent.restart_requested = True
            agent.running = False


def _redirect_output_to_log(home: Path) -> Path:
    """서비스(Windows 의 pythonw)에는 콘솔이 없다 — 출력을 <폴더>/agent.log 에 덧붙인다."""
    path = home / AGENT_LOG
    try:
        if path.stat().st_size > AGENT_LOG_ROTATE_BYTES:
            os.replace(str(path), str(path) + ".1")
    except OSError:
        pass
    stream = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = sys.stderr = stream
    return path


def windows_service_command(home: Path) -> list:
    """시작 프로그램에 적는 명령. pythonw 는 콘솔 창 없이 돈다."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return [str(pythonw) if pythonw.is_file() else sys.executable,
            str(Path(__file__).resolve()), "run", "--dir", str(home), "--background"]


def start_background_agent(home: Path) -> bool:
    """콘솔 없이 지금 띄운다. 설치한 PowerShell 창을 닫아도 남도록 그 창과 떼어 놓는다."""
    if not WINDOWS:
        return False
    detached = 0x00000008 | 0x00000200                  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    for breakaway in (0x01000000, 0):                    # CREATE_BREAKAWAY_FROM_JOB(허용될 때만)
        try:
            subprocess.Popen(windows_service_command(home), cwd=str(home), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                             creationflags=detached | breakaway)
            return True
        except OSError:
            continue
    return False


def _windows_autostart(command) -> None:
    """현재 사용자의 시작 프로그램에 등록(command) 또는 제거(None)."""
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
        if command is None:
            try:
                winreg.DeleteValue(key, WINDOWS_RUN_VALUE)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, WINDOWS_RUN_VALUE, 0, winreg.REG_SZ, subprocess.list2cmdline(command))


def windows_autostart_command() -> str:
    if not WINDOWS:
        return ""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
            return str(winreg.QueryValueEx(key, WINDOWS_RUN_VALUE)[0])
    except OSError:
        return ""


def _windows_restart(home: Path) -> bool:
    """서비스로 도는 에이전트를 다시 시작한다. 돌고 있지 않으면 지금 띄운다."""
    if agent_running(home):
        (home / RESTART_REQUEST).write_text("")
        return True
    _take_request(home / RESTART_REQUEST)
    if start_background_agent(home):
        return True
    print("에이전트를 띄우지 못했습니다. 직접 실행: " + subprocess.list2cmdline(windows_service_command(home)))
    return False


def _windows_stop(home: Path) -> None:
    if not agent_running(home):
        return
    (home / STOP_REQUEST).write_text("")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and agent_running(home):
        time.sleep(0.5)


def _read_log_from(path: Path, offset: int) -> str:
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(offset if offset <= size else 0)      # 그사이 로그를 새로 시작했으면 처음부터
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _windows_service_check(home: Path, offset: int) -> None:
    """띄운 에이전트가 실제로 시작했는지 로그로 확인한다. 시작할 때 인코더를 다시 시험하므로 1분쯤
    걸릴 수 있다."""
    log_path = home / AGENT_LOG
    print("서비스 시작을 확인하는 중입니다(인코더 시험에 1분쯤 걸릴 수 있습니다)...")
    deadline = time.monotonic() + 120
    tail = ""
    while time.monotonic() < deadline:
        time.sleep(1)
        tail = _read_log_from(log_path, offset)
        if "에이전트 %s 시작" % __version__ in tail:
            print("서비스 동작 확인됨")
            return
        if "오류:" in tail or "Traceback" in tail or "이미 돌고 있습니다" in tail:
            break
    lines = [line for line in tail.strip().splitlines() if line.strip()]
    print("서비스가 아직 돌지 않습니다. 로그를 확인하세요: %s" % log_path)
    for line in lines[-5:]:
        print("  " + line)


def command_status(args) -> int:
    config = _require_config(args)
    caps = detect_capabilities(config.data["ffmpeg"], config.encoder)
    print("에이전트 %s" % __version__)
    for entry in config.servers:
        print("  서버     : %s — 러너 %s (#%s)" % (entry.get("url"), entry.get("name"), entry.get("runner_id")))
    slots = max(1, int(config.data["slots"])) if config.data.get("slots") else recommended_slots(caps)
    _describe(caps, config.data["ffmpeg"], slots, config.tmp_dir, usable_rclone_remotes(config.data))
    if WINDOWS:
        print("  서비스   : %s, %s (로그: %s)" % (
            "시작 프로그램에 등록됨" if windows_autostart_command() else "등록 안 됨",
            "실행 중" if agent_running(config.home) else "멈춤", config.home / AGENT_LOG))
    return 0


def command_remove_server(args) -> int:
    """서버 하나와의 연결만 끊는다. 다른 서버 등록은 그대로다."""
    config = _require_config(args)
    target = Api(args.url, "").base_url
    remaining = [entry for entry in config.servers if entry.get("url") != target]
    if len(remaining) == len(config.servers):
        raise AgentError("등록되지 않은 서버입니다: %s (등록된 서버: %s)" % (
            target, ", ".join(str(entry.get("url")) for entry in config.servers)))
    if not remaining:
        raise AgentError("마지막 서버입니다. 이 기기를 아예 빼려면 uninstall 을 쓰세요.")
    config.data["servers"] = remaining
    config.save()
    print("이 서버와의 연결을 지웠습니다: %s (그 서버의 러너 목록에서도 삭제하세요)" % target)
    _restart_service(config.home)
    return 0


def command_configure(args) -> int:
    """등록은 그대로 두고 이 기기의 설정만 바꾼다(rclone 리모트·총 슬롯)."""
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
    print("  총 슬롯  : %s" % (config.data.get("slots") or "추천값"))
    _restart_service(config.home)
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
    _restart_service(Path(args.dir).expanduser().resolve())
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


def _restart_service(home: Path) -> None:
    system = platform.system()
    if system == "Darwin" and _launch_agent_path().is_file():
        subprocess.call(["launchctl", "kickstart", "-k", "gui/%d/%s" % (os.getuid(), SERVICE_LABEL)])
    elif system == "Linux" and _systemd_unit_path().is_file():
        _systemctl("restart", SYSTEMD_UNIT)
    elif system == "Windows" and windows_autostart_command():
        if _windows_restart(home):
            print("에이전트를 다시 시작합니다(몇 초 걸립니다). 로그: %s" % (home / AGENT_LOG))
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
        # 이미 돌고 있으면 새 설정(추가한 서버 등)을 읽도록 다시 시작한다.
        code = _systemctl("enable", SYSTEMD_UNIT) or _systemctl("restart", SYSTEMD_UNIT)
        if code == 0:
            print("서비스 등록됨: %s" % path)
            if os.geteuid() != 0:
                print("로그아웃 뒤에도 돌리려면: loginctl enable-linger %s" % os.environ.get("USER", ""))
        else:
            print("systemctl 등록 실패 — 직접 실행: " + " ".join(command))
        return code
    if system == "Windows":
        if args.action == "uninstall":
            _windows_autostart(None)
            _windows_stop(home)
            print("서비스 제거됨")
            return 0
        _windows_autostart(windows_service_command(home))
        print("서비스 등록됨(로그인하면 자동 시작): 시작 프로그램 '%s'" % WINDOWS_RUN_VALUE)
        log_path = home / AGENT_LOG
        try:
            offset = log_path.stat().st_size
        except OSError:
            offset = 0
        if not _windows_restart(home):
            return 1
        print("로그: %s" % log_path)
        _windows_service_check(home, offset)
        return 0
    if args.action == "install":
        print("이 플랫폼은 서비스 자동 등록을 지원하지 않습니다. 직접 실행하세요:\n  " + " ".join(command))
    return 0


def _xml(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _quote(value: str) -> str:
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def remove_own_autostart(home: Path) -> None:
    """돌고 있는 에이전트가 스스로 자동 시작을 해제한다. 등록된 서비스가 이 폴더의 것일 때만 지운다(다른
    폴더를 서비스로 돌리면서 이 폴더는 직접 실행했을 수 있다). service uninstall 과 달리 자기 자신이
    끝나기를 기다리지 않는다 — macOS·Linux 는 마지막에 서비스를 멈추고, 그 SIGTERM 으로 이 프로세스가
    끝난다(KeepAlive·Restart=always 가 다시 띄우지 않도록 파일부터 지운다)."""
    system = platform.system()
    if system == "Darwin":
        path = _launch_agent_path()
        if path.is_file() and "<string>%s</string>" % _xml(str(home)) in path.read_text(
                encoding="utf-8", errors="replace"):
            path.unlink()
            subprocess.call(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), SERVICE_LABEL)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif system == "Linux" and shutil.which("systemctl"):
        path = _systemd_unit_path()
        if path.is_file() and _quote(str(home)) in path.read_text(encoding="utf-8", errors="replace"):
            _systemctl("disable", SYSTEMD_UNIT)
            path.unlink()
            _systemctl("daemon-reload")
            _systemctl("stop", "--no-block", SYSTEMD_UNIT)
    elif system == "Windows":
        if " --dir %s " % subprocess.list2cmdline([str(home)]) in windows_autostart_command() + " ":
            _windows_autostart(None)


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
                               "서버의 원본 리모트와 같은 이름이어야 한다. install 은 이 기기에 없는 "
                               "리모트를 건너뛰고 지금 목록에 더하며, configure 는 목록을 바꾼다")
        item.add_argument("--rclone", metavar="PATH", help="rclone 실행 파일(기본: PATH 의 rclone)")
        item.add_argument("--rclone-config", metavar="PATH", help="rclone 설정 파일(기본: rclone 기본 위치)")

    install = with_dir("install", "이 기기를 서버에 등록(등록된 폴더면 서버 추가)")
    install.add_argument("--url", help="Shyni 서버 주소")
    install.add_argument("--token", help="관리자 화면에서 발급한 등록 토큰")
    install.add_argument("--name", help="러너 이름(기본: 호스트 이름)")
    install.add_argument("--tmp", help="변환 임시 폴더(기본: <폴더>/tmp)")
    install.add_argument("--ffmpeg", help="쓸 ffmpeg 경로(기본: 자동 선택·설치)")
    install.add_argument("--encoder", help="auto(기본) | software | nvenc | vaapi | qsv | videotoolbox")
    install.add_argument("--slots", type=int, help="이 기기의 총 동시 작업 수(기본: 추천값)")
    add_rclone_options(install)
    install.add_argument("--no-service", action="store_true", help="서비스로 등록하지 않음")
    install.add_argument("--force", action="store_true", help="(0.6.1 부터 필요 없음 — 이미 등록한 서버면 늘 다시 등록한다)")
    install.set_defaults(handler=command_install)
    run = with_dir("run", "작업을 받아 실행")
    # 서비스(Windows 시작 프로그램)가 쓰는 실행 방식: 출력은 <폴더>/agent.log, 다른 명령의 요청을 따른다.
    run.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    run.set_defaults(handler=command_run)
    with_dir("status", "이 기기의 인코더·설정 표시").set_defaults(handler=command_status)
    with_dir("update", "공개 저장소에서 새 에이전트 받기").set_defaults(handler=command_update)
    configure = with_dir("configure", "이 기기의 설정 바꾸기(rclone 리모트·작업 상한)")
    add_rclone_options(configure)
    configure.add_argument("--slots", type=int, help="총 동시 작업 수(0 이면 추천값으로)")
    configure.set_defaults(handler=command_configure)
    remove = with_dir("remove-server", "서버 하나와의 연결 지우기")
    remove.add_argument("--url", required=True, help="지울 Shyni 서버 주소")
    remove.set_defaults(handler=command_remove_server)
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

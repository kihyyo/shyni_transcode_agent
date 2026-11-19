#!/bin/sh
# Shyni 트랜스코딩 에이전트 설치 (macOS · Linux, Windows 는 Git Bash 에서도 됨)
#
# Shyni 관리자 화면(설정 → 관리 → 트랜스코더 → 러너 추가)이 보여 주는 명령으로 실행한다:
#
#   curl -fsSL https://raw.githubusercontent.com/kihyyo/shyni_transcode_agent/main/install.sh | sh -s -- \
#       --url https://shyni.example --token shr_... [--dir DIR] [--tmp DIR] [--name NAME] [--no-service]
#
# Windows 의 PowerShell 에서는 install.ps1 을 쓴다(관리자 화면에서 Windows 를 고르면 그 명령이 나온다).
#
# 이 스크립트는 OS 를 보고 Python 을 찾아 에이전트 파일 하나를 받아 넘긴다. GPU 감지와 H/W 인코더 시험,
# ffmpeg 준비, 서버 등록, 자동 시작 등록은 에이전트의 install 명령이 한다. 옵션은 그대로 전달된다.
set -eu

AGENT_URL="${SHYNI_AGENT_SOURCE_URL:-https://raw.githubusercontent.com/kihyyo/shyni_transcode_agent/main/shyni_transcode_agent.py}"

case "$(uname -s 2>/dev/null || echo unknown)" in
  Darwin) OS=macos ;;
  Linux) OS=linux ;;
  MINGW*|MSYS*|CYGWIN*) OS=windows ;;     # Git Bash 등 — 에이전트는 Windows 용 Python 으로 돈다
  *) OS=other ;;
esac
WINDOWS=0
[ "$OS" = windows ] && WINDOWS=1

DIR="$HOME/shyni-transcoder"
previous=""
for argument in "$@"; do
  if [ "$previous" = "--dir" ]; then
    DIR="$argument"
  fi
  previous="$argument"
done

# Windows 의 Python 은 /c/Users/… 같은 경로를 모른다 — 에이전트에 넘기는 경로 옵션을 C:\… 로 바꾼다.
to_native() {
  if [ "$WINDOWS" = 1 ] && command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$1"
  else
    printf '%s\n' "$1"
  fi
}
if [ "$WINDOWS" = 1 ]; then
  count=$#
  previous=""
  while [ "$count" -gt 0 ]; do
    argument=$1
    shift
    count=$((count - 1))
    case "$previous" in
      --dir|--tmp|--ffmpeg|--rclone|--rclone-config) argument=$(to_native "$argument") ;;
    esac
    set -- "$@" "$argument"
    previous="$argument"
  done
fi

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for candidate in python3 python py; do
    # Windows 에서는 Windows 용 Python 만 쓴다(MSYS2 의 Python 은 Windows 서비스·GPU 를 못 다룬다).
    # Windows 의 python3 "앱 실행 별칭"은 Store 를 여는 껍데기라 실제로 실행해 본다.
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) and (sys.platform == "win32") == (sys.argv[1] == "1") else 1)' "$WINDOWS" 2>/dev/null; then
      PYTHON="$(command -v "$candidate")"
      break
    fi
  done
fi
if [ -z "$PYTHON" ]; then
  echo "Python 3.9 이상이 필요합니다." >&2
  case "$OS" in
    macos) echo "  macOS: xcode-select --install  (또는 brew install python)" >&2 ;;
    windows) echo "  Windows: PowerShell 용 설치 명령(install.ps1)을 쓰면 Python 도 함께 설치합니다." >&2 ;;
    *)
      if command -v apt-get >/dev/null 2>&1; then echo "  sudo apt-get install -y python3" >&2
      elif command -v dnf >/dev/null 2>&1; then echo "  sudo dnf install -y python3" >&2
      elif command -v pacman >/dev/null 2>&1; then echo "  sudo pacman -S python" >&2
      elif command -v apk >/dev/null 2>&1; then echo "  sudo apk add python3" >&2
      elif command -v zypper >/dev/null 2>&1; then echo "  sudo zypper install -y python3" >&2
      else echo "  이 OS 의 패키지 관리자로 python3 를 설치하세요." >&2
      fi ;;
  esac
  exit 1
fi

mkdir -p "$DIR/agent"
chmod 700 "$DIR"
# 저장소를 내려받아 `sh install.sh …` 로 실행했으면 같은 폴더의 파일을 쓴다. `curl … | sh` 로
# 실행하면 $0 이 셸 이름이라 현재 폴더를 보지 않고 공개 저장소에서 받는다.
SCRIPT_DIR=""
case "$0" in
  install.sh|*/install.sh) [ -f "$0" ] && SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)" ;;
esac
TARGET="$DIR/agent/shyni_transcode_agent.py"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/shyni_transcode_agent.py" ]; then
  cp "$SCRIPT_DIR/shyni_transcode_agent.py" "$TARGET"
else
  curl -fsSL --proto '=https' "$AGENT_URL" -o "$TARGET.download"
  mv "$TARGET.download" "$TARGET"
fi

exec "$PYTHON" "$(to_native "$TARGET")" install --dir "$(to_native "$DIR")" "$@"

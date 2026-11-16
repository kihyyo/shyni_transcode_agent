#!/bin/sh
# Shyni 트랜스코딩 에이전트 설치 (macOS · Linux)
#
# Shyni 관리자 화면(설정 → 관리 → 트랜스코더 → 러너 추가)이 보여 주는 명령으로 실행한다:
#
#   curl -fsSL https://raw.githubusercontent.com/kihyyo/shyni_transcode_agent/main/install.sh | sh -s -- \
#       --url https://shyni.example --token shr_... [--dir DIR] [--tmp DIR] [--name NAME] [--no-service]
#
# 이 스크립트는 에이전트 파일 하나를 받아 두고 넘긴다. ffmpeg 준비, H/W 인코더 시험, 서버 등록,
# 자동 시작 서비스 등록은 에이전트의 install 명령이 한다. 옵션은 그대로 전달된다.
set -eu

AGENT_URL="${SHYNI_AGENT_SOURCE_URL:-https://raw.githubusercontent.com/kihyyo/shyni_transcode_agent/main/shyni_transcode_agent.py}"

DIR="$HOME/shyni-transcoder"
previous=""
for argument in "$@"; do
  if [ "$previous" = "--dir" ]; then
    DIR="$argument"
  fi
  previous="$argument"
done

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      PYTHON="$(command -v "$candidate")"
      break
    fi
  done
fi
if [ -z "$PYTHON" ]; then
  echo "Python 3.9 이상이 필요합니다. (macOS: xcode-select --install, Linux: python3 패키지)" >&2
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

exec "$PYTHON" "$TARGET" install --dir "$DIR" "$@"

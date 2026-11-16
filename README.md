# Shyni 트랜스코딩 에이전트

Turns a machine with a hardware video encoder (an Apple Silicon Mac, an Intel or NVIDIA
box) into a remote transcoding worker for a Shyni media server, in the style of a GitHub
self-hosted runner. One Python file, standard library only.

H/W 인코더가 없는 서버(OCI 같은 클라우드 서버)에서 Shyni 를 돌릴 때, 변환이 필요한 재생을
H/W 가 있는 다른 기기에 맡기는 에이전트입니다.

- 에이전트가 서버로 **먼저** 접속해 작업을 기다립니다. 이 기기는 포트를 열지 않고, 공유기
  뒤에 있어도 됩니다.
- 러너는 비디오 변환(해상도 낮추기·HDR 톤매핑)만 맡습니다. 컨테이너 교체·오디오 변환은
  원본 화질 그대로라 집 회선으로 다시 올려 보낼 이유가 없어 서버가 합니다.
- 원본은 이 기기가 직접 읽습니다. 115 는 서버가 준 서명 주소로, rclone 마운트(구글드라이브
  등)는 이 기기의 rclone 에 같은 이름의 리모트가 있을 때 그 리모트로 읽습니다. 그럴 수 없는
  원본은 Shyni 서버가 내줍니다(추가 설정 없이 동작하는 기본 경로).
- 변환 결과는 이 기기 → Shyni 서버 → 재생기 순으로 갑니다. 재생기는 평소와 같은 서버 주소만
  쓰므로 앱을 바꿀 필요가 없고, 재생 세션과 이어보기도 그대로 서버가 관리합니다.
- 서버 한 대에 여러 러너를 붙일 수 있습니다. 서버는 응답 가능한 작업자 가운데 H/W 가 되는
  쪽, 그중 빈 슬롯이 많은 쪽에 일을 줍니다.
- 러너 하나를 여러 서버에 붙일 수도 있습니다(아래 "여러 서버에서 같이 쓰기"). 동시 작업 수는
  이 기기가 셉니다 — 서버들이 슬롯을 미리 나눠 갖지 않고, 필요한 서버가 그때그때 씁니다.

## 필요한 것

- macOS 또는 Linux, Python 3.9 이상, `curl`
- 트랜스코더 메뉴(설정 → 관리 → 트랜스코더)가 있는 Shyni 서버
- 이 기기에서 Shyni 서버 주소로 나가는 연결. 외부에 있는 서버라면 https 주소

Windows 는 `run` 을 직접 실행하면 동작하도록 만들었지만 시험하지 않았고, 설치 스크립트와
자동 시작은 지원하지 않습니다.

## 리버스 프록시

러너는 변환한 HLS 조각(6초에 15MB 안쪽)을 서버로 올립니다. Shyni 앞에 nginx 가 있으면
기본 업로드 한도가 1MB 라서 480p 조각부터 `413` 으로 막힙니다. 설치는 되지만 변환 재생이
실패하고, 에이전트 로그에 "리버스 프록시가 업로드를 막았습니다(HTTP 413)"가 남습니다.
에이전트가 쓰는 경로에만 한도를 엽니다.

```nginx
# 기존 server { … } 블록 안, location / 옆에 추가
location /transcode-agent/ {
    proxy_pass http://127.0.0.1:8000;   # location / 와 같은 대상
    # location / 에 있는 proxy_set_header 줄도 그대로 복사한다
    client_max_body_size 64m;           # 비디오 조각 업로드(6초에 15MB 안쪽)
    proxy_request_buffering off;        # 받는 대로 넘긴다
    proxy_read_timeout 120s;            # 작업 대기(25초)보다 길게
}
```

- Caddy 는 기본 한도가 없어 따로 할 일이 없습니다.
- Cloudflare 프록시(주황 구름)를 거치면 요청당 업로드 100MB(무료·Pro), 응답 대기 100초
  한도가 있습니다. 에이전트는 이 안에서 동작합니다. 봇 차단(Bot Fight Mode 등)을 켰다면
  `/transcode-agent/` 를 예외로 두세요.
- 같은 내부망에 있는 기기라면 프록시를 거치지 않는 서버 주소(`http://192.168.x.x:포트`)로
  등록해도 됩니다.

## rclone 으로 원본 직접 읽기

서버의 라이브러리가 rclone 마운트에 있으면, 이 기기에도 같은 리모트를 두어 원본을 서버를 거치지
않고 읽을 수 있습니다. 서버는 **리모트 이름과 경로만** 보내고, 자격은 이 기기의 rclone 설정에
있는 것을 씁니다. 설정하지 않으면 서버가 원본을 내주므로 그대로도 동작합니다. 서버가 밖에
있어 원본까지 서버를 거치게 하고 싶지 않을 때 설정하세요.

1. 이 기기에 rclone 을 설치하고, 서버의 마운트 관리에 적힌 것과 **같은 이름**의 리모트를 rclone
   설정에 넣습니다. GDS 리모트는 [wiserain rclone](https://github.com/wiserain/rclone) 이어야
   합니다(읽기 자격을 rclone 이 실행 중에 받습니다).
2. 설치할 때 `--rclone-remote <이름>` 을 붙이거나, 이미 설치했다면 다음을 실행합니다.

   ```sh
   python3 ~/shyni-transcoder/agent/shyni_transcode_agent.py configure --rclone-remote gds
   ```

   rclone 은 PATH 와 흔한 설치 위치(`/usr/local/bin`, `/opt/homebrew/bin` 등)에서 찾아 전체
   경로로 저장합니다(서비스는 PATH 가 짧습니다). 다른 곳에 있거나 설정 파일이 기본 위치가 아니면
   `--rclone <경로>`, `--rclone-config <경로>` 를 함께 줍니다. 리모트가 여러 개면
   `--rclone-remote` 를 반복합니다.

에이전트는 허용한 리모트만 서버에 알리고, 그 리모트의 경로만 읽습니다. 작업이 오면 127.0.0.1 에
`rclone serve http` 를 띄워(접속 암호는 그때그때 만듦) ffmpeg 가 그 뒤에서 읽게 합니다.

이 기기의 rclone 이 그 파일을 읽지 못하면(이름만 같고 내용이 다른 리모트, GDS 인데 wiserain
빌드가 아닌 rclone 등) 재생이 실패하지 않고 서버가 내주는 원본으로 바꿔 읽습니다. 로그에
`rclone 리모트 '…' 로 원본을 읽지 못해 서버에서 받습니다` 가 남고, 그 리모트는 한 시간 동안
서버에 알리지 않습니다(관리자 화면에 "rclone 없음"으로 보입니다). 고친 뒤에는 에이전트를 다시
시작하면 바로 다시 씁니다. 확인은 이 기기에서:

```sh
rclone lsf "gds:{서버 마운트 관리의 루트 폴더 ID}" --max-depth 1
```

## 설치

Shyni 관리자 화면의 **설정 → 관리 → 트랜스코더 → 러너 추가**에서 "설치 명령 만들기"를 누르면
아래와 같은 명령이 나옵니다. 러너로 쓸 기기에서 실행합니다.

```sh
curl -fsSL https://raw.githubusercontent.com/kihyyo/shyni_transcode_agent/main/install.sh | sh -s -- \
  --url https://shyni.example --token shr_xxxxxxxx --dir "$HOME/shyni-transcoder"
```

토큰은 한 번만 쓸 수 있고 1시간 뒤 만료됩니다. 설치가 하는 일:

1. 에이전트 파일을 `<폴더>/agent/` 에 받습니다.
2. 쓸 ffmpeg 를 고릅니다. 이 기기에 이미 있는 ffmpeg 로 H/W 인코딩이 되면 그것을 쓰고,
   안 되면 이 기기 종류에 맞는 [jellyfin-ffmpeg](https://github.com/jellyfin/jellyfin-ffmpeg)
   휴대용 빌드를 `<폴더>/ffmpeg/` 에 받습니다(버전과 SHA-256 을 고정해 확인).
3. 인코더 목록을 믿지 않고 실제 프레임을 인코딩해 봅니다. 통과한 H/W 인코더만 쓰고,
   없으면 libx264(S/W)로 등록합니다.
4. 서버에 등록하고 자격을 `<폴더>/config.json`(소유자만 읽기)에 저장합니다.
5. 로그인하면 자동으로 시작하도록 서비스로 등록합니다(macOS LaunchAgent, Linux systemd).

| 옵션 | 뜻 |
|---|---|
| `--url` | 이 기기에서 접속할 Shyni 서버 주소 |
| `--token` | 관리자 화면에서 받은 등록 토큰 |
| `--dir` | 에이전트 폴더 (기본 `~/shyni-transcoder`) |
| `--tmp` | 변환 임시 폴더 (기본 `<폴더>/tmp`). 여유 공간이 넉넉한 디스크를 지정 |
| `--name` | 관리자 화면에 보일 러너 이름 (기본 호스트 이름) |
| `--slots` | 이 기기의 총 동시 작업 수 (기본 추천값). 붙어 있는 서버를 모두 합친 한도입니다 |
| `--encoder` | `auto`(기본) · `software` · `nvenc` · `vaapi` · `qsv` · `videotoolbox` |
| `--ffmpeg` | 쓸 ffmpeg 경로를 직접 지정 (자동 선택·내려받기 생략) |
| `--rclone-remote` | 이 기기의 rclone 으로 직접 읽을 리모트 이름 (위 "rclone 으로 원본 직접 읽기") |
| `--rclone`, `--rclone-config` | rclone 실행 파일과 설정 파일 위치 (기본값이 아닐 때) |
| `--no-service` | 서비스로 등록하지 않음. 직접 `run` 으로 실행 |
| `--force` | 이미 등록한 서버에 다시 등록 |

임시 폴더에는 아직 서버로 올리지 않은 조각만 남으므로 보통 수백 MB 안쪽입니다.

### 슬롯

이 기기가 동시에 돌릴 변환 수(총 슬롯)는 이 기기가 정합니다. 기본은 추천값으로, H/W 인코더가
있으면 고성능 코어 수 + 2(Apple M1 = 6), S/W 면 물리 코어 2개당 1개(최대 4)입니다. 바꾸려면
이 기기에서 실행합니다.

```sh
python3 ~/shyni-transcoder/agent/shyni_transcode_agent.py configure --slots 8   # 0 이면 추천값으로
```

관리자 화면의 러너별 "최대 슬롯"은 그 서버가 이 기기에서 쓸 몫을 총 슬롯 안에서 줄이는
값입니다. 비워 두면 총 슬롯까지 씁니다.

### 여러 서버에서 같이 쓰기

서버마다 관리자 화면에서 설치 명령을 만들어 이 기기에서 한 번씩 실행하면 됩니다. 이미 등록된
폴더(`--dir`)에 다른 서버의 명령을 실행하면 그 서버가 **추가**됩니다(ffmpeg·임시 폴더·rclone
설정은 그대로).

- 에이전트 하나가 모든 서버에 붙어 있고, 총 슬롯은 서버들이 같이 씁니다. 총 6개면 한 서버가
  6개를 다 쓸 수도, 세 서버가 2개씩 쓸 수도 있습니다.
- 에이전트는 각 서버에 "다른 서버가 지금 몇 개 쓰는지"를 알리고, 작업 수가 바뀌면 바로 다시
  알립니다. 서버끼리는 아무것도 주고받지 않습니다.
- 두 서버가 마지막 슬롯을 동시에 주면 늦은 쪽을 거절하고, 그 서버는 다른 작업자에게 맡깁니다.

서버 하나만 빼려면:

```sh
python3 ~/shyni-transcoder/agent/shyni_transcode_agent.py remove-server --url https://shyni.example
```

### 플랫폼별 H/W

| 기기 | 인코더 | 준비할 것 |
|---|---|---|
| Apple Silicon / Intel Mac | VideoToolbox | 없음 |
| NVIDIA (Linux) | NVENC | NVIDIA 드라이버 |
| Intel / AMD (Linux) | VAAPI, QSV | `/dev/dri` 접근 권한(`render` 그룹), VAAPI 드라이버 |

GPU 드라이버는 설치하지 않습니다(관리자 권한과 커널에 맞는 버전이 필요합니다). 드라이버를
갖춘 뒤 에이전트를 다시 시작하면 인코더를 다시 시험합니다.

## 쓰기

```sh
python3 ~/shyni-transcoder/agent/shyni_transcode_agent.py status      # 붙은 서버·인코더·설정 확인
python3 ~/shyni-transcoder/agent/shyni_transcode_agent.py run         # 직접 실행(--no-service 일 때)
python3 ~/shyni-transcoder/agent/shyni_transcode_agent.py update      # 새 버전 받기
python3 ~/shyni-transcoder/agent/shyni_transcode_agent.py uninstall   # 서비스·등록 정보 제거
```

로그는 서비스로 돌 때 macOS 는 `~/Library/Logs/shyni-transcode-agent.log`, Linux 는
`journalctl --user -u shyni-transcode-agent` 에 쌓입니다. 사용 여부와 최대 슬롯, 삭제는 Shyni 관리자 화면에서 합니다.
에이전트는 스스로 갱신하지 않습니다. 서버가 "에이전트 업데이트 필요"로 표시하면 이 기기에서
`update` 를 실행하세요.

## 보안

에이전트를 설치한다는 것은 그 Shyni 서버에 이 기기의 변환 능력을 빌려준다는 뜻입니다.
서버가 이 기기에서 할 수 있는 일은 좁게 묶여 있습니다.

- **서버는 명령을 보내지 않습니다.** 변환 사양(모드·해상도·오디오·자막)만 보내고, ffmpeg
  명령은 에이전트가 만듭니다. 사양은 아는 항목만, 범위 안의 값으로 받습니다.
- **입력은 http(s) 원본, 출력은 작업 폴더뿐입니다.** 서버 자신이 아닌 내부망·loopback 주소는
  원본으로 받지 않습니다. 서버가 이 기기를 발판으로 같은 망의 다른 장비에 요청하게 두지
  않기 위해서입니다.
- **rclone 은 허용한 리모트만 씁니다.** 설치할 때 이름을 적어 준 리모트의 경로만 읽고, 서버가
  rclone 설정이나 자격을 보내올 수는 없습니다.
- **클라우드 자격은 명령줄에 남지 않습니다.** ffmpeg 는 에이전트의 127.0.0.1 프록시로 원본을
  읽고, 프록시가 자격을 붙입니다. 자격이 만료되면 프록시가 서버에서 새로 받습니다.
- **코드는 스스로 바뀌지 않습니다.** 서버는 에이전트 코드를 내려보낼 수 없습니다.
- 등록 토큰은 일회용이고, 등록 뒤에는 러너마다 다른 자격으로 인증합니다. 서버에는 그 해시만
  남습니다. 관리자 화면에서 러너를 삭제하면 그 자격은 바로 무효가 됩니다.

서버 주소가 `http://` 이면 자격과 영상이 평문으로 오갑니다. 같은 내부망이 아니면 https 를
쓰세요.

## 동작

```
재생기 ──(평소와 같은 주소·티켓)──▶ Shyni 서버 ◀──(long-poll, 결과 업로드)── 에이전트 ──▶ ffmpeg(H/W)
                                                                        │
                                              구글드라이브 · 115 ◀────────┘ 원본을 직접 읽음
```

1. 에이전트가 서버에 접속해 명령을 기다립니다. 이 대기가 곧 "응답 가능" 신호입니다.
2. 서버가 작업자를 고르고 변환 사양과 원본(클라우드 주소와 자격, 또는 "서버가 제공")을 넘깁니다.
3. 에이전트가 ffmpeg 를 띄우고, 다 만들어진 HLS 조각과 playlist 를 서버로 올립니다.
4. 재생 위치보다 60초쯤 앞서면 서버가 멈추라고 하고(0 CPU), 재생이 따라오면 다시 깨웁니다.
5. 재생이 끝나거나 탐색하면 서버가 작업을 멈추고, 에이전트는 작업 폴더를 지웁니다.

## 문제 해결

- **등록 토큰이 없거나 만료되었습니다** — 관리자 화면에서 명령을 다시 만드세요.
- **관리자 화면에 "오프라인"** — 에이전트가 돌고 있는지(위 로그), 이 기기에서 서버
  주소로 접속되는지 확인하세요.
- **macOS 서비스 로그에 "No route to host"** — 같은 내부망의 서버(`http://192.168.x.x`)로 등록했을
  때 macOS 가 서비스의 로컬 네트워크 접근을 묻거나 막은 것입니다. 시스템 설정 → 개인정보 보호 및
  보안 → **로컬 네트워크**에서 Python 을 켜세요.
- **변환 재생이 실패하고 로그에 413** — 위 [리버스 프록시](#리버스-프록시)를 보세요.
- **H/W 가 아니라 S/W 로 등록됨** — `status` 로 다시 확인하고, 위 표의 준비 사항을 보세요.
  로그에 "인코더 시험 실패"와 ffmpeg 의 오류가 남습니다.
- **rclone 리모트를 넣었는데 관리자 화면에 "rclone 없음"** — 이 기기의 rclone 이 그 원본을 읽지
  못해 서버 중계로 넘어간 것입니다. 위 "rclone 으로 원본 직접 읽기"의 확인 명령을 실행해 보세요.
- **macOS 에서 외장 디스크(`/Volumes/…`)를 폴더로 쓸 때** — launchd 서비스로 도는 프로그램은
  외장 디스크에 접근할 수 없고, 묻는 창도 뜨지 않습니다. 관리자 화면에는 "오프라인"으로만
  보입니다. 시스템 설정 → 개인정보 보호 및 보안 → **전체 디스크 접근 권한**에 설치가 알려 주는
  python3 경로를 추가한 뒤 `service install` 을 다시 실행하거나, `--dir`·`--tmp` 를 내장 디스크로
  두세요. 임시 폴더에는 아직 서버로 올리지 않은 조각만 남아 내장 디스크로도 충분합니다.
  설치 직후 "Python 이 이동식 볼륨의 파일에 접근하려고 합니다" 창이 뜨면 허용해도 됩니다.

## 개발

```sh
python3 -m unittest discover -s . -p 'test_*.py'
```

ffmpeg 명령 구성(`build_*_args`, 인코더 실측)은 Shyni 서버가 직접 변환할 때와 같은 결과를
내야 합니다. 같은 원본이 서버에서 변환되든 러너에서 변환되든 재생기가 받는 스트림이 같아야
하기 때문입니다. 서버 쪽에 두 구현을 맞대는 테스트가 있으므로, 명령 구성을 바꿀 때는 서버와
함께 바꿉니다. 서버와 에이전트의 약속은 `PROTOCOL_VERSION` 하나이고, 서로 호환되지 않게
바뀌면 이 번호를 올립니다.

## 라이선스

MIT. 설치 중에 내려받는 jellyfin-ffmpeg 는 GPL 로 배포되는 별도의 프로그램이며 이 저장소에
포함되어 있지 않습니다.

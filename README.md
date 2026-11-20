# Shyni 트랜스코딩 에이전트

Turns a machine with a hardware video encoder (an Apple Silicon Mac, a Linux box, or a
Windows PC with an NVIDIA, AMD or Intel GPU) into a remote transcoding worker for a Shyni
media server, in the style of a GitHub self-hosted runner. One Python file, standard
library only. The installer detects the OS and GPU and picks the encoder itself.

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
- 이 기기의 H/W 가 지원하면 H.264 뿐 아니라 H.265(HEVC)·AV1 로도 변환합니다. 같은 화질을 더
  낮은 비트레이트로 보냅니다(아래 "출력 코덱").
- 러너 하나를 여러 서버에 붙일 수도 있습니다(아래 "여러 서버에서 같이 쓰기"). 동시 작업 수는
  이 기기가 셉니다 — 서버들이 슬롯을 미리 나눠 갖지 않고, 필요한 서버가 그때그때 씁니다.

## 필요한 것

- macOS, Linux 또는 Windows 10·11
- Python 3.9 이상. Windows 는 없으면 설치 명령이 winget 으로 이 사용자에게만 설치합니다(관리자
  권한 불필요). macOS·Linux 는 `curl` 도 필요합니다.
- 트랜스코더 메뉴(설정 → 관리 → 트랜스코더)가 있는 Shyni 서버. 관리자 화면의 Windows 설치 명령과
  AMD(AMF) 인코더는 서버도 이를 아는 버전이어야 합니다 — 이전 서버는 AMF 러너를 "인코더 없음"으로
  봅니다(NVIDIA·Intel 러너는 이전 서버에서도 됩니다).
- 이 기기에서 Shyni 서버 주소로 나가는 연결. 외부에 있는 서버라면 https 주소

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
있는 것을 씁니다. 같은 이름의 리모트가 없으면 서버가 원본을 내주므로 그대로도 동작합니다. 서버가
밖에 있어 원본까지 서버를 거치게 하고 싶지 않을 때 이 기기의 rclone 에 그 리모트를 두세요.

1. 이 기기에 rclone 을 설치하고, 서버의 마운트 관리에 적힌 것과 **같은 이름**의 리모트를 rclone
   설정에 넣습니다. GDS 리모트는 [wiserain rclone](https://github.com/wiserain/rclone) 이어야
   합니다(읽기 자격을 rclone 이 실행 중에 받습니다).
2. 관리자 화면이 만들어 주는 설치 명령에는 그 서버의 rclone 리모트 이름이 `--rclone-remote <이름>`
   으로 들어 있습니다(0.6.1). 이 기기의 rclone 설정에 같은 이름이 있으면 그대로 쓰고, 없는 것은
   알리고 건너뜁니다. rclone 이 없는 기기도 그대로 설치됩니다. 쓰지 않으려면 명령에서 그 옵션을
   빼세요. 이미 설치했다면 다음으로 목록을 바꿉니다(`--rclone-remote=` 는 모두 끔).

   ```sh
   python3 ~/shyni-transcoder/agent/shyni_transcode_agent.py configure --rclone-remote gds
   ```

   rclone 은 PATH 와 흔한 설치 위치(`/usr/local/bin`, `/opt/homebrew/bin` 등)에서 찾아 전체
   경로로 저장합니다(서비스는 PATH 가 짧습니다). 다른 곳에 있거나 설정 파일이 기본 위치가 아니면
   `--rclone <경로>`, `--rclone-config <경로>` 를 함께 줍니다. 리모트가 여러 개면
   `--rclone-remote` 를 반복합니다.

에이전트는 허용한 리모트만 서버에 알리고, 그 리모트의 경로만 읽습니다. 작업이 오면 127.0.0.1 에
`rclone rcd --rc-serve` 를 띄워(접속 암호는 그때그때 만들어 환경변수로만 넘김) ffmpeg 가 그 뒤에서
읽게 합니다. rclone 은 경로를 폴더 이름으로 바로 찾으므로 파일이 수천 개인 폴더도 목록을 받지
않습니다(`serve http` 는 그런 폴더 하나에 13초가 걸렸습니다). 찾은 폴더는 기억하고, 폴더를 다시
만들어 못 찾으면 rclone 을 새로 띄워 다시 찾습니다.

서버도 그 파일을 내줄 수 있으면(서버에 같은 마운트가 있음) 0.5.0 부터는 서버를 받쳐 둡니다.

- rclone 이 그 경로를 아직 못 찾았으면(rclone 을 막 띄움·새 폴더: 4~8초) 서버에서 받기 시작합니다.
  준비되면 지금 받는 속도로 몇 초 앞 바이트에서 rclone 으로 넘어가므로, 서버가 느린 인터넷 너머여도
  서버 속도에 묶이지 않습니다.
- rclone 이 3초 안에 첫 바이트를 주지 않거나 받는 도중 끊기면 서버에서 받습니다.

작업을 받으면 앞 5MB·뒤 4MB 를 동시에 받고, 앞부분으로 MKV·MP4 구조를 읽어 ffmpeg 가 열면서 읽을
범위(글꼴이 든 머리 전체, 끝이나 앞의 큰 MP4 색인)도 함께 받아 둡니다. 외부 자막도 작업을 받자마자
받아 둡니다(0.5.1).

서버가 꺼지면 5초 간격으로 다시 연결합니다(기록은 처음과 1분마다). 서버를 재시작해도 몇 초 안에 다시
붙습니다.

이 기기의 rclone 이 그 파일을 읽지 못하면(이름만 같고 내용이 다른 리모트, GDS 인데 wiserain
빌드가 아닌 rclone 등) 재생이 실패하지 않고 서버가 내주는 원본으로 바꿔 읽습니다. 로그에
`rclone 리모트 '…' 로 원본을 읽지 못해 서버에서 받습니다` 가 남고, 그 리모트는 한 시간 동안
서버에 알리지 않습니다(관리자 화면에 "rclone 안 씀"으로 보입니다). 고친 뒤에는 에이전트를 다시
시작하면 바로 다시 씁니다. 확인은 이 기기에서:

```sh
rclone lsf "gds:{서버 마운트 관리의 루트 폴더 ID}" --max-depth 1
```

## 설치

Shyni 관리자 화면의 **설정 → 관리 → 트랜스코더 → 러너 추가**에서 "설치 명령 만들기"를 누르고,
러너로 쓸 기기의 OS(macOS · Linux / Windows)를 고르면 그 기기에서 실행할 명령이 나옵니다. 기본은
그 화면을 연 브라우저의 OS 입니다.

macOS · Linux (터미널):

```sh
curl -fsSL https://raw.githubusercontent.com/kihyyo/shyni_transcode_agent/main/install.sh | sh -s -- \
  --url https://shyni.example --token shr_xxxxxxxx --dir "$HOME/shyni-transcoder"
```

Windows (PowerShell, 관리자 권한 불필요):

```powershell
& ([scriptblock]::Create((irm 'https://raw.githubusercontent.com/kihyyo/shyni_transcode_agent/main/install.ps1'))) --url 'https://shyni.example' --token 'shr_xxxxxxxx' --dir "$HOME\shyni-transcoder"
```

Windows 의 Git Bash 에서는 macOS · Linux 명령도 됩니다(경로를 Windows 형식으로 바꿔 넘깁니다).
저장소를 받아 둔 경우 `sh install.sh …`, `powershell -ExecutionPolicy Bypass -File install.ps1 …` 로도
실행할 수 있습니다.

토큰은 한 번만 쓸 수 있고 1시간 뒤 만료됩니다. 설치가 하는 일:

1. Python 을 찾고(Windows 는 없으면 winget 으로 설치) 에이전트 파일을 `<폴더>/agent/` 에 받습니다.
2. 이 기기의 OS 와 GPU 를 봅니다. Windows 는 레지스트리의 그래픽 카드 목록으로 NVIDIA → AMD → Intel
   (외장 그래픽 먼저) 순으로 시험할 인코더를 정하고, Linux 는 `/dev/nvidia*`·`/dev/dri` 장치를,
   macOS 는 VideoToolbox 를 봅니다.
3. 쓸 ffmpeg 를 고릅니다. 이 기기에 이미 있는 ffmpeg 로 H/W 인코딩이 되면 그것을 쓰고,
   안 되면 이 기기 종류에 맞는 [jellyfin-ffmpeg](https://github.com/jellyfin/jellyfin-ffmpeg)
   휴대용 빌드를 `<폴더>/ffmpeg/` 에 받습니다(버전과 SHA-256 을 고정해 확인).
4. 인코더 목록을 믿지 않고 실제 프레임을 인코딩해 봅니다. 통과한 H/W 인코더만 쓰고,
   없으면 libx264(S/W)로 등록합니다. H.265·AV1 인코더도 같이 시험합니다.
5. 서버에 등록하고 자격을 `<폴더>/config.json`(소유자만 읽기)에 저장합니다.
6. 로그인하면 자동으로 시작하도록 서비스로 등록하고 바로 띄웁니다(macOS LaunchAgent, Linux systemd,
   Windows 는 현재 사용자의 시작 프로그램에 콘솔 창 없이 도는 `pythonw` 로 등록). Windows 는 설치한
   PowerShell 창을 닫아도 계속 돕니다.

| 옵션 | 뜻 |
|---|---|
| `--url` | 이 기기에서 접속할 Shyni 서버 주소 |
| `--token` | 관리자 화면에서 받은 등록 토큰 |
| `--dir` | 에이전트 폴더 (기본 `~/shyni-transcoder`) |
| `--tmp` | 변환 임시 폴더 (기본 `<폴더>/tmp`). 여유 공간이 넉넉한 디스크를 지정 |
| `--name` | 관리자 화면에 보일 러너 이름 (기본 호스트 이름) |
| `--slots` | 이 기기의 총 동시 작업 수 (기본 추천값). 붙어 있는 서버를 모두 합친 한도입니다 |
| `--encoder` | `auto`(기본) · `software` · `nvenc` · `vaapi` · `qsv` · `amf` · `videotoolbox` |
| `--ffmpeg` | 쓸 ffmpeg 경로를 직접 지정 (자동 선택·내려받기 생략) |
| `--rclone-remote` | 이 기기의 rclone 으로 직접 읽을 리모트 이름. 관리자 화면의 명령에 그 서버의 리모트가 들어 있고, 이 기기에 없는 것은 건너뜀 (위 "rclone 으로 원본 직접 읽기") |
| `--rclone`, `--rclone-config` | rclone 실행 파일과 설정 파일 위치 (기본값이 아닐 때) |
| `--no-service` | 서비스로 등록하지 않음. 직접 `run` 으로 실행 |
| `--force` | 0.6.1 부터 필요 없음(이미 등록한 서버면 늘 다시 등록하고, 서버가 이전 러너를 지움) |

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

| 기기 | 인코더 | 출력 코덱 | 디코딩 | 준비할 것 |
|---|---|---|---|---|
| Apple Silicon / Intel Mac | VideoToolbox | H.264, H.265 | VideoToolbox | 없음 |
| NVIDIA (Linux, Windows) | NVENC | H.264, H.265, AV1(RTX 40 계열부터) | CUDA | NVIDIA 드라이버 |
| Intel / AMD (Linux) | VAAPI, QSV | H.264, H.265, AV1(지원하는 GPU) | VAAPI(VAAPI 인코더와 함께일 때) | `/dev/dri` 접근 권한(`render` 그룹), VAAPI 드라이버 |
| AMD (Windows) | AMF | H.264, H.265, AV1(RX 7000 계열부터) | 없음(CPU) | AMD Adrenalin 드라이버 |
| Intel (Windows) | QSV | H.264, H.265, AV1(Arc·Core Ultra 부터) | QSV(D3D11 에서 축소까지 되는 드라이버, 아니면 CPU) | Intel 그래픽 드라이버 |

Windows 에서 외장 그래픽과 내장 그래픽이 같이 있으면 외장(NVIDIA·AMD)을 먼저 시험합니다. 원격 데스크톱
(RDP)으로 접속한 세션에서는 GPU 를 못 쓸 수 있습니다 — 그때는 기기 앞에서 로그인한 세션에서 돌리세요.

GPU 드라이버는 설치하지 않습니다(관리자 권한과 커널에 맞는 버전이 필요합니다). 드라이버를
갖춘 뒤 에이전트를 다시 시작하면 인코더를 다시 시험합니다.

### 출력 코덱

에이전트는 시작할 때 H.264 H/W 인코더와 같은 계열의 H.265·AV1 인코더(`hevc_videotoolbox`,
`hevc_nvenc`·`av1_nvenc`, `hevc_vaapi`·`av1_vaapi`, `hevc_qsv`·`av1_qsv`)도 실제 프레임으로
시험해, 통과한 코덱을 서버에 알립니다(`video_encoders`). 소프트웨어 H.265·AV1 은 쓰지 않습니다.
결과는 `status` 의 "출력 코덱" 줄에 나옵니다.

- **어느 코덱을 쓸지는 서버에서 정합니다.** 이 기기에 따로 설정할 것은 없습니다. 서버 관리
  화면(설정 → 관리 → 트랜스코더)의 러너별 "인코딩 코덱"에서 H.265·AV1 을 끌 수 있고(H.264 는 늘
  켜져 있음), 서버는 재생 기기가 하드웨어로 재생할 수 있는 코덱 가운데 가장 좋은 것(AV1 → H.265
  → H.264)을 작업마다 골라 보냅니다. 재생 기기가 AV1 을 못 풀면 H.265 로, 그것도 안 되면 H.264
  로 변환합니다.
- 비트레이트는 화질을 유지하는 만큼 낮춥니다. 480p 기준 H.264 1.5 Mbps, H.265 1 Mbps, AV1
  0.9 Mbps 입니다(H.265 는 H.264 의 2/3, AV1 은 3/5).
- H.265·AV1 은 HLS 를 fMP4 로 보냅니다. 자막을 같이 싣는 재생에 필요한 fMP4 + WebVTT 조합도
  시작할 때 시험하고(`hls_fmp4_subtitles`), 이 기기의 ffmpeg 가 통과하지 못하면 자막이 있는
  재생만 H.264 로 변환합니다(`status` 에 "자막을 같이 싣는 재생은 H.264"로 나옵니다).
- 720p 이하로 변환할 때 원본 오디오가 무거우면(480p 는 300 kbps, 720p 는 400 kbps 이상) 서버가
  AAC 2.0 128 kbps 로 낮추라고 보냅니다.
- 출력은 원본이 10비트(또는 4:2:2·4:4:4)여도 늘 8비트 4:2:0 입니다(0.6.3). 그 전에는 H.264 를
  libx264·NVENC·QSV 로 낼 때 원본 형식을 그대로 넘겨, 10비트 원본에서 libx264 는 재생 기기
  대부분이 못 푸는 High 10 을 내고 NVENC 는 변환을 시작하지 못했습니다.
- macOS(VideoToolbox H.265), Linux Intel VAAPI(H.265), Windows NVIDIA(RTX 3060 Ti, NVENC
  H.264·H.265 — 이 세대는 AV1 인코딩이 없어 시험에서 빠짐)에서 확인했습니다. NVENC AV1(RTX 40
  이후)과 AMD AMF 는 시험하지 못했습니다. AMF 는 품질 고정 대신 비트레이트 상한
  모드(`-rc vbr_peak`)로 변환합니다. 시험을 통과하지 못한 코덱은 쓰지 않으므로 H.264 변환에는
  영향이 없습니다.
- H.265·AV1 은 서버와 에이전트가 모두 0.4 이상일 때 씁니다. 한쪽이 그보다 낮으면 H.264 로
  변환합니다.

### 디코딩

디코딩도 시작할 때 시험합니다. H.264, HEVC·VP9·AV1(8·10비트)을 GPU 로 실제로 풀어 보고, 푼
코덱만 GPU 로 풉니다(못 푸는 코덱에 GPU 디코딩을 걸면 ffmpeg 이 실패합니다). 그다음 480p 변환을
GPU 디코딩과 CPU 디코딩으로 한 번씩 돌려, CPU 를 확실히 덜 쓸 때만 GPU 디코딩을 켭니다. Intel
VAAPI 와 NVIDIA(0.6.3, NVENC 와 ffmpeg 의 `scale_cuda` 가 있을 때)는 프레임을 GPU 에 둔 채
축소·인코딩하고, HDR 원본은 톤매핑 때문에 CPU 로 풉니다. GPU
디코딩으로 시작한 변환이 첫 출력 전에 실패하면 CPU 디코딩으로 다시 시작합니다. 결과는 `status` 의
"디코딩" 줄과 서버 관리 화면에 나옵니다. 서버는 원본을 GPU 로 풀 수 있는 러너를 먼저 고릅니다.

NVIDIA 는 0.6.2 까지 GPU 로 푼 프레임을 CPU 로 내려받아 CPU 에서 줄였습니다. RTX 3060 Ti +
i7-8700 에서 이 방식은 CPU 를 30% 남짓만 덜 써 켜질 때가 드물었고, GPU 에서 줄이면 1080p HEVC
10비트 → 720p 동시 8편이 실시간 1.5배 → 3.1배, CPU 합 68초 → 6초가 됐습니다(AV1 10비트는
0.9배 → 3.1배). NVDEC 가 못 푸는 형식(HEVC 4:2:2, H.264 4:4:4 등)은 0.5초 안에 실패해 CPU
디코딩으로 다시 시작합니다. 1080p HDR 은 톤매핑을 CPU 로 해서, 이 CPU 에서는 동시 4편쯤이 실시간
한계입니다.

Windows 의 Intel 그래픽(0.6.2)은 QSV 로 디코딩·축소·인코딩을 모두 GPU 에서 하되, D3D11 위에서
축소(`scale_qsv`)까지 되는 드라이버에서만 켭니다. 프레임을 CPU 로 내려받는 방식(d3d11va)은 GPU 를
인코더와 나눠 써서 CPU 디코딩보다 느려 쓰지 않습니다. 8세대(UHD 630) 구형 드라이버는 D3D11 축소가
안 되어 CPU 로 풉니다 — 그 PC 에서는 CPU 디코딩이 가장 빨랐습니다. DXVA2 위에서는 축소가 되지만
모니터가 꺼지면 장치를 못 만들어 쓰지 않습니다. GPU 디코딩이 실제로 켜지는 Intel 기기에서는
아직 시험하지 못했습니다.

## 쓰기

```sh
python3 ~/shyni-transcoder/agent/shyni_transcode_agent.py status      # 붙은 서버·인코더·설정 확인
python3 ~/shyni-transcoder/agent/shyni_transcode_agent.py run         # 직접 실행(--no-service 일 때)
python3 ~/shyni-transcoder/agent/shyni_transcode_agent.py update      # 새 버전 받기
python3 ~/shyni-transcoder/agent/shyni_transcode_agent.py uninstall   # 서비스·등록 정보 제거
```

Windows(PowerShell)는 `python3` 대신 `py`, 경로는 `"$HOME\shyni-transcoder\agent\shyni_transcode_agent.py"`
입니다(예: `py "$HOME\shyni-transcoder\agent\shyni_transcode_agent.py" status`).

로그는 서비스로 돌 때 macOS 는 `~/Library/Logs/shyni-transcode-agent.log`, Linux 는
`journalctl --user -u shyni-transcode-agent`, Windows 는 `<폴더>\agent.log`(10MB 를 넘으면
`agent.log.1` 로 넘김)에 쌓입니다. 한 폴더의 에이전트는 하나만 돕니다 — 서비스가 돌고 있으면
`run` 을 직접 실행해도 "이미 돌고 있습니다"로 끝납니다. Windows 에서 `configure`·`update`·
`remove-server` 를 실행하면 돌고 있는 에이전트가 몇 초 안에 스스로 다시 시작합니다. 사용 여부와 최대 슬롯, 삭제는 Shyni 관리자 화면에서 합니다.

관리자 화면에서 러너를 **삭제**하면(0.6.1, 서버도 그 버전 이후) 돌고 있는 에이전트가 몇 초 안에 그
서버 등록을 이 기기에서도 지웁니다. 붙은 서버가 하나도 남지 않으면 자동 시작(서비스·시작 프로그램)을
해제하고 끝납니다. 폴더는 남겨 두므로 같은 설치 명령을 다시 실행하면 받아 둔 ffmpeg 를 그대로 쓰고,
필요 없으면 폴더를 지우면 됩니다. 에이전트가 꺼져 있을 때 지웠어도 다시 설치하면 그 등록을 바꿔 끼웁니다
— `--force` 는 필요 없습니다. 이미 등록된 기기에서 설치 명령을 또 실행하면 서버가 이전 러너를 지우고 새로
등록하므로 러너가 둘로 남지 않습니다(사용 여부·최대 슬롯 같은 관리자 설정은 처음 값으로 돌아갑니다).
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
- **rclone 은 허용한 리모트만 씁니다.** 설치 명령에 적힌 리모트(관리자 화면의 명령에는 그 서버의
  리모트 이름이 들어 있음) 가운데 이 기기에 있는 것의 경로만 읽고, 서버가 rclone 설정이나 자격을
  보내올 수는 없습니다.
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
3. 에이전트가 ffmpeg 를 띄우고, 다 만들어진 HLS 조각과 playlist 를 서버로 올립니다. 원본의 앞(1MB)과
   뒤(4MB)는 작업을 받자마자 동시에 받아 두고 같은 파일의 다음 작업에도 씁니다 — ffmpeg 가 파일을 열며
   앞뒤를 오가는 작은 요청이 원격에서는 하나에 1~2초라서입니다. 서버는 곧 변환할 듯한 파일을 미리
   알려 주기도 합니다(`warm`).
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
- **관리자 화면에 "rclone 안 씀"** — 허용한 리모트가 없다는 뜻입니다(rclone 을 못 찾았다는 뜻이
  아닙니다). 예전 관리자 화면의 설치 명령에는 리모트가 들어 있지 않았습니다 — `configure
  --rclone-remote <이름>` 으로 켜세요. 리모트를 넣었는데도 이렇게 보이면 이 기기의 rclone 이 그 원본을 읽지 못해
  서버 중계로 넘어간 것입니다. 위 "rclone 으로 원본 직접 읽기"의 확인 명령을 실행해 보세요.
- **macOS 에서 외장 디스크(`/Volumes/…`)를 폴더로 쓸 때** — launchd 서비스로 도는 프로그램은
  외장 디스크에 접근할 수 없고, 묻는 창도 뜨지 않습니다. 관리자 화면에는 "오프라인"으로만
  보입니다. 시스템 설정 → 개인정보 보호 및 보안 → **전체 디스크 접근 권한**에 설치가 알려 주는
  python3 경로를 추가한 뒤 `service install` 을 다시 실행하거나, `--dir`·`--tmp` 를 내장 디스크로
  두세요. 임시 폴더에는 아직 서버로 올리지 않은 조각만 남아 내장 디스크로도 충분합니다.
  설치 직후 "Python 이 이동식 볼륨의 파일에 접근하려고 합니다" 창이 뜨면 허용해도 됩니다.

- **Windows 에서 "S/W 로 등록됨"** — `status` 의 GPU 이름과 인코더를 확인하세요. 그래픽 드라이버를
  최신으로 올리고(NVIDIA·AMD Adrenalin·Intel), 원격 데스크톱 세션이 아닌 곳에서 `service install` 을
  다시 실행하면 인코더를 다시 시험합니다.
- **Windows 에서 로그인해야만 돕니다** — 시작 프로그램으로 등록하므로 그 사용자가 로그인해 있는 동안
  돕니다(macOS·Linux 의 사용자 서비스와 같습니다). 자동 로그인을 켜 두면 재부팅 뒤에도 붙습니다.
- **Windows 백신이 pythonw 나 ffmpeg 를 막을 때** — `<폴더>` 를 검사 예외에 넣으세요. 막히면
  `agent.log` 에 ffmpeg 시작 실패가 남습니다.

## 개발

```sh
python3 -m unittest discover -s . -p 'test_*.py'
```

Shyni 서버도 이 파일을 그대로 불러 ffmpeg 명령 구성(`build_*_args`), 인코더·디코더 실측,
비트레이트·오디오 규칙에 씁니다. 같은 원본이 서버에서 변환되든 러너에서 변환되든 재생기가 받는
스트림이 같도록 구현을 한 벌만 둡니다. 원본은 Shyni 저장소의 `server/app/services/` 에 있고
(`tools/transcode-agent/` 의 파일은 그것을 가리키는 링크), 이 저장소에는 그 파일을 그대로
올립니다. 그래서 명령 구성 부분은 서버의 Python 3.12 에서도 돌아야 하고, 표준 라이브러리만
씁니다. 서버와 에이전트의 약속은 `PROTOCOL_VERSION` 하나이고, 서로 호환되지 않게 바뀌면 이 번호를
올립니다.

## 라이선스

MIT. 설치 중에 내려받는 jellyfin-ffmpeg 는 GPL 로 배포되는 별도의 프로그램이며 이 저장소에
포함되어 있지 않습니다.

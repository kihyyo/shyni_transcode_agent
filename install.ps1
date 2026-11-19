# Shyni 트랜스코딩 에이전트 설치 (Windows PowerShell 5.1 이상)
#
# Shyni 관리자 화면(설정 → 관리 → 트랜스코더 → 러너 추가)에서 Windows 를 고르면 나오는 명령으로 실행한다:
#
#   & ([scriptblock]::Create((irm 'https://raw.githubusercontent.com/kihyyo/shyni_transcode_agent/main/install.ps1'))) `
#       --url 'https://shyni.example' --token 'shr_...' [--dir DIR] [--tmp DIR] [--name NAME] [--no-service]
#
# 저장소를 받아 둔 경우: powershell -ExecutionPolicy Bypass -File install.ps1 --url ... --token ...
#
# 이 스크립트는 Python 을 찾고(없으면 winget 으로 사용자 범위에 설치), 에이전트 파일 하나를 받아 넘긴다.
# GPU 감지와 H/W 인코더 시험, ffmpeg 준비, 서버 등록, 로그인 시 자동 시작 등록은 에이전트의 install 명령이
# 한다. 옵션은 그대로 전달된다. 관리자 권한은 필요 없다.
#
# 창을 닫지 않도록 exit 를 쓰지 않는다(irm 으로 받은 스크립트는 지금 창에서 돈다). 실패는 throw 로 알린다.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'     # 5.1 의 진행 표시줄은 내려받기를 크게 늦춘다
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

$AgentUrl = 'https://raw.githubusercontent.com/kihyyo/shyni_transcode_agent/main/shyni_transcode_agent.py'
if ($env:SHYNI_AGENT_SOURCE_URL) { $AgentUrl = $env:SHYNI_AGENT_SOURCE_URL }

$AgentArgs = @($args)
$Dir = Join-Path $HOME 'shyni-transcoder'
for ($i = 0; $i -lt $AgentArgs.Count - 1; $i++) {
    if ($AgentArgs[$i] -eq '--dir') { $Dir = [string]$AgentArgs[$i + 1] }
}

function Resolve-Python {
    # 실제 python.exe 경로를 돌려준다. Windows 의 python·python3 "앱 실행 별칭"은 Store 를 여는
    # 껍데기라 목록에 있어도 실행해 버전을 확인한다. 인자에 큰따옴표를 넣지 않는다(5.1 이 깨뜨린다).
    param([string[]]$Command)
    # 5.1 은 Stop 일 때 리디렉션한 네이티브 stderr 를 오류로 바꿔 멈춘다 — 이 함수 안에서만 끈다.
    $ErrorActionPreference = 'Continue'
    $exe = $Command[0]
    $rest = @($Command | Select-Object -Skip 1)
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { return $null }
    try {
        $out = & $exe @rest -c 'import sys; print(sys.executable if sys.version_info >= (3, 9) else str())' 2>$null
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    $path = ([string]($out | Select-Object -Last 1)).Trim()
    if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    return $null
}

function Find-Python {
    if ($env:PYTHON) { return $env:PYTHON }
    foreach ($command in @(@('py', '-3'), @('python'), @('python3'))) {
        $found = Resolve-Python $command
        if ($found) { return $found }
    }
    if (-not $env:LOCALAPPDATA) { return $null }
    # python.org·winget 설치본의 기본 위치(PATH 에 아직 없을 때)
    foreach ($folder in @('Python313', 'Python312', 'Python311', 'Python310', 'Python39')) {
        $candidate = Join-Path $env:LOCALAPPDATA "Programs\Python\$folder\python.exe"
        if (Test-Path -LiteralPath $candidate) {
            $found = Resolve-Python @($candidate)
            if ($found) { return $found }
        }
    }
    return $null
}

$Python = Find-Python
if (-not $Python) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'Python 3.9 이상이 필요합니다. https://www.python.org/downloads/windows/ 에서 설치한 뒤 이 명령을 다시 실행하세요.'
    }
    Write-Host 'Python 이 없어 winget 으로 Python 3.12 를 설치합니다(이 사용자에게만, 관리자 권한 불필요)...'
    & winget install --id Python.Python.3.12 --exact --scope user --silent --accept-package-agreements --accept-source-agreements
    # 새 PATH 는 이 창에 바로 반영되지 않는다 — 레지스트리에서 다시 읽는다.
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    $Python = Find-Python
    if (-not $Python) {
        throw 'Python 을 설치하지 못했습니다. https://www.python.org/downloads/windows/ 에서 설치한 뒤 이 명령을 다시 실행하세요.'
    }
}
Write-Host "Python: $Python"

$AgentDir = Join-Path $Dir 'agent'
New-Item -ItemType Directory -Force -Path $AgentDir | Out-Null
$Target = Join-Path $AgentDir 'shyni_transcode_agent.py'
# 저장소를 받아 -File 로 실행했으면 같은 폴더의 파일을 쓴다. irm 으로 받아 실행하면 공개 저장소에서 받는다.
$Local = $null
if ($PSCommandPath) { $Local = Join-Path (Split-Path -Parent $PSCommandPath) 'shyni_transcode_agent.py' }
if ($Local -and (Test-Path -LiteralPath $Local)) {
    Copy-Item -LiteralPath $Local -Destination $Target -Force
} else {
    Invoke-WebRequest -UseBasicParsing -Uri $AgentUrl -OutFile "$Target.download"
    Move-Item -Force -LiteralPath "$Target.download" -Destination $Target
}

& $Python $Target install --dir $Dir @AgentArgs
if ($LASTEXITCODE -ne 0) {
    throw "설치가 끝나지 않았습니다(종료 코드 $LASTEXITCODE). 위의 메시지를 확인하세요."
}

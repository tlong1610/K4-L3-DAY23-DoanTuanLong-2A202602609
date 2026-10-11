# Mo shell Linux de lam lab tren Windows.
#   .\devenv\lab.ps1                 -> mo bash trong container
#   .\devenv\lab.ps1 make test       -> chay 1 lenh roi thoat
#   .\devenv\lab.ps1 -Rebuild        -> build lai image (khi doi requirements.txt)
param([switch]$Rebuild, [Parameter(ValueFromRemainingArguments = $true)][string[]]$Cmd)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$image = "lab23-env"; $name = "lab23"

docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Docker daemon chua chay -- mo Docker Desktop truoc." }

docker image inspect $image *> $null
if ($Rebuild -or $LASTEXITCODE -ne 0) {
    docker rm -f $name *> $null
    docker build -t $image -f "$repo\devenv\Dockerfile" $repo
    if ($LASTEXITCODE -ne 0) { throw "build image that bai" }
}

$running = docker ps -q -f "name=^$name$"
if (-not $running) {
    docker rm -f $name *> $null
    docker run -d --name $name -v "${repo}:/lab" $image | Out-Null
}

if ($Cmd) { docker exec -it $name bash -lc ($Cmd -join " ") }
else      { docker exec -it $name bash }

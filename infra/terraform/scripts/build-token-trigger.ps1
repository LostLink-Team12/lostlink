$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$buildRoot = Join-Path $repoRoot ('infra/terraform/.terraform/token-trigger-' + [guid]::NewGuid().ToString('N'))
$packageRoot = Join-Path $buildRoot 'package'
New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null
# AL2023 Python 3.12 has glibc 2.34; manylinux 2.28 wheels are compatible.
uv pip install --python-version 3.12 --python-platform x86_64-manylinux_2_28 --only-binary=:all: --target $packageRoot -r (Join-Path $repoRoot 'requirements.lock')
if ($LASTEXITCODE -ne 0) { throw 'Linux Lambda dependency packaging failed.' }
Copy-Item -LiteralPath (Join-Path $repoRoot 'libs/src/lostlink_common') -Destination $packageRoot -Recurse
Copy-Item -LiteralPath (Join-Path $repoRoot 'services/venue-service/venue_app') -Destination $packageRoot -Recurse
Copy-Item -LiteralPath (Join-Path $repoRoot 'infra/terraform/token-trigger/lambda_entry.py') -Destination $packageRoot
Invoke-WebRequest -Uri 'https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem' -OutFile (Join-Path $packageRoot 'global-bundle.pem')
$zipPath = Join-Path $buildRoot 'token-trigger.zip'
[IO.Compression.ZipFile]::CreateFromDirectory($packageRoot, $zipPath)
$hash = [Convert]::ToBase64String([Security.Cryptography.SHA256]::HashData([IO.File]::ReadAllBytes($zipPath)))
@{ token_trigger_package=$zipPath.Replace('\','/'); token_trigger_sha256=$hash } | ConvertTo-Json

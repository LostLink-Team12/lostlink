$ErrorActionPreference = 'Stop'
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$helm = Join-Path $repositoryRoot 'infra/terraform/.terraform/tools/helm/windows-amd64/helm.exe'
$env:AWS_PROFILE = 'lostlink-dev'
$env:AWS_DEFAULT_REGION = 'ap-southeast-1'
$env:KUBECONFIG = Join-Path $repositoryRoot 'infra/terraform/.terraform/validation/kubeconfig'
$env:HELM_REPOSITORY_CONFIG = Join-Path $PSScriptRoot '.local/helm-repositories.yaml'
$env:HELM_REPOSITORY_CACHE = Join-Path $PSScriptRoot '.local/helm-cache'
foreach ($service in @('venue', 'item', 'matching', 'claim', 'notification')) {
    $chart = Join-Path $repositoryRoot "infra/helm/$service-service"
    & $helm dependency build $chart --skip-refresh
    if ($LASTEXITCODE -ne 0) { throw "Dependency build failed: $service" }
    & $helm upgrade --install $service $chart -n lostlink --kube-context lostlink-dev `
        -f (Join-Path $PSScriptRoot ".local/$service.yaml")
    if ($LASTEXITCODE -ne 0) { throw "Release failed: $service" }
}
& $helm upgrade --install platform (Join-Path $repositoryRoot 'infra/helm/platform') `
    -n lostlink --kube-context lostlink-dev -f (Join-Path $PSScriptRoot '.local/platform.yaml')
if ($LASTEXITCODE -ne 0) { throw 'Platform release failed' }

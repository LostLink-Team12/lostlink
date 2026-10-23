$ErrorActionPreference = 'Stop'
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$localRoot = Join-Path $PSScriptRoot '.local'
$helm = Join-Path $repositoryRoot 'infra/terraform/.terraform/tools/helm/windows-amd64/helm.exe'
$env:AWS_PROFILE = 'lostlink-dev'
$env:AWS_DEFAULT_REGION = 'ap-southeast-1'
$env:KUBECONFIG = Join-Path $repositoryRoot 'infra/terraform/.terraform/validation/kubeconfig'
$env:HELM_REPOSITORY_CONFIG = Join-Path $localRoot 'helm-repositories.yaml'
$env:HELM_REPOSITORY_CACHE = Join-Path $localRoot 'helm-cache'

function Invoke-Helm {
    & $helm @args
    if ($LASTEXITCODE -ne 0) { throw "Helm failed: $($args[0])" }
}

$deploymentOutputs = terraform "-chdir=$repositoryRoot/infra/terraform/environments/dev" output -json |
    ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Cannot read Terraform outputs' }
Invoke-Helm repo add eks https://aws.github.io/eks-charts
Invoke-Helm repo add kedacore https://kedacore.github.io/charts
Invoke-Helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/
Invoke-Helm repo add prometheus-community https://prometheus-community.github.io/helm-charts

Invoke-Helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller `
    --version 3.6.1 -n kube-system --kube-context lostlink-dev `
    --set "clusterName=$($deploymentOutputs.cluster_name.value)" `
    --set 'region=ap-southeast-1' --set "vpcId=$($deploymentOutputs.vpc_id.value)" `
    --set 'replicaCount=1' --set 'enableServiceMutatorWebhook=false' `
    --set 'serviceAccount.name=aws-load-balancer-controller' `
    --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=$($deploymentOutputs.c3.value.alb_controller_role_arn)" `
    --wait --timeout 5m

Invoke-Helm upgrade --install metrics-server metrics-server/metrics-server --version 3.14.0 `
    -n kube-system --kube-context lostlink-dev --wait --timeout 5m

$kedaValues = @'
resources:
  operator:
    requests: {cpu: 50m, memory: 128Mi}
    limits: {cpu: 500m, memory: 384Mi}
  metricServer:
    requests: {cpu: 25m, memory: 64Mi}
    limits: {cpu: 250m, memory: 256Mi}
volumes:
  keda:
    extraVolumes:
      - name: rds-trust
        configMap: {name: lostlink-trust}
    extraVolumeMounts:
      - {name: rds-trust, mountPath: /trust, readOnly: true}
'@
$kedaPath = Join-Path $localRoot 'keda.yaml'
Set-Content -LiteralPath $kedaPath -Value $kedaValues -Encoding utf8
Invoke-Helm upgrade --install keda kedacore/keda --version 2.21.0 `
    -n keda --kube-context lostlink-dev -f $kedaPath --wait --timeout 5m

Invoke-Helm upgrade --install kube-state-metrics prometheus-community/kube-state-metrics `
    --version 8.6.0 -n kube-system --kube-context lostlink-dev `
    --set 'resources.requests.cpu=25m' --set 'resources.requests.memory=64Mi' `
    --set 'resources.limits.cpu=250m' --set 'resources.limits.memory=256Mi' `
    --wait --timeout 5m

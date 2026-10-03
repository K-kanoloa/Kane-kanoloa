param([switch]$WithBridge)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Logs = Join-Path $RepoRoot ".runtime-logs"
$ManifestPath = Join-Path $Logs "dev-stack-manifest.json"
$Ports = @(3000, 8000)
if ($WithBridge) { $Ports += 8010 }

function Test-Port {
  param([int]$Port)
  $client = [System.Net.Sockets.TcpClient]::new()
  try { return $client.ConnectAsync("127.0.0.1", $Port).Wait(500) -and $client.Connected }
  catch { return $false }
  finally { $client.Dispose() }
}

if (Test-Path -LiteralPath $ManifestPath) {
  $manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
  $launcher = Get-Process -Id $manifest.launcherPid -ErrorAction SilentlyContinue
  $requestedServices = if ($WithBridge) { @("api", "web", "bridge") } else { @("api", "web") }
  if ($manifest.repoRoot -eq $RepoRoot -and $launcher -and
      @($requestedServices | Where-Object { $_ -notin $manifest.services.name }).Count -eq 0) {
    & (Join-Path $PSScriptRoot "wait-for-stack.ps1") -WithBridge:$WithBridge
    Write-Host "[start:stack] Kane is already ready: http://127.0.0.1:3000"
    exit 0
  }
}

$occupied = @($Ports | Where-Object { Test-Port $_ })
if ($occupied.Count) {
  throw "Ports already occupied: $($occupied -join ', '). Use npm run stop:stack to stop the old Kane stack first. No processes were replaced."
}

New-Item -ItemType Directory -Path $Logs -Force | Out-Null
$node = (Get-Command node -CommandType Application | Select-Object -First 1).Source
$arguments = @("scripts/dev-stack.mjs")
if ($WithBridge) { $arguments += "--with-bridge" }
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$launcher = Start-Process -FilePath $node -ArgumentList $arguments -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput (Join-Path $Logs "stack-$stamp.stdout.log") `
  -RedirectStandardError (Join-Path $Logs "stack-$stamp.stderr.log")

try {
  & (Join-Path $PSScriptRoot "wait-for-stack.ps1") -WithBridge:$WithBridge
  if ($launcher.HasExited) { throw "Kane launcher exited before readiness was confirmed." }
  Write-Host "[start:stack] Kane ready: http://127.0.0.1:3000 (Web + API)"
} catch {
  # Only roll back the stack started by this invocation.
  if (Test-Path -LiteralPath $ManifestPath) {
    $manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    if ($manifest.repoRoot -eq $RepoRoot -and $manifest.launcherPid -eq $launcher.Id) {
      & (Join-Path $PSScriptRoot "stop-stack.ps1") -Ports $Ports
    }
  }
  throw
}

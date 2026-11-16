# Baut lhm/LhmDeps.csproj und stellt sicher, dass LibreHardwareMonitorLib.dll samt Abhängigkeiten in lhm_out liegt.
$ErrorActionPreference = "Stop"
$out = "lhm_out"
dotnet build lhm/LhmDeps.csproj -c Release -o $out
if ($LASTEXITCODE -ne 0) { throw "dotnet build fehlgeschlagen" }
Remove-Item "$out/LhmDeps.*" -ErrorAction SilentlyContinue

$pkgRoot = Join-Path $env:USERPROFILE ".nuget/packages"
if (-not (Test-Path "$out/LibreHardwareMonitorLib.dll")) {
    Write-Host "LibreHardwareMonitorLib.dll nicht in der Build-Ausgabe - suche im NuGet-Cache ..."
    $all = Get-ChildItem (Join-Path $pkgRoot "librehardwaremonitorlib") -Recurse -Filter "LibreHardwareMonitorLib.dll" -ErrorAction SilentlyContinue
    $all | ForEach-Object { Write-Host "  gefunden: $($_.FullName)" }
    # Bevorzugt .NET Framework 4.7.2, sonst .NET Standard 2.0
    $pick = $all | Where-Object { $_.FullName -match "net472" } | Select-Object -First 1
    if (-not $pick) { $pick = $all | Where-Object { $_.FullName -match "netstandard2" } | Select-Object -First 1 }
    if (-not $pick) {
        Write-Host "Paketinhalt:"
        Get-ChildItem (Join-Path $pkgRoot "librehardwaremonitorlib") -Recurse | ForEach-Object { Write-Host "  $($_.FullName)" }
        throw "LibreHardwareMonitorLib.dll im NuGet-Paket nicht gefunden"
    }
    Copy-Item $pick.FullName $out/
    Write-Host "kopiert: $($pick.FullName)"
}

# Fehlende Abhängigkeiten (gleicher Fallback, nur net472/netstandard2.0-Varianten)
foreach ($dep in "HidSharp", "DiskInfoToolkit", "RAMSPDToolkit-NDD", "BlackSharp.Core") {
    if (-not (Test-Path "$out/$dep.dll")) {
        $f = Get-ChildItem $pkgRoot -Recurse -Filter "$dep.dll" -ErrorAction SilentlyContinue |
             Where-Object { $_.FullName -match "net472|net48|netstandard2" } | Select-Object -First 1
        if ($f) { Copy-Item $f.FullName $out/; Write-Host "nachkopiert: $($f.FullName)" }
    }
}
Get-ChildItem $out | Select-Object Name, Length | Format-Table -AutoSize

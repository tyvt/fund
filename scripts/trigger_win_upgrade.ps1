# Diagnose Windows Update service and trigger upgrade
Write-Host "=== Windows Update Service ==="
Get-Service wuauserv, bits, cryptsvc, msiserver | Format-Table Name, Status, StartType -AutoSize

Write-Host "=== Update Policy ==="
$policy = Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate' -ErrorAction SilentlyContinue
if ($policy) {
    $policy | Format-List
} else {
    Write-Host "No custom Windows Update group policy found"
}

Write-Host "=== Trigger UsoClient ==="
try {
    UsoClient StartScan
    Write-Host "StartScan: OK"
} catch {
    Write-Host "StartScan failed: $($_.Exception.Message)"
}

Start-Sleep -Seconds 15

try {
    UsoClient StartDownload
    Write-Host "StartDownload: OK"
} catch {
    Write-Host "StartDownload failed: $($_.Exception.Message)"
}

Start-Sleep -Seconds 30

try {
    UsoClient StartInstall
    Write-Host "StartInstall: OK"
} catch {
    Write-Host "StartInstall failed: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "=== Open Windows Update settings ==="
Start-Process "ms-settings:windowsupdate"

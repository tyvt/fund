# Enable and start Windows Update services, then trigger 22H2 upgrade
Write-Host "=== Enabling Windows Update services ==="

# Enable wuauserv (Windows Update)
Set-Service -Name wuauserv -StartupType Manual -ErrorAction Stop
Start-Service -Name wuauserv -ErrorAction Stop
Write-Host "wuauserv: $($((Get-Service wuauserv).Status))"

# Enable BITS (Background Intelligent Transfer)
Set-Service -Name bits -StartupType Manual -ErrorAction SilentlyContinue
Start-Service -Name bits -ErrorAction SilentlyContinue
Write-Host "bits: $($((Get-Service bits).Status))"

# Ensure cryptsvc running
Start-Service -Name cryptsvc -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "=== Triggering update scan/download/install ==="
UsoClient StartScan
Start-Sleep -Seconds 20
UsoClient StartDownload
Start-Sleep -Seconds 30
UsoClient StartInstall

Write-Host ""
Write-Host "=== Checking for updates via COM ==="
$s = New-Object -ComObject Microsoft.Update.Session
$sc = $s.CreateUpdateSearcher()
try {
    $r = $sc.Search("IsInstalled=0 and Type='Software'")
    Write-Host "Found $($r.Updates.Count) pending updates:"
    foreach ($u in $r.Updates) {
        Write-Host "  - $($u.Title)"
    }
} catch {
    Write-Host "Search: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "Opening Windows Update settings..."
Start-Process "ms-settings:windowsupdate"
Write-Host "Please check the Windows Update window for 22H2 feature update."

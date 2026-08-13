# Check Windows version and trigger update scan
$cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
Write-Host "=== Current System ==="
Write-Host "$($cv.ProductName) $($cv.DisplayVersion) Build $($cv.CurrentBuild).$($cv.UBR)"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Write-Host "Admin: $isAdmin"

try {
    $tpm = Get-Tpm -ErrorAction SilentlyContinue
    Write-Host "TPM Ready: $($tpm.TpmReady)"
} catch {
    Write-Host "TPM: cannot detect"
}

try {
    $sb = Confirm-SecureBootUEFI -ErrorAction SilentlyContinue
    Write-Host "Secure Boot: $sb"
} catch {
    Write-Host "Secure Boot: cannot detect"
}

Write-Host ""
Write-Host "=== Searching Windows Update ==="
$s = New-Object -ComObject Microsoft.Update.Session
$sc = $s.CreateUpdateSearcher()
try {
    $r = $sc.Search("IsInstalled=0 and Type='Software'")
    Write-Host "Found $($r.Updates.Count) pending updates"
    foreach ($u in $r.Updates) {
        Write-Host "  - $($u.Title)"
    }
} catch {
    Write-Host "Search failed: $($_.Exception.Message)"
}

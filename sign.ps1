# Signs dist\ByteDog.exe with the internal "ROSCODE TECH" code-signing certificate
# (CurrentUser\My, private key non-exportable), so UAC shows "Verified publisher:
# ROSCODE TECH" on machines that trust that certificate. Called by build.bat.
# Exit codes: 0 signed and verified, 2 no certificate (build stays unsigned), 1 failed.
param([string]$Exe = (Join-Path $PSScriptRoot 'dist\ByteDog.exe'))
$ErrorActionPreference = 'Stop'
$Subject = 'CN=ROSCODE TECH, O=ROSCODE TECH'

$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
    Where-Object { $_.Subject -eq $Subject -and $_.NotAfter -gt (Get-Date) } |
    Sort-Object NotAfter -Descending | Select-Object -First 1
if (-not $cert) {
    Write-Warning "No valid '$Subject' code-signing certificate on this machine; $Exe left unsigned."
    exit 2
}
if (-not (Test-Path $Exe)) {
    Write-Error "$Exe not found; build it first."
    exit 1
}

$result = Set-AuthenticodeSignature -FilePath $Exe -Certificate $cert -HashAlgorithm SHA256
$check = Get-AuthenticodeSignature -FilePath $Exe
if ($check.Status -ne 'Valid' -or $check.SignerCertificate.Thumbprint -ne $cert.Thumbprint) {
    Write-Error "Signing failed: $($result.StatusMessage) / verify: $($check.Status) $($check.StatusMessage)"
    exit 1
}
Write-Host "Signed $Exe as $($cert.Subject) (thumbprint $($cert.Thumbprint))"
exit 0

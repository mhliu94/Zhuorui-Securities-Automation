param(
    [string]$CertificatePath,
    [ValidateSet("LocalMachine", "CurrentUser")]
    [string]$StoreScope = "LocalMachine"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $CertificatePath) {
    $CertificatePath = Join-Path $Root "certs\zhuorui-monitor-cert.cer"
}
if (-not (Test-Path -LiteralPath $CertificatePath)) {
    throw "Windows certificate file not found. Run setup_zhuorui_monitor_https.ps1 first."
}

$Certificate = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($CertificatePath)
$StoreLocation = if ($StoreScope -eq "LocalMachine") {
    [System.Security.Cryptography.X509Certificates.StoreLocation]::LocalMachine
} else {
    [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser
}
$Store = New-Object System.Security.Cryptography.X509Certificates.X509Store(
    [System.Security.Cryptography.X509Certificates.StoreName]::Root,
    $StoreLocation
)
try {
    $Store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
    $Existing = $Store.Certificates.Find(
        [System.Security.Cryptography.X509Certificates.X509FindType]::FindByThumbprint,
        $Certificate.Thumbprint,
        $false
    )
    if ($Existing.Count -eq 0) {
        $Store.Add($Certificate)
        Write-Host "Trusted the Zhuorui HTTPS certificate in the $StoreScope root store."
    } else {
        Write-Host "The Zhuorui HTTPS certificate is already trusted in the $StoreScope root store."
    }
    Write-Host "Thumbprint: $($Certificate.Thumbprint)"
} finally {
    $Store.Close()
    $Certificate.Dispose()
}

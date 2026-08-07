param(
    [string]$CommonName = $env:COMPUTERNAME,
    [string[]]$AdditionalDnsName = @(),
    [string[]]$AdditionalIpAddress = @(),
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$CertificateDir = Join-Path $Root "certs"
$CertificatePath = Join-Path $CertificateDir "zhuorui-monitor-cert.pem"
$CertificateDerPath = Join-Path $CertificateDir "zhuorui-monitor-cert.cer"
$PrivateKeyPath = Join-Path $CertificateDir "zhuorui-monitor-key.pem"
$ConfigPath = Join-Path $CertificateDir "openssl-zhuorui-monitor.cnf"

if ((Test-Path -LiteralPath $CertificatePath) -and (Test-Path -LiteralPath $PrivateKeyPath) -and -not $Force) {
    Write-Host "HTTPS certificate already exists."
    Write-Host "Certificate: $CertificatePath"
    Write-Host "Private key: $PrivateKeyPath"
    exit 0
}

if ($Force) {
    Remove-Item -LiteralPath $CertificatePath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $CertificateDerPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PrivateKeyPath -Force -ErrorAction SilentlyContinue
}

$OpenSslCandidates = @(
    (Join-Path ${env:ProgramFiles} "Git\usr\bin\openssl.exe"),
    (Join-Path ${env:ProgramFiles} "OpenSSL-Win64\bin\openssl.exe")
)
$OpenSslCommand = Get-Command openssl.exe -ErrorAction SilentlyContinue
if ($OpenSslCommand) {
    $OpenSslCandidates = @($OpenSslCommand.Source) + $OpenSslCandidates
}
$OpenSslPath = $OpenSslCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $OpenSslPath) {
    throw "OpenSSL was not found. Install OpenSSL or Git for Windows."
}

New-Item -ItemType Directory -Path $CertificateDir -Force | Out-Null

$SafeCommonName = ($CommonName -replace '[^A-Za-z0-9._-]', '-')
$DnsNames = @($SafeCommonName, "localhost") + $AdditionalDnsName
$DnsNames = $DnsNames | Where-Object { $_ } | ForEach-Object { $_ -replace '[^A-Za-z0-9.*_-]', '-' } | Select-Object -Unique
$IpAddresses = @("127.0.0.1") + $AdditionalIpAddress
try {
    $LocalAddresses = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
        Select-Object -ExpandProperty IPAddress
    $IpAddresses += $LocalAddresses
} catch {
    # Local addresses are optional; localhost remains in the certificate.
}
$IpAddresses = $IpAddresses | Where-Object {
    $ParsedAddress = $null
    [System.Net.IPAddress]::TryParse($_, [ref]$ParsedAddress)
} | Select-Object -Unique

$SanEntries = @()
$SanEntries += $DnsNames | ForEach-Object { "DNS:$_" }
$SanEntries += $IpAddresses | ForEach-Object { "IP:$_" }
$OpenSslConfig = @"
[req]
prompt = no
distinguished_name = subject
x509_extensions = server_extensions

[subject]
CN = $SafeCommonName

[server_extensions]
subjectAltName = $($SanEntries -join ',')
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
"@
$OpenSslConfig | Set-Content -LiteralPath $ConfigPath -Encoding ascii

try {
    & $OpenSslPath req `
        -x509 `
        -nodes `
        -newkey rsa:3072 `
        -sha256 `
        -days 397 `
        -keyout $PrivateKeyPath `
        -out $CertificatePath `
        -config $ConfigPath
    if ($LASTEXITCODE -ne 0) {
        throw "OpenSSL exited with code $LASTEXITCODE."
    }
    & $OpenSslPath x509 -in $CertificatePath -outform der -out $CertificateDerPath
    if ($LASTEXITCODE -ne 0) {
        throw "OpenSSL could not create the Windows certificate file."
    }
} finally {
    Remove-Item -LiteralPath $ConfigPath -Force -ErrorAction SilentlyContinue
}

$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$LoginIdentity = "$env:USERDOMAIN\$env:USERNAME"
if ($LoginIdentity -eq $Identity) {
    & icacls.exe $PrivateKeyPath /inheritance:r /grant:r "${Identity}:(F)" | Out-Null
} else {
    & icacls.exe $PrivateKeyPath /inheritance:r /grant:r "${Identity}:(F)" /grant:r "${LoginIdentity}:(R)" | Out-Null
}
if ($LASTEXITCODE -ne 0) {
    Write-Warning "The certificate was created, but private-key permissions could not be restricted."
}

Write-Host "Created a self-signed HTTPS certificate for:"
$SanEntries | ForEach-Object { Write-Host "  $_" }
Write-Host "Certificate: $CertificatePath"
Write-Host "Windows certificate: $CertificateDerPath"
Write-Host "Private key: $PrivateKeyPath"
Write-Warning "Browsers will show a trust warning until this certificate is trusted or replaced with one from a public certificate authority."

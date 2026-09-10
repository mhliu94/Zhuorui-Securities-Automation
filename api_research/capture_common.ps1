function Get-CaptureSettings {
    param([string]$ConfigPath, [switch]$VerifyBoot)
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
    if (-not $ConfigPath) { $ConfigPath = Join-Path $ProjectRoot 'zhuorui_config.json' }
    $Candidates = @((Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
        (Join-Path $ProjectRoot '.venv-api\Scripts\python.exe'), (Join-Path $ProjectRoot '.venv\Scripts\python.exe'))
    $BootstrapPython = $Candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $BootstrapPython) {
        $Command = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $Command) { $Command = Get-Command py.exe -ErrorAction SilentlyContinue }
        if (-not $Command) { throw 'Install Python, then create the API or capture environment.' }
        $BootstrapPython = $Command.Source
    }
    $Options = @('--config', $ConfigPath)
    if ($VerifyBoot) { $Options += '--verify-boot' }
    $Json = & $BootstrapPython (Join-Path $PSScriptRoot 'capture_settings.py') @Options
    if ($LASTEXITCODE -ne 0) { throw 'Capture configuration or boot validation failed.' }
    return ($Json | ConvertFrom-Json)
}

function ConvertTo-ProcessArguments {
    param([string[]]$Values)
    # Start-Process joins arguments; quote each value with Windows CRT rules.
    return (($Values | ForEach-Object {
        '"' + ([regex]::Replace([regex]::Replace($_, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1')) + '"'
    }) -join ' ')
}

function Assert-TradingStopped {
    $Trading = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^(python|pythonw|cmd)(\.exe)?$' -and
        $_.CommandLine -match '(zhuorui_market_order\.(py|cmd)|start_zhuorui_listener\.ps1|zhuorui\.ui\.automation)'
    })
    if ($Trading.Count) { throw 'Stop the trading listener before changing the original emulator boot.' }
}

function Assert-CaptureBackup {
    param($Settings, $Session)
    if (-not $Session.backup_verified -or $Session.avd -ne $Settings.avd -or
        $Session.device -ne $Settings.device -or $Session.original_avd -ne $Settings.original_avd_dir) {
        throw 'The verified backup does not match the configured original emulator.'
    }
}

function Initialize-TestAvd {
    param($Settings, [string]$Kind)
    $Json = & $Settings.python_executable (Join-Path $PSScriptRoot 'create_test_avd.py') --config $Settings.config_path --kind $Kind
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the configured test AVD.' }
    return ($Json | ConvertFrom-Json)
}

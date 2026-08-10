param(
    [string]$OutputPath = (Join-Path $PSScriptRoot "zhuorui-zero-idle-dump.jar"),
    [string]$SdkRoot = "",
    [string]$JavaHomePath = "",
    [string]$JunitJar = ""
)

$ErrorActionPreference = "Stop"

function Get-AndroidVersion([System.IO.DirectoryInfo]$Directory) {
    $Match = [regex]::Match($Directory.Name, "(\d+(?:\.\d+)*)$")
    if ($Match.Success) {
        return [version]$Match.Groups[1].Value
    }
    return [version]"0.0"
}

if ([string]::IsNullOrWhiteSpace($SdkRoot)) {
    $SdkRoot = @(
        $env:ANDROID_SDK_ROOT,
        $env:ANDROID_HOME,
        (Join-Path $env:LOCALAPPDATA "Android\Sdk")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}
if (-not $SdkRoot) {
    throw "No Android SDK root was found. Pass -SdkRoot or set ANDROID_SDK_ROOT."
}

$Platform = Get-ChildItem -Directory (Join-Path $SdkRoot "platforms") |
    Where-Object {
        (Test-Path (Join-Path $_.FullName "android.jar")) -and
        (Test-Path (Join-Path $_.FullName "uiautomator.jar"))
    } |
    Sort-Object @{ Expression = { Get-AndroidVersion $_ }; Descending = $true } |
    Select-Object -First 1
$BuildTools = Get-ChildItem -Directory (Join-Path $SdkRoot "build-tools") |
    Where-Object { Test-Path (Join-Path $_.FullName "d8.bat") } |
    Sort-Object @{ Expression = { Get-AndroidVersion $_ }; Descending = $true } |
    Select-Object -First 1

if ([string]::IsNullOrWhiteSpace($JavaHomePath)) {
    $JavaHomePath = @(
        $env:JAVA_HOME,
        "C:\Program Files\Android\Android Studio\jbr"
    ) | Where-Object {
        $_ -and (Test-Path -LiteralPath (Join-Path $_ "bin\javac.exe"))
    } | Select-Object -First 1
}
if (-not $JavaHomePath) {
    throw "No Java development kit was found. Pass -JavaHomePath or set JAVA_HOME."
}
$Javac = Join-Path $JavaHomePath "bin\javac.exe"
if ([string]::IsNullOrWhiteSpace($JunitJar)) {
    $JunitJar = @(
        (Join-Path (Split-Path $JavaHomePath -Parent) "lib\junit4.jar"),
        "C:\Program Files\Android\Android Studio\lib\junit4.jar"
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}

if (-not $Platform) {
    throw "No Android SDK platform with android.jar and uiautomator.jar was found."
}
if (-not $BuildTools) {
    throw "No Android SDK build-tools installation with d8.bat was found."
}
if (-not (Test-Path $Javac)) {
    throw "Android Studio's bundled javac.exe was not found at $Javac."
}
if (-not (Test-Path $JunitJar)) {
    throw "Android Studio's JUnit jar was not found at $JunitJar."
}

$AndroidJar = Join-Path $Platform.FullName "android.jar"
$UiAutomatorJar = Join-Path $Platform.FullName "uiautomator.jar"
$D8 = Join-Path $BuildTools.FullName "d8.bat"
$Source = Join-Path $PSScriptRoot "ZeroIdleHierarchyDumpTest.java"
$TempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$TempRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $TempBase ("zhuorui-zero-idle-" + [guid]::NewGuid()))
)
if (-not $TempRoot.StartsWith($TempBase, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Temporary build path is outside the system temporary directory: $TempRoot"
}
$Classes = Join-Path $TempRoot "classes"
$BuiltJar = Join-Path $TempRoot "zhuorui-zero-idle-dump.jar"
$PreviousJavaHome = $env:JAVA_HOME

try {
    $env:JAVA_HOME = $JavaHomePath
    New-Item -ItemType Directory -Path $Classes -Force | Out-Null
    & $Javac `
        -source 8 `
        -target 8 `
        -classpath "$AndroidJar;$UiAutomatorJar;$JunitJar" `
        -d $Classes `
        $Source
    if ($LASTEXITCODE -ne 0) {
        throw "javac failed with exit code $LASTEXITCODE."
    }

    $ClassFile = Join-Path $Classes "com\zhuorui\automation\ZeroIdleHierarchyDumpTest.class"
    & $D8 `
        --lib $AndroidJar `
        --lib $UiAutomatorJar `
        --lib $JunitJar `
        --output $BuiltJar `
        $ClassFile
    if ($LASTEXITCODE -ne 0) {
        throw "d8 failed with exit code $LASTEXITCODE."
    }
    Copy-Item -LiteralPath $BuiltJar -Destination $OutputPath -Force
} finally {
    if ($null -eq $PreviousJavaHome) {
        Remove-Item Env:JAVA_HOME -ErrorAction SilentlyContinue
    } else {
        $env:JAVA_HOME = $PreviousJavaHome
    }
    $VerifiedTempRoot = [System.IO.Path]::GetFullPath($TempRoot)
    if (
        (Test-Path $VerifiedTempRoot) -and
        $VerifiedTempRoot.StartsWith($TempBase, [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        Remove-Item -LiteralPath $VerifiedTempRoot -Recurse -Force
    }
}

Write-Host "Built $OutputPath"

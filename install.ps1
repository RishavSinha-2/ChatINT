# ChatInt Windows 1-Line Installer
# Usage: irm https://raw.githubusercontent.com/RishavSinha-2/ChatINT/main/install.ps1 | iex

$ErrorActionPreference = 'Stop'

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "       ChatInt Windows Installer          " -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

try {
    # 1. Fetch latest release metadata from GitHub
    Write-Host "Checking for latest ChatInt release..." -ForegroundColor Gray
    $repo = "RishavSinha-2/ChatINT"
    $apiUrl = "https://api.github.com/repos/$repo/releases/latest"
    $headers = @{ "User-Agent" = "ChatInt-Installer" }
    
    try {
        $release = Invoke-RestMethod -Uri $apiUrl -Headers $headers -UseBasicParsing
        $version = $release.tag_name
    } catch {
        # Fallback to hardcoded current release if API limit is hit
        $version = "v0.5.1"
    }

    Write-Host "Found version: $version" -ForegroundColor Green

    # Check if already installed and up to date
    $installedExe = Join-Path $env:LOCALAPPDATA "Programs\ChatInt\ChatInt.exe"
    if (Test-Path $installedExe) {
        try {
            $curVer = (Get-Item $installedExe).VersionInfo.ProductVersion
            if ($curVer) {
                $curVerClean = $curVer.Trim().TrimStart('v')
                $targetVerClean = $version.Trim().TrimStart('v')
                if ($curVerClean -eq $targetVerClean -and -not $env:FORCE_REINSTALL) {
                    Write-Host ""
                    Write-Host "[OK] ChatInt $version is already installed and up to date!" -ForegroundColor Green
                    Write-Host "  Launch it from your Start Menu or Desktop shortcut." -ForegroundColor Yellow
                    Write-Host ""
                    return
                } elseif ($curVerClean -ne $targetVerClean) {
                    Write-Host "Upgrading ChatInt from v$curVerClean to $version..." -ForegroundColor Cyan
                }
            }
        } catch {
            # Continue with normal installation if version check fails
        }
    }

    # 2. Determine download URL
    $downloadUrl = "https://github.com/$repo/releases/download/$version/ChatInt-Setup-x64.exe"
    $tempInstaller = Join-Path ([System.IO.Path]::GetTempPath()) "ChatInt-Setup-$version.exe"

    # 3. Download the installer
    Write-Host "Downloading ChatInt installer..." -ForegroundColor Gray
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13
    if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
        curl.exe -L --fail --silent --show-error -o $tempInstaller $downloadUrl
    } else {
        Invoke-WebRequest -Uri $downloadUrl -OutFile $tempInstaller -UseBasicParsing
    }

    # 4. Run silent installer (installs to %LOCALAPPDATA%\Programs\ChatInt without UAC)
    Write-Host "Installing ChatInt..." -ForegroundColor Gray
    $installProc = Start-Process -FilePath $tempInstaller -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART" -Wait -PassThru

    # 5. Clean up temporary installer
    Remove-Item -Path $tempInstaller -Force -ErrorAction SilentlyContinue

    if ($installProc.ExitCode -eq 0) {
        Write-Host ""
        Write-Host "[OK] ChatInt $version installed successfully!" -ForegroundColor Green
        Write-Host "  Launch it from your Start Menu or Desktop shortcut." -ForegroundColor Yellow
        Write-Host ""
    } else {
        Write-Warning "Installer exited with code: $($installProc.ExitCode)"
    }
} catch {
    Write-Error "Failed to install ChatInt: $_"
}

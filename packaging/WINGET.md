# ChatInt WinGet Publication Guide

This document outlines the workflow and manifest specifications for publishing ChatInt to the official Windows Package Manager repository ([microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs)).

---

## 1. Distribution Strategy

ChatInt follows a **dual publishing strategy**:
1. **WinGet Package (`ChatInt-Setup-x64.exe`)**:
   - Built via Inno Setup (`packaging/ChatInt.iss`).
   - Installs to `%LOCALAPPDATA%\Programs\ChatInt` without requiring administrator privileges or UAC prompts.
   - User database, `.env`, and logs persist across updates in `%APPDATA%\ChatInt`.
   - Start Menu and Desktop shortcuts are managed automatically.
2. **Portable ZIP (`ChatInt-Portable-vX.X.X.zip`)**:
   - Released directly on GitHub Releases.
   - Self-contained portable mode next to the executable (`data/` and `.env`).
   - Not published to WinGet (WinGet exclusively handles the installer).

---

## 2. Package Specifications

| Property | Value |
| :--- | :--- |
| **PackageIdentifier** | `RishavSinha.ChatInt` |
| **PackageName** | `ChatInt` |
| **Publisher** | `Rishav Sinha` |
| **License** | `MIT` |
| **InstallerType** | `inno` |
| **InstallerUrl** | `https://github.com/RishavSinha-2/ChatINT/releases/download/v<version>/ChatInt-Setup-x64.exe` |
| **InstallModes** | `[ interactive, silent, silentWithProgress ]` |
| **InstallerScope** | `user` |
| **Architecture** | `x64` |
| **Silent Switches** | `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART` |
| **SilentWithProgress Switches** | `/SILENT /SUPPRESSMSGBOXES /NORESTART` |
| **UpgradeBehavior** | `install` |

---

## 3. Submission Workflow

### Step 1: Build Release Artifacts
Run the release build script:
```powershell
uv run python packaging/build_release.py
```
This builds:
- `dist/v<version>/ChatInt.exe`
- `dist/v<version>/ChatInt-Portable-v<version>.zip`
- `dist/installer/ChatInt-Setup-x64.exe` (using Inno Setup)
- Prints SHA-256 hashes for all artifacts.

### Step 2: Publish GitHub Release
1. Create a release tag on GitHub (e.g. `v0.5.1`).
2. Upload `ChatInt-Setup-x64.exe` and `ChatInt-Portable-v0.5.1.zip` as release assets.
3. Publish the release.

### Step 3: Submit to WinGet using `wingetcreate`
Use Microsoft's official `wingetcreate` tool:
```powershell
wingetcreate new https://github.com/RishavSinha-2/ChatINT/releases/download/v0.5.1/ChatInt-Setup-x64.exe
```
`wingetcreate` will:
1. Download the installer and compute the SHA-256 hash automatically.
2. Detect the `inno` installer type and prompt for metadata confirmation.
3. Validate manifests against WinGet schema.
4. Submit a Pull Request directly to `microsoft/winget-pkgs` using your GitHub token.

---

## 4. Sample WinGet Manifests

For manual PR submission, create the manifests under:
`manifests/r/RishavSinha/ChatInt/<version>/`

### Manifest 1: `RishavSinha.ChatInt.version.yaml`
```yaml
PackageIdentifier: RishavSinha.ChatInt
PackageVersion: 0.5.1
DefaultLocale: en-US
ManifestType: version
ManifestVersion: 1.6.0
```

### Manifest 2: `RishavSinha.ChatInt.installer.yaml`
```yaml
PackageIdentifier: RishavSinha.ChatInt
PackageVersion: 0.5.1
InstallerLocale: en-US
InstallerType: inno
Scope: user
InstallModes:
  - interactive
  - silent
  - silentWithProgress
InstallerSwitches:
  Silent: /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
  SilentWithProgress: /SILENT /SUPPRESSMSGBOXES /NORESTART
UpgradeBehavior: install
ReleaseDate: 2026-11-15
Installers:
  - Architecture: x64
    InstallerUrl: https://github.com/RishavSinha-2/ChatINT/releases/download/v0.5.1/ChatInt-Setup-x64.exe
    InstallerSha256: <SHA256_FROM_BUILD_RELEASE_PY>
ManifestType: installer
ManifestVersion: 1.6.0
```

### Manifest 3: `RishavSinha.ChatInt.locale.en-US.yaml`
```yaml
PackageIdentifier: RishavSinha.ChatInt
PackageVersion: 0.5.1
PackageLocale: en-US
Publisher: Rishav Sinha
PublisherUrl: https://github.com/RishavSinha-2/ChatINT
PublisherSupportUrl: https://github.com/RishavSinha-2/ChatINT/issues
PackageName: ChatInt
PackageUrl: https://github.com/RishavSinha-2/ChatINT
License: MIT
LicenseUrl: https://github.com/RishavSinha-2/ChatINT/blob/main/LICENSE
ShortDescription: High-performance desktop AI chat interface.
Description: |
  ChatInt is a native desktop client featuring multi-tab sessions, full markdown & KaTeX math rendering,
  local SQLite history, streaming token cost analytics, DPAPI-encrypted API keys, and custom providers.
Tags:
  - ai
  - chat
  - llm
  - openai
  - anthropic
  - gemini
  - ollama
ManifestType: defaultLocale
ManifestVersion: 1.6.0
```

---

## 5. Upgrade & Uninstall Verification

- **Upgrades (`winget upgrade RishavSinha.ChatInt`)**:
  - The installer runs silently with `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`.
  - Inno Setup script detects `WizardSilent() == True` and leaves `%APPDATA%\ChatInt` intact.
  - The newer `.exe` replaces the old binary in `%LOCALAPPDATA%\Programs\ChatInt`.
  - App opens with all chats, models, and settings preserved.
- **Uninstalls (`winget uninstall RishavSinha.ChatInt` or Windows Settings)**:
  - Interactive uninstallation displays a prompt asking if user wishes to delete their chat database and history.
  - Silent uninstalls preserve `%APPDATA%\ChatInt` to prevent accidental loss during scripted maintenance.

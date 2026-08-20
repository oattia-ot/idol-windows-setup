# INSTALL.md

## Prerequisites

- Windows Server or Windows 10/11, x64
- PowerShell 5.1+ (run `$PSVersionTable.PSVersion` to check)
- Administrator privileges (the script enforces this via `#Requires -RunAsAdministrator`)
- WINDOWS x64 component ZIP packages for Knowledge Discovery 26.2 (Linux
  packages, e.g. folders/files with `_LINUX_X86_64`, are detected and blocked)
- At least 4GB RAM and 10GB free disk space on the install drive

## 1. Get the files

Copy the `KD-Installer` folder to the target Windows machine, e.g.
`C:\Tools\KD-Installer`.

## 2. Prep the environment (run once)

Zips downloaded via browser/email/OneDrive get flagged "downloaded from
the internet" by Windows, which blocks unsigned `.ps1`/`.psm1` files from
running even as Administrator. Run the prep script once to clear that and
set an execution policy that allows local scripts.

**Recommended - use the bootstrap wrapper:**

```
Right-click Setup.bat -> "Run as administrator"
```

`Setup.bat` is a plain batch file, so it isn't affected by PowerShell's
script execution policy the way `Initialize-Environment.ps1` is. It
launches the PowerShell script with a one-time `-ExecutionPolicy Bypass`
override for that single process (this does not change any saved system
setting - `Initialize-Environment.ps1` itself still sets the persistent
policy, as described below).

**Alternative - run it directly:** if your policy is already permissive
enough to load unsigned local scripts (e.g. `RemoteSigned`), you can skip
the wrapper:

```powershell
cd C:\Tools\KD-Installer
.\Initialize-Environment.ps1
```

If instead you see an error like *"...cannot be loaded... is not
digitally signed..."*, your policy is currently `AllSigned` (or
`Restricted`, which errors with different wording) - in that case
`Initialize-Environment.ps1` can't even be loaded to run its own
policy-fixing step, so use `Setup.bat` above, or run this one-off command
instead:

```powershell
powershell -ExecutionPolicy Bypass -File .\Initialize-Environment.ps1
```

This will:
- Unblock every file recursively (`Unblock-File`)
- Set execution policy to `RemoteSigned` for the current process (or
  `-ExecutionPolicyScope CurrentUser` to persist it across sessions)
- Confirm you're elevated and on PowerShell 5.1+
- Verify all expected module files are present
- Optionally create `config\my-config.json` from the template for you to edit

If your organization enforces execution policy via Group Policy at the
`MachinePolicy` or `UserPolicy` scope, even `-ExecutionPolicy Bypass` will
be silently overridden and none of the above will work. Check which scope
is enforcing it with:

```powershell
Get-ExecutionPolicy -List
```

If `MachinePolicy` or `UserPolicy` shows anything other than `Undefined`,
you'll need to ask your admin for an exception, or have the scripts
code-signed, rather than trying to override it locally.

## 3. Configure

Edit `config\default-config.json`, or copy it to a new file (e.g.
`config\my-config.json`) and pass `-ConfigPath` to use it instead:

```json
{
  "BasePath": "C:\\KnowledgeDiscovery\\26.2",
  "ZipPath": "C:\\KD-Packages",
  "LicenseHost": "localhost",
  "LicensePort": "20000",
  "LicenseKeyPath": "C:\\OpenText\\DemoVMs\\licensekey.dat",
  "ThisHost": "localhost",
  "Components": ["LicenseServer", "Content", "Community", "Agentstore", "Category", "CFS"],
  "InstallService": true,
  "StartMode": "Auto",
  "Ports": { "Content": "9100", "Category": "9020", "...": "..." }
}
```

- **LicenseKeyPath**: Full path to the source license key file. During install
  (and Configure) the file is copied into the extracted LicenseServer folder
  (preferring any `LicenseServer_*_WINDOWS_*` subfolder) and renamed to
  `licensekey.dat`.
- Component `.cfg` files receive `LicenseServerHost` (from `LicenseHost`) but
  **do not** receive a `LicenseServerPort` line.
- **`idol.common.cfg` is updated only under LicenseServer** to:
  `LicenseServerHost=licenseserver` and `Clients=*.*.*.*`.
- **Service start order (Install only)**: services are registered but not started
  during the per-component install loop. After all components finish:
  1. Start **LicenseServer** (requires `licensekey.dat` already copied)
  2. Only if LicenseServer is Running, start all other KD-* services

Any field can also be overridden per-run with an environment variable named
`KD_<FIELDNAME>` in upper case, e.g.:

```powershell
$env:KD_LICENSEHOST = "license.corp.local"
$env:KD_LICENSEPORT = "27000"
$env:KD_LICENSEKEYPATH = "D:\\keys\\mykey.dat"
```

Precedence (lowest → highest): config file → environment variable → interactive prompt (if not `-NonInteractive`).

## 4. Dry run first

Always validate before making changes:

```powershell
cd C:\Tools\KD-Installer
.\Install-KD.ps1 -Mode Install -DryRun
```

This runs all pre-flight validation and prints exactly what would be
extracted, configured, and installed as services — without touching disk
or the service control manager. Dry-run still computes ZIP SHA-256 hashes
(and compares to `sha256.txt` if present) but does not prompt or extract.

## 4.1 ZIP integrity (SHA-256) before extraction

On Install / Upgrade / Repair / Extract-Only the toolkit:

1. Hashes every `*.zip` under `ZipPath`.
2. Prints a colored report and a purple PowerShell tip to generate `sha256.txt`.
3. If `ZipPath\sha256.txt` is present, compares digests and lists matches and differences.
4. Interactive mode requires `yes` / `y` before any extraction.
5. Non-interactive mode aborts only when a ZIP's digest **differs** from `sha256.txt`.

Place optional `sha256.txt` beside the packages (`HASH filename` per line, UTF-8).
Generate it on the download machine:

```powershell
# Run inside ZipPath
Get-ChildItem -File |
    Where-Object { $_.Name -ne "sha256.txt" } |
    Get-FileHash -Algorithm SHA256 |
    ForEach-Object { "$($_.Hash) $($_.Path | Split-Path -Leaf)" } |
    Out-File -Encoding UTF8 sha256.txt
```

## 5. Install

```powershell
.\Install-KD.ps1 -Mode Install -NonInteractive
```

Or interactively (prompts for base path, ZIP folder, license host/port, hostname):

```powershell
.\Install-KD.ps1 -Mode Install
```

Progress and results for each component/stage print to the console and to
a timestamped log file under `<BasePath>\logs\`.

## 6. If something fails partway through

The installer records per-component progress in
`<BasePath>\kd-install-state.json`. Fix the underlying issue (e.g. supply
the correct Windows ZIP, free up a port) and re-run with `-Resume`:

```powershell
.\Install-KD.ps1 -Mode Install -Resume -NonInteractive
```

Already-completed stages (extract/configure/service-install) for each
component are skipped; only incomplete or failed stages are retried.

## 7. Upgrade / Repair / Uninstall

```powershell
# Upgrade: re-run install; existing services are detected and left alone
.\Install-KD.ps1 -Mode Upgrade -NonInteractive

# Repair: uninstall then reinstall a component set
.\Install-KD.ps1 -Mode Repair -NonInteractive

# Uninstall: stop/remove services and delete component folders
.\Install-KD.ps1 -Mode Uninstall -NonInteractive
```

## 8. Post-Install Configuration (New in v0.3.0)

You can re-apply configuration (change license server, hostname, or ports) without reinstalling:

```powershell
# Using the convenient wrapper (recommended)
.\Configure-KD.ps1 -NonInteractive

# Or directly
.\Install-KD.ps1 -Mode Configure -NonInteractive
```

### Generate configuration with Lua (ZeroBrane Studio friendly)

If you have **ZeroBrane Studio** or Lua installed, you can generate `config/my-config.json` interactively:

```powershell
# Run the Lua generator (interactive)
lua tools\generate-kd-config.lua

# Or non-interactively with parameters
lua tools\generate-kd-config.lua --non-interactive --basepath "C:\KD\26.2" --licensehost "license.corp.local"
```

Then apply it:

```powershell
.\Configure-KD.ps1 -ConfigPath config\my-config.json -NonInteractive
```

**Easiest way:** Double-click `tools\run-generate-kd-config.bat`

The Lua generator works great inside **ZeroBrane Studio** — open `generate-kd-config.lua` and press **F6**.

It also supports environment variables (`KD_BASEPATH`, `KD_LICENSEHOST`, etc.).

## 8. Post-install

```powershell
Get-Service KD-* | Start-Service
Get-Service KD-*
```

Admin UI examples (only shown for components listed in `Components`; ports come from `Ports` in your config JSON):

- Content: `http://<ThisHost>:<Ports.Content>/action=admin`
- Community: `http://<ThisHost>:<Ports.Community>/action=admin`
- NiFi: `https://<ThisHost>:<NiFi.WebHttpsPort or Ports.NiFi>/nifi`

## 9. Knowledge Discovery Workflow (from Official Documentation)

According to the official **Knowledge Discovery 26.2 Expert** guide, a typical deployment follows this workflow:

1. **Look at Your Content** — Understand what data you have and how you want to use it.
2. **Retrieve Content** — Use Connectors to extract data from repositories.
3. **Enrich Your Content** — Improve data quality (categorization, entity extraction, etc.).
4. **Configure Knowledge Discovery** — Set up components, ports, index paths, and security.
5. **Consider Security** — Plan authentication and access control.
6. **Index Your Content** — Ingest data into the **Content** component.
7. **Use Your Content** — Query, analyze, and build applications on top of the index.

> **Tip**: By default indexes live under the relative `./index` folders that ship with the OpenText templates (recommended). A custom `IndexPath` is optional and only applied when you explicitly set it in the JSON config or dashboard.

## Troubleshooting

### Extracted files are corrupted / nested folders

As of v0.3.10 the installer no longer extracts with PowerShell/.NET.
It calls **Unzip-One.bat**, which uses the native Windows `tar.exe` and
automatically strips the first-level folder that OpenText packages contain.
The result is identical to a manual Explorer unzip.

You can also run the bulk helper yourself:

```cmd
Unzip-All.bat "C:\KD-Setup\zip-folder" "C:\KnowledgeDiscovery\26.2"
```

If extraction still fails, add an AV exclusion for the BasePath and for `tar.exe`.

### "Install command ran but service not found afterward"

This means the executable ran but didn't register a Windows service — almost
always because the CLI switches in `ServiceInstallArgsTemplate` don't match
what that specific binary expects (these vary by OpenText product/version
and are **not guaranteed correct out of the box** in this POC).

To fix:
1. Find the exact executable the installer tried to run — it's printed in
   the failure detail as `Command: <path> <args>`.
2. Run it manually with a help flag to see its real switches, e.g.:
   ```powershell
   & "C:\KnowledgeDiscovery\26.2\LicenseServer\...\licenseserver.exe" -help
   & "C:\KnowledgeDiscovery\26.2\LicenseServer\...\licenseserver.exe" -?
   ```
   Also check the component's own install guide/README if one shipped in the zip.
3. Update `ServiceInstallArgsTemplate` (and `ServiceUninstallArgsTemplate`)
   in your config file to match. Placeholders `{ServiceName}`,
   `{DisplayName}`, and `{StartMode}` are substituted automatically, e.g.:
   ```json
   "ServiceInstallArgsTemplate": ["-svcinstall", "-name", "{ServiceName}", "-mode", "{StartMode}"]
   ```
4. Re-run with `-Resume` — extraction and configuration stages already
   completed will be skipped; only the service-install stage retries.

The failure detail also includes the process's exit code, stdout, and
stderr where available, which usually point at the actual problem (missing
license, invalid switch, wrong working directory, etc.).

### "No suitable Windows ZIP found (Linux-only packages are blocked)"

As of this version the message distinguishes two cases:
- **No file matching `*<Component>*.zip` found** — check `ZipPath` and that
  the filename actually contains the component name (e.g. `Community` vs
  `Agentstore` — OpenText package names sometimes differ slightly from the
  component IDs in `Components`; adjust the config list to match your
  actual package filenames).
- **Found packages but all are Linux packages** — you need the Windows x64
  build of that component.



## Optional Apache NiFi 2.x component

The installer supports **Apache NiFi 2.x** as an optional component.

1. Download `nifi-2.10.0-bin.zip` from https://nifi.apache.org/download/ and place it in your `ZipPath`
   (or let the installer download it when prompted).
2. Add `"NiFi"` to the `Components` array in your config.
3. Configure the `NiFi` section (heap defaults to the Heavy profile 8g/16g, sensitive props key, port 8443).
4. If the dashboard marks NiFi as **Missing**, you can still export config for other components, then install NiFi from the CLI with **07) Install Java Services (NiFi|Find)** (`.\Install-KD.ps1 -Mode InstallJavaServices`). That mode stops/removes any existing KD-NiFi/KD-Find services, downloads the NiFi ZIP if needed, ensures Java 21 + NSSM, force re-extracts (overwrite), and registers/starts **`KD-NiFi`** and **`KD-Find`**.

**Extract before NAR copy:** the installer extracts `nifi-*-bin.zip` until
`BasePath\NiFi\bin\nifi.cmd` exists, **then** copies staged `.nar` connectors
into `NiFi\extensions`. A partial `NiFi\extensions`-only folder does not skip
binary extraction.

**Service (NSSM):** The installer registers Windows service **`KD-NiFi`** with the
toolkit helper **`nifi/Setup-NiFi-Service.ps1`** (NSSM wrapping `nifi.cmd run`). Any previous registration of `KD-NiFi` is replaced; legacy names `KD-Apache-NiFi` and `ApacheNifiService` are removed if present so only one NiFi service remains.

```powershell
Get-Service KD-NiFi
Start-Service KD-NiFi
# logs: BasePath\NiFi\logs\nssm-stdout.log / nssm-stderr.log / nifi-app.log
```

Manual fallback (same service name): `nifi\Setup-NiFi-Service.ps1 -NifiHome "<BasePath>\NiFi" -NonInteractive`

Full details, first-login credentials, and heap guidance are in:

**[docs/INSTALL-NIFI.md](docs/INSTALL-NIFI.md)**


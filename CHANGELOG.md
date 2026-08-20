## v0.6r70m15 — Single field ExternalIIPSAN (drop ExternalIpAddress) (2026-08-20)

### Config cleanup
- Removed duplicate `NiFi.ExternalIpAddress` from `default-config.json` and `my-config.json`.
- **Canonical field:** `NiFi.ExternalIIPSAN` (UI, SSL EXTRA_IP_SANS, host-override, nifi.web.proxy.host).

### Compatibility
- On dashboard load / installer read: if `ExternalIIPSAN` is empty and legacy `ExternalIpAddress` is set, the value is migrated into `ExternalIIPSAN`.
- On Save Changes: legacy keys are stripped so they are not written back to `my-config.json`.

### Behaviour unchanged
- Empty ExternalIIPSAN → EXTRA_IP_SANS = `127.0.0.1`
- Non-empty → EXTRA_IP_SANS = that value

## v0.6r70m14 — Fix Save Changes flipping back to Unsaved (2026-08-20)

### Bug
After **Save Changes**, the status returned to **Unsaved changes** because the
post-save NiFi **Copy source → target** call ran `setDirty(true)`.

### Fix
- `copyNarsSourceToTarget({ markDirty, quiet })` — `markDirty` defaults to `true`
  for the manual button; **Save Changes** passes `markDirty: false`.
- Save now stages NARs **before** writing `my-config.json`, so
  `NiFi.NarExtensions` is refreshed into the config that is saved, then
  `setDirty(false)` remains in effect.

## v0.6r70m13 — Verify & harden NiFi extension (NAR) loading (2026-08-20)

### Verified loading chain
- `nifi.properties`: `nifi.nar.library.autoload.directory=./extensions` (NiFi loads `*.nar` from `<NiFi>/extensions` at startup).
- Staging source: `<SetupPath>\nifi\nifi-connectors\*.nar`
- Runtime target: `<BasePath>\NiFi\extensions\*.nar`

### Gaps fixed
- **Install NiFi service** (menu 07 / `invoke_kd_install_nifi_service`): NAR copy was commented out; now always restores connectors into `extensions` before service registration.
- **Configure** (`invoke_kd_configure` for NiFi): now also copies staged NARs into `extensions` so a Configure-only run still prepares extensions for the next start.

### Already correct (confirmed)
- Full Install: NAR copy runs after NiFi extract and **before** service start / Flow Controller wait.
- Dashboard: **Copy source → target** and **Save Changes** both call `/api/copy-nars`.
- Copy refuses to create `extensions` until a real NiFi tree exists (`bin/nifi.cmd`).

## v0.6r70m12 — NiFi Flow Controller wait 5 min + Save copies NARs (2026-08-20)

### NiFi start readiness timeout
- Waiting for log marker `"Flow Controller started successfully."` extended from **180s → 300s (5 minutes)**.
- Default in `start_kd_nifi_and_wait_for_flow` and all Install / ManageServices call sites updated.
- Log message now reports `timeout 300s / 5 min`.

### Dashboard Save Changes → also Copy source → target
- When **Save Changes** succeeds on the UI dashboard, it also runs the same logic as the NiFi Connectors **Copy source → target** button (`/api/copy-nars`).
- Stages `*.nar` from `SetupPath\nifi\nifi-connectors` into `BasePath\NiFi\extensions`.
- Config save is not rolled back if the NAR copy fails; a separate toast reports the copy error.

## v0.6r70m11 — NiFi keystore path = ssl/intermediate/nifi (2026-08-20)

### Authoritative keystore / truststore location
NiFi PKCS12 files are loaded exclusively from:
```
<SETUP_FOLDER>\ssl\intermediate\nifi\keystore.p12
<SETUP_FOLDER>\ssl\intermediate\nifi\truststore.p12
```
Example:
```
C:\KD-Setup\idol-windows-setup-main\ssl\intermediate\nifi\keystore.p12
C:\KD-Setup\idol-windows-setup-main\ssl\intermediate\nifi\truststore.p12
```
- SSL root discovery prefers any tree that already contains `intermediate/nifi/keystore.p12`.
- When SetupPath is `...\idol-windows-setup`, the sibling `...\idol-windows-setup-main\ssl` is also searched.
- Files are copied into `<NiFi>\conf\` and `nifi.security.*` passwords still come from `ssl\.idol-ssl-passwords.env`.

## v0.6r70m10 — SSL paths use SetupPath/ssl layout (2026-08-20)

### SSL password source
- Primary on-disk password file is now:
  `<SETUP_FOLDER>/ssl/.idol-ssl-passwords.env`
  (`IDOL_CERT_KEYSTORE_PASS` / `IDOL_CERT_TRUSTSTORE_PASS`)
- SetupPath from `my-config.json` is preferred when resolving the SSL tree.
- Still accepts env vars and fallback locations under `env/` and toolkit `ssl/`.

### SSL certificate / keystore paths
Canonical layout under SetupPath (matches `tools/generate_ssl.py` output):
```
<SETUP_FOLDER>/ssl/
  .idol-ssl-passwords.env
  intermediate/
    nifi/
      keystore.p12
      truststore.p12
    certs/
      idol-nifi.cert.pem
      idol-nifi.key.pem
      idol-nifi-fullchain.cert.pem
      ca-chain.cert.pem
      ca.cert.pem
      intermediate.cert.pem
      ...
```
- NiFi configure copies `intermediate/nifi/keystore.p12` + `truststore.p12` into `<NiFi>/conf/`.
- Passwords from `ssl/.idol-ssl-passwords.env` are written into:
  `nifi.security.keystorePasswd`, `nifi.security.keyPasswd`, `nifi.security.truststorePasswd`.
- Known install roots (`C:\KD-Setup\idol-windows-setup-main\ssl`, etc.) are included in the search path.

## v0.6r70m9 — NiFi security keystore/truststore from generated SSL (2026-08-20)

### NiFi SSL material deployment
- During NiFi **Configure**, the installer now deploys the PKCS12 files produced by `tools/generate_ssl.py`:
  - Source: `ssl/intermediate/nifi/keystore.p12` and `truststore.p12`
  - Destination: `<BasePath>/NiFi/conf/keystore.p12` and `truststore.p12`
- Passwords are loaded from (first match wins):
  1. `IDOL_CERT_KEYSTORE_PASS` / `IDOL_CERT_TRUSTSTORE_PASS` environment variables
  2. `ssl/.idol-ssl-passwords.env`
  3. `env/.idol-ssl-passwords.env` / `env/ssl-passwords.txt`

### nifi.properties security section
The following keys are updated from the generated SSL material (no duplicate keys):
```
nifi.security.keystore=./conf/keystore.p12
nifi.security.keystoreType=PKCS12
nifi.security.keystorePasswd=<KeyStore password from generate_ssl.py>
nifi.security.keyPasswd=<same as keystorePasswd>
nifi.security.truststore=./conf/truststore.p12
nifi.security.truststoreType=PKCS12
nifi.security.truststorePasswd=<TrustStore password from generate_ssl.py>
```
- Applied in both the main Install/Upgrade path and `invoke_kd_configure`.
- If PKCS12 files or passwords are missing, a warning is logged and other NiFi settings are still applied.

## v0.6r70m8 — ExternalIIPSAN host-override + yellow user prompts (2026-08-20)

### ExternalIIPSAN → Browser URL host-override
- When `NiFi.ExternalIIPSAN` is **non-empty**, the dashboard Browser-URL **host-override** field is set to that value and applied (skip network tests so URLs update immediately).
- When `ExternalIIPSAN` is **empty**, existing host-override behaviour is left unchanged (user-controlled field stays as-is).
- Sync runs after config load and whenever the ExternalIIPSAN settings field changes.
- Legacy `ExternalIpAddress` is still accepted as a fallback when ExternalIIPSAN is absent.

### User prompts and questions → yellow
- All user-facing questions, confirmations, and input prompts now use **yellow** text for visibility.
- Python: `_prompt` / `_prompt_yes_no` in `install_kd.py`, plus direct prompts in `kd/installer.py`, `kd/prerequisites.py`, `kd/validation.py`, `manage_kd_services.py`, and `tools/generate_ssl.py`.
- PowerShell: `Read-Host` prompts in `Install-KD.ps1`, `Generate-SSL.ps1`, `Clean-Unnecessary.ps1`, `Cleanup-KD.ps1`, `Initialize-Environment.ps1`, `nifi/Setup-NiFi-Service.ps1`, and `tools/Push-Version.ps1` now print the question in yellow via `Write-Host -ForegroundColor Yellow` before reading input.
- Informational, warning, error, and success messages are unchanged.

## v0.6r70m7 — NiFi WebHttpsHost, ExternalIIPSAN & SSL EXTRA_IP_SANS (2026-08-20)

### Dashboard UI (NiFi card)
- Added **WebHttpsHost** field immediately after **WebHttpsPort**.
- Added **ExternalIIPSAN** field after WebHttpsHost (IP address(es) for certificate SANs and `nifi.web.proxy.host`).
- Both fields are saved to / loaded from `config/my-config.json` under `NiFi.WebHttpsHost` and `NiFi.ExternalIIPSAN`.
- Existing configs that only have the legacy `ExternalIpAddress` key still work (UI and installer fall back to it).

### EXTRA_IP_SANS logic
- Rule applied consistently in the installer and SSL generator:
  - If `ExternalIIPSAN` (or legacy `ExternalIpAddress`) is empty → `EXTRA_IP_SANS = 127.0.0.1`
  - Otherwise → `EXTRA_IP_SANS = <value of ExternalIIPSAN>`
- Used when building `nifi.web.proxy.host` and when invoking `tools/generate_ssl.py --extra-ip`.

### SSL certificate generation
- `install_kd.py` now passes `NiFi.ExternalIIPSAN` / `NiFi.WebHttpsHost` from `my-config.json` into `tools/generate_ssl.py`.
- Empty ExternalIIPSAN defaults to `--extra-ip 127.0.0.1`.
- Non-wildcard WebHttpsHost is passed as `--external-hostname`.

### NiFi configuration deployment
- During Configure, the toolkit template `nifi/conf/nifi.properties` is copied to the target `NiFi/conf/nifi.properties`.
- Placeholders are then replaced with real values:
  - `NIFI_WEB_HTTPS_PORT` ← `WebHttpsPort`
  - `NIFI_WEB_HTTPS_HOST` ← `WebHttpsHost`
  - `EXTRA_IP_SANS` ← ExternalIIPSAN rule above
- Updates are performed via key-based property rewrite (no duplicate keys).

### Config files
- `config/default-config.json` and `config/my-config.json` now include `WebHttpsHost` and `ExternalIIPSAN`.
- `tools/replacements.json` default for EXTRA_IP_SANS set to `127.0.0.1` and proxy.host includes it.

## v0.6r68m13 — Apply warning next to Apply button (2026-08-17)

### Dashboard UI
- The **Apply** confirmation warning (`custom-apply-warn`) now appears **inline to the right of** the sticky **Apply to config files** button (same toolbar row), instead of above the replacements table.

## v0.6r68m12 — Sticky Customization actions + clearer Apply (2026-08-17)

### Dashboard UI
- **Reload / Sync Ports & Save / Apply** moved into the sticky main-tab bar, directly under the Setup / Customization buttons.
- The action row is shown only on the Customization tab and stays pinned while scrolling the replacements table.
- After Save, status text reminds you to click **Apply** (Save only writes `replacements.json`; Apply writes the `*.cfg` files).
- Apply status reports file + replacement counts and the setup root path used.

### Apply behaviour
- Section-scoped Apply skips rows where `from == to` (no-op), so CatDRE/Agentstore rows that already match do not rewrite files.
- Apply response includes `details`, `replacementCount`, and `setupRoot` for easier diagnosis.

## v0.6r68m11 — Fix Update-ConfigFiles.ps1 section Match crash (2026-08-17)

### Bug fix — section-scoped replacements never applied
- `tools/Update-ConfigFiles.ps1` crashed on the first section-scoped entry with:
  `Cannot convert argument "options", with value: "…", for "Match" to type "RegexOptions"`.
- Cause: `[regex]::Match($content, $pattern, $afterHeader)` — the static overload has no
  `(input, pattern, startat)` form; the third argument is `RegexOptions`, so an integer
  offset (e.g. 918) failed the cast and aborted the script before `category.cfg` /
  `community.cfg` (DataDre / CatDRE / AgentDre) could be written.
- Fix: use an instance regex and `Match($content, $afterHeader)` for the “next section”
  search. Section-scoped `aciport` updates in `category.cfg` / `community.cfg` now apply.

## v0.6r68m10 — Customization UI: Section column (2026-08-17)

### KD Config Dashboard — Customization table
- The Customization tab now shows a **Section** column for every replacement row.
- When a replacement in `tools/replacements.json` has a `"section"` key, the value is displayed and is editable.
- Clearing the field removes the key on save (global / file-wide replace).
- Placeholder `(global)` indicates no section constraint.
- Hint text updated to mention optional INI section scoping.

### kd-config-server.py — section-aware Apply / Validate
- `/api/validate-replacements` and `/api/apply-replacements` now honour the optional `"section"` property, matching `tools/Update-ConfigFiles.ps1`.
- When `"section"` is set, the `from` string is searched/replaced only inside the named `[Section]` body; missing section or missing match is skipped quietly.

## v0.6r68m9 — Section-scoped config replacements, menu UX (2026-08-17)

### Update-ConfigFiles.ps1 — section-aware replacements
- Replacements in `tools/replacements.json` may now include an optional `"section": "SectionName"` key.
- When present, the literal `from` → `to` substitution is applied **only inside the named INI `[SectionName]` block**.
- If the section is missing or the `from` string is not found (globally or inside the section), that individual replacement is **skipped** without error; the file is only written when content actually changes.
- Example: `aciport=` under `[DataDre]` vs `[CatDRE]` in `category.cfg` (and `[DataDre]` / `[AgentDre]` in `community.cfg`) can be updated independently.

### replacements.json
- `config/cfg/category/category.cfg`: `aciport` entries now use `"section": "DataDre"` / `"CatDRE"` (DataDre `from` aligned to current `9060` → `9100`).
- `config/cfg/community/community.cfg`: `aciport` entries now use `"section": "DataDre"` / `"AgentDre"`.

### Install-KD.ps1 menu UX
- Main menu options **00)** and **01)** are now shown in **red** (was green) to highlight the required new-deployment path.
- Manage Windows services submenu (mode **09**):
  - **1) Status only** is now the first option and the default (`Enter choice [1]:`).
  - Previous Start / Stop / Restart shifted to 2 / 3 / 4; Delete / Create / Cancel unchanged.

### Docs
- README.md updated for section-scoped replacements, red menu colours for 00/01, and ManageServices default = Status.

## v0.6r68m2 — SHA-256 integrity gate, BrowserUrls, NiFi extract-before-NAR (2026-08-17)

### Pre-extraction ZIP SHA-256 verification
- Before any component package is extracted (Install / Upgrade / Repair / Extract-Only),
  the installer:
  1. Computes SHA-256 for every `*.zip` under `ZipPath` (colored report: blue headers, cyan hashes).
  2. Shows a purple PowerShell tip to generate `sha256.txt` on the download machine.
  3. Loads `sha256.txt` (or `SHA256.txt` / `sha256sums.txt`) from `ZipPath` and compares.
  4. Lists **MATCHED**, **DIFFERENT HASHES**, missing-in-file, and extra-in-file entries.
  5. Interactive: requires `yes` / `y` to continue; mismatches are listed first.
  6. Non-interactive: aborts automatically on hard hash mismatches; continues if all match
     or if `sha256.txt` is absent (warning only).
- Helpers in `kd/validation.py`: `compute_file_sha256`, `generate_zip_sha_report`,
  `load_sha256_txt`, `compare_zip_hashes`, `confirm_zip_hashes`.
- Wired in `install_kd.py` after environment validation, before installer dispatch.

### BrowserUrls completeness
- Dashboard `DEFAULT_PATHS` / `INITIAL.BrowserUrls` now emit Admin UI + Status + Service Log
  for KD components (LicenseServer Status uses `/a=getlicenseinfo`).
- Fixed invalid duplicate `"Status"` keys under Content in default/my-config JSON.

### NiFi: extract binary ZIP before NAR copy
- NAR copy into `BasePath\NiFi\extensions` no longer runs against a partial tree.
- `_nifi_is_extracted()` requires `bin\nifi.cmd` (or equivalent); a lone `extensions`
  folder does not count as extracted and no longer skips `nifi-*-bin.zip`.
- Order enforced: extract `nifi-*-bin.zip` → then copy staged `.nar` files to `extensions`.


## v0.6r68m1 — Disable custom IndexPath by default (2026-08-16)

### Highlights
- **Custom `IndexPath` is now optional and off by default.**  
  When `IndexPath` is absent from the JSON config the toolkit no longer invents
  `<BasePath>\Indexes` or writes a non-standard `[Index] IndexPath=...` key into
  component `.cfg` files. The OpenText relative layout (`MainPath=./index/main`,
  `TemplateDirectory=./templates`, etc.) is preserved — this is the recommended
  default and avoids the Admin UI “Missing data file” (`AXEADMIN-2147483364`)
  error that appeared when the injected path interfered with `admin.dat` /
  template resolution.
- `_ensure_index_path` only creates a directory and persists the value when the
  user has explicitly set a non-empty `IndexPath`.
- `apply_versioned_base_path` no longer forces an IndexPath when none was present.
- Config examples (`default-config.json`, `my-config.json`), README and INSTALL.md
  updated to reflect the new default.

### Still supported
- Advanced users can still set `"IndexPath": "D:\\MyIndexes"` in the JSON or the
  config dashboard; the key is then written into the relevant `.cfg` files and
  the directory is created.

# CHANGELOG

## v6 - 2026-08-11

- Fixed NiFi/NSSM service removal when KD-NiFi is stopped, stale, partially deleted, or inaccessible to NSSM.
- Added SCM (`sc.exe delete`) fallback with verification/waiting before service reinstall.
- Suppressed misleading native NSSM `Can't open service!` errors during expected cleanup/recovery.
- Preserved v5 NiFi automatic download, faster downloader fallback, resume support, manual-copy option, and UI warning updates.

## v0.6r64m2 — Push-Version: auto .gitignore + large-push hardening

### Highlights
- **`tools/Push-Version.ps1` now auto-creates a `.gitignore`** at the repo
  root on first run (if one doesn't already exist), excluding component
  `*.zip`/`*.msi`/`*.exe` packages, `logs/`, `ssl/`, `env/`, extracted
  `nifi/nifi-connectors/*.nar` files, and Python cache. This is aimed
  directly at the most common cause of push failures on this repo: a large
  binary (e.g. the NiFi or NiFiIngest ZIP) getting swept into `git add -A`
  and blowing up the push to hundreds of MB.
- **Pre-commit large-file warning**: before committing, the script checks
  every staged file and warns (with size + path, and a confirm prompt) on
  anything ≥ 20 MB, so an oversized file already tracked from before
  `.gitignore` existed gets caught locally instead of failing at push time.
- **Push hardening**: before `git push`, the script now raises
  `http.postBuffer` and disables the low-speed abort for the repo, and
  retries the push once automatically on failure — both targeted at
  `HTTP 408` / `unexpected disconnect while reading sideband packet` /
  `the remote end hung up unexpectedly`, which typically come from a large
  push timing out over HTTPS.
- The `git push failed` error message now explains the large-file cause
  specifically (with the command to find the largest tracked objects) and
  an SSH-remote fallback, instead of only listing generic auth/permission
  causes.
- Docs: `docs/PUSH-VERSION.md` §7 (`.gitignore`) rewritten to describe the
  now-automatic behavior, and §9 (Troubleshooting) gained a dedicated row
  for the HTTP 408 / sideband-disconnect failure with the full remediation
  path (including history rewrite via `git filter-repo`/BFG if the large
  file is further back than the latest commit).

## v0.6r64m1 — Automatic NAR extraction + NiFi Connectors UI

### Highlights
- **NiFi Connectors: automatic NAR extraction.** The config dashboard's
  **Extract from ZIP** button has been removed. As soon as a valid
  `NiFiIngest_*.zip` is detected under `ZipPath` (via the existing ZIP
  packages check), its `*.nar` files are now extracted into
  `SetupPath\nifi\nifi-connectors` automatically — no manual step required.
  Re-extraction is skipped once a given ZIP has already been processed, so
  it only fires again if `ZipPath` or the matched NiFiIngest filename changes.
- **NiFi Connectors: Source/Target lists side by side.** The Source
  (`SetupPath\nifi\nifi-connectors`) and Target (`BasePath\NiFi\extensions`)
  NAR lists are now shown in a two-column layout instead of stacked, making
  it easier to compare what's been extracted vs. what's staged for NiFi.
  Falls back to a single column on narrow windows.
- **"+" add-connector default folder changed.** The manual "add connector"
  file-picker now opens in the NiFi Connectors **Source** folder by default
  (instead of the Target/extensions folder), since that's normally where
  extracted connectors already live. Falls back to Target if Source doesn't
  exist yet. The copy destination is unchanged (always
  `BasePath\NiFi\extensions`).
- Docs: added a dedicated **NiFi Connectors (NAR files)** section to
  `docs/INSTALL-NIFI.md` (new section 4) and to `README.md` (§7 Apache
  NiFi), and updated `config/ui-config/README-KD-Config-Dashboard.txt` to
  describe the automatic behavior.

## v0.6r63m4 — Menu 11 UpdateConfigFiles (PowerShell, no Lua)

### Highlights
- New installer menu option **11) Update config files** (`-Mode UpdateConfigFiles`).
- `tools/Update-ConfigFiles.ps1` applies `tools/replacements.json` (API key + ports)
  and syncs target Ports into `config/my-config.json` — **no Lua required**.
- Fixes "lua is not recognized" on Windows; Lua helper remains optional.

## v0.6r63m3 — LLM key replacement helper + docs

### Highlights
- Added `tools/replace.lua` + `tools/replacements.json` for bulk text replacement
  (`YOUR_AI_LLM_PROVIDER_KEY` in AnswerServer RAG scripts, and optional port overrides
  in `config/cfg/**/*.cfg`).
- Documented the helper in `README.md` (Step 3) and in
  `config/ui-config/README-KD-Config-Dashboard.txt`.

## v0.6r63m2 — AnswerServer RAG folder deploy

### Highlights
- **AnswerServer post-extract deploy**: `_deploy_cfg_templates` now copies both
  top-level files *and* subdirectories from `config/cfg/<component>/`.
  For AnswerServer this overwrites `answerserver.cfg` and deploys the entire
  `rag/` tree (Lua/Python scripts, prompts, tokenizer cache, etc.) into the
  extracted AnswerServer folder so relative paths such as `./rag/grok.lua`
  and `./rag/prompt_template.txt` resolve correctly after install.

## v0.6r56m7 — ETA (estimated time remaining) during setup

### Highlights
- **Progress-based ETA** on the live Install clock: uses finished components vs
  total work units (configure phase + optional service-start phase).
- Tick format: `elapsed M:SS | 3/14 (21%) units | ~ETA 12:30 left | now: Content`
- ETA recalculates as each component finishes (linear estimate from actual pace).

## v0.6r56m6 — Live elapsed-time clock during setup

### Highlights
- **ElapsedClock**: background timer ticks every 15s for the whole Install
  (`⏱ Install still running… elapsed M:SS`) and every 20s per ZIP extract.
- Final line reports total duration when the phase finishes or fails.

## v0.6r56m5 — Large-ZIP extract no longer stuck; Find start hardened

### Highlights
- **Extraction**: size-based timeout (10 min–2 h), live progress from `Unzip-One.bat`,
  bulk Mark-of-the-Web unblock (one PowerShell call, not per-file), faster content check.
  `Unzip-One.bat` prints progress and uses robocopy for large tree moves.
- **Find**: start timeout raised to **180s**; clearer logs; respects `Find.InstallService`;
  guidance if KD-Find does not reach Running on first install.

## v0.6r56m4 — AnswerBankAgentStore, ConversationAgentStore, AnswerServer

### Highlights
- **AnswerBankAgentStore** / **ConversationAgentStore** (Agentstore-like): assembled from
  Content; templates under `config/cfg/answerbankagentstore/` (ports **9450/9451/9452**) and
  `config/cfg/conversationagentstore/` (**9550/9551/9552**); services `KD-AnswerBankAgentStore`,
  `KD-ConversationAgentStore`.
- **AnswerServer** (native): templates under `config/cfg/answerserver/`, ACI port **12000**,
  service `KD-AnswerServer` (needs `*AnswerServer*.zip`).
- Extended `_AGENTSTORE_LIKE`, discovery Content-reuse, IndexPath, browser URLs, cleanup.

## v0.6r56m3 — StatsServer support

### Highlights
- **StatsServer** (native): uses existing toolkit templates under `config/cfg/statsserver/`,
  default ACI port **19870** (EventPort 19871, ServicePort 19872 in template).
- Registered in `_CFG_TEMPLATE_COMPONENTS`, Components/Ports JSON, browser URLs, and cleanup
  (`KD-StatsServer`). Start timeout 60s like other native engines.

## v0.6r56m2-qms — QMS + QMSAgentStore support

### Highlights
- **QMS** (native): toolkit templates under `config/cfg/qms/`, default ACI port **16000**.
- **QMSAgentStore** (Agentstore-like): assembled from Content package, templates under
  `config/cfg/qmsagentstore/` (ports **9150 / 9151 / 9152**), service `KD-QMSAgentStore`.
- Generalized `_AGENTSTORE_LIKE` set in `kd/installer.py` so additional agentstores
  can be added with config + templates only.
- `default-config.json` / `my-config.json`: Components + Ports for QMS / QMSAgentStore.
- `cleanup.json`: `KD-QMS`, `KD-QMSAgentStore`.
- Browser URL summary includes QMS and QMSAgentStore admin pages.
- Discovery: QMSAgentStore reuses Content ZIP; executable scoring prefers `agentstore.exe`
  for Agentstore-like components and QMS binaries.

## v0.6.20-python — Yellow console color for === section / Phase headers

### Highlights
- **`kd/logging.py`**: when the `rich` package is available, any `log.info` message
  that starts with `===` (e.g. `=== Phase 1: ...`, `=== Processing component: ...`)
  is rendered in **bold yellow** on the console. File log remains plain text.

## v0.6.19-python — SSL regenerate prompt (Install/Upgrade/Repair); confirm before SSL delete (Uninstall)

### Highlights
- **Install / Upgrade / Repair (interactive)**: when a complete SSL certificate set
  already exists under the toolkit `ssl/` folder, the installer now asks:

      SSL certificates already exist. Regenerate them? (Y/N) [N]:

  Answering **Y** runs `tools/generate_ssl.py --auto --kd-services --force`.
  Default is **N** (keep existing certs). When certs are missing/incomplete the
  previous generate prompt (default Y) still applies.

- **Uninstall (interactive)**: Phase 4 no longer deletes the toolkit SSL folder
  silently. The user is asked, then must type **YES** to confirm:

      Delete toolkit SSL folder(s)?
        <path>
      This removes generated certificates, keys and passwords. (Y/N) [N]:
      Confirm deletion of SSL folder(s)? Type YES to proceed:

  Non-interactive uninstall leaves the SSL folder in place (no silent delete).

- Menu text and `-Help` updated to describe the new SSL prompts.


## v0.6.18-python — Non-NiFi services use native -install (NiFi stays NSSM)

### Highlights
- **Standard KD components** (LicenseServer, Content, Community, Agentstore,
  Category, …) register Windows services via the **component binary’s own
  `-install`** path again — matching `idol-windows-setup-main` / OpenText
  native service registration:

      content.exe -install -servicename KD-Content -displayname "OpenText KD Content"

  (`ServiceInstallArgsTemplate` from config still applies.)

- **NiFi is unchanged**: still registered with **NSSM**
  (`cmd.exe` + `AppParameters=/c nifi.cmd run`, AppDirectory = `NiFi\bin`).

- **Menu option 9 → Create all** and `manage_kd_services.py create` use the
  same split: native `-install` for non-NiFi, NSSM for NiFi.

- Applied from analysis of non-NiFi service handling in
  `idol-windows-setup-main` into this toolkit (v0.6.17 had switched *all*
  components to NSSM).

## v0.6.17-python — Manage services: Delete all + Create all (NSSM only)

### Highlights
- **Menu option 9 – Manage Windows services** now includes:

  | # | Action |
  |---|--------|
  | 1 | Start all |
  | 2 | Stop all |
  | 3 | Restart all |
  | 4 | Status only |
  | **5** | **Delete all services** (stop + remove registration; install folders kept) |
  | **6** | **Create all services** via **NSSM only** (from config `Components`) |

- **All KD Windows services are registered with NSSM** (not the component
  binary `-install` path):
  - Standard components (Content, Community, Agentstore, Category,
    LicenseServer, …): `nssm install KD-<Name> <component.exe>` with
    `AppDirectory` = executable parent, rotated stdout/stderr under `logs\`.
  - **NiFi** unchanged: NSSM → `cmd.exe /c nifi.cmd run`.
  - Applies to **Install**, **Repair**, and **create** (`manage_kd_services.py create`
    / menu 9 option 6).

- CLI:
  ```
  python manage_kd_services.py delete --all-deployed --force
  python manage_kd_services.py create --non-interactive
  .\Manage-KDServices.ps1 create -NonInteractive
  ```



## v0.6.16-python — Uninstall deletes all Components folders; Hostname/MAC only on Install

### Highlights
- **Uninstall Phase 3** now reliably targets **every** name in config
  `Components` (e.g. LicenseServer, Content, Community, Agentstore, Category, NiFi):
  - Case-insensitive folder resolution under `BasePath`
  - Stronger `_force_rmtree` (more retries, `attrib` clear, walk-delete, Windows
    `rmdir /s /q` fallback)
  - Extra service stop before each folder delete; for NiFi, best-effort
    `taskkill /F /IM java.exe` to release locks
  - Phase **3b** retry pass for any folder still present
  - Clears `IndexPath` when it lives under `BasePath`
  - Final log lists any component folders that could not be removed

- **Hostname / MAC prompts** (`Hostname to use for this install`,
  `MAC address to use for this install`) are shown **only for menu option 1 –
  Install** (including Extract-only which is still Mode=Install).
  Modes 2–9 (Configure, Upgrade, Repair, Uninstall, Install NiFi Service,
  Set NiFi credentials, Manage services) no longer prompt for network identity;
  they use config / auto-detected values silently.



## v0.6.15-python — Default ports (Content 9100 / Agentstore 9050); toolkit cfg overwrite; Community aes.key

### Highlights
- **Default ports** aligned with the toolkit `config/cfg` templates and OpenText layout:

  | Component | Port |
  |-----------|------|
  | Content | **9100** |
  | Category | 9020 |
  | Community | 9030 |
  | Agentstore | **9050** |
  | LicenseServer | 20000 |
  | NiFi HTTPS UI | 8443 |

  Updated in `config/default-config.json`, `config/my-config.json`, README, and
  Admin UI next-step URLs (`install_kd.py`).

- **After ZIP extraction**, the installer **overwrites** the extracted component
  root with toolkit templates from `config/cfg/<component>/`:

  - `config/cfg/content/` → `BasePath\Content\`
  - `config/cfg/community/` → `BasePath\Community\`
  - `config/cfg/agentstore/` → `BasePath\Agentstore\`
  - `config/cfg/category/` → `BasePath\Category\`

  Files typically include `<component>.cfg` and `idol.common.cfg`. Logged as
  `Overwrote cfg from toolkit: …`.

- **Community AES key**: after cfg overwrite, if `aes.key` is missing under
  `BasePath\Community`, the installer runs (working directory = Community root):

      autpassword -x -tAES -oKeyFile=aes.key

  then sets `[Security] SecurityInfoKeys` in `community.cfg` to the **absolute**
  path of the new key (e.g. `C:\KnowledgeDiscovery\26.2\Community\aes.key`).
  If `autpassword.exe` is not found, a WARNING is logged and Configure can be
  re-run after creating the key manually.

- **Runtime patches** after template deploy still apply `LicenseHost`,
  `[Server] Port` (from the `Ports` map), and `IndexPath` from the JSON config.
  Port writes target **`[Server]`** (matching OpenText templates), not `[Service]`.

- Agentstore assembly from Content now prefers toolkit `config/cfg/agentstore/*`
  over copying `content.cfg`.



## v0.6.14-python — NiFi service uses /c nifi.cmd run; overwrite toolkit nifi.cmd

### Highlights
- **NSSM AppParameters** for **KD-NiFi** are now exactly:

      /c nifi.cmd run

  with **AppDirectory** = `BasePath\NiFi\bin` (same layout as the OpenText
  NSSM service editor: Application = `cmd.exe`, Arguments = `/c nifi.cmd run`).
- **After extracting the NiFi ZIP**, the installer overwrites the extracted
  `NiFi\bin\nifi.cmd` with the toolkit copy (`nifi/nifi.cmd`).
- **`run-nifi.bat` removed** from the install path:
  - `_deploy_run_nifi_bat` replaced by `_deploy_nifi_cmd`
  - `Setup-NiFi-Service.ps1` no longer looks for or prefers `run-nifi.bat`
  - Docs / operator messages updated to `bin\nifi.cmd start|stop|status|run`


## v0.6.13-python — Fix ManageServices menu (AllDeployed splat)

### Highlights
- **Menu option 9 – Manage Windows services** no longer fails with:

      A positional parameter cannot be found that accepts argument '-AllDeployed'

  Root cause: `Install-KD.ps1` built an **array splat**
  `@($svcAction, '-AllDeployed', '-NonInteractive')` and called
  `Manage-KDServices.ps1` with `& $managePs1 @manageArgs`. On Windows
  PowerShell 5.1 those `'-AllDeployed'` / `'-NonInteractive'` strings were
  treated as *positional* argument values (not named switches), so binding
  failed and the error was attributed to `Install-KD.ps1`.

- **Fix:** menu 9 now invokes `manage_kd_services.py` **directly** with the
  already-resolved Python from step [1/4]:

      python manage_kd_services.py <start|stop|restart|status> --all-deployed --non-interactive

  Same backend used by `Manage-KDServices.ps1` and the
  `Start-All-KDServices.bat` / `Stop-All-KDServices.bat` /
  `Status-All-KDServices.bat` wrappers (those were already correct because
  they use `powershell -File ... start -AllDeployed`).

- All four interactive choices work: **Start all**, **Stop all**,
  **Restart all**, **Status only**.

## v0.6.12-python — NiFi credentials via direct Java; Setup-NiFi-Service.ps1 parse fix

### Highlights
- **NiFi single-user credentials** no longer call `nifi.cmd set-single-user-credentials`.
  Apache NiFi's Windows batch has a known bug (NIFI-14133) that builds
  `CREDENTIALS` incorrectly so the Java tool always sees **0 arguments**:

      Unexpected number of arguments [0]
      Usage: SetSingleUserCredentials <username> <password>

  The installer now invokes the Java class **directly** (Apache-documented
  workaround), working directory = NIFI_HOME:

      java -cp "lib/bootstrap/*;lib/*;conf"
           -Dnifi.properties.file.path=conf/nifi.properties
           org.apache.nifi.authentication.single.user.command.SetSingleUserCredentials
           <username> <password>

  Password must be at least 12 characters (NiFi requirement).
- **`nifi/Setup-NiFi-Service.ps1`** rewritten as pure ASCII with UTF-8 BOM so
  Windows PowerShell 5.1 no longer reports `Missing closing '}'` (caused by
  UTF-8 multi-byte characters / encoding mismatch on the prior file).

## v0.6.11-python — Menu: Set NiFi credentials + Manage Windows services

### Highlights
- **Menu option 8 – Set NiFi UI credentials** (`-Mode SetNiFiCredentials`):
  applies `nifi.cmd set-single-user-credentials` on an already-extracted
  `BasePath\NiFi` tree. Interactive runs prompt for username/password
  (defaults from config `NiFi.Username` / `NiFi.Password`). Non-interactive
  uses config or CLI `--nifi-username` / `--nifi-password`.
- **Menu option 9 – Manage Windows services** (`-Mode ManageServices`):
  asks whether to **start**, **stop**, **restart**, or show **status** for
  every deployed `KD-*` service, then calls `Manage-KDServices.ps1 -AllDeployed`.
- Same actions remain available via `Start-All-KDServices.bat` /
  `Stop-All-KDServices.bat` / `Status-All-KDServices.bat`.

## v0.6.10-python — NiFi credentials quoting fix; Setup-NiFi-Service.ps1; start/stop-all scripts

### Highlights
- **Fixed NiFi single-user credentials** (`nifi.cmd set-single-user-credentials`):
  the previous invocation used a subprocess *list* plus `cmd /s /c` with
  embedded quotes. Windows `list2cmdline` re-wrapped the `/c` argument, `/s`
  then stripped the outer quotes and left escaped quotes that never reached
  the Java tool — resulting in:

      Unexpected number of arguments [0]
      Usage: SetSingleUserCredentials <username> <password>

  while the installer still treated exit code 0 as success.
  - Now builds a single CreateProcess command-line string:
    `cmd.exe /V:OFF /d /c nifi.cmd set-single-user-credentials "<user>" "<pass>"`
  - `/V:OFF` disables delayed expansion so passwords containing `!` are safe.
  - Output containing the usage / zero-args message is treated as failure
    even when the process exits 0.
- **`nifi/Setup-NiFi-Service.ps1` rewritten** to match what the installer
  actually invokes:
  - Parameters: `-NifiHome`, `-ServiceName` (default `KD-NiFi`),
    `-NssmPath`, `-NonInteractive`, `-Start`, `-StartMode`
  - Prefers deployed `bin\run-nifi.bat run` under NSSM (`%COMSPEC%`)
  - Removes legacy `KD-Apache-NiFi` / `ApacheNifiService`
  - Sets `AppEnvironmentExtra` + permanent `JAVA_HOME` / `NIFI_HOME` when missing
  - NSSM via PATH / toolkit / winget
- **Start / stop / status all services** (operator-friendly wrappers):
  - `Start-All-KDServices.bat`  → start every deployed `KD-*` service
  - `Stop-All-KDServices.bat`   → stop every deployed `KD-*` service
  - `Status-All-KDServices.bat` → status of every deployed `KD-*` service
  - All call `Manage-KDServices.ps1` with `-AllDeployed` (same as
    `python manage_kd_services.py ... --all-deployed`).

## v0.6.9-python — NiFi Username / Password in config JSON

### Highlights
- **``NiFi.Username`` / ``NiFi.Password``** added to ``config/default-config.json``
  and ``config/my-config.json`` (defaults: ``admin`` /
  ``ChangeMe-AdminPassword123!``).
- During **Install** and **Configure**, when both values are set the installer
  runs ``nifi.cmd set-single-user-credentials`` so the UI login is fixed
  instead of a random first-start password.
- Documented in ``docs/INSTALL-NIFI.md``.

## v0.6.8-python — SSL passwords in ssl/; uninstall deletes toolkit ssl/; Install NiFi Service menu

### Highlights
- **SSL password files are always written into the SSL output folder**
  (``ssl/.idol-ssl-passwords.ps1``, ``.env``, and ``ssl-passwords.txt``), in
  addition to the optional ``env/`` copy for existing tooling.
- **Uninstall (menu option 5)** now also deletes the toolkit SSL folder
  (``kd-win-setup-python\ssl`` and ``C:\KD-Setup\kd-win-setup-python\ssl``
  when present) after removing services and component folders.
- **New mode / menu option 7: Install NiFi Service**
  (``-Mode InstallNiFiService``) registers only the ``KD-NiFi`` Windows
  service via NSSM for an already-extracted ``BasePath\NiFi`` tree, then
  starts it and waits for the Flow Controller marker.

## v0.6.7-python — Start / stop any deployed KD-* Windows service

### Highlights
- **``manage_kd_services.py`` / ``Manage-KDServices.ps1``** can now target
  **any deployed** Windows service named ``KD-*``, not only the Components
  list in the config JSON:
  - New ``--all-deployed`` / ``-AllDeployed`` (``-a``) discovers every
    registered ``KD-*`` service on the machine and starts / stops / status /
    restart / delete / uninstall those.
  - ``--components`` accepts either form: ``Content,NiFi`` **or**
    ``KD-Content,KD-NiFi``.
  - If config ``Components`` is empty and no override is given, discovery
    is used automatically so installed services are still manageable.
- ``get_kd_service_name()`` is idempotent for names that already start with
  ``KD-``; new ``service_name_to_component()`` maps ``KD-Content`` → ``Content``.

## v0.6.6-python — NiFi soft WARNING when Flow Controller marker vs service state; Admin UI ports from config

### Highlights
- **NiFi start classification** refined:
  - If ``Flow Controller started successfully.`` is seen in ``nifi-app.log``
    but the Windows service is **not RUNNING**, the outcome is a soft success
    with **WARNING (orange)** — not ERROR (red). Same when the marker exists
    from an earlier start and the service is currently stopped.
  - Service RUNNING without the marker within the timeout remains WARNING.
  - Only a hard miss (no marker, service not RUNNING) stays ERROR.
  - ``log.step_result`` supports ``warning=True`` → ``[WARN]`` in orange.
- **Post-install Admin UI next-step URLs** now use the port from the JSON
  config ``Ports`` map for each installed component (Content, Community),
  and NiFi UI from ``NiFi.WebHttpsPort`` / ``Ports.NiFi``. The Content line is
  only printed when Content is in ``Components`` (no hardcoded 9000 when
  Content is not configured).

## v0.6.5-python — NiFi start waits for Flow Controller + live timer

### Highlights
- **NiFi service start** now tails ``NIFI_HOME\logs\nifi-app.log`` until the
  line ``Flow Controller started successfully.`` appears (Apache NiFi bootstrap
  complete). A live elapsed-time counter is shown while waiting; when the
  marker is found the total start duration is printed (e.g. ``42.3s``).
- Timeout default **180s** (NiFi bootstrap is slower than IDOL services).
  If the Windows service is RUNNING but the marker is not seen in time, the
  start is still treated as success with a warning (check the log).
- Wired into the Install post-start phase and ``manage_kd_services.py``
  ``start`` / ``restart`` for the NiFi component.

## v0.6.4-python — Uninstall validation fix + SSL cert auto-generation on Install

### Highlights
- **Uninstall is no longer blocked by port-in-use checks.** Port availability
  and Visual C++ redistributable checks run only for Install / Upgrade / Repair.
  Uninstall and Configure still validate admin rights / OS / basic environment
  but do not fail when KD services already hold the configured ports.
- **SSL certificate pre-check on Install / Upgrade / Repair.** If the `ssl`
  folder (toolkit `ssl/` or `C:\KD-Setup\...\ssl`) is missing or incomplete
  (no ca.cert.pem / intermediate / ca-chain), the installer runs
  `tools/generate_ssl.py --auto --kd-services` (preferring the path
  `C:\KD-Setup\kd-win-setup-python\tools\generate_ssl.py` when present).
  Generation can be declined interactively; `-Force` continues without certs.
- **`nifi/Setup-NiFi-Service.ps1`** updated to the operator-supplied helper
  (article-style NSSM registration). The main installer still prefers the
  script when present and falls back to inline NSSM (`KD-NiFi`) if the script
  does not accept the `-NonInteractive` / `-NifiHome` parameters.

## v0.6.3-python — Setup-NiFi-Service.ps1 is the NiFi service path; bat removed

### Highlights
- **`nifi/Setup-NiFi-Service.ps1` is the supported helper** for registering Windows
  service **KD-NiFi** via NSSM (`run-nifi.bat run`). The main installer
  (`install_kd_nifi_service`) invokes this script with `-NonInteractive` during
  Install/Repair when `NiFi.InstallService` is true; inline NSSM remains a fallback
  if the script is missing.
- **`nifi/install-kd-nifi-service.bat` removed** from the toolkit and from all docs /
  operator messages. Use `Setup-NiFi-Service.ps1` instead (manual or automated).
- **Uninstall** stops and removes NiFi services with **`nssm stop`** then
  **`nssm remove <name> confirm`** for `KD-NiFi`, and also cleans legacy names
  `KD-Apache-NiFi` and `ApacheNifiService`.
- Service name remains **`KD-NiFi`**; DisplayName **OpenText KD Apache NiFi**.

## v0.6.2-python — Components-only service start; readable multi-line log details

### Highlights
- **Service start respects `Components` strictly.** If `LicenseServer` is not in
  the list (e.g. `"Components": ["NiFi"]`), it is never started or required.
  When it *is* present it still starts first, but a failure no longer blocks
  starting the remaining components (NiFi does not depend on LicenseServer).
- **NiFi start timeout raised to 90s** (bootstrap is slower than IDOL services).
- **Log `step_result` details are multi-line and readable** — long JAVA_HOME/PATH
  and path-heavy messages no longer wrap as one dense line on the console/file.

## v0.6.1-python — JAVA_HOME\bin on PATH, run-nifi.bat launcher, NiFi.InstallService option

### Highlights
- **When setting `JAVA_HOME`, `%JAVA_HOME%\bin` is also added to the permanent
  machine PATH** (REG_EXPAND_SZ, via the registry — avoids `setx PATH` 1024-char
  truncation). Applied in `install_kd_java()`, `confirm_kd_java_for_nifi()` (even
  when Java is already present), and when registering the `KD-NiFi` NSSM service.
  New helpers: `ensure_java_bin_on_path()`, `ensure_java_home_and_path()`.
- **`nifi/run-nifi.bat` is deployed into `BasePath\NiFi\bin\` during Install
  and Configure.** The launcher sets `JAVA_HOME` / prepends `%JAVA_HOME%\bin` to
  PATH for the process, then runs `nifi.cmd`. Modes: `start` (default, background),
  `run` (foreground — used by the Windows service), `stop`, `status`, `restart`.
- **NSSM `KD-NiFi` service prefers `run-nifi.bat run`** when the helper is present
  (falls back to `nifi.cmd run`). Same behaviour in the manual fallback
  `nifi/Setup-NiFi-Service.ps1` (supersedes the old install-kd-nifi-service.bat).
- **New config option `NiFi.InstallService`** (default `true`). When `false`,
  NiFi is still extracted and configured, but the Windows service is not
  registered — use `bin\run-nifi.bat start` or re-run with
  `NiFi.InstallService: true` / `nifi\Setup-NiFi-Service.ps1` later.
  Global `InstallService: false` still disables services for every component.

## v0.6.0-python — NSSM via winget, permanent JAVA_HOME/NIFI_HOME, Components-only service scope

### Highlights
- **NSSM is now installed via `winget install --id NSSM.NSSM -e`** first
  (the preferred method), verified with `nssm version`. Falls back to the
  bundled `nifi/nssm.exe`, `PATH`, or a direct download from nssm.cc if
  winget isn't available. New helpers in `kd.prerequisites`:
  `install_nssm_via_winget()`, `test_nssm_version()`; `find_nssm()` also now
  looks under the winget package install locations.
  `nifi/Setup-NiFi-Service.ps1` does the same winget install + verify when needed.
- **`JAVA_HOME` and `NIFI_HOME` are now set as permanent, machine-wide
  Windows environment variables** (`setx <NAME> <value> /M`) whenever the
  `KD-NiFi` service is installed - in addition to the existing NSSM
  `AppEnvironmentExtra` setting used by the service process itself. **Only
  set if not already present**: `kd.prerequisites.get_permanent_env_var()` /
  `set_permanent_env_var_if_missing()` check the registry first
  (`HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment`) so an
  existing operator-set value is never overwritten. Same check-then-setx
  logic applied to `install_kd_java()`'s belt-and-braces `JAVA_HOME` write
  and to the NiFi service registration path (via `reg query`).
- **Service management is strictly scoped to `Components` in the config
  JSON.** Install already only ever touched configured components; Uninstall
  and Repair no longer sweep up other `KD-*` services found on the machine
  by default. Opt back into the old sweep-up behavior with
  `"CleanupLeftoverServices": true` in your config.
- **Repair/Uninstall explicitly check whether each configured service is
  running before stopping it**, then continue with removal - clarified in
  logging and comments (`kd.installer.invoke_kd_uninstall` Phase 1).
- Docs (`docs/INSTALL-NIFI.md`) updated with the `winget`/`nssm version`
  steps, the permanent environment variable behavior, and the
  Components-only service scope.

## v0.5.9-python — NiFi Windows service switched back to NSSM (single service name)

### Highlights
- **NiFi Windows service is installed with NSSM** again (bundled `nifi/nssm.exe`).
  WinSW is no longer used for NiFi. Service name is always **`KD-NiFi`**.
- **Redundant legacy service removed**: the old standalone helper used
  `KD-Apache-NiFi`. Install and uninstall now stop/delete that name if present
  so only one NiFi service remains.
- `kd.prerequisites.ensure_nssm()` / `find_nssm()` locate the bundled binary,
  PATH, common install dirs, or download NSSM 2.24 from nssm.cc when needed.
- `install_kd_nifi_service()` configures NSSM with:
  - Application = `%COMSPEC%`, AppParameters = `/c "nifi.cmd" run`
  - `AppEnvironmentExtra` JAVA_HOME when available
  - rotated stdout/stderr under `NiFi\logs\nssm-*.log`
  - clean stop via AppStopMethodConsole + AppKillProcessTree
- Manual fallback is **`nifi/Setup-NiFi-Service.ps1`** (service name **`KD-NiFi`**; prefer `Install-KD.ps1`).
- Docs (`INSTALL.md`, `docs/INSTALL-NIFI.md`, README) updated for the NSSM flow.



## v0.5.8-python — Auto-detect and clean up a broken existing Temurin registration

### Highlights
- **Fixed MSI 1603 caused by "Windows Installer reconfigured the product...
  Reconfiguration success or error status: 1603"** - this happens when a
  Temurin JDK 21 product is already *registered* (e.g. from an earlier
  interrupted install attempt) but its files/folder are missing or
  incomplete. `msiexec /i` then implicitly switches to a repair of the
  broken product instead of a fresh install, and the repair fails
  (`WixRemoveFoldersEx: Missing folder property: INSTALLDIR`,
  `SECUREREPAIR: SecureRepair Failed`).
- `kd.prerequisites._find_registered_temurin_product()` now checks the
  Windows Installer uninstall registry (both native and `WOW6432Node`) for
  an existing Temurin JDK 21 registration before attempting install:
  - If it's a **working** install (java.exe present at `InstallLocation`),
    uses it directly and sets `JAVA_HOME` - no reinstall needed.
  - If it's **broken** (registered but files missing), runs
    `msiexec /x <ProductCode> /qn /norestart` to cleanly remove the stale
    registration first, so the following `/i` is a genuine fresh install.

## v0.5.7-python — Better diagnostics for Java install failures (MSI 1603)

### Highlights
- **`install_kd_java()` now catches the two most common causes of generic MSI
  error 1603 before even attempting the install:**
  - **Not elevated**: checks `IsUserAnAdmin()` up front and fails fast with a
    clear "re-run as Administrator" message instead of a cryptic 1603 (silent
    MSI install writes to Program Files + HKLM, which needs elevation).
  - **Pending reboot**: checks `PendingFileRenameOperations` and the CBS
    `RebootPending` key (very common on freshly-imaged/patched Azure/AWS VMs)
    and warns before installing.
- **Automatic verbose logging + diagnosis**: always runs `msiexec` with
  `/l*v <logfile>` and, on any non-0/3010 exit code, reads back the log's
  error lines and includes them directly in the failure `Detail` - no more
  "re-run this command by hand to see what happened".
- **Download retry**: if the downloaded MSI looks truncated (<100 MB; the real
  Temurin 21 x64 MSI is normally ~180-200 MB), retries the download once
  before giving up.

## v0.5.6-python — Fixed HTTP 403 on Adoptium/GitHub/Apache downloads (User-Agent)

### Highlights
- **Fixed `HTTP Error 403: Forbidden`** when downloading the Temurin JDK installer
  (and would have hit the same wall for WinSW and NiFi mirror downloads too).
  Root cause: Python's default `User-Agent` (`Python-urllib/3.x`) with no `Accept`
  header is commonly blocked by CDN/WAF bot-protection in front of
  `api.adoptium.net`, GitHub releases, and Apache mirrors - unrelated to auth,
  permissions, or the TLS fix from the previous release.
  `kd.prerequisites.ensure_kd_tls_opener()` now also sets a realistic
  browser-like `User-Agent` + `Accept: */*` on the shared urllib opener, so
  every download in this module (Java, WinSW, NiFi ZIP, VC++ redist) picks it
  up automatically.

## v0.5.5-python — Self-healing TLS fix + single NiFi dependency confirmation

### Highlights
- **`CERTIFICATE_VERIFY_FAILED` fix is now self-healing.** Previously it relied on
  `certifi` already being installed (via `pip install -r requirements.txt`), so it
  silently did nothing on machines that hadn't picked up the new dependency yet.
  `kd.prerequisites.ensure_kd_tls_opener()` now:
  1. Auto-installs `certifi` via `pip install --quiet certifi` if it isn't importable.
  2. Layers `ssl`'s own `load_default_certs()` (OS trust store).
  3. Explicitly loads certs from the Windows `CA`/`ROOT` stores via
     `ssl.enum_certificates()` - a dependency-free fallback that works even
     without PyPI access, as long as the relevant root CA is already trusted
     by Windows (e.g. via GPO).
- **Single upfront confirmation for all NiFi dependencies.** Instead of several
  separate Y/N prompts scattered through the run (Java, NiFi ZIP, ...), the
  installer now asks once, before doing anything: "Install any missing NiFi
  dependencies now? (Y/N) [Y]". Answering N exits setup cleanly (exit code 1)
  with next steps. Answering Y (or `-NonInteractive`) proceeds and auto-installs
  Java/WinSW/NiFi without further prompts.

## v0.5.4-python — Fixed download SSL errors; explicit stop-then-delete for services

### Highlights
- **Fixed `CERTIFICATE_VERIFY_FAILED` on WinSW/NiFi/VC++/Java downloads.** Added
  `kd.prerequisites.ensure_kd_tls_opener()`, which installs a urllib opener using
  a proper CA bundle (certifi's Mozilla bundle, layered with the OS trust store)
  before every download in that module. This is a common failure on Windows
  Python installs (official python.org installer, imaged machines, embeddable
  distributions) that don't have a working system CA bundle wired up by default.
  Added `certifi` to `requirements.txt`.
- **Explicit stop-if-running-then-delete for services**, both directions:
  - New `kd.service_manager.remove_kd_service_if_present()`: checks whether a
    service exists and is running; if running, stops it first, then deletes;
    if already stopped, deletes directly (no redundant stop call); no-op if
    not present at all.
  - **Install/Repair**: `install_kd_service()` and `install_kd_nifi_service()`
    now use this to clean up any stale/broken same-named service *before*
    registering a fresh one, instead of silently skipping installation when a
    service with that name already exists.
  - **Uninstall**: `uninstall_kd_service()`'s final cleanup step now uses the
    same shared helper instead of a hand-rolled stop/delete retry loop.

## v0.5.3-python — Java 21 auto-install for NiFi

### Highlights
- The Java 21 pre-flight check for NiFi no longer just warns - it now **auto-installs
  Eclipse Temurin JDK 21 and sets `JAVA_HOME`** when a suitable Java isn't detected:
  - `kd.prerequisites.install_kd_java()` downloads the official Temurin 21 MSI from
    `api.adoptium.net` and installs it silently
    (`ADDLOCAL=FeatureMain,FeatureEnvironment,FeatureJavaHome`), then sets `JAVA_HOME`
    machine-wide (`setx ... /M`, on top of the MSI's own `FeatureJavaHome`) and updates
    the current process's environment so the rest of the run sees it immediately.
  - `confirm_kd_java_for_nifi()` prompts interactively (default Yes), installs
    automatically under `-NonInteractive`, and can be disabled via
    `NiFi.AutoInstallJava: false` in config (falls back to warn-only).
- New config key `NiFi.AutoInstallJava` (default `true`).
- Updated `docs/INSTALL-NIFI.md` to describe the automatic install flow.

## v0.5.2-python — NiFi Windows service switched to WinSW (official OpenText pattern)

### Highlights
- **NiFi Windows service** is now installed with **WinSW** instead of NSSM, matching
  the pattern OpenText's own IDOL documentation uses for other non-native-exe
  components - see ["Install Find as a Service on Windows"](https://www.microfocus.com/documentation/idol/knowledge-discovery-26.1/Find_26.1_Documentation/admin/Content/Installation/WindowsService.htm):
  "The recommended way to install Find as a Windows service is to use the
  interactive installer... creates a Windows service using WinSW... download a
  copy of WinSW, and rename the winsw.exe executable file to find.exe."
- `kd.prerequisites.ensure_winsw()` downloads WinSW (pinned to v2.12.0) and places
  it at `NiFi\bin\KD-NiFi.exe`, replacing `ensure_nssm()` / `find_nssm()`.
- `kd.service_manager.install_kd_nifi_service()` now writes a `KD-NiFi.xml` WinSW
  descriptor (executable=`%COMSPEC%`, arguments wrap `nifi.cmd run`, heap/log/
  restart/JAVA_HOME settings) beside the renamed exe, then runs `KD-NiFi.exe install`.
  Because WinSW implements the real Windows Service Control Handler API, the
  resulting service works normally with `sc.exe`, `Services.msc`, and
  `Start-Service`/`Stop-Service` afterwards.
- `uninstall_kd_service()` now calls `KD-NiFi.exe stop` / `uninstall` for NiFi
  (falls through to the existing generic `sc delete` retry loop as a safety net).
- Updated `docs/INSTALL-NIFI.md`, `README.md`, `INSTALL.md` to describe the
  WinSW-based approach and cite the OpenText precedent.

## v0.5.1-python — NiFi Windows service aligned with official guidance

### Highlights
- **NiFi Windows service** now explicitly follows the community / OpenText-recommended
  pattern (Apache itself only documents Linux/macOS service install):
  - Service is registered with **NSSM** (`nifi.cmd` + `AppParameters=run`).
  - `JAVA_HOME` is propagated into the service environment via NSSM
    `AppEnvironmentExtra` when available (avoids the common “service cannot see
    interactive-user JAVA_HOME” failure).
- Added **Java 21+ pre-flight check** when `NiFi` is in the Components list
  (NiFi 2.x requirement; OpenText NiFi Ingest docs also require a supported JRE/JDK).
  The check is soft under `-DryRun` / `-Force` and can be overridden interactively.
- Corrected outdated comments and `INSTALL.md` text that still mentioned an
  `sc.exe`-based NiFi service (the implementation has used NSSM since v0.5.0).
- Documentation (`docs/INSTALL-NIFI.md`, `INSTALL.md`) now consistently describes
  the NSSM-based approach.

## v0.5.0-python — Optional Apache NiFi 2.x component

### Highlights
- **Apache NiFi 2.x** can be installed as an optional component under the same `BasePath`
  (e.g. `C:\KnowledgeDiscovery\26.2\NiFi`).
- New config section `NiFi` (HeapXms/HeapXmx default **8g/16g**, SensitivePropsKey,
  WebHttpsPort, VersionHint, AutoDownload) and `Ports.NiFi` (default 8443).
- **Interactive download prompt**: when NiFi is requested and no ZIP is found in ZipPath,
  the installer asks whether to download the official binary from nifi.apache.org
  (Apache CDN mirrors). Set `NiFi.AutoDownload: true` for non-interactive use.
- `kd.discovery` recognises `*nifi*-bin*.zip` packages and locates `nifi.cmd`.
- `kd.installer` configures `nifi.properties` + `bootstrap.conf` (heap + sensitive key)
  and registers a Windows service `KD-NiFi` via **NSSM** (auto-downloaded if missing,
  superseded by WinSW in v0.5.2).
- Full documentation in `docs/INSTALL-NIFI.md`.
- Uninstall / Configure / Resume paths treat NiFi like any other component.


## v0.4.1-python — Automatic extraction, Python zipfile fallback removed

### Highlights
- `kd/installer.py` no longer contains any Python-side ZIP extraction logic
  (the unused `zipfile` + Zip-Slip fallback in `expand_kd_zip_native` is gone).
  Extraction is delegated entirely to **`Unzip-One.bat`** (native `tar.exe`);
  the Python function is now a thin, checked `subprocess` call into that .bat file.
- Installer no longer prints "extract manually and press Enter" instructions.
  `_ensure_component_extracted` now locates the matching ZIP via
  `kd.discovery.find_kd_component_zip` and calls `Unzip-One.bat` automatically
  for any component folder that isn't already populated.
- `Unzip-One.bat` / `Unzip-All.bat` are unchanged and still work standalone if
  you prefer to pre-extract packages yourself.

## v0.4.0-python — Python backend + Pester/pytest test suite

### Highlights
- Full conversion of the PowerShell installer to a **Python backend** (`kd/` package)
  with a thin **PowerShell UI wrapper** (`Install-KD.ps1`) for elevation and interactive menus.
- **pytest unit tests** for pure logic modules (config, discovery, ini_config, state,
  service_manager helpers, validation helpers) — 41 tests, no admin rights required.
- **Pester smoke tests** (`Tests/PythonLogic.Tests.ps1`) that invoke pytest and exercise
  the Python CLI boundary from PowerShell.
- Fixed a latent bug in `Update-KDIniFile` port: values starting with a digit no longer
  corrupt the replacement because of `\1` + digit backreference ambiguity (`\g<1>` used).

### Test coverage (recommended next steps from original POC – done)
- KD.IniConfig / kd.ini_config
- KD.Discovery / kd.discovery
- KD.Validation / kd.validation
- KD.Config / kd.config
- KD.State / kd.state



## v0.3.10 — Extraction via Unzip-One.bat (native tar.exe)

### Highlights
- PowerShell no longer extracts ZIPs itself.
- `Expand-KDZipNative` now calls **Unzip-One.bat**, which uses the native
  Windows `tar.exe` and automatically strips the first-level folder that
  OpenText packages contain.
- Result is bit-identical to a manual Explorer unzip and avoids the
  antivirus corruption that previously produced broken .exe/.dll files.
- `Unzip-All.bat` is also included for bulk manual extraction.
- Fallback order: Unzip-One.bat → Shell.Application → .NET ZipFile.


## v0.3.8 — Shell.Application extraction (fixes AV/EDR zip failures)

### Highlights
- **Extraction now uses Shell.Application first** (the same engine Windows Explorer uses).
  Antivirus/EDR products almost always allow-list Explorer's extractor, so this eliminates
  the common failure where PowerShell/.NET extraction silently produces truncated .exe/.dll
  files while manual unzip works fine.
- Falls back to the previous System.IO.Compression + size-verification path only if
  Shell.Application fails or times out.
- Zip-Slip protection is still applied before any extraction begins.
- Module version bumped to v0.3.8.


## v0.3.5 — Install does not start services mid-loop; LicenseServer first

### Highlights
- Per-component install **only registers** Windows services (no Start/Stop during the loop).
- After all components are installed:
  1. Start **LicenseServer** (licensekey.dat already copied during extract).
  2. Only if LicenseServer reaches Running, start all other KD-* services.
- Configure mode no longer starts/stops services.

## v0.3.4 — LicenseServer-only idol.common.cfg + auto-start after key

### Highlights
- **`idol.common.cfg` is updated only for the LicenseServer component** (not every component).
- After `licensekey.dat` is copied into the LicenseServer extract folder and the service is registered, **the LicenseServer service is started automatically**.

## v0.3.3 — Robust service stop before uninstall

### Highlights
- **Uninstall now stops ALL services first** (Phase 1) before removing any of them (Phase 2). This prevents the common "Service KD-X still present" failure when SCM cannot delete a still-running service.
- New `Stop-KDServiceForce` helper: Stop-Service → `sc stop` → wait-until-Stopped → force-kill process by PID if needed.
- `Uninstall-KDService` always finishes with `sc.exe delete` (and a retry) even when the component binary's own `-remove` is used.
- `Cleanup-KD.ps1` updated with the same stop-all-then-delete flow.

## v0.3.2 — License key install + idol.common.cfg + no LicenseServerPort

### Highlights
- **Do not write `LicenseServerPort`** into any component `.cfg` files (removed from both Install and Configure paths).
- New config key **`LicenseKeyPath`**: path to the source license key file. On extract/configure of LicenseServer the file is copied into the unzipped LicenseServer folder (preferring any `LicenseServer_*_WINDOWS_*` subdir) and renamed to **`licensekey.dat`**.
- **`idol.common.cfg`** (when present under a component) is updated to:
  - `LicenseServerHost=licenseserver`
  - `Clients=*.*.*.*`
- `default-config.json`, `my-config.json`, Lua generator, and `INSTALL.md` updated accordingly.
- Env var `KD_LICENSEKEYPATH` and CLI `--licensekeypath` supported by the Lua generator.

## v0.3.1 — Enhanced configuration support (IndexPath + official workflow guidance)

### Highlights
- Added `IndexPath` setting (applied to Content, Category, Community).
- `Invoke-KDConfigure` now supports `[Index]` section.
- Lua generator supports `--indexpath` and prompts for it interactively.
- Added **Knowledge Discovery Workflow** section in `INSTALL.md` based on official documentation.

## v0.3.0 (kd-win-setup-v16) — Added dedicated Configure mode + wrapper and Lua config generator.

### Highlights
- New `-Mode Configure` and `Invoke-KDConfigure` — apply configuration changes without re-extracting or reinstalling services.
- **`Configure-KD.ps1`** — user-friendly wrapper (supports `-GenerateConfig`).
- **`tools/generate-kd-config.lua`** — Interactive + CLI Lua generator for `my-config.json` (excellent with ZeroBrane Studio).
- Fixed `my-config.json` syntax error.
- Updated INSTALL.md with full configuration workflow.

### Usage
```powershell
# Generate config with Lua (recommended)
lua tools\generate-kd-config.lua

# Apply configuration
.\Configure-KD.ps1 -NonInteractive
```

## v0.2.0 (kd-win-setup-claude-work-v14) — Merged full v9 modular architecture + features into v13 base; added/completed missing Install/Uninstall/Repair/HealthCheck orchestration; PS 5.1+ compatible; production-ready POC structure

### Integrated / Fixed from v9 into v13 foundation
- All 8 robust modules now present and active (KD.Config, KD.Validation, KD.State, KD.IniConfig, KD.Discovery (with improved exe ranking + expanded exclusions), KD.ServiceManager (with captured process I/O + template args), KD.Logging (leveled + file), KD.Installer (orchestration))
- Pre-flight validation (admin, OS, RAM, disk, ports, ZIP reachability) via KD.Validation.psm1
- Resume support via KD.State.psm1 + kd-install-state.json
- Safe atomic .cfg edits with backup (KD.IniConfig)
- Idempotent service install/uninstall with proper arg templating from config
- DryRun, NonInteractive, Force, multi-mode (Install/Upgrade/Repair/Uninstall) fully wired in Install-KD.ps1
- Agentstore special handling (derived from Content package) preserved and enhanced
- Structured logging + step results
- Health check after install/repair/upgrade
- Completed the partial KD.Installer.psm1 (added Invoke-KDUninstall, Invoke-KDRepair, Invoke-KDHealthCheck using existing building blocks)
- Added Service*ArgsTemplate to default-config.json for service CLI compatibility
- Updated docs (README, INSTALL, this CHANGELOG)
- Minor: ensured no ternary operators (PS5.1 compat), proper error handling in new functions

### Known limitations (carried forward)
- Same as v0.1.0-poc: no checksum verification, no secret vaulting, single-host focus, Upgrade is re-install (does not auto-diff binaries)

## v0.1.0-poc — Modular rework of kd-win-setup-grok-work-v3.ps1

### Analysis of the original script

- Single 284-line script mixing input collection, extraction, .cfg editing,
  and service installation with no separation of concerns.
- No pre-flight environment validation (disk space, ports, admin rights
  were assumed, not checked).
- `Update-IniFile` built regex patterns by directly interpolating
  `$Section`/`$Key` into the pattern string. Values containing regex
  metacharacters (e.g. a key or section name with `.`, `(`, `[`) would
  produce an incorrect or silently wrong match — a latent correctness bug.
- No backup taken before rewriting a `.cfg` file, so a bad regex substitution
  had no recovery path.
- No resume: an interrupted run had to be restarted from scratch, and the
  service-exists check was the only idempotency guard.
- No uninstall/repair/rollback path at all — services and folders had to be
  removed by hand.
- Hardcoded port `9000` for Content/Category inline in the extraction loop
  rather than in configuration.
- Logging was `Start-Transcript` only — no levels, no structured format for
  later parsing.

### Fixed

- **Executable discovery could select a bundled prerequisite installer
  instead of the actual service binary.** `Find-KDComponentExecutable`
  only excluded `uninstall|setup|java|jre|jdk|remove|delete` from
  candidates, so a bundled `vcredist.exe` (Visual C++ Redistributable) or
  similar prerequisite installer inside the component zip could be picked
  as "the" executable, launching its own GUI installer window and
  returning MSI-style exit codes (e.g. 1638 = a version is already
  installed) that have nothing to do with KD itself. Exclusion list
  expanded to cover `vcredist`, `vcruntime`, `msvcp`/`msvcr`, `dotnet`,
  `ndp<N>`, `directx`, `dxsetup`, `prereq`, `redist`, `crashpad`.
  Candidate ranking also now gives a bonus to filenames containing the
  component name. All candidates considered are logged so the pick is
  auditable.
- Service install arguments were malformed (see below).
  manually wrapped `{ServiceName}`/`{DisplayName}` in literal quote
  characters before passing them as array elements to the native
  executable. PowerShell's array-based process invocation already quotes
  arguments correctly; the extra literal quotes were passed straight
  through to the child process and could cause it to reject the
  arguments silently (install "succeeds" with no error, but no service
  is created).
- Process output capture switched from `& exe args 2>&1` to
  `Start-Process` with redirected stdout/stderr files plus real exit
  code capture — the old approach could return an empty `$output` even
  on failure, giving no diagnostic signal.

### Added

- `ServiceInstallArgsTemplate` / `ServiceUninstallArgsTemplate` in
  `default-config.json` — the actual CLI switches used to install/remove a
  Windows service are now config-driven with `{ServiceName}`,
  `{DisplayName}`, `{StartMode}` placeholders, instead of hardcoded in
  `KD.ServiceManager.psm1`. **Known limitation:** these defaults are
  carried over from the original script and have not been verified
  against real OpenText KD 26.2 Windows binaries — confirm via the
  executable's own `-help`/`-?` output and adjust config as needed (see
  `INSTALL.md` Troubleshooting).
- `Find-KDComponentZip` now returns a structured result distinguishing
  "no matching zip found" from "only Linux packages found" instead of a
  single generic message for both.
- `Initialize-Environment.ps1` — one-time prep script, run before
  `Install-KD.ps1`. Recursively unblocks files (fixes the "not digitally
  signed" error seen when a zip is extracted from OneDrive/browser/email),
  sets execution policy for the current process or user, confirms
  elevation/PowerShell version, verifies all module files are present, and
  optionally scaffolds `config\my-config.json` from the template.
- `config/default-config.json` — centralized configuration; ports, paths,
  components, license target all externalized.
- Environment-variable override support (`KD_<FIELD>`).
- `KD.Validation.psm1` — admin rights, OS/arch, PowerShell version, RAM,
  disk space, port availability, ZIP source reachability checks, run before
  any install action.
- `KD.State.psm1` — per-component stage tracking (`Extracted` /
  `Configured` / `ServiceInstalled` / `Complete`) persisted to
  `kd-install-state.json`, enabling `-Resume`.
- `Uninstall` and `Repair` modes in `KD.Installer.psm1`.
- `-DryRun` supported across every mode.
- `Backup-KDFile` / atomic temp-file write in `KD.IniConfig.psm1`.
- Structured logging (`KD.Logging.psm1`) with Debug/Info/Warn/Error levels,
  console + timestamped log file.
- Post-install health check (`Invoke-KDHealthCheck`) verifying each
  installed service reaches `Running`.

### Changed

- `Update-KDIniFile` now escapes `Section`/`Key` via `[regex]::Escape`
  before building patterns (bug fix vs. original `Update-IniFile`).
- ZIP selection (`Find-KDComponentZip`) now prefers explicitly
  Windows-marked packages before falling back to unmarked ones, rather than
  taking the first filename match — reduces the chance of silently
  extracting a Linux package.
- Windows-executable guard (`Test-KDWindowsExecutable`, PE header check)
  preserved from the original and reused across install/uninstall/repair.

### Known issues / not yet implemented (see README "Known limitations")

- No package checksum/signature verification.
- No credential vaulting; license host/port stored as plain config.
- No automated test suite (unit/integration) included in this POC pass.
- Upgrade mode does not diff/replace binaries for already-installed
  components — it only fills in missing ones.

### Recommended next steps

1. Add Pester unit tests for `KD.IniConfig`, `KD.Discovery`, and
   `KD.Validation` (pure functions, easy to test with fixture files).
2. Add SHA256 checksum manifest for ZIP packages and verify before extract.
3. Add a `-WhatIf`/`ShouldProcess` implementation for native PowerShell
   confirmation support instead of the custom `-DryRun` switch.
4. Move license/credential values to a secret store (DPAPI-protected file
   or a corporate vault) instead of plain JSON.
5. Add a true Upgrade path that stops services, replaces binaries, and
   restarts, with version comparison.

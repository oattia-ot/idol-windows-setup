"""
Visual C++ Redistributable check + optional auto-install.
Missing VC++ is the #1 cause of STATUS_DLL_NOT_FOUND (-1073741515) on service start.
"""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from .logging import log

# ANSI yellow for user-facing questions / input prompts
_PROMPT_YELLOW = "\033[33m"
_PROMPT_RESET = "\033[0m"

try:
    import winreg
except ImportError:
    winreg = None  # type: ignore

try:
    import certifi
except ImportError:
    certifi = None  # type: ignore


_TLS_OPENER_INSTALLED = False


def _ensure_certifi_installed() -> None:
    """
    If certifi isn't importable (e.g. requirements.txt was never re-installed
    after this feature was added), install it silently via pip so the TLS fix
    below actually works without the user having to remember a manual step.
    """
    global certifi
    if certifi is not None:
        return
    try:
        log.info("  certifi not found - installing it now (pip install certifi)...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "certifi"],
            capture_output=True,
            timeout=120,
        )
        import certifi as _certifi  # re-import after install
        certifi = _certifi
        log.info("  certifi installed.")
    except Exception as e:
        log.warn(f"  Could not auto-install certifi: {e}")


def _load_windows_store_certs(ctx: ssl.SSLContext) -> int:
    """
    Load certs directly from the Windows CA/ROOT certificate stores into ctx.
    Dependency-free fallback that doesn't need certifi or PyPI access at all -
    covers machines where PyPI isn't reachable but the Windows cert store
    already trusts the relevant root (e.g. via a corporate GPO-pushed CA, or
    the OS's own root program). ctx.load_default_certs() is supposed to do
    this automatically but its behaviour varies across Python builds, so this
    is an explicit belt-and-braces pass.
    """
    if sys.platform != "win32":
        return 0
    loaded = 0
    for store in ("CA", "ROOT"):
        try:
            for cert_der, encoding, _trust in ssl.enum_certificates(store):
                if encoding != "x509_asn":
                    continue
                try:
                    ctx.load_verify_locations(cadata=cert_der)
                    loaded += 1
                except ssl.SSLError:
                    pass
        except Exception:
            continue
    return loaded


def ensure_kd_tls_opener() -> None:
    """
    Install a urllib opener with a working CA bundle for all downloads in this
    module (NSSM, NiFi ZIP, VC++ redist, Temurin JDK). Also sets a realistic
    browser-like User-Agent, since Adoptium/GitHub/Apache mirrors commonly
    return HTTP 403 Forbidden for the default "Python-urllib/x.y" User-Agent
    (bot-protection at the CDN/WAF level, unrelated to auth or permissions).

    Fixes CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate,
    which is common on Windows Python installs (official python.org installer,
    imaged machines, embeddable distributions) that don't have a properly
    wired-up system CA bundle. Stacks three independent sources so this works
    even if one is unavailable (e.g. no PyPI access, or certifi never
    installed):
      1. certifi's bundled Mozilla CA set (auto-installed via pip if missing).
      2. ssl's own load_default_certs() (OS trust store, when it works).
      3. An explicit pass over the Windows CA/ROOT stores (dependency-free
         fallback - picks up corporate/internal root CAs pushed via GPO).
    Idempotent - safe to call from every download function.
    """
    global _TLS_OPENER_INSTALLED
    if _TLS_OPENER_INSTALLED:
        return

    _ensure_certifi_installed()

    try:
        ctx = ssl.create_default_context(cafile=certifi.where()) if certifi else ssl.create_default_context()
        try:
            ctx.load_default_certs()
        except Exception:
            pass
        extra = _load_windows_store_certs(ctx)

        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        opener.addheaders = [
            ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) kd-win-setup-python/1.0"),
            ("Accept", "*/*"),
        ]
        urllib.request.install_opener(opener)
        _TLS_OPENER_INSTALLED = True
        log.debug(f"TLS opener installed (certifi={'yes' if certifi else 'no'}, windows_store_certs={extra})")
    except Exception as e:
        log.warn(f"Could not configure a trusted CA bundle for downloads: {e}")


def test_kd_visual_cpp_redist(architecture: str = "X64", min_version: str = "14.0.0.0") -> Dict[str, Any]:
    """Check the registry key written by the VC++ 2015-2022 redistributable."""
    if winreg is None:
        return {
            "Name": f"Visual C++ Redistributable ({architecture})",
            "Pass": False,
            "Installed": False,
            "Version": None,
            "Detail": "winreg not available (not running on Windows?)",
        }

    reg_path = rf"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\{architecture}"
    installed = False
    version_string = None

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path) as key:
            installed_val, _ = winreg.QueryValueEx(key, "Installed")
            if installed_val == 1:
                installed = True
                try:
                    version_string, _ = winreg.QueryValueEx(key, "Version")
                except OSError:
                    pass
    except OSError:
        pass

    meets = False
    if installed and version_string:
        try:
            clean = version_string.lstrip("v")
            from packaging.version import Version  # optional; fallback below

            meets = Version(clean) >= Version(min_version)
        except Exception:
            # packaging not installed or parse failure – presence is good enough
            meets = True
    elif installed:
        meets = True

    pass_ = installed and meets
    if pass_:
        detail = f"Installed: {version_string}"
    elif installed:
        detail = f"Installed version {version_string} is older than required {min_version}"
    else:
        detail = "Not installed (registry key not found)"

    return {
        "Name": f"Visual C++ Redistributable ({architecture})",
        "Pass": pass_,
        "Installed": installed,
        "Version": version_string,
        "Detail": detail,
    }


def install_kd_visual_cpp_redist(architecture: str = "X64", dry_run: bool = False) -> Dict[str, Any]:
    url = (
        "https://aka.ms/vs/17/release/vc_redist.x64.exe"
        if architecture.upper() == "X64"
        else "https://aka.ms/vs/17/release/vc_redist.x86.exe"
    )
    if dry_run:
        return {
            "Success": True,
            "Detail": f"[DryRun] Would download {url} and run /install /quiet /norestart",
            "RebootRequired": False,
        }

    installer = Path(tempfile.gettempdir()) / f"vc_redist.{architecture.lower()}.exe"
    try:
        ensure_kd_tls_opener()
        log.info(f"Downloading Visual C++ Redistributable ({architecture}) from {url}")
        urllib.request.urlretrieve(url, installer)

        log.info(f"Installing Visual C++ Redistributable ({architecture}) silently...")
        proc = subprocess.run(
            [str(installer), "/install", "/quiet", "/norestart"],
            capture_output=True,
            timeout=300,
        )
        exit_code = proc.returncode
        # 0 = success, 3010 = success + reboot needed, 1638 = newer already present
        success = exit_code in (0, 3010, 1638)
        detail = f"ExitCode: {exit_code}"
        if exit_code == 3010:
            detail += " (installed; a reboot is required)"
        if exit_code == 1638:
            detail += " (compatible or newer version already present)"
        if not success:
            detail += " (install failed)"
        return {
            "Success": success,
            "Detail": detail,
            "RebootRequired": exit_code == 3010,
        }
    except Exception as e:
        return {
            "Success": False,
            "Detail": f"Exception during download/install: {e}",
            "RebootRequired": False,
        }
    finally:
        try:
            installer.unlink(missing_ok=True)
        except Exception:
            pass


def confirm_kd_visual_cpp_redist(
    architecture: str = "X64",
    dry_run: bool = False,
    non_interactive: bool = False,
) -> Dict[str, Any]:
    check = test_kd_visual_cpp_redist(architecture)
    if check["Pass"]:
        return {"Name": check["Name"], "Pass": True, "Detail": check["Detail"]}

    log.warn(
        f"{check['Name']} not found - {check['Detail']}. "
        "This will cause KD service installs to fail with STATUS_DLL_NOT_FOUND."
    )

    if dry_run:
        return {
            "Name": check["Name"],
            "Pass": True,
            "Detail": f"[DryRun] Missing ({check['Detail']}); would auto-download and install",
        }

    if not non_interactive:
        try:
            print(f"{_PROMPT_YELLOW}{check['Name']} is missing. Download and install it now? (Y/N) [Y]: {_PROMPT_RESET}", end="", flush=True)
            answer = input().strip()
            if answer.upper() == "N":
                return {
                    "Name": check["Name"],
                    "Pass": False,
                    "Detail": "Not installed; user declined automatic install",
                }
        except EOFError:
            pass  # non-interactive fallback

    install_result = install_kd_visual_cpp_redist(architecture)
    if not install_result["Success"]:
        return {
            "Name": check["Name"],
            "Pass": False,
            "Detail": (
                f"Automatic install failed - {install_result['Detail']}. "
                f"Download manually from https://aka.ms/vs/17/release/vc_redist.{architecture.lower()}.exe"
            ),
        }

    recheck = test_kd_visual_cpp_redist(architecture)
    detail = recheck["Detail"]
    if install_result.get("RebootRequired"):
        detail += " - a REBOOT is required before KD services will start correctly"
    return {"Name": check["Name"], "Pass": recheck["Pass"], "Detail": detail}


# ---------------------------------------------------------------------------
# Java version check (required by Apache NiFi 2.x / OpenText guidance)
# ---------------------------------------------------------------------------

def test_kd_java_version(min_major: int = 21) -> Dict[str, Any]:
    """
    Best-effort check that a suitable Java runtime is available.
    NiFi 2.x (and therefore the KD optional NiFi component) requires Java 21+.
    Official OpenText NiFi Ingest docs also require a supported JRE/JDK.
    """
    java_home = None
    version_str = None
    major = 0
    detail_parts = []

    # Prefer JAVA_HOME
    jh = os.environ.get("JAVA_HOME") or os.environ.get("JDK_HOME")
    if jh:
        java_home = Path(jh)
        detail_parts.append(f"JAVA_HOME={jh}")

    # Locate java executable
    java_exe = None
    if java_home and (java_home / "bin" / "java.exe").is_file():
        java_exe = java_home / "bin" / "java.exe"
    else:
        try:
            r = subprocess.run(
                ["where", "java.exe"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    p = Path(line.strip())
                    if p.is_file() and "WindowsApps" not in str(p):
                        java_exe = p
                        break
        except Exception:
            pass

    if not java_exe:
        return {
            "Name": f"Java {min_major}+ (required for NiFi 2.x)",
            "Pass": False,
            "Version": None,
            "JavaHome": None,
            "Detail": "java.exe not found on PATH and JAVA_HOME is not set. "
                      "Install Eclipse Temurin JDK 21 (or later) from https://adoptium.net/ "
                      "and set JAVA_HOME.",
        }

    try:
        proc = subprocess.run(
            [str(java_exe), "-version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        # java -version writes to stderr
        out = (proc.stderr or "") + (proc.stdout or "")
        # Look for version strings like "21.0.3", "openjdk version \"21.0.2\"", "21-ea"
        import re
        m = re.search(r'version\s+"?(\d+)(?:\.(\d+))?', out, re.IGNORECASE)
        if not m:
            m = re.search(r'(\d+)\.(\d+)\.(\d+)', out)
        if m:
            major = int(m.group(1))
            version_str = m.group(0).strip().strip('"')
        detail_parts.append(f"Found: {java_exe}")
        if version_str:
            detail_parts.append(f"Version: {version_str}")
    except Exception as e:
        return {
            "Name": f"Java {min_major}+ (required for NiFi 2.x)",
            "Pass": False,
            "Version": None,
            "JavaHome": str(java_home) if java_home else None,
            "Detail": f"Could not execute java -version: {e}",
        }

    passed = major >= min_major
    if passed:
        detail = "; ".join(detail_parts) if detail_parts else f"Java {major} detected"
    else:
        detail = (
            f"Detected Java major version {major or 'unknown'} "
            f"(need >={min_major}). {'; '.join(detail_parts)}. "
            "Install Eclipse Temurin JDK 21+ from https://adoptium.net/ and set JAVA_HOME."
        )

    return {
        "Name": f"Java {min_major}+ (required for NiFi 2.x)",
        "Pass": passed,
        "Version": version_str,
        "JavaHome": str(java_home) if java_home else None,
        "Detail": detail,
    }


def find_temurin_jdk_install_dir(major: int = 21) -> Optional[Path]:
    """Look for an already-installed Eclipse Temurin JDK matching the given major version."""
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    base = program_files / "Eclipse Adoptium"
    if not base.is_dir():
        return None
    candidates = sorted(base.glob(f"jdk-{major}*"), reverse=True)
    for c in candidates:
        if (c / "bin" / "java.exe").is_file():
            return c
    return None


TEMURIN_FEATURE_VERSION = "21"
TEMURIN_INSTALLER_URL = (
    f"https://api.adoptium.net/v3/installer/latest/{TEMURIN_FEATURE_VERSION}/ga/"
    f"windows/x64/jdk/hotspot/normal/eclipse"
)


def get_permanent_env_var(name: str) -> Optional[str]:
    """
    Read a permanent, machine-wide environment variable directly from the
    registry (HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment),
    independent of what's in the current process's os.environ (which may be
    stale/unset even though the permanent value already exists).
    Returns None if it isn't set there, or if not on Windows.
    """
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value if value else None
    except (FileNotFoundError, OSError):
        return None
    except Exception:
        return None


def set_permanent_env_var_if_missing(name: str, value: str) -> Dict[str, Any]:
    """
    Set a permanent, machine-wide environment variable (setx <name> <value> /M)
    ONLY if it doesn't already exist there - never overwrites an operator's
    existing/intentional value. Updates the current process's os.environ
    either way (so the rest of this run sees the right value), and reports
    whether a write actually happened.
    """
    existing = get_permanent_env_var(name)
    if existing:
        os.environ[name] = existing
        return {
            "Success": True,
            "Written": False,
            "Value": existing,
            "Detail": f"{name} already permanently set to '{existing}' - left unchanged",
        }

    try:
        proc = subprocess.run(["setx", name, value, "/M"], capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            os.environ[name] = value
            return {
                "Success": True,
                "Written": True,
                "Value": value,
                "Detail": f"Set permanent machine environment variable {name}={value}",
            }
        return {
            "Success": False,
            "Written": False,
            "Value": None,
            "Detail": f"setx {name} failed ({(proc.stderr or '').strip()})",
        }
    except Exception as e:
        return {
            "Success": False,
            "Written": False,
            "Value": None,
            "Detail": f"Exception running setx {name}: {e}",
        }


def _path_entry_present(path_value: str, entry: str, java_home: Optional[str] = None) -> bool:
    """
    True if *entry* (or an equivalent expanded form) is already on PATH.
    Treats %JAVA_HOME%\\bin and <java_home>\\bin as the same slot.
    """
    parts = [p.strip() for p in path_value.split(";") if p.strip()]
    entry_norm = entry.rstrip("\\/").lower().replace("/", "\\")
    expanded = None
    if java_home:
        expanded = str(Path(java_home) / "bin").rstrip("\\/").lower().replace("/", "\\")
    for p in parts:
        p_norm = p.rstrip("\\/").lower().replace("/", "\\")
        if p_norm == entry_norm:
            return True
        if expanded and p_norm == expanded:
            return True
        # %JAVA_HOME%\bin already present (any casing / slash style)
        if p_norm.replace("%", "") == "java_home\\bin" or p_norm == "%java_home%\\bin":
            return True
    return False


def ensure_java_bin_on_path(java_home: Optional[str | Path] = None) -> Dict[str, Any]:
    """
    Ensure ``%JAVA_HOME%\\bin`` is on the permanent machine PATH.

    Writes the *unexpanded* entry ``%JAVA_HOME%\\bin`` (REG_EXPAND_SZ) so PATH
    tracks whatever JAVA_HOME is set to later. Also prepends the expanded
    ``<java_home>\\bin`` to the current process PATH so the rest of this
    installer run can find java.exe without a new shell.

    Safe / idempotent: if either form is already present, PATH is left alone.
    Avoids ``setx PATH`` (1024-char truncation risk) and edits the registry
    directly when possible.
    """
    jh = str(java_home or os.environ.get("JAVA_HOME") or get_permanent_env_var("JAVA_HOME") or "").strip()
    entry = r"%JAVA_HOME%\bin"

    if not jh:
        return {
            "Success": False,
            "Written": False,
            "Detail": "JAVA_HOME is not set; cannot add %JAVA_HOME%\\bin to PATH",
        }

    # Always fix up this process so subsequent steps (NiFi service, java -version) work
    expanded_bin = str(Path(jh) / "bin")
    current = os.environ.get("PATH", "")
    if expanded_bin.lower() not in current.lower() and entry.lower() not in current.lower():
        os.environ["PATH"] = expanded_bin + (";" + current if current else "")

    if winreg is None or sys.platform != "win32":
        return {
            "Success": True,
            "Written": False,
            "Detail": f"Non-Windows or no winreg; process PATH updated with {expanded_bin}",
        }

    env_key = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, env_key, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            try:
                path_val, path_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                path_val, path_type = "", winreg.REG_EXPAND_SZ

            if not isinstance(path_val, str):
                path_val = str(path_val or "")

            if _path_entry_present(path_val, entry, jh):
                return {
                    "Success": True,
                    "Written": False,
                    "Detail": f"PATH already contains {entry} (or {expanded_bin}) - left unchanged",
                }

            new_path = entry + (";" + path_val if path_val else "")
            # Prefer REG_EXPAND_SZ so %JAVA_HOME% is expanded at runtime
            write_type = winreg.REG_EXPAND_SZ if path_type in (winreg.REG_EXPAND_SZ, winreg.REG_SZ) else path_type
            winreg.SetValueEx(key, "Path", 0, write_type, new_path)

        # Broadcast WM_SETTINGCHANGE so new processes see the update without reboot
        try:
            import ctypes
            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x001A
            SMTO_ABORTIFHUNG = 0x0002
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", SMTO_ABORTIFHUNG, 5000, None
            )
        except Exception:
            pass

        return {
            "Success": True,
            "Written": True,
            "Detail": f"Added {entry} to permanent machine PATH",
        }
    except PermissionError:
        return {
            "Success": False,
            "Written": False,
            "Detail": "Access denied updating machine PATH (need Administrator). "
                      f"Add {entry} manually under System Properties > Environment Variables.",
        }
    except Exception as e:
        return {
            "Success": False,
            "Written": False,
            "Detail": f"Could not update machine PATH: {e}",
        }


def ensure_java_home_and_path(java_home: Optional[str | Path] = None) -> Dict[str, Any]:
    """
    Ensure JAVA_HOME is set permanently (if missing) and ``%JAVA_HOME%\\bin``
    is on the permanent machine PATH. Returns a combined result dict.
    """
    jh = java_home
    if jh is None:
        jh = os.environ.get("JAVA_HOME") or get_permanent_env_var("JAVA_HOME")
    if jh is None:
        found = find_temurin_jdk_install_dir(21)
        if found:
            jh = found

    details: List[str] = []
    success = True

    if jh:
        env_result = set_permanent_env_var_if_missing("JAVA_HOME", str(jh))
        details.append(env_result["Detail"])
        if not env_result["Success"]:
            success = False
        path_result = ensure_java_bin_on_path(jh)
        details.append(path_result["Detail"])
        if not path_result["Success"]:
            success = False
        return {
            "Success": success,
            "JavaHome": str(jh),
            "Detail": "; ".join(details),
        }

    return {
        "Success": False,
        "JavaHome": None,
        "Detail": "No JAVA_HOME value available to set",
    }


def _is_elevated() -> bool:
    """True if the current process has Administrator rights (Windows)."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return True  # can't tell - don't block on it


def _pending_reboot() -> bool:
    """
    True if Windows has a reboot pending (PendingFileRenameOperations or
    CBS RebootPending). Very common on freshly-imaged/patched Azure VMs and
    is one of the most frequent causes of MSI error 1603.
    """
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager",
        ) as key:
            try:
                winreg.QueryValueEx(key, "PendingFileRenameOperations")
                return True
            except FileNotFoundError:
                pass
    except Exception:
        pass
    try:
        winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
        ).Close()
        return True
    except Exception:
        pass
    return False


def _tail_msi_log(log_path: Path, lines: int = 25) -> str:
    """Return the last N lines of an msiexec /l*v log, filtered to error lines when possible."""
    try:
        text = log_path.read_text(encoding="utf-16", errors="ignore")
        if not text.strip():
            text = log_path.read_text(encoding="utf-8", errors="ignore")
        all_lines = [l for l in text.splitlines() if l.strip()]
        error_lines = [l for l in all_lines if "error" in l.lower() or "return value 3" in l.lower()]
        tail = error_lines[-lines:] if error_lines else all_lines[-lines:]
        return "\n".join(tail)
    except Exception as e:
        return f"(could not read {log_path}: {e})"


def _find_registered_temurin_product(major: int = 21) -> Optional[Dict[str, str]]:
    """
    Look for an existing Eclipse Temurin JDK registration in the Windows
    Installer uninstall registry, independent of whether
    find_temurin_jdk_install_dir() found working files on disk.

    This catches the classic "reconfigured the product ... error 1603"
    failure: a previous install attempt left a product registered (so a new
    /i install becomes an implicit repair) but the files/folder are
    missing/incomplete, and the repair then fails (WixRemoveFoldersEx /
    SECUREREPAIR errors). Returns
    {"ProductCode": ..., "InstallLocation": ..., "DisplayName": ...} or None.
    """
    if winreg is None:
        return None
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, path in roots:
        try:
            with winreg.OpenKey(hive, path) as base:
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(base, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(base, subkey_name) as sub:
                            try:
                                display_name = winreg.QueryValueEx(sub, "DisplayName")[0]
                            except FileNotFoundError:
                                continue
                            name_lower = display_name.lower()
                            if "temurin" in name_lower and "jdk" in name_lower and str(major) in display_name:
                                install_loc = ""
                                try:
                                    install_loc = winreg.QueryValueEx(sub, "InstallLocation")[0]
                                except FileNotFoundError:
                                    pass
                                return {
                                    "ProductCode": subkey_name,
                                    "InstallLocation": install_loc,
                                    "DisplayName": display_name,
                                }
                    except Exception:
                        continue
        except Exception:
            continue
    return None


def install_kd_java(version: str = TEMURIN_FEATURE_VERSION, dry_run: bool = False) -> Dict[str, Any]:
    """
    Download and silently install Eclipse Temurin JDK 21 (the JRE/JDK NiFi 2.x
    and OpenText's NiFi Ingest guidance require), then set JAVA_HOME.

    - Installs via the official Adoptium MSI, using its FeatureJavaHome /
      FeatureEnvironment options so JAVA_HOME and PATH are set machine-wide
      by the installer itself.
    - Also runs an explicit `setx JAVA_HOME ... /M` as a belt-and-braces
      fallback, and updates os.environ for the current process so the rest
      of this run (and the NiFi bootstrap.conf JAVA_HOME propagation) sees
      it immediately without needing a new shell.
    - Checks elevation and pending-reboot state up front (the two most common
      causes of generic MSI error 1603), and always captures a verbose
      msiexec log so a real failure reason is surfaced automatically instead
      of asking the user to re-run diagnostics by hand.
    - Detects a stale/broken existing Temurin registration (a previous
      partial install attempt) and cleanly uninstalls it first, instead of
      letting msiexec silently switch to a "repair" that then fails with
      1603 because the original install source/files are missing.
    """
    if dry_run:
        return {
            "Success": True,
            "Detail": f"[DryRun] Would download and silently install Eclipse Temurin JDK {version}, "
                      f"then set JAVA_HOME",
            "JavaHome": None,
        }

    if not _is_elevated():
        return {
            "Success": False,
            "Detail": "Not running as Administrator. Silent MSI install (writes to Program Files and "
                      "HKLM) requires elevation. Re-run this installer from an elevated ('Run as "
                      "Administrator') PowerShell/CMD prompt.",
            "JavaHome": None,
        }

    if _pending_reboot():
        log.warn("  Windows has a reboot pending (PendingFileRenameOperations/CBS RebootPending). "
                 "This is a common cause of MSI error 1603. Reboot and re-run if the install below fails.")

    # Is Temurin already registered? If it's a working install, use it as-is.
    # If it's registered but broken (files missing), uninstall it first so
    # the next /i is a genuine fresh install rather than an implicit repair
    # of a broken product (the classic cause of the 1603 seen above).
    existing = _find_registered_temurin_product(int(version))
    if existing:
        install_loc = Path(existing["InstallLocation"]) if existing["InstallLocation"] else None
        if install_loc and (install_loc / "bin" / "java.exe").is_file():
            home_path = ensure_java_home_and_path(install_loc)
            detail = (
                f"Found existing working install: {existing['DisplayName']} -> {install_loc}; "
                f"{home_path['Detail']}"
            )
            log.info(f"  {detail}")
            return {"Success": True, "Detail": detail, "JavaHome": install_loc}

        log.warn(f"  Found a broken/incomplete existing registration for '{existing['DisplayName']}' "
                 f"(ProductCode {existing['ProductCode']}) - removing it before reinstalling "
                 f"to avoid an implicit repair failure...")
        subprocess.run(
            ["msiexec", "/x", existing["ProductCode"], "/qn", "/norestart"],
            capture_output=True,
            timeout=300,
        )
        time.sleep(2)

    tmp_msi = Path(tempfile.gettempdir()) / f"temurin-jdk{version}-installer.msi"
    msi_log = Path(tempfile.gettempdir()) / f"temurin-jdk{version}-install.log"
    try:
        ensure_kd_tls_opener()

        for attempt in (1, 2):
            log.info(f"Downloading Eclipse Temurin JDK {version} from Adoptium "
                     f"({TEMURIN_INSTALLER_URL})... [attempt {attempt}/2]")
            urllib.request.urlretrieve(TEMURIN_INSTALLER_URL, tmp_msi)
            size = tmp_msi.stat().st_size
            if size >= 100_000_000:  # Temurin 21 x64 MSI is normally ~180-200 MB
                break
            log.warn(f"  Download looks truncated ({size / 1_000_000:.1f} MB); retrying...")
            tmp_msi.unlink(missing_ok=True)
        else:
            size = tmp_msi.stat().st_size if tmp_msi.exists() else 0

        if not tmp_msi.exists() or tmp_msi.stat().st_size < 1_000_000:
            return {
                "Success": False,
                "Detail": f"Download produced a suspiciously small file "
                          f"({tmp_msi.stat().st_size if tmp_msi.exists() else 0} bytes) after 2 attempts. "
                          f"Check network access to api.adoptium.net.",
                "JavaHome": None,
            }
        size_mb = tmp_msi.stat().st_size // (1024 * 1024)
        log.info(f"  Downloaded {size_mb} MB. Installing silently (this can take a minute)...")

        proc = subprocess.run(
            [
                "msiexec", "/i", str(tmp_msi), "/qn", "/norestart",
                "/l*v", str(msi_log),
                "ADDLOCAL=FeatureMain,FeatureEnvironment,FeatureJavaHome",
            ],
            capture_output=True,
            timeout=600,
        )
        # 0 = success, 3010 = success but a reboot is recommended
        if proc.returncode not in (0, 3010):
            log_tail = _tail_msi_log(msi_log) if msi_log.exists() else "(no log produced)"
            hint = ""
            if proc.returncode == 1603:
                hint = (
                    " Error 1603 is a generic MSI failure - most often: another install/Windows Update "
                    "is in progress, a reboot is pending, an existing Java Runtime install is in a broken "
                    "state, or antivirus is blocking the installer. "
                )
            return {
                "Success": False,
                "Detail": f"msiexec exited with code {proc.returncode}.{hint}"
                          f"Full log: {msi_log}\nLast lines:\n{log_tail}",
                "JavaHome": None,
            }

        java_home = find_temurin_jdk_install_dir(int(version))
        if not java_home:
            return {
                "Success": False,
                "Detail": "msiexec reported success but the JDK install directory was not found "
                          "under 'Program Files\\Eclipse Adoptium'",
                "JavaHome": None,
            }

        # Belt-and-braces: explicit machine-wide JAVA_HOME (only if not already
        # set - e.g. by the MSI's own FeatureJavaHome, or a pre-existing value),
        # plus %JAVA_HOME%\bin on PATH, and update this process's environment.
        home_path = ensure_java_home_and_path(java_home)
        if not home_path["Success"]:
            log.warn(f"  {home_path['Detail']}; the MSI's own FeatureJavaHome/FeatureEnvironment may still have set them")

        detail = f"Installed Eclipse Temurin JDK {version} -> {java_home}; {home_path['Detail']}"
        if proc.returncode == 3010:
            detail += " (a reboot is recommended for the change to apply to already-open shells)"
        log.info(f"  {detail}")
        return {"Success": True, "Detail": detail, "JavaHome": java_home}
    except Exception as e:
        return {"Success": False, "Detail": f"Exception during Java install: {e}", "JavaHome": None}
    finally:
        try:
            tmp_msi.unlink(missing_ok=True)
        except Exception:
            pass


def confirm_kd_java_for_nifi(
    dry_run: bool = False,
    non_interactive: bool = False,
    auto_install: bool = True,
) -> Dict[str, Any]:
    """
    Run the Java 21 check when NiFi is requested. If Java 21+ isn't found,
    auto-install Eclipse Temurin JDK 21 and set JAVA_HOME:
      - Interactive: prompts first (default Yes).
      - Non-interactive or auto_install=True: installs without prompting.
      - Set NiFi.AutoInstallJava: false in config to only warn instead.
    """
    check = test_kd_java_version(21)
    if check["Pass"]:
        # Java is present — still ensure JAVA_HOME + %JAVA_HOME%\bin on PATH
        jh = check.get("JavaHome") or os.environ.get("JAVA_HOME") or get_permanent_env_var("JAVA_HOME")
        path_detail = ""
        if jh and not dry_run:
            home_path = ensure_java_home_and_path(jh)
            path_detail = f"; {home_path['Detail']}"
            log.info(f"  JAVA_HOME/PATH: {home_path['Detail']}")
        elif jh and dry_run:
            path_detail = f"; [DryRun] would ensure JAVA_HOME={jh} and %JAVA_HOME%\\bin on PATH"
        return {"Name": check["Name"], "Pass": True, "Detail": check["Detail"] + path_detail}

    log.warn(check["Detail"])

    if dry_run:
        return {
            "Name": check["Name"],
            "Pass": True,
            "Detail": f"[DryRun] {check['Detail']} - would auto-install Eclipse Temurin JDK 21, "
                      f"set JAVA_HOME, and add %JAVA_HOME%\\bin to PATH",
        }

    if not auto_install:
        return {
            "Name": check["Name"],
            "Pass": False,
            "Detail": f"{check['Detail']} (NiFi.AutoInstallJava is false; not installing automatically)",
        }

    do_install = True
    if not non_interactive:
        try:
            print(
                f"{_PROMPT_YELLOW}Java 21+ is required for Apache NiFi 2.x but was not detected. "
                f"Download and install Eclipse Temurin JDK 21 now? (Y/N) [Y]: {_PROMPT_RESET}",
                end="",
                flush=True,
            )
            answer = input().strip()
            do_install = answer.upper() != "N"
        except EOFError:
            do_install = True  # non-interactive stdin: default to installing

    if not do_install:
        return {
            "Name": check["Name"],
            "Pass": False,
            "Detail": f"User declined automatic install. {check['Detail']}",
        }

    install_result = install_kd_java(dry_run=False)
    if not install_result["Success"]:
        return {
            "Name": check["Name"],
            "Pass": False,
            "Detail": f"Automatic Java install failed - {install_result['Detail']}. "
                      f"Install manually from https://adoptium.net/ and set JAVA_HOME.",
        }

    recheck = test_kd_java_version(21)
    return {
        "Name": check["Name"],
        "Pass": recheck["Pass"],
        "Detail": f"{install_result['Detail']} | {recheck['Detail']}",
    }


# ---------------------------------------------------------------------------
# Apache NiFi official binary download
# ---------------------------------------------------------------------------

NIFI_DEFAULT_VERSION = "2.10.0"
# Prefer CDN / downloads.apache.org; archive is last-resort (often much slower).
NIFI_DOWNLOAD_MIRRORS = [
    "https://dlcdn.apache.org/nifi/{version}/nifi-{version}-bin.zip",
    "https://downloads.apache.org/nifi/{version}/nifi-{version}-bin.zip",
    "https://archive.apache.org/dist/nifi/{version}/nifi-{version}-bin.zip",
]
NIFI_MIN_ZIP_BYTES = 10_000_000  # ~10 MB floor; real package is ~700–900 MB


def _nifi_probe_url(url: str, timeout: float = 8.0) -> Optional[float]:
    """
    Lightweight reachability + latency probe.
    Uses a 1-byte Range GET (works when HEAD is blocked). Returns seconds or None.
    """
    ensure_kd_tls_opener()
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(
            url,
            headers={"Range": "bytes=0-0", "User-Agent": "KD-Windows-Installer/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            if code not in (200, 206):
                return None
            resp.read(1)
        return time.perf_counter() - t0
    except Exception:
        return None


def resolve_nifi_download_url(version: str) -> str:
    """
    Pick the fastest reachable mirror for the given NiFi version.
    Probes CDN/downloads first; archive is only used if others fail.
    """
    ensure_kd_tls_opener()
    urls = [t.format(version=version) for t in NIFI_DOWNLOAD_MIRRORS]
    scored: List[tuple] = []
    log.info("Probing Apache NiFi download mirrors for best speed…")
    for url in urls:
        host = url.split("/")[2]
        latency = _nifi_probe_url(url)
        if latency is None:
            log.info(f"  mirror {host}: unreachable / timed out")
            continue
        log.info(f"  mirror {host}: ok ({latency * 1000:.0f} ms)")
        scored.append((latency, url))
    if scored:
        scored.sort(key=lambda x: x[0])
        best = scored[0][1]
        log.info(f"  selected: {best.split('/')[2]}")
        return best
    # Fall back to primary even if probes failed (some networks block Range)
    log.warn("All mirror probes failed — falling back to dlcdn.apache.org")
    return urls[0]


def _find_aria2c_exe() -> Optional[Path]:
    """Find aria2c, if installed. aria2 can use multiple connections for one file."""
    for name in ("aria2c.exe", "aria2c"):
        which = shutil.which(name)
        if which:
            return Path(which)
    common = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "aria2" / "aria2c.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "aria2" / "aria2c.exe",
    ]
    for cand in common:
        if cand.is_file():
            return cand
    return None


def _download_with_aria2c(url: str, dest: Path, resume_from: int = 0) -> bool:
    """High-speed segmented download when aria2c is available."""
    aria2 = _find_aria2c_exe()
    if not aria2:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(aria2),
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--continue=true",
        "--file-allocation=none",
        "--max-connection-per-server=8",
        "--split=8",
        "--min-split-size=4M",
        "--max-tries=5",
        "--retry-wait=2",
        "--connect-timeout=15",
        "--timeout=60",
        "--summary-interval=2",
        "--user-agent=KD-Windows-Installer/1.0",
        "--dir", str(dest.parent),
        "--out", dest.name,
        url,
    ]
    if resume_from:
        log.info(f"  Resuming high-speed download from {resume_from // (1024 * 1024)} MB…")
    else:
        log.info("  Using aria2c with 8 parallel connections for faster transfer…")
    try:
        proc = subprocess.run(cmd, check=False)
        return proc.returncode == 0 and dest.is_file() and dest.stat().st_size >= NIFI_MIN_ZIP_BYTES
    except Exception as e:
        log.warn(f"  aria2c download failed: {e}")
        return False


def _find_curl_exe() -> Optional[Path]:
    """Prefer system curl.exe (Windows 10+ ships one; much faster than urllib)."""
    for cand in (
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "curl.exe",
    ):
        if cand.is_file():
            return cand
    which = shutil.which("curl") or shutil.which("curl.exe")
    return Path(which) if which else None


def _download_with_curl(url: str, dest: Path, resume_from: int = 0) -> bool:
    """
    High-speed download via curl.exe with progress meter and optional resume.
    Returns True on success.
    """
    curl = _find_curl_exe()
    if not curl:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    # -L follow redirects, -f fail on HTTP errors, -C - resume, --retry for flaky links
    cmd = [
        str(curl),
        "-L",
        "-f",
        "--retry", "3",
        "--retry-delay", "2",
        "--connect-timeout", "20",
        "--max-time", "0",  # no overall cap (large package)
        "-A", "KD-Windows-Installer/1.0",
        "-o", str(dest),
        url,
    ]
    if resume_from > 0 and dest.is_file():
        cmd[1:1] = ["-C", "-"]  # continue at end of file
        log.info(f"  Resuming with curl from {resume_from // (1024 * 1024)} MB…")
    else:
        log.info("  Using curl.exe for faster transfer (progress below)…")
    try:
        # Let curl render its own progress bar on stderr/stdout
        proc = subprocess.run(cmd, check=False)
        return proc.returncode == 0 and dest.is_file() and dest.stat().st_size >= NIFI_MIN_ZIP_BYTES
    except Exception as e:
        log.warn(f"  curl download failed: {e}")
        return False


def _download_with_urllib(url: str, dest: Path, resume_from: int = 0) -> None:
    """
    Streaming urllib download with MB progress and optional Range resume.
    Raises on failure.
    """
    ensure_kd_tls_opener()
    headers = {"User-Agent": "KD-Windows-Installer/1.0"}
    mode = "wb"
    expected_total: Optional[int] = None
    if resume_from > 0 and dest.is_file():
        headers["Range"] = f"bytes={resume_from}-"
        mode = "ab"
        log.info(f"  Resuming urllib download from {resume_from // (1024 * 1024)} MB…")

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        code = getattr(resp, "status", None) or resp.getcode()
        if resume_from > 0 and code == 200:
            # Server ignored Range — restart full download
            mode = "wb"
            resume_from = 0
        length_hdr = resp.headers.get("Content-Length")
        if length_hdr and length_hdr.isdigit():
            expected_total = int(length_hdr) + (resume_from if code == 206 else 0)

        chunk = 1024 * 1024  # 1 MiB
        done = resume_from
        last_log = time.time()
        t0 = time.perf_counter()
        with open(dest, mode) as out:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                out.write(buf)
                done += len(buf)
                now = time.time()
                if now - last_log >= 2.0 or (expected_total and done >= expected_total):
                    elapsed = max(time.perf_counter() - t0, 0.001)
                    speed = (done - resume_from) / elapsed / (1024 * 1024)
                    if expected_total:
                        pct = min(100.0, 100.0 * done / expected_total)
                        log.info(
                            f"  … {done // (1024 * 1024)} / {expected_total // (1024 * 1024)} MB "
                            f"({pct:.0f}%) @ {speed:.1f} MB/s"
                        )
                    else:
                        log.info(f"  … {done // (1024 * 1024)} MB @ {speed:.1f} MB/s")
                    last_log = now


def download_nifi_zip(
    zip_path: str | Path,
    version: str = NIFI_DEFAULT_VERSION,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Download the official Apache NiFi binary ZIP into ZipPath.

    Optimizations vs the original urllib-only path:
      - Probe mirrors and pick the lowest-latency host (avoid slow archive by default)
      - Prefer Windows curl.exe when present (typically much faster)
      - Resume partial ``.zip.partial`` files after interruption
      - Progress logging every ~2s for urllib fallback
    """
    target_dir = Path(zip_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"nifi-{version}-bin.zip"
    dest = target_dir / filename

    if dest.is_file() and dest.stat().st_size > NIFI_MIN_ZIP_BYTES:
        return {
            "Success": True,
            "Path": dest,
            "Detail": f"Already present: {dest} ({dest.stat().st_size // (1024 * 1024)} MB)",
            "Downloaded": False,
        }

    url = resolve_nifi_download_url(version)

    if dry_run:
        return {
            "Success": True,
            "Path": dest,
            "Detail": f"[DryRun] Would download {url} → {dest}",
            "Downloaded": False,
        }

    tmp = dest.with_suffix(".zip.partial")
    resume_from = tmp.stat().st_size if tmp.is_file() else 0

    log.info(f"Downloading official Apache NiFi {version}")
    log.info(f"  Source     : {url}")
    log.info(f"  Destination: {dest}  (~700–900 MB — please wait)")
    if resume_from:
        log.info(f"  Partial file found ({resume_from // (1024 * 1024)} MB) — will resume")

    try:
        # Prefer aria2c when available because it can download one large ZIP
        # using multiple HTTP connections. Fall back to curl, then urllib.
        ok = _download_with_aria2c(url, tmp, resume_from=resume_from)
        if not ok:
            ok = _download_with_curl(url, tmp, resume_from=resume_from)
        if not ok:
            # curl/aria2 missing or failed — streaming urllib with progress
            if not _find_curl_exe() and not _find_aria2c_exe():
                log.info("  aria2c/curl not found — using Python streaming download with progress")
            else:
                log.warn("  Fast transfer failed — falling back to Python streaming download")
            # If curl left a tiny/corrupt partial, restart
            if tmp.is_file() and tmp.stat().st_size < NIFI_MIN_ZIP_BYTES // 10:
                tmp.unlink(missing_ok=True)
                resume_from = 0
            else:
                resume_from = tmp.stat().st_size if tmp.is_file() else 0
            _download_with_urllib(url, tmp, resume_from=resume_from)

        if not tmp.is_file() or tmp.stat().st_size < NIFI_MIN_ZIP_BYTES:
            size = tmp.stat().st_size if tmp.is_file() else 0
            tmp.unlink(missing_ok=True)
            return {
                "Success": False,
                "Path": None,
                "Detail": (
                    f"Download produced a suspiciously small file ({size} bytes). "
                    "Check network / mirror, or place nifi-*-bin.zip in ZipPath manually."
                ),
                "Downloaded": False,
            }

        tmp.replace(dest)
        size_mb = dest.stat().st_size // (1024 * 1024)
        log.info(f"  Download complete: {dest} ({size_mb} MB)")
        return {
            "Success": True,
            "Path": dest,
            "Detail": f"Downloaded {filename} ({size_mb} MB) from {url.split('/')[2]}",
            "Downloaded": True,
        }
    except Exception as e:
        return {
            "Success": False,
            "Path": None,
            "Detail": f"Failed to download NiFi {version}: {e}",
            "Downloaded": False,
        }


def confirm_nifi_download(
    zip_path: str | Path,
    version: str = NIFI_DEFAULT_VERSION,
    dry_run: bool = False,
    non_interactive: bool = False,
    auto_download: bool = True,
) -> Dict[str, Any]:
    """
    Ensure a suitable NiFi ZIP exists in ZipPath.

    If the ZIP is missing, download the official Apache binary automatically
    by default.  Callers that explicitly pass ``auto_download=False`` may
    still use the interactive confirmation path.
    """
    # IMPORTANT: this dependency gate is specifically for the official Apache
    # NiFi binary package. Do not treat a product-specific package such as
    # NiFiIngest_26.3.0_WINDOWS_X86_64.zip as satisfying the Apache nifi-*-bin.zip
    # dependency. The user explicitly opted in at the prompt above, so the
    # download must start immediately when the official ZIP is absent.
    target_dir = Path(zip_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    official_candidates = sorted(
        [p for p in target_dir.glob("nifi-*-bin.zip") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if official_candidates:
        z = official_candidates[0]
        return {
            "Success": True,
            "Path": z,
            "Detail": f"NiFi ZIP already present: {z.name}",
            "Downloaded": False,
            "SkippedPrompt": True,
        }

    log.warn(f"NiFi ZIP is missing from {target_dir} (expected nifi-*-bin.zip)")

    if dry_run:
        return {
            "Success": True,
            "Path": None,
            "Detail": f"[DryRun] Would offer to download official nifi-{version}-bin.zip from Apache",
            "Downloaded": False,
        }

    do_download = auto_download
    if not non_interactive and not auto_download:
        try:
            print(
                f"{_PROMPT_YELLOW}Download the official Apache NiFi {version} binary ZIP from nifi.apache.org now? (Y/N) [Y]: {_PROMPT_RESET}",
                end="",
                flush=True,
            )
            answer = input().strip()
            do_download = answer.upper() != "N"
        except EOFError:
            do_download = False

    if not do_download:
        return {
            "Success": False,
            "Path": None,
            "Detail": "User declined (or non-interactive) – place nifi-*-bin.zip in ZipPath manually",
            "Downloaded": False,
        }

    return download_nifi_zip(zip_path, version=version, dry_run=False)


# ---------------------------------------------------------------------------
# NSSM (Non-Sucking Service Manager) - Windows service wrapper for Apache NiFi.
# NiFi is not a self-registering Win32 service binary (no -install switch), so
# we wrap bin\nifi.cmd run under NSSM. The installer ships nifi/nssm.exe; if it
# is missing we also look on PATH and can download the official 2.24 release.
# Service name is always KD-NiFi (same as other KD-* components).
# ---------------------------------------------------------------------------

NSSM_VERSION = "2.24"
# Official release ZIP (contains win64/nssm.exe and win32/nssm.exe)
NSSM_DOWNLOAD_URL = "https://nssm.cc/release/nssm-2.24.zip"


def _package_root() -> Path:
    """Repo / package root (parent of the kd/ package)."""
    return Path(__file__).resolve().parent.parent


def find_nssm() -> Optional[Path]:
    """
    Locate nssm.exe in priority order:
      1. Bundled next to the installer: <repo>/nifi/nssm.exe
      2. PATH
      3. Common install locations (incl. the winget-installed NSSM.NSSM package)
    """
    bundled = _package_root() / "nifi" / "nssm.exe"
    if bundled.is_file() and bundled.stat().st_size > 50_000:
        return bundled

    import shutil
    which = shutil.which("nssm") or shutil.which("nssm.exe")
    if which:
        p = Path(which)
        if p.is_file():
            return p

    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    local_app_data = Path(os.environ.get("LocalAppData", r"C:\Users\Default\AppData\Local"))
    candidates = [
        Path(r"C:\Program Files\nssm\nssm.exe"),
        Path(r"C:\Program Files (x86)\nssm\nssm.exe"),
        Path(r"C:\Tools\nssm\nssm.exe"),
    ]
    # winget installs packages under WinGet\Packages\<PackageId>_<hash>\...
    for base in (
        local_app_data / "Microsoft" / "WinGet" / "Packages",
        program_files / "WinGet" / "Packages",
    ):
        if base.is_dir():
            try:
                for pkg_dir in base.glob("NSSM.NSSM*"):
                    candidates.extend(pkg_dir.rglob("nssm.exe"))
            except Exception:
                pass

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def test_nssm_version(nssm_path: Optional[str | Path] = None) -> Dict[str, Any]:
    """
    Verify NSSM is available and report its version, equivalent to running
    `nssm version` from an elevated shell. Used as the post-install
    confirmation step after `winget install --id NSSM.NSSM -e` or the
    bundled/downloaded fallback.
    """
    nssm = Path(nssm_path) if nssm_path else find_nssm()
    if not nssm or not nssm.is_file():
        return {
            "Name": "NSSM",
            "Pass": False,
            "Version": None,
            "Detail": "nssm.exe not found on PATH or in any known install location",
        }
    try:
        proc = subprocess.run([str(nssm), "version"], capture_output=True, text=True, timeout=15)
        out = (proc.stdout or proc.stderr or "").strip()
        return {
            "Name": "NSSM",
            "Pass": bool(out),
            "Version": out or None,
            "Detail": out if out else f"'{nssm} version' produced no output (exit {proc.returncode})",
        }
    except Exception as e:
        return {
            "Name": "NSSM",
            "Pass": False,
            "Version": None,
            "Detail": f"Could not execute '{nssm} version': {e}",
        }


def install_nssm_via_winget(dry_run: bool = False) -> Dict[str, Any]:
    """
    Install NSSM via winget, the preferred method on Windows 10/11:

        winget install --id NSSM.NSSM -e

    Falls back gracefully (returns Success: False with a clear Detail) if
    winget isn't available - e.g. older Windows builds, or Windows Sandbox/
    Server Core images that don't ship App Installer. Callers should fall
    back to the bundled/download path (ensure_nssm) in that case.
    """
    import shutil as _shutil

    winget = _shutil.which("winget")
    if not winget:
        return {
            "Success": False,
            "Path": None,
            "Detail": "winget not found on PATH - cannot install NSSM.NSSM automatically via winget",
        }

    if dry_run:
        return {
            "Success": True,
            "Path": None,
            "Detail": f"[DryRun] Would run: {winget} install --id NSSM.NSSM -e",
        }

    log.info("Installing NSSM via winget (winget install --id NSSM.NSSM -e)...")
    try:
        proc = subprocess.run(
            [winget, "install", "--id", "NSSM.NSSM", "-e",
             "--accept-source-agreements", "--accept-package-agreements", "--silent"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        # winget returns 0 on fresh install; "already installed" style messages
        # are treated as success too since the end state (NSSM present) is what matters.
        already = "already installed" in out.lower() or "no available upgrade" in out.lower()
        if proc.returncode != 0 and not already:
            return {
                "Success": False,
                "Path": None,
                "Detail": f"winget install exited with code {proc.returncode}: {out.strip()[:400]}",
            }
    except Exception as e:
        return {"Success": False, "Path": None, "Detail": f"Exception running winget: {e}"}

    found = find_nssm()
    if not found:
        return {
            "Success": False,
            "Path": None,
            "Detail": "winget reported success but nssm.exe could not be located afterwards "
                      "(PATH may need a new shell session to refresh)",
        }
    verify = test_nssm_version(found)
    detail = f"Installed via winget -> {found}"
    if verify.get("Version"):
        detail += f" ({verify['Version']})"
    return {"Success": True, "Path": found, "Detail": detail}


def ensure_nssm(dry_run: bool = False, prefer_winget: bool = True) -> Dict[str, Any]:
    """
    Ensure nssm.exe is available, in this order:
      1. Already present (bundled under nifi/nssm.exe, PATH, or a known install dir).
      2. `winget install --id NSSM.NSSM -e` (preferred on Windows 10/11 - matches
         the official NSSM winget manifest), then verified with `nssm version`.
      3. Fallback: download the official NSSM 2.24 ZIP from nssm.cc and extract
         the win64 binary into <repo>/nifi/nssm.exe.
    """
    existing = find_nssm()
    if existing:
        return {
            "Success": True,
            "Path": existing,
            "Detail": f"NSSM found: {existing}",
            "Downloaded": False,
        }

    if dry_run:
        return {
            "Success": True,
            "Path": _package_root() / "nifi" / "nssm.exe",
            "Detail": "[DryRun] Would run 'winget install --id NSSM.NSSM -e' "
                      "(falling back to nssm.cc download if winget is unavailable)",
            "Downloaded": False,
        }

    if prefer_winget:
        winget_result = install_nssm_via_winget(dry_run=False)
        if winget_result["Success"]:
            return {
                "Success": True,
                "Path": winget_result["Path"],
                "Detail": winget_result["Detail"],
                "Downloaded": True,
            }
        log.warn(f"  winget install of NSSM did not succeed ({winget_result['Detail']}); "
                 f"falling back to direct download from nssm.cc...")

    dest = _package_root() / "nifi" / "nssm.exe"
    log.info(f"NSSM not found - downloading v{NSSM_VERSION} from nssm.cc ...")
    ensure_kd_tls_opener()
    try:
        import tempfile
        import zipfile

        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / "nssm.zip"
            urllib.request.urlretrieve(NSSM_DOWNLOAD_URL, zip_path)
            if zip_path.stat().st_size < 10_000:
                return {
                    "Success": False,
                    "Path": None,
                    "Detail": f"NSSM download was too small ({zip_path.stat().st_size} bytes)",
                    "Downloaded": False,
                }
            with zipfile.ZipFile(zip_path, "r") as zf:
                # Prefer win64/nssm.exe inside nssm-2.24/
                members = [m for m in zf.namelist() if m.lower().endswith("win64/nssm.exe")]
                if not members:
                    members = [m for m in zf.namelist() if m.lower().endswith("nssm.exe")]
                if not members:
                    return {
                        "Success": False,
                        "Path": None,
                        "Detail": "nssm.exe not found inside downloaded ZIP",
                        "Downloaded": False,
                    }
                data = zf.read(members[0])
            dest.write_bytes(data)
        if dest.is_file() and dest.stat().st_size > 50_000:
            log.info(f"  NSSM ready: {dest}")
            return {
                "Success": True,
                "Path": dest,
                "Detail": f"Downloaded NSSM {NSSM_VERSION} -> {dest}",
                "Downloaded": True,
            }
        return {
            "Success": False,
            "Path": None,
            "Detail": "NSSM extract produced an unexpected file",
            "Downloaded": False,
        }
    except Exception as e:
        return {
            "Success": False,
            "Path": None,
            "Detail": f"Could not download NSSM: {e}. "
                      f"Place nssm.exe at {_package_root() / 'nifi' / 'nssm.exe'} "
                      f"or on PATH (https://nssm.cc/download).",
            "Downloaded": False,
        }

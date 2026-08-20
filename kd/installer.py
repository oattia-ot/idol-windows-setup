"""
Main orchestration: Install / Uninstall / Repair / Configure / HealthCheck.
Extraction is delegated entirely to Unzip-One.bat (native tar.exe, strips the
OpenText root folder). There is no Python-side zip extraction here; the
installer just shells out to the .bat file and checks the result.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import discovery, ini_config, service_manager, state
from .logging import ElapsedClock, format_elapsed, log

# ANSI yellow for user-facing questions / input prompts
_PROMPT_YELLOW = "\033[33m"
_PROMPT_RESET = "\033[0m"



def _deploy_nifi_cmd(component_path: Path) -> Optional[Path]:
    """
    Overwrite the extracted NiFi bin/nifi.cmd with the toolkit copy
    (kd-win-setup-python/nifi/nifi.cmd).

    The toolkit nifi.cmd is the launcher used by the NSSM service
    (AppParameters: /c nifi.cmd run, AppDirectory: <NIFI_HOME>/bin).
    """
    toolkit_root = Path(__file__).resolve().parent.parent
    src = toolkit_root / "nifi" / "nifi.cmd"
    if not src.is_file():
        log.warn(f"  Toolkit nifi.cmd not found at {src}")
        return None

    # Prefer bin/ next to an existing nifi.cmd; fall back to component root bin/
    bin_dir = None
    for cand in component_path.rglob("nifi.cmd"):
        bin_dir = cand.parent
        break
    if bin_dir is None:
        bin_dir = component_path / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)

    dest = bin_dir / "nifi.cmd"
    try:
        shutil.copy2(src, dest)
        return dest
    except Exception as e:
        log.warn(f"  Could not deploy nifi.cmd to {dest}: {e}")
        return None



def _deploy_nifi_properties_template(component_path: Path) -> Optional[Path]:
    """
    Copy toolkit nifi/conf/nifi.properties over the target NiFi conf/nifi.properties
    so KD placeholders (NIFI_WEB_HTTPS_HOST / NIFI_WEB_HTTPS_PORT / EXTRA_IP_SANS)
    are present before value substitution. Returns the destination path or None.
    """
    toolkit = Path(__file__).resolve().parent.parent
    src = toolkit / "nifi" / "conf" / "nifi.properties"
    if not src.is_file():
        return None
    # Prefer standard layout
    dest = component_path / "conf" / "nifi.properties"
    if not dest.parent.is_dir():
        # Nested Apache layout
        for candidate in component_path.rglob("nifi.properties"):
            dest = candidate
            break
        else:
            return None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return dest
    except Exception as e:
        log.warn(f"Failed to copy nifi.properties template: {e}")
        return None



def _find_ssl_root(setup_path: Optional[Path] = None) -> Optional[Path]:
    """
    Locate the SSL output directory produced by tools/generate_ssl.py.

    Canonical layout (SETUP_FOLDER = SetupPath or toolkit root):
      <SETUP_FOLDER>/ssl/
      <SETUP_FOLDER>/ssl/.idol-ssl-passwords.env
      <SETUP_FOLDER>/ssl/intermediate/nifi/keystore.p12
      <SETUP_FOLDER>/ssl/intermediate/nifi/truststore.p12
      <SETUP_FOLDER>/ssl/intermediate/certs/   (PEM reference certs)

    Preference order prioritises a tree that already contains
    intermediate/nifi/keystore.p12 (the NiFi PKCS12 keystore location).
    """
    toolkit = Path(__file__).resolve().parent.parent
    candidates: List[Path] = []

    def _add(p: Path) -> None:
        candidates.append(p)

    if setup_path is not None:
        sp = Path(str(setup_path).strip().strip('"'))
        if sp.name.lower() == "ssl":
            _add(sp)
        else:
            _add(sp / "ssl")
            # Sibling folder variants (idol-windows-setup vs idol-windows-setup-main)
            parent = sp.parent
            for sibling in ("idol-windows-setup-main", "idol-windows-setup", "kd-win-setup-python"):
                _add(parent / sibling / "ssl")

    _add(toolkit / "ssl")
    # Explicit known KD install roots — intermediate\nifi is the keystore home
    _add(Path(r"C:\KD-Setup\idol-windows-setup-main\ssl"))
    _add(Path(r"C:\KD-Setup\idol-windows-setup\ssl"))
    _add(Path(r"C:\KD-Setup\kd-win-setup-python\ssl"))

    seen: set = set()
    # Pass 1: require intermediate/nifi/keystore.p12 (authoritative)
    ranked: List[Path] = []
    for c in candidates:
        try:
            key = str(c.resolve()) if c.exists() else str(c)
        except Exception:
            key = str(c)
        if key in seen:
            continue
        seen.add(key)
        ranked.append(c)

    for c in ranked:
        try:
            if (c / "intermediate" / "nifi" / "keystore.p12").is_file():
                return c
        except Exception:
            continue

    # Pass 2: intermediate/nifi directory exists (p12 may be generated later)
    for c in ranked:
        try:
            if (c / "intermediate" / "nifi").is_dir():
                return c
        except Exception:
            continue

    # Pass 3: any ssl tree with intermediate/
    for c in ranked:
        try:
            if c.is_dir() and (c / "intermediate").is_dir():
                return c
        except Exception:
            continue
    return None


def _load_ssl_passwords(
    ssl_root: Optional[Path] = None,
    setup_path: Optional[Path] = None,
) -> Dict[str, str]:
    """
    Load KeyStore / TrustStore passwords produced by tools/generate_ssl.py.

    Primary source (per setup layout):
      <SETUP_FOLDER>/ssl/.idol-ssl-passwords.env

    Search order:
      1. IDOL_CERT_KEYSTORE_PASS / IDOL_CERT_TRUSTSTORE_PASS environment variables
      2. <ssl_root>/.idol-ssl-passwords.env          (preferred on-disk location)
      3. <SetupPath>/ssl/.idol-ssl-passwords.env
      4. <toolkit>/ssl/.idol-ssl-passwords.env
      5. <toolkit>/env/.idol-ssl-passwords.env / ssl-passwords.txt
      6. Plain-text ssl-passwords.txt next to any of the above

    File format (.env):
      IDOL_CERT_KEYSTORE_PASS=...
      IDOL_CERT_TRUSTSTORE_PASS=...

    Returns dict with keys keystore_pass, truststore_pass (empty string if unknown).
    """
    result = {"keystore_pass": "", "truststore_pass": ""}
    env_ks = (os.environ.get("IDOL_CERT_KEYSTORE_PASS") or "").strip()
    env_ts = (os.environ.get("IDOL_CERT_TRUSTSTORE_PASS") or "").strip()
    if env_ks:
        result["keystore_pass"] = env_ks
    if env_ts:
        result["truststore_pass"] = env_ts
    if result["keystore_pass"] and result["truststore_pass"]:
        return result

    toolkit = Path(__file__).resolve().parent.parent
    env_files: List[Path] = []
    # 1) Explicit ssl_root (already resolved)
    if ssl_root is not None:
        env_files.append(ssl_root / ".idol-ssl-passwords.env")
        env_files.append(ssl_root / "ssl-passwords.txt")
    # 2) SetupPath/ssl/  — canonical operator location
    if setup_path is not None:
        sp = Path(setup_path)
        env_files.append(sp / "ssl" / ".idol-ssl-passwords.env")
        env_files.append(sp / "ssl" / "ssl-passwords.txt")
        if sp.name.lower() == "ssl":
            env_files.append(sp / ".idol-ssl-passwords.env")
            env_files.append(sp / "ssl-passwords.txt")
    # 3) Toolkit defaults + known KD install roots
    env_files.extend(
        [
            toolkit / "ssl" / ".idol-ssl-passwords.env",
            toolkit / "ssl" / "ssl-passwords.txt",
            Path(r"C:\KD-Setup\idol-windows-setup-main\ssl") / ".idol-ssl-passwords.env",
            Path(r"C:\KD-Setup\idol-windows-setup\ssl") / ".idol-ssl-passwords.env",
            toolkit / "env" / ".idol-ssl-passwords.env",
            toolkit / "env" / "ssl-passwords.txt",
        ]
    )
    seen_files: set = set()
    for ef in env_files:
        try:
            key = str(ef.resolve()) if ef.exists() else str(ef)
        except Exception:
            key = str(ef)
        if key in seen_files:
            continue
        seen_files.add(key)
        if not ef.is_file():
            continue
        try:
            for line in ef.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    k = k.strip()
                    # PowerShell form: $env:IDOL_CERT_KEYSTORE_PASS = '...'
                    if k.lower().startswith("$env:"):
                        k = k[5:].strip()
                    k = k.strip()
                    v = v.strip().strip("'").strip('"')
                    if k in ("IDOL_CERT_KEYSTORE_PASS", "KeyStore password") and v and not result["keystore_pass"]:
                        result["keystore_pass"] = v
                    if k in ("IDOL_CERT_TRUSTSTORE_PASS", "TrustStore password") and v and not result["truststore_pass"]:
                        result["truststore_pass"] = v
                elif line.lower().startswith("keystore password:"):
                    v = line.split(":", 1)[1].strip()
                    if v and not result["keystore_pass"]:
                        result["keystore_pass"] = v
                elif line.lower().startswith("truststore password:"):
                    v = line.split(":", 1)[1].strip()
                    if v and not result["truststore_pass"]:
                        result["truststore_pass"] = v
        except Exception:
            continue
        if result["keystore_pass"] and result["truststore_pass"]:
            break
    return result


def _deploy_nifi_ssl_material(
    component_path: Path,
    setup_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Copy generated NiFi keystore.p12 / truststore.p12 into the target conf/
    directory and return paths + passwords for nifi.properties updates.

    Expected source layout (tools/generate_ssl.py under SETUP_FOLDER):
      <SETUP_FOLDER>/ssl/.idol-ssl-passwords.env
      <SETUP_FOLDER>/ssl/intermediate/nifi/keystore.p12
      <SETUP_FOLDER>/ssl/intermediate/nifi/truststore.p12
      <SETUP_FOLDER>/ssl/intermediate/certs/idol-nifi.cert.pem   (PEM reference)
      <SETUP_FOLDER>/ssl/intermediate/certs/idol-nifi.key.pem
      <SETUP_FOLDER>/ssl/intermediate/certs/ca-chain.cert.pem

    Destination (NiFi home):
      <NiFi>/conf/keystore.p12
      <NiFi>/conf/truststore.p12

    Returns:
      {
        "ok": bool,
        "keystore": Path|None,
        "truststore": Path|None,
        "keystore_pass": str,
        "truststore_pass": str,
        "ssl_root": Path|None,
        "detail": str,
      }
    """
    empty: Dict[str, Any] = {
        "ok": False,
        "keystore": None,
        "truststore": None,
        "keystore_pass": "",
        "truststore_pass": "",
        "ssl_root": None,
        "detail": "",
    }
    ssl_root = _find_ssl_root(setup_path)
    if ssl_root is None:
        empty["detail"] = (
            "SSL output folder not found under SetupPath/ssl or toolkit/ssl "
            "(run tools/generate_ssl.py first)"
        )
        return empty

    # Authoritative NiFi PKCS12 location (matches generate_ssl.py output):
    #   <SETUP_FOLDER>\ssl\intermediate\nifi\keystore.p12
    #   <SETUP_FOLDER>\ssl\intermediate\nifi\truststore.p12
    # Example: C:\KD-Setup\idol-windows-setup-main\ssl\intermediate\nifi\
    src_dir = ssl_root / "intermediate" / "nifi"
    src_ks = src_dir / "keystore.p12"
    src_ts = src_dir / "truststore.p12"
    certs_dir = ssl_root / "intermediate" / "certs"
    if not src_ks.is_file() or not src_ts.is_file():
        empty["ssl_root"] = ssl_root
        empty["detail"] = (
            f"NiFi PKCS12 files missing under {src_dir} "
            f"(expected keystore.p12 + truststore.p12 from generate_ssl.py; "
            f"certs dir present={certs_dir.is_dir()})"
        )
        return empty

    # Resolve conf/ next to nifi.properties when possible
    conf_dir = component_path / "conf"
    if not conf_dir.is_dir():
        for props in component_path.rglob("nifi.properties"):
            conf_dir = props.parent
            break
    try:
        conf_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        empty["ssl_root"] = ssl_root
        empty["detail"] = f"Cannot create conf dir {conf_dir}: {e}"
        return empty

    dest_ks = conf_dir / "keystore.p12"
    dest_ts = conf_dir / "truststore.p12"
    try:
        shutil.copy2(src_ks, dest_ks)
        shutil.copy2(src_ts, dest_ts)
    except Exception as e:
        empty["ssl_root"] = ssl_root
        empty["detail"] = f"Failed to copy PKCS12 files to {conf_dir}: {e}"
        return empty

    passwords = _load_ssl_passwords(ssl_root=ssl_root, setup_path=setup_path)
    return {
        "ok": True,
        "keystore": dest_ks,
        "truststore": dest_ts,
        "keystore_pass": passwords.get("keystore_pass") or "",
        "truststore_pass": passwords.get("truststore_pass") or "",
        "ssl_root": ssl_root,
        "detail": (
            f"keystore+truststore copied from {src_dir} -> {conf_dir} "
            f"(passwords from {ssl_root / '.idol-ssl-passwords.env'})"
        ),
    }


def _nifi_security_property_updates(ssl_material: Dict[str, Any]) -> Dict[str, str]:
    """
    Build nifi.security.* property updates from deployed SSL material.
    Paths are relative to NiFi home (./conf/...) as required by NiFi.
    Passwords come from generate_ssl.py output; keyPasswd matches keystorePasswd.
    """
    updates: Dict[str, str] = {
        "nifi.security.keystore": "./conf/keystore.p12",
        "nifi.security.keystoreType": "PKCS12",
        "nifi.security.truststore": "./conf/truststore.p12",
        "nifi.security.truststoreType": "PKCS12",
    }
    ks_pass = (ssl_material.get("keystore_pass") or "").strip()
    ts_pass = (ssl_material.get("truststore_pass") or "").strip()
    if ks_pass:
        updates["nifi.security.keystorePasswd"] = ks_pass
        updates["nifi.security.keyPasswd"] = ks_pass
    if ts_pass:
        updates["nifi.security.truststorePasswd"] = ts_pass
    return updates


def _set_nifi_single_user_credentials(
    component_path: Path,
    username: str,
    password: str,
) -> Dict[str, Any]:
    """
    Apply fixed UI login credentials for NiFi single-user mode.

    Prefer calling the Java class **directly** (bypassing nifi.cmd):

        java -cp "lib/bootstrap/*" -Dnifi.properties.file.path=conf/nifi.properties
             org.apache.nifi.authentication.single.user.command.SetSingleUserCredentials
             <username> <password>

    Reason: Apache NiFi's Windows nifi.cmd has a long-standing bug
    (NIFI-14133 / NIFI-9500) where:

        set "CREDENTIALS= "%~2 " "%~3 ""

    fails to forward username/password, so the Java tool always reports:

        Unexpected number of arguments [0]
        Usage: SetSingleUserCredentials <username> <password>

    Working directory is NIFI_HOME (parent of bin/), not bin/.
    Password should be at least 12 characters (NiFi requirement).
    """
    import os

    username = (username or "").strip()
    password = (password or "").strip()
    if not username or not password:
        return {
            "Success": True,
            "Skipped": True,
            "Detail": "NiFi.Username / NiFi.Password not set - leaving single-user credentials unchanged",
        }

    if len(password) < 12:
        return {
            "Success": False,
            "Skipped": False,
            "Detail": (
                f"NiFi password must be at least 12 characters (got {len(password)}). "
                "Update NiFi.Password in config and retry."
            ),
        }

    # Locate nifi.cmd only to resolve NIFI_HOME / bin layout
    nifi_cmd = None
    for cand in component_path.rglob("nifi.cmd"):
        nifi_cmd = cand
        break
    if nifi_cmd is None or not nifi_cmd.is_file():
        return {
            "Success": False,
            "Skipped": False,
            "Detail": f"nifi.cmd not found under {component_path}",
        }

    nifi_home = nifi_cmd.parent.parent  # .../NiFi/bin/nifi.cmd -> .../NiFi
    conf_dir = nifi_home / "conf"
    props = conf_dir / "nifi.properties"
    bootstrap_lib = nifi_home / "lib" / "bootstrap"

    if not props.is_file():
        return {
            "Success": False,
            "Skipped": False,
            "Detail": f"nifi.properties not found at {props}",
        }

    # Resolve java.exe
    java_exe = None
    java_home = (
        os.environ.get("JAVA_HOME")
        or os.environ.get("JDK_HOME")
        or ""
    ).strip().strip('"')
    if java_home:
        candidate = Path(java_home) / "bin" / "java.exe"
        if candidate.is_file():
            java_exe = candidate
    if java_exe is None:
        # fall back to PATH
        import shutil as _shutil
        which = _shutil.which("java") or _shutil.which("java.exe")
        if which:
            java_exe = Path(which)
    if java_exe is None or not java_exe.is_file():
        return {
            "Success": False,
            "Skipped": False,
            "Detail": "java.exe not found (set JAVA_HOME to a JDK 21 install)",
        }

    # Classpath: bootstrap jars + conf (matches Apache workaround / nifi.sh)
    # NiFi 2.x may also need lib/* entries for the single-user command class.
    cp_parts = []
    if bootstrap_lib.is_dir():
        cp_parts.append(str(bootstrap_lib / "*"))
    # Broader fallback used by some NiFi 2.x layouts
    lib_dir = nifi_home / "lib"
    if lib_dir.is_dir():
        cp_parts.append(str(lib_dir / "*"))
    cp_parts.append(str(conf_dir))
    classpath = ";".join(cp_parts)

    main_class = (
        "org.apache.nifi.authentication.single.user.command.SetSingleUserCredentials"
    )
    cmd = [
        str(java_exe),
        "-cp", classpath,
        f"-Dnifi.properties.file.path={props}",
        main_class,
        username,
        password,
    ]

    log.info(
        f"  Setting NiFi single-user UI credentials before service start "
        f"(user='{username}') ..."
    )
    log.info(
        f"  Command: java -cp <nifi lib> "
        f"-Dnifi.properties.file.path=conf/nifi.properties "
        f"SetSingleUserCredentials \"{username}\" \"***\""
    )
    log.info(f"  Working directory (NIFI_HOME): {nifi_home}")
    log.info(f"  JAVA: {java_exe}")

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(nifi_home),
            capture_output=True,
            text=True,
            timeout=120,
            shell=False,
        )
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        out_lower = out.lower()
        usage_failure = any(
            marker in out_lower
            for marker in (
                "unexpected number of arguments",
                "usage: setsingleusercredentials",
                "could not find or load main class",
                "classnotfoundexception",
            )
        )
        if proc.returncode == 0 and not usage_failure:
            log.info(
                f"  NiFi single-user credentials applied for user '{username}' "
                f"(login-identity-providers.xml updated)"
            )
            if out:
                for line in out.splitlines()[-8:]:
                    if line.strip():
                        log.info(f"    {line.rstrip()}")
            return {
                "Success": True,
                "Skipped": False,
                "Detail": f"Single-user credentials set for user '{username}'",
            }

        # Fallback: try via nifi.cmd with a helper .cmd that preserves %1 %2
        # (still fails on buggy nifi.cmd CREDENTIALS= line, but covers older layouts)
        if "could not find or load main class" in out_lower or "classnotfoundexception" in out_lower:
            log.warn(
                "  Direct Java invocation could not load SetSingleUserCredentials; "
                "trying nifi.cmd helper..."
            )
            helper = nifi_cmd.parent / "_kd_set_nifi_creds.cmd"
            helper_body = (
                "@echo off\r\n"
                "setlocal DisableDelayedExpansion\r\n"
                f'cd /d "{nifi_cmd.parent}"\r\n'
                "call nifi.cmd set-single-user-credentials %*\r\n"
                "exit /b %ERRORLEVEL%\r\n"
            )
            helper.write_text(helper_body, encoding="ascii")
            try:
                proc2 = subprocess.run(
                    ["cmd.exe", "/V:OFF", "/d", "/c", str(helper), username, password],
                    cwd=str(nifi_cmd.parent),
                    capture_output=True,
                    text=True,
                    timeout=120,
                    shell=False,
                )
                out2 = ((proc2.stdout or "") + "\n" + (proc2.stderr or "")).strip()
                out2_lower = out2.lower()
                usage2 = any(
                    m in out2_lower
                    for m in (
                        "unexpected number of arguments",
                        "usage: setsingleusercredentials",
                    )
                )
                if proc2.returncode == 0 and not usage2:
                    log.info(
                        f"  NiFi single-user credentials applied for user '{username}' "
                        f"(via nifi.cmd helper)"
                    )
                    return {
                        "Success": True,
                        "Skipped": False,
                        "Detail": f"Single-user credentials set for user '{username}'",
                    }
                out = out + "\n--- nifi.cmd helper ---\n" + out2
                proc = proc2
                usage_failure = usage2 or usage_failure
            finally:
                try:
                    helper.unlink()
                except OSError:
                    pass

        detail = f"set-single-user-credentials exit {proc.returncode}"
        if usage_failure:
            detail = (
                "set-single-user-credentials failed "
                "(NiFi Windows nifi.cmd bug NIFI-14133 and/or classpath issue)"
            )
        if out:
            detail += f": {out[-500:]}"
        log.warn(f"  NiFi set-single-user-credentials failed: {detail}")
        return {"Success": False, "Skipped": False, "Detail": detail}
    except subprocess.TimeoutExpired:
        log.warn("  NiFi set-single-user-credentials timed out after 120s")
        return {
            "Success": False,
            "Skipped": False,
            "Detail": "set-single-user-credentials timed out after 120s",
        }
    except Exception as e:
        log.warn(f"  NiFi set-single-user-credentials failed: {e}")
        return {
            "Success": False,
            "Skipped": False,
            "Detail": f"set-single-user-credentials failed: {e}",
        }



def _nifi_install_service_enabled(config: Dict[str, Any]) -> bool:
    """
    Whether to register KD-NiFi as a Windows service.
    Global InstallService is the master switch; NiFi.InstallService can
    opt out of service registration for NiFi only (still extracts/configures).
    """
    if not config.get("InstallService", True):
        return False
    nifi_cfg = config.get("NiFi") or {}
    # Default True when key omitted - installing NiFi as a service is the
    # normal path when InstallService is true and NiFi is in Components.
    return bool(nifi_cfg.get("InstallService", True))


def _update_properties_file(
    file_path: Path,
    updates: Dict[str, str],
    no_backup: bool = False,
) -> bool:
    """
    Update key=value lines in a Java-style properties file (nifi.properties,
    bootstrap.conf, etc.). Creates a timestamped backup by default.
    Keys that do not exist are appended at the end.
    """
    if not file_path.is_file():
        return False
    try:
        if not no_backup:
            ini_config.backup_kd_file(file_path)
        content = file_path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        found = set()
        new_lines: List[str] = []
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("#") or "=" not in line:
                new_lines.append(line)
                continue
            key = line.split("=", 1)[0].strip()
            if key in updates:
                nl = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
                prefix = line[: len(line) - len(line.lstrip())]
                new_lines.append(f"{prefix}{key}={updates[key]}{nl}")
                found.add(key)
            else:
                new_lines.append(line)
        for key, val in updates.items():
            if key not in found:
                new_lines.append(f"{key}={val}\r\n")
        tmp = file_path.with_suffix(file_path.suffix + ".tmp")
        tmp.write_text("".join(new_lines), encoding="utf-8")
        tmp.replace(file_path)
        return True
    except Exception as e:
        log.warn(f"Failed to update properties file {file_path}: {e}")
        return False


def _unblock_file(path: Path) -> None:
    """Best-effort removal of Mark-of-the-Web for a single file (Windows only)."""
    try:
        # Zone.Identifier alternate data stream - no PowerShell spawn per file
        ads = Path(str(path) + ":Zone.Identifier")
        try:
            ads.unlink(missing_ok=True)  # type: ignore[call-arg]
        except TypeError:
            if ads.exists():
                ads.unlink()
        except OSError:
            pass
    except Exception:
        pass


def _unblock_tree(root: Path) -> None:
    """
    Bulk Mark-of-the-Web removal for an extracted tree.
    One PowerShell call for the whole folder (not one process per file -
    per-file Unblock-File made large extracts appear stuck for minutes).
    """
    if not root.is_dir():
        return
    try:
        log.info(f"  Unblocking Mark-of-the-Web under {root.name}...")
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f'Get-ChildItem -LiteralPath "{root}" -Recurse -File -ErrorAction SilentlyContinue '
                f"| Unblock-File -ErrorAction SilentlyContinue",
            ],
            capture_output=True,
            timeout=300,
        )
    except Exception:
        # Fallback: only top-level binaries
        try:
            for f in root.glob("*.exe"):
                _unblock_file(f)
            for f in root.glob("*.cmd"):
                _unblock_file(f)
        except Exception:
            pass


def _force_rmtree(path: Path, retries: int = 5, delay: float = 1.5) -> None:
    """
    Robust recursive delete for Windows.
    Clears read-only / hidden / system attributes, retries on Access Denied
    (common with OpenText license id.dat and uid files still briefly locked),
    then falls back to ``rmdir /s /q``.
    """
    import os
    import stat

    def _onerror(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except Exception:
            pass

    path = Path(path)
    if not path.exists():
        return

    # Clear attributes on the whole tree first (best-effort)
    try:
        if os.name == "nt":
            subprocess.run(
                ["attrib", "-R", "-S", "-H", str(path / "*"), "/S", "/D"],
                capture_output=True,
                timeout=90,
            )
            subprocess.run(
                ["attrib", "-R", "-S", "-H", str(path)],
                capture_output=True,
                timeout=15,
            )
    except Exception:
        pass

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            shutil.rmtree(path, onerror=_onerror)
            if not path.exists():
                return
        except Exception as e:
            last_err = e
        # Fallback: walk and delete files one by one
        try:
            for root, dirs, files in os.walk(str(path), topdown=False):
                for name in files:
                    fp = Path(root) / name
                    try:
                        os.chmod(fp, stat.S_IWRITE | stat.S_IREAD)
                        fp.unlink(missing_ok=True)
                    except Exception:
                        pass
                for name in dirs:
                    dp = Path(root) / name
                    try:
                        os.chmod(dp, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
                        dp.rmdir()
                    except Exception:
                        pass
            try:
                path.rmdir()
            except Exception:
                pass
            if not path.exists():
                return
        except Exception as e:
            last_err = e

        # Windows native recursive delete (handles some locks shutil cannot)
        if os.name == "nt" and path.exists():
            try:
                proc = subprocess.run(
                    ["cmd.exe", "/c", "rmdir", "/s", "/q", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if not path.exists():
                    return
                if proc.returncode != 0 and (proc.stderr or proc.stdout):
                    last_err = (proc.stderr or proc.stdout or "").strip() or last_err
            except Exception as e:
                last_err = e

        if attempt < retries:
            time.sleep(delay)

    if path.exists():
        raise OSError(f"Could not fully delete {path}: {last_err}")


def _resolve_component_folder(base_path: Path, component: str) -> Optional[Path]:
    """
    Locate the on-disk folder for a configured component under BasePath.
    Prefer the exact name; fall back to case-insensitive match so Uninstall
    still finds folders created with different casing.
    """
    exact = base_path / component
    if exact.exists():
        return exact
    if not base_path.is_dir():
        return exact  # caller treats non-existent as already gone
    target = component.lower()
    try:
        for child in base_path.iterdir():
            if child.is_dir() and child.name.lower() == target:
                return child
    except Exception:
        pass
    return exact

def _extract_timeout_seconds(zip_path: Path) -> int:
    """
    Size-based extract timeout. Large OpenText packages (1–4+ GB) can take
    20–40+ minutes on slow disks; a fixed 600s caused false timeouts / 'stuck'.
    Estimate ~3 MB/s worst-case + 5 min headroom; clamp 10 min .. 2 hours.
    """
    try:
        size_mb = zip_path.stat().st_size / (1024 * 1024)
    except OSError:
        size_mb = 500.0
    # seconds ≈ size_mb / 3 + 300, clamped
    estimated = int(size_mb / 3.0) + 300
    return max(600, min(7200, estimated))


def expand_kd_zip_native(zip_path: str | Path, destination_path: str | Path) -> None:
    """
    Extract a single ZIP by shelling out to Unzip-One.bat (native tar.exe,
    strips the OpenText root folder). All extraction logic lives in the
    .bat file itself; this is just a thin, checked subprocess call.

    Live stdout/stderr is streamed to the log so large ZIPs do not appear
    'stuck'. Timeout scales with ZIP size (up to 2 hours).
    """
    zip_path = Path(zip_path).resolve()
    dest = Path(destination_path).resolve()

    if not zip_path.is_file():
        raise FileNotFoundError(f"ZIP not found: {zip_path}")

    script_root = Path(__file__).resolve().parent.parent
    unzip_bat = script_root / "Unzip-One.bat"
    if not unzip_bat.is_file():
        raise FileNotFoundError(f"Unzip-One.bat not found in {script_root}")

    try:
        size_mb = zip_path.stat().st_size / (1024 * 1024)
    except OSError:
        size_mb = 0.0
    timeout = _extract_timeout_seconds(zip_path)
    log.info(
        f"  Extracting via Unzip-One.bat (native tar.exe) -> {dest} "
        f"[{size_mb:.1f} MB, timeout {timeout}s / {timeout // 60} min]"
    )
    log.info("  (large packages can take several minutes - progress lines appear below)")

    # Nested clock so extract alone shows its own elapsed ticks
    with ElapsedClock(f"Extract {zip_path.name}", interval=20.0):
        lines: list[str] = []
        try:
            proc = subprocess.Popen(
                [str(unzip_bat), str(zip_path), str(dest)],
                cwd=str(script_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            deadline = time.time() + timeout
            while True:
                if time.time() > deadline:
                    proc.kill()
                    try:
                        proc.wait(timeout=30)
                    except Exception:
                        pass
                    raise TimeoutError(
                        f"Extraction timed out after {timeout}s for '{zip_path.name}' "
                        f"({size_mb:.1f} MB). Re-run Install (resume) or extract manually "
                        f"with Unzip-One.bat, then continue."
                    )
                line = proc.stdout.readline()
                if line:
                    text = line.rstrip()
                    if text:
                        lines.append(text)
                        log.info(f"  | {text}")
                elif proc.poll() is not None:
                    rest = proc.stdout.read()
                    if rest:
                        for part in rest.splitlines():
                            part = part.rstrip()
                            if part:
                                lines.append(part)
                                log.info(f"  | {part}")
                    break
                else:
                    time.sleep(0.2)
            returncode = proc.returncode if proc.returncode is not None else -1
        except TimeoutError:
            raise
        except Exception as e:
            raise RuntimeError(
                f"Failed to run Unzip-One.bat for '{zip_path.name}': {e}"
            ) from e

        if returncode != 0:
            tail = "\n".join(lines[-30:]) if lines else "(no output)"
            raise RuntimeError(
                f"Unzip-One.bat exited with code {returncode} for '{zip_path.name}'.\n{tail}"
            )

        if not dest.is_dir():
            raise RuntimeError(
                f"Unzip-One.bat reported success but destination missing: {dest}"
            )
        has_any = False
        try:
            for child in dest.iterdir():
                if child.is_file() or (
                    child.is_dir() and child.name.lower() != "_tmp_extract"
                ):
                    has_any = True
                    break
        except Exception:
            has_any = False
        if not has_any:
            raise RuntimeError(
                f"Unzip-One.bat reported success but no files were found in {dest}."
            )
        log.info(f"  Unzip-One.bat completed for {zip_path.name}")


def _folder_has_content(path: Path) -> bool:
    """
    True if path exists and contains at least one file.
    Prefer shallow checks first so multi-GB trees do not block on full rglob.
    """
    if not path.is_dir():
        return False
    try:
        for child in path.iterdir():
            if child.is_file():
                return True
            if child.is_dir() and child.name.lower() not in ("_tmp_extract",):
                # one level down is enough for OpenText layout
                try:
                    for grand in child.iterdir():
                        if grand.is_file() or grand.is_dir():
                            return True
                except Exception:
                    continue
        # last resort (small trees / odd layouts)
        return any(p.is_file() for p in path.rglob("*"))
    except Exception:
        return False


def _nifi_is_extracted(component_path: Path) -> bool:
    """
    True only when a real Apache NiFi binary tree is present.

    ``NiFi/extensions`` alone (created by an early NAR copy) must NOT count
    as extracted — otherwise ``nifi-*-bin.zip`` is skipped and NARs are
    written into an incomplete tree with no ``bin/nifi.cmd``.
    """
    if not component_path.is_dir():
        return False
    # Standard layout after Unzip-One strips the top-level folder:
    #   <BasePath>/NiFi/bin/nifi.cmd
    #   <BasePath>/NiFi/conf/nifi.properties
    if (component_path / "bin" / "nifi.cmd").is_file():
        return True
    if (component_path / "conf" / "nifi.properties").is_file():
        return True
    # Nested layout (top-level folder not stripped): .../nifi-2.x.y/bin/nifi.cmd
    try:
        for cmd in component_path.rglob("nifi.cmd"):
            if cmd.is_file() and cmd.parent.name.lower() == "bin":
                return True
    except Exception:
        pass
    return False


def _script_root() -> Path:
    return Path(__file__).resolve().parent.parent


# Components that ship toolkit .cfg templates under config/cfg/<name>/
_CFG_TEMPLATE_COMPONENTS = frozenset(
    {
        "content",
        "community",
        "agentstore",
        "category",
        "qms",
        "qmsagentstore",
        "statsserver",
        "answerbankagentstore",
        "conversationagentstore",
        "answerserver",
    }
)

# Components assembled from Content (no own ZIP) - Agentstore-style engines
_AGENTSTORE_LIKE = frozenset(
    {
        "Agentstore",
        "QMSAgentStore",
        "AnswerBankAgentStore",
        "ConversationAgentStore",
    }
)

# Package-only entries that may appear in Components (e.g. from the config
# dashboard ZIP panel) but are NOT core KD services. They provide artifacts
# (NiFi connector .nar files) and must never go through .cfg configure or
# Windows service install.
_PACKAGE_ONLY_COMPONENTS = frozenset(
    {
        "nifiingest",
    }
)


def _is_package_only_component(component: str) -> bool:
    return (component or "").strip().lower() in _PACKAGE_ONLY_COMPONENTS


def _open_component_firewall(component: str, config: Dict[str, Any], dry_run: bool = False) -> None:
    """
    Open Windows Firewall for NiFi / Find (and any Ports.* entry) so external
    clients can reach the service. Idempotent; safe to call on resume/start.
    """
    name = (component or "").strip().lower()
    ports_map = config.get("Ports") or {}
    port = None
    if name == "nifi":
        nifi_cfg = config.get("NiFi") or {}
        port = nifi_cfg.get("WebHttpsPort") or ports_map.get("NiFi") or "8443"
    elif name == "find":
        find_cfg = config.get("Find") or {}
        port = find_cfg.get("ServerPort") or ports_map.get("Find") or "8080"
    elif name == "licenseserver":
        port = config.get("LicensePort") or ports_map.get("LicenseServer")
    else:
        port = ports_map.get(component) or ports_map.get(component.strip())
    if port is None or str(port).strip() == "":
        return
    try:
        service_manager.open_kd_firewall_ports(
            port, component=component, dry_run=dry_run
        )
    except Exception as e:
        log.warn(f"  Firewall open for {component} port {port} failed: {e}")



def _stage_nars_from_nifiingest_zip(
    zip_path: str | Path,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Find a NiFiIngest_*.zip under ZipPath and extract every *.nar member into
    <SetupPath>/nifi/nifi-connectors (source staging for NiFi extensions).

    NiFiIngest is not a deployable KD component — it only supplies connector
    NARs used when NiFi is installed.
    """
    zip_dir = Path(zip_path) if zip_path else Path()
    dest = Path(__file__).resolve().parent.parent / "nifi" / "nifi-connectors"
    if not zip_dir.is_dir():
        return {
            "Success": False,
            "Count": 0,
            "Detail": f"ZipPath not found: {zip_dir}",
        }

    candidates = sorted(
        [
            p
            for p in zip_dir.iterdir()
            if p.is_file()
            and p.suffix.lower() == ".zip"
            and "nifiingest" in p.name.lower()
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return {
            "Success": True,
            "Count": 0,
            "Skipped": True,
            "Detail": f"No NiFiIngest_*.zip found under {zip_dir} (optional)",
        }

    zip_file = candidates[0]
    if dry_run:
        return {
            "Success": True,
            "Count": 0,
            "Detail": f"[DryRun] Would extract *.nar from {zip_file.name} -> {dest}",
        }

    try:
        dest.mkdir(parents=True, exist_ok=True)
        extracted = 0
        with zipfile.ZipFile(zip_file, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                base = Path(name).name
                if not base.lower().endswith(".nar"):
                    continue
                # Flatten: always write basename into nifi-connectors/
                target = dest / base
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                extracted += 1
        return {
            "Success": True,
            "Count": extracted,
            "Detail": f"Staged {extracted} .nar file(s) from {zip_file.name} -> {dest}",
            "Zip": str(zip_file),
        }
    except Exception as e:
        return {
            "Success": False,
            "Count": 0,
            "Detail": f"Failed to stage NARs from {zip_file.name}: {e}",
        }




def _copy_nifi_nar_extensions(base_path: Path, dry_run: bool = False) -> Dict[str, Any]:
    """
    After (re)extracting NiFi, copy every staged *.nar connector from the
    toolkit's source folder:
        <SetupPath>\\nifi\\nifi-connectors
    into the NiFi extensions folder NiFi actually loads from:
        <BasePath>\\NiFi\\extensions

    Mirrors the Config Dashboard's "Copy source -> target" NiFi Connectors
    action, so connectors staged there are restored automatically after a
    fresh overwrite-extract instead of requiring a manual copy every time.
    A missing/empty source folder is not an error - just nothing to copy.

    Refuses to create ``NiFi/extensions`` unless a real NiFi binary tree is
    already present (``bin/nifi.cmd``). That prevents NAR copy from running
    *before* ``nifi-*-bin.zip`` extraction and leaving a partial tree that
    later skips extraction.
    """
    src = _script_root() / "nifi" / "nifi-connectors"
    nifi_root = base_path / "NiFi"
    dest = nifi_root / "extensions"

    if not _nifi_is_extracted(nifi_root):
        return {
            "Success": False,
            "Count": 0,
            "Detail": (
                f"NiFi is not extracted under {nifi_root} (no bin/nifi.cmd). "
                "Extract nifi-*-bin.zip first, then copy NAR connectors."
            ),
        }

    if not src.is_dir():
        return {"Success": True, "Count": 0, "Detail": f"No connector source folder at {src} (nothing to copy)"}

    nars = sorted(src.glob("*.nar"))
    if not nars:
        return {"Success": True, "Count": 0, "Detail": f"No .nar files staged in {src} (nothing to copy)"}

    if dry_run:
        return {
            "Success": True,
            "Count": len(nars),
            "Detail": f"[DryRun] Would copy {len(nars)} .nar file(s) {src} -> {dest}",
        }

    try:
        dest.mkdir(parents=True, exist_ok=True)
        copied = []
        errors = []
        for p in nars:
            try:
                shutil.copy2(p, dest / p.name)
                copied.append(p.name)
            except Exception as e:
                errors.append(f"{p.name}: {e}")
        if errors:
            return {
                "Success": False,
                "Count": len(copied),
                "Detail": f"Copied {len(copied)}/{len(nars)} .nar file(s) -> {dest}; errors: {'; '.join(errors)}",
            }
        log.info(f"  Copied {len(copied)} .nar file(s) -> {dest}: {', '.join(copied)}")
        return {
            "Success": True,
            "Count": len(copied),
            "Detail": f"Copied {len(copied)} .nar file(s) -> {dest}",
        }
    except Exception as e:
        return {"Success": False, "Count": 0, "Detail": f"Failed to copy .nar files to {dest}: {e}"}


def _stage_nifi_nars_from_component(component_path: Path, dry_run: bool = False) -> Dict[str, Any]:
    """
    After (re)extracting a component ZIP (e.g. Content), stage every *.nar
    connector found under that component's own "nifi" subfolder into the
    toolkit's connector staging folder:
        <ComponentPath>\\\\nifi\\\\*.nar  ->  <SetupPath>\\\\nifi\\\\nifi-connectors

    This mirrors the NiFiIngest ZIP workflow (extract *.nar -> nifi-connectors),
    but the source here is a "nifi" subfolder shipped inside the component's
    own ZIP rather than a separate NiFiIngest_*.zip. Staged connectors are
    later picked up by _copy_nifi_nar_extensions() and copied into
    <BasePath>\\\\NiFi\\\\extensions the same way as any other staged NAR.
    A missing/empty "nifi" subfolder is not an error - just nothing to stage.
    """
    src = component_path / "nifi"
    dest = _script_root() / "nifi" / "nifi-connectors"

    if not src.is_dir():
        return {"Success": True, "Count": 0, "Detail": f"No nifi subfolder at {src} (nothing to stage)"}

    nars = sorted(src.rglob("*.nar"))
    if not nars:
        return {"Success": True, "Count": 0, "Detail": f"No .nar files found in {src} (nothing to stage)"}

    if dry_run:
        return {
            "Success": True,
            "Count": len(nars),
            "Detail": f"[DryRun] Would stage {len(nars)} .nar file(s) {src} -> {dest}",
        }

    try:
        dest.mkdir(parents=True, exist_ok=True)
        copied = []
        errors = []
        for p in nars:
            try:
                shutil.copy2(p, dest / p.name)
                copied.append(p.name)
            except Exception as e:
                errors.append(f"{p.name}: {e}")
        if errors:
            return {
                "Success": False,
                "Count": len(copied),
                "Detail": f"Staged {len(copied)}/{len(nars)} .nar file(s) -> {dest}; errors: {'; '.join(errors)}",
            }
        log.info(f"  Staged {len(copied)} .nar file(s) -> {dest}: {', '.join(copied)}")
        return {
            "Success": True,
            "Count": len(copied),
            "Detail": f"Staged {len(copied)} .nar file(s) -> {dest}",
        }
    except Exception as e:
        return {"Success": False, "Count": 0, "Detail": f"Failed to stage .nar files to {dest}: {e}"}


def _deploy_find_config_json(
    component_path: Path,
    home_dir_name: str = "home",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    After Find ZIP extraction, copy toolkit config/cfg/find/config.json into
    <BasePath>/Find/<home>/config.json (overwriting if present).

    Sets defaultLogin (e.g. admin/admin) and backend host/port defaults before
    the service starts, so operators are not stuck with a random password.
    """
    src = _script_root() / "config" / "cfg" / "find" / "config.json"
    if not src.is_file():
        return {
            "Success": False,
            "Detail": f"Toolkit Find config not found at {src}",
        }

    home_dir = component_path / (home_dir_name or "home")
    dest = home_dir / "config.json"

    if dry_run:
        return {
            "Success": True,
            "Detail": f"[DryRun] Would copy {src} -> {dest}",
        }

    try:
        home_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        log.info(f"  Deployed Find config.json -> {dest}")
        return {
            "Success": True,
            "Detail": str(dest),
        }
    except Exception as e:
        return {
            "Success": False,
            "Detail": f"Failed to copy Find config.json: {e}",
        }


def _deploy_cfg_templates(component: str, component_path: Path, dry_run: bool = False) -> int:
    """
    After ZIP extraction, overwrite the extracted component root with the
    toolkit cfg templates from config/cfg/<component>/ (files + subdirs).

    For AnswerServer this copies answerserver.cfg *and* the entire rag/
    folder (scripts, prompts, tokenizer cache, etc.) into the component root
    so relative paths in the cfg (./rag/...) resolve correctly.

    Returns the number of top-level items (files + dirs) successfully deployed
    (0 if no templates exist for component).
    """
    key = component.lower()
    if key not in _CFG_TEMPLATE_COMPONENTS:
        return 0

    src_dir = _script_root() / "config" / "cfg" / key
    if not src_dir.is_dir():
        log.info(f"  No toolkit cfg templates at {src_dir} (skipping overwrite)")
        return 0

    items = [p for p in src_dir.iterdir() if not p.name.startswith(".")]
    if not items:
        log.info(f"  Toolkit cfg template folder empty: {src_dir}")
        return 0

    if dry_run:
        for src in items:
            kind = "dir" if src.is_dir() else "file"
            log.info(
                f"  [DryRun] Would deploy cfg {kind}: {src.name} -> {component_path / src.name}"
            )
        return len(items)

    component_path.mkdir(parents=True, exist_ok=True)
    count = 0
    for src in items:
        dest = component_path / src.name
        try:
            if src.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                # Ignore bytecode caches and other non-runtime noise
                shutil.copytree(
                    src,
                    dest,
                    ignore=shutil.ignore_patterns(
                        "__pycache__", "*.pyc", "*.pyo", ".git", ".DS_Store"
                    ),
                )
                log.info(f"  Deployed cfg folder from toolkit: {src.name}/ -> {dest}")
            else:
                shutil.copy2(src, dest)
                log.info(f"  Overwrote cfg from toolkit: {src.name} -> {dest}")
            count += 1
        except Exception as e:
            log.warn(f"  Failed to deploy {src.name} -> {dest}: {e}")
    if count:
        log.step_result(
            f"Deploy cfg templates for {component}",
            True,
            f"{count} item(s) from config/cfg/{key}/",
        )
    return count


def _find_autpassword(component_path: Path) -> Optional[Path]:
    """Locate autpassword.exe under the Community extract tree."""
    for name in ("autpassword.exe", "autpassword"):
        direct = component_path / name
        if direct.is_file():
            return direct
    for p in component_path.rglob("autpassword.exe"):
        if p.is_file():
            return p
    for p in component_path.rglob("autpassword"):
        if p.is_file():
            return p
    return None


def _ensure_community_aes_key(
    component_path: Path,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Ensure Community has an AES key file (aes.key) and point
    community.cfg [Security] SecurityInfoKeys at it.

    If aes.key is missing, run the OpenText helper from the Community folder:

        autpassword -x -tAES -oKeyFile=aes.key

    working directory = Community root (component_path).
    """
    aes_key = component_path / "aes.key"
    cfg_candidates = [
        component_path / "community.cfg",
        *sorted(component_path.rglob("community.cfg")),
    ]
    cfg_path = next((p for p in cfg_candidates if p.is_file()), component_path / "community.cfg")

    if aes_key.is_file():
        log.info(f"  aes.key already present: {aes_key}")
        if not dry_run and cfg_path.is_file():
            abs_key = str(aes_key.resolve())
            ok = ini_config.update_kd_ini_file(cfg_path, "Security", "SecurityInfoKeys", abs_key)
            if ok:
                log.info(f"  SecurityInfoKeys updated -> {abs_key}")
            else:
                log.warn(f"  Could not update SecurityInfoKeys in {cfg_path}")
        return {"Success": True, "Created": False, "KeyPath": str(aes_key), "Detail": "aes.key already present"}

    autpassword = _find_autpassword(component_path)
    if not autpassword:
        detail = (
            "aes.key missing and autpassword.exe not found under Community; "
            "create the key manually then re-run Configure"
        )
        log.warn(f"  {detail}")
        return {"Success": False, "Created": False, "KeyPath": str(aes_key), "Detail": detail}

    if dry_run:
        log.info(
            f"  [DryRun] Would create aes.key: cd {component_path} && "
            f"{autpassword.name} -x -tAES -oKeyFile=aes.key"
        )
        return {"Success": True, "Created": False, "KeyPath": str(aes_key), "Detail": "[DryRun]"}

    log.info(f"  Creating aes.key via {autpassword.name} in {component_path}")
    try:
        proc = subprocess.run(
            [str(autpassword), "-x", "-tAES", "-oKeyFile=aes.key"],
            cwd=str(component_path),
            capture_output=True,
            text=True,
            timeout=90,
            shell=False,
        )
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()

        if proc.returncode != 0:
            detail = f"autpassword exit {proc.returncode}"
            if out:
                detail += f": {out[:500]}"
            log.warn(f"  {detail}")
            return {"Success": False, "Created": False, "KeyPath": str(aes_key), "Detail": detail}
        if not aes_key.is_file():
            detail = "autpassword reported success but aes.key was not created"
            log.warn(f"  {detail}")
            if out:
                log.warn(f"  autpassword output: {out[:500]}")
            return {"Success": False, "Created": False, "KeyPath": str(aes_key), "Detail": detail}
        log.info(f"  aes.key created: {aes_key}")
        if out:
            log.info(f"  autpassword: {out[:300]}")
    except subprocess.TimeoutExpired:
        detail = "autpassword timed out while creating aes.key"
        log.warn(f"  {detail}")
        return {"Success": False, "Created": False, "KeyPath": str(aes_key), "Detail": detail}
    except Exception as e:
        detail = f"autpassword failed: {e}"
        log.warn(f"  {detail}")
        return {"Success": False, "Created": False, "KeyPath": str(aes_key), "Detail": detail}

    abs_key = str(aes_key.resolve())
    if cfg_path.is_file():
        ok = ini_config.update_kd_ini_file(cfg_path, "Security", "SecurityInfoKeys", abs_key)
        if ok:
            log.info(f"  [Security] SecurityInfoKeys={abs_key}")
            log.step_result("Community aes.key + SecurityInfoKeys", True, abs_key)
        else:
            log.warn(f"  aes.key created but SecurityInfoKeys update failed in {cfg_path}")
            return {
                "Success": False,
                "Created": True,
                "KeyPath": abs_key,
                "Detail": f"SecurityInfoKeys update failed in {cfg_path}",
            }
    else:
        log.warn(f"  aes.key created but community.cfg not found under {component_path}")

    return {"Success": True, "Created": True, "KeyPath": abs_key, "Detail": f"Created {abs_key}"}


def _apply_standard_cfg_patches(
    component: str,
    component_path: Path,
    config: Dict[str, Any],
    dry_run: bool = False,
) -> Optional[Path]:
    """
    Apply runtime config values on top of (optional) toolkit cfg templates:
    LicenseHost, [Server] Port from Ports map, and (only when explicitly set)
    a custom IndexPath.

    When IndexPath is absent the OpenText relative ./index paths under [Paths]
    are left untouched (the recommended default).
    Returns the primary .cfg path used, or None if none found.
    """
    cfg_files = [
        p
        for p in component_path.rglob("*.cfg")
        if "bak" not in p.name.lower() and p.name.lower() != "idol.common.cfg"
    ]
    # Prefer the component-named cfg at the root when present
    preferred = component_path / f"{component.lower()}.cfg"
    # Agentstore-like engines use agentstore.cfg (not qmsagentstore.cfg)
    agentstore_cfg = component_path / "agentstore.cfg"
    if preferred.is_file():
        cfg = preferred
    elif component in _AGENTSTORE_LIKE and agentstore_cfg.is_file():
        cfg = agentstore_cfg
    elif cfg_files:
        cfg = cfg_files[0]
    else:
        return None

    if dry_run:
        log.info(f"  [DryRun] Would patch {cfg.name} (LicenseHost / Port"
                 f"{' / IndexPath' if config.get('IndexPath') else ''})")
        return cfg

    ini_config.update_kd_ini_file(cfg, "License", "LicenseServerHost", config["LicenseHost"])
    # Intentionally do NOT write LicenseServerPort
    ports = config.get("Ports") or {}
    if component in ports:
        # Primary ACI port lives under [Server] in OpenText templates
        port_val = str(ports[component])
        ini_config.update_kd_ini_file(cfg, "Server", "Port", port_val)
        log.info(f"  [Server] Port={port_val} in {cfg.name}")
    # Only inject the non-standard [Index] IndexPath= when the user has
    # explicitly configured a custom IndexPath. Otherwise leave the template
    # relative paths (./index/...) alone.
    if config.get("IndexPath") and (
        component in ("Content", "Category", "Community")
        or component in _AGENTSTORE_LIKE
    ):
        ini_config.update_kd_ini_file(cfg, "Index", "IndexPath", config["IndexPath"])
        log.info(f"  [Index] IndexPath={config['IndexPath']} in {cfg.name}")
    return cfg


def _ensure_component_extracted(
    component: str,
    component_path: Path,
    base_path: Path,
    zip_path: str,
    dry_run: bool = False,
    non_interactive: bool = False,
    force: bool = False,
    nifi_version: str | None = None,
) -> bool:
    """
    Verify the target component folder has content; if missing/empty, extract
    it automatically by calling Unzip-One.bat (native tar.exe, strips the
    OpenText root folder). All extraction logic lives in that .bat file -
    this function just locates the matching ZIP and invokes it.
    Agentstore / QMSAgentStore have no ZIP - they are assembled from Content later.

    force=True skips the "already has content" short-circuit and always
    re-extracts, even if the destination folder already exists with files
    in it. Unzip-One.bat itself removes the destination folder before
    extracting (rd /s /q), so this guarantees a clean overwrite of the
    target folder rather than silently reusing whatever was there before -
    used by the standalone Install Java Services (NiFi|Find) menu, since
    that is an explicit (re)install action.
    """
    # Agentstore-like components are derived from Content – only need Content folder ready
    if component in _AGENTSTORE_LIKE:
        content_path = base_path / "Content"
        if _folder_has_content(content_path):
            log.info(f"  {component}: Content folder is present (will copy after check)")
            return True
        log.warn(f"  {component} needs the Content folder extracted first.")
        # Fall through to Content-style extraction using Content component name
        check_component = "Content"
        check_path = content_path
    else:
        check_component = component
        check_path = component_path

    # NiFi binary ZIP is an explicit prerequisite. Check/download it BEFORE
    # the extracted-folder short-circuit. A previous successful extraction
    # must not suppress the download when the ZIP was later removed.
    if check_component.lower() == "nifi" and not dry_run:
        try:
            from . import prerequisites
            nifi_version = str(nifi_version or prerequisites.NIFI_DEFAULT_VERSION)
            zip_dir = Path(zip_path) if zip_path else Path()
            zip_result = discovery.find_kd_component_zip(str(zip_dir), check_component)
            if not (zip_result.get("Success") and zip_result.get("Zip")):
                log.warn(
                    f"  NiFi ZIP is missing from {zip_dir}. "
                    f"Starting automatic download of nifi-{nifi_version}-bin.zip."
                )
                dl = prerequisites.download_nifi_zip(
                    zip_dir,
                    version=nifi_version,
                    dry_run=False,
                )
                if dl.get("Success"):
                    log.info(f"  NiFi automatic download succeeded: {dl.get('Path')}")
                else:
                    log.error(
                        "  NiFi automatic download failed: "
                        f"{dl.get('Detail', 'unknown download error')}"
                    )
                    return False
        except Exception as e:
            log.error(f"  NiFi automatic download could not be started: {e}")
            return False

    # NiFi needs a real binary tree (bin/nifi.cmd), not just an extensions/
    # folder left behind by a previous NAR copy.
    is_nifi = check_component.lower() == "nifi"
    already_ready = (
        _nifi_is_extracted(check_path) if is_nifi else _folder_has_content(check_path)
    )

    if not force and already_ready:
        log.info(f"  Found extracted content in {check_path}")
        return True

    if force and already_ready:
        log.info(f"  {check_component}: forcing re-extract - {check_path} will be overwritten")
    elif is_nifi and _folder_has_content(check_path) and not _nifi_is_extracted(check_path):
        log.warn(
            f"  {check_path} exists but is incomplete (no bin/nifi.cmd). "
            "Will extract nifi-*-bin.zip before copying NAR connectors."
        )

    if dry_run:
        log.info(f"  [DryRun] Would extract into: {check_path}")
        return True

    # Locate the matching ZIP. For NiFi the prerequisite/download check above
    # has already ensured that it exists (unless this is a dry-run).
    zip_dir = Path(zip_path) if zip_path else Path()
    zip_result = discovery.find_kd_component_zip(str(zip_dir), check_component)

    if not (zip_result.get("Success") and zip_result.get("Zip")):
        log.error(f"  No matching ZIP found for {check_component} in {zip_dir}")
        if zip_result.get("Reason"):
            log.error(f"  Detail: {zip_result['Reason']}")
        return False

    zf = Path(zip_result["Zip"])
    log.info(f"  Extracting {zf.name} -> {check_path}")
    try:
        expand_kd_zip_native(zf, check_path)
    except Exception as e:
        log.error(f"  Extraction failed for {check_component}: {e}")
        return False

    if is_nifi:
        if not _nifi_is_extracted(check_path):
            log.error(
                f"  Extraction reported success but NiFi launcher is missing under "
                f"{check_path} (expected bin/nifi.cmd). NAR copy will be skipped."
            )
            return False
    elif not _folder_has_content(check_path):
        log.error(f"  Extraction reported success but {check_path} is still empty.")
        return False

    log.info(f"  Extracted content ready in {check_path}")

    # Unblock Mark-of-the-Web in one bulk call (not per-file - that looked stuck)
    try:
        _unblock_tree(check_path)
    except Exception:
        pass

    return True



def _ensure_index_path(
    config: Dict[str, Any],
    base_path: Path,
    dry_run: bool = False,
    config_path: Optional[str | Path] = None,
) -> Optional[Path]:
    """
    Ensure a *custom* IDOL IndexPath directory exists (when one is configured).

    By default the toolkit leaves IndexPath unset. In that case the OpenText
    Content / Category / Community / Agentstore templates keep their relative
    paths under [Paths] (e.g. MainPath=./index/main, TemplateDirectory=./templates).
    This is the recommended / default layout and avoids the non-standard
    [Index] IndexPath= key that was previously injected.

    Only when the user explicitly sets a non-empty "IndexPath" in the JSON
    config (or the config dashboard) do we:

    - create that directory, and
    - later write [Index] IndexPath=... into the component .cfg files
      (see _apply_standard_cfg_patches).

    When config_path is supplied and a custom IndexPath is active, the value
    is also persisted back into the JSON so the dashboard and Update-ConfigFiles
    stay in sync.
    """
    raw_index_path = str(config.get("IndexPath") or "").strip()

    if not raw_index_path:
        # Default behaviour: keep the relative ./index folders from the
        # OpenText templates. Do not invent a custom IndexPath.
        log.info(
            "  IndexPath not set — using default relative index folders "
            "(./index/...) under each component. Custom IndexPath disabled."
        )
        log.step_result(
            "IndexPath",
            True,
            "default relative (./index) — custom IndexPath disabled",
        )
        return None

    index_path = Path(raw_index_path)

    if dry_run:
        log.info(f"  [DryRun] Would create custom IndexPath: {index_path}")
        return index_path

    if config_path:
        try:
            from .config import update_config_key

            if update_config_key(config_path, "IndexPath", str(index_path)):
                log.info(f"  Synced custom IndexPath -> {config_path}")
            else:
                log.warn(f"  Could not sync IndexPath into {config_path}")
        except Exception as e:
            log.warn(f"  Could not persist IndexPath to {config_path}: {e}")

    try:
        index_path.mkdir(parents=True, exist_ok=True)

        if not index_path.is_dir():
            raise RuntimeError(
                f"IndexPath exists but is not a directory: {index_path}"
            )

        log.info(f"  Custom IndexPath ready: {index_path}")
        log.step_result(
            "Create IndexPath",
            True,
            str(index_path),
        )

        return index_path

    except Exception as e:
        log.error(f"  Failed to create IndexPath {index_path}: {e}")
        log.step_result(
            "Create IndexPath",
            False,
            str(e),
        )
        raise


def invoke_kd_install(
    config: Dict[str, Any],
    dry_run: bool = False,
    force: bool = False,
    resume: bool = False,
    extract_only: bool = False,
    non_interactive: bool = False,
    config_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """
    Install Knowledge Discovery components.

    If a custom IndexPath is present in the config it is created before any
    component configuration or service start. When IndexPath is absent
    (the default) the OpenText relative ./index folders are left untouched.

    config_path, when supplied, is the on-disk JSON config file (e.g.
    config/my-config.json) that config was loaded from; it is used to persist
    a custom IndexPath back to disk when one is active (see _ensure_index_path).
    """
    base_path = Path(config["BasePath"])

    if not dry_run:
        base_path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Optional custom IndexPath. When unset we keep the default relative
    # ./index layout from the OpenText templates (recommended).
    # ------------------------------------------------------------------
    try:
        _ensure_index_path(
            config=config,
            base_path=base_path,
            dry_run=dry_run,
            config_path=config_path,
        )
    except Exception as e:
        log.error(f"IndexPath initialization failed: {e}")
        return {
            "Success": False,
            "Completed": [],
            "Failed": ["IndexPath"],
            "Detail": f"Could not create IndexPath: {e}",
        }

    st = state.get_kd_state(base_path)
    completed: List[str] = []
    failed: List[str] = []

    components = list(config.get("Components") or [])
    n_comp = len(components)

    # Phase A: one unit per component
    # Phase B: one unit per component when starting services
    start_phase = bool(
        config.get("InstallService", True)
        and not extract_only
        and not dry_run
    )

    total_units = n_comp + (n_comp if start_phase else 0)

    with ElapsedClock(
        "Install",
        interval=15.0,
        total_units=max(1, total_units),
        unit_label="units",
    ) as clock:
        return _invoke_kd_install_body(
            config=config,
            base_path=base_path,
            st=st,
            completed=completed,
            failed=failed,
            dry_run=dry_run,
            force=force,
            resume=resume,
            extract_only=extract_only,
            non_interactive=non_interactive,
            clock=clock,
            total_components=n_comp,
            start_phase=start_phase,
        )


def _invoke_kd_install_body(
    config: Dict[str, Any],
    base_path: Path,
    st: Any,
    completed: List[str],
    failed: List[str],
    dry_run: bool,
    force: bool,
    resume: bool,
    extract_only: bool,
    non_interactive: bool,
    clock: Optional[ElapsedClock] = None,
    total_components: int = 0,
    start_phase: bool = False,
) -> Dict[str, Any]:
    components = list(config.get("Components") or [])
    if total_components <= 0:
        total_components = len(components)
    units_per_phase = float(total_components) if total_components else 1.0
    total_all = units_per_phase * (2.0 if start_phase else 1.0)

    def _sync_progress(task: str = "") -> None:
        if clock is None:
            return
        # Phase A progress = components finished so far
        done = float(len(completed) + len(failed))
        clock.set_progress(min(done, total_all), total_all)
        if task:
            clock.set_task(task)
        else:
            clock.set_task("")

    for component in components:
        log.info(f"=== Processing component: {component} ===")
        _sync_progress(component)
        component_path = base_path / component
        is_agentstore_like = component in _AGENTSTORE_LIKE

        try:
            # --- Package-only (e.g. NiFiIngest): not a core KD service ---
            # Dashboard may list NiFiIngest under Components because its ZIP
            # is present. It only supplies connector .nar files for NiFi —
            # never .cfg configure or Windows service install.
            if _is_package_only_component(component):
                log.info(
                    f"  {component} is package-only (NiFi connector NARs) — "
                    "not a core KD service; skipping .cfg / service install"
                )
                stage = _stage_nars_from_nifiingest_zip(
                    config.get("ZipPath", ""),
                    dry_run=dry_run,
                )
                log.step_result(
                    f"Stage {component} connector .nar files -> nifi-connectors",
                    bool(stage.get("Success")),
                    stage.get("Detail") or "",
                )
                if not dry_run:
                    st = state.set_kd_component_stage(base_path, st, component, "Complete")
                completed.append(component)
                _sync_progress()
                continue

            if is_agentstore_like and state.test_kd_component_stage_complete(st, component, "Complete"):
                log.info(f"  Skipping {component} (already complete)")
                completed.append(component)
                _sync_progress()
                continue

            # --- Stage: Extract (automatic, via Unzip-One.bat) ---
            # Resume trusts the persisted "Extracted" state flag, but that flag
            # can go stale: an interrupted/crashed extraction, a disk-full mid
            # copy, or a folder that was later partially cleared out can all
            # leave the flag set while the folder itself is incomplete. Verify
            # the folder actually has content before trusting the flag - if
            # it doesn't, fall through and let _ensure_component_extracted
            # (which itself checks content first) re-extract and self-heal.
            resolved_check_path = (
                (base_path / "Content")
                if component in _AGENTSTORE_LIKE
                else component_path
            )
            extracted_flag = state.test_kd_component_stage_complete(st, component, "Extracted")
            # NiFi: only treat as extracted when bin/nifi.cmd (or equivalent) exists.
            # A lone NiFi/extensions folder from a prior NAR copy must not skip
            # extraction of nifi-*-bin.zip.
            if component.lower() == "nifi":
                folder_ok = _nifi_is_extracted(resolved_check_path)
            else:
                folder_ok = _folder_has_content(resolved_check_path)
            if extracted_flag and folder_ok:
                log.info(f"  Skipping extract check for {component} (resume)")
            else:
                if extracted_flag and not folder_ok:
                    log.warn(
                        f"  State says {component} was Extracted, but "
                        f"{resolved_check_path} is missing/incomplete - re-extracting"
                    )
                ready = _ensure_component_extracted(
                    component=component,
                    component_path=component_path,
                    base_path=base_path,
                    zip_path=config.get("ZipPath", ""),
                    dry_run=dry_run,
                    non_interactive=non_interactive,
                    force=bool(extracted_flag and not folder_ok),
                )
                if not ready:
                    log.step_result(
                        f"Extract check for {component}",
                        False,
                        "Extraction failed or no matching ZIP found in ZipPath",
                    )
                    failed.append(component)
                    continue
                log.step_result(f"Extract check for {component}", True, str(component_path))
                if not dry_run:
                    st = state.set_kd_component_stage(base_path, st, component, "Extracted")

            # --- NiFi post-extract: restore staged NAR connectors ---
            # Only after a real NiFi tree exists (extract must happen first).
            if component.lower() == "nifi" and not dry_run:
                if not _nifi_is_extracted(component_path):
                    log.warn(
                        "Skipping NAR copy: NiFi binary tree is not extracted yet "
                        f"(missing bin/nifi.cmd under {component_path}). "
                        "Extract nifi-*-bin.zip first."
                    )
                else:
                    nar_result = _copy_nifi_nar_extensions(
                        base_path,
                        dry_run=False,
                    )
                    log.step_result(
                        "Copy NiFi connector .nar files -> extensions",
                        nar_result.get("Success", False),
                        nar_result.get("Detail", ""),
                    )
                    if not nar_result.get("Success", False):
                        log.warn(
                            "NiFi was extracted successfully, but one or more "
                            "NAR connectors could not be copied."
                        )

            # --- Content post-extract: stage NAR connectors shipped under
            # Content's own "nifi" subfolder into nifi-connectors, the same
            # way NiFiIngest ZIP connectors are staged. ---
            if component.lower() == "content" and not dry_run:
                stage_result = _stage_nifi_nars_from_component(
                    component_path,
                    dry_run=False,
                )
                if stage_result.get("Count", 0) > 0 or not stage_result.get("Success", False):
                    log.step_result(
                        "Stage Content .nar connectors -> nifi-connectors",
                        stage_result.get("Success", False),
                        stage_result.get("Detail", ""),
                    )
                if not stage_result.get("Success", False):
                    log.warn(
                        "Content was extracted successfully, but one or more "
                        "NAR connectors under its nifi subfolder could not be staged."
                    )

            # --- Agentstore-like post-extract (copy from Content) ---
            if is_agentstore_like and not dry_run:
                content_path = base_path / "Content"
                if content_path.exists():
                    log.info(f"  Assembling {component} from Content -> {component_path}")
                    component_path.mkdir(parents=True, exist_ok=True)
                    for item in content_path.iterdir():
                        dest_item = component_path / item.name
                        if item.is_dir():
                            if dest_item.exists():
                                shutil.rmtree(dest_item, ignore_errors=True)
                            shutil.copytree(item, dest_item)
                        else:
                            shutil.copy2(item, dest_item)
                    try:
                        _unblock_tree(component_path)
                    except Exception:
                        pass

                    content_exe = component_path / "content.exe"
                    agent_exe = component_path / "agentstore.exe"
                    if content_exe.exists() and not agent_exe.exists():
                        shutil.copy2(content_exe, agent_exe)
                        log.info("  Created agentstore.exe from content.exe")

                    # Toolkit cfg templates overwrite Content copies
                    n_tpl = _deploy_cfg_templates(component, component_path, dry_run=False)
                    if n_tpl == 0:
                        agent_cfg = component_path / "agentstore.cfg"
                        if not agent_cfg.exists():
                            content_cfg = component_path / "content.cfg"
                            if content_cfg.exists():
                                shutil.copy2(content_cfg, agent_cfg)
                                log.info("  Created agentstore.cfg from content.cfg (no toolkit template)")
                    log.step_result(f"Assemble {component} from Content", True, str(component_path))

            # --- License key copy for LicenseServer ---
            if component == "LicenseServer" and not dry_run:
                key_path = config.get("LicenseKeyPath")
                if key_path and Path(key_path).is_file():
                    target_dir = component_path
                    versioned = next(
                        (d for d in component_path.iterdir() if d.is_dir() and "licenseserver" in d.name.lower() and "windows" in d.name.lower()),
                        None,
                    )
                    if versioned:
                        target_dir = versioned
                    else:
                        for p in component_path.rglob("*"):
                            if p.name.lower() == "licenseserver.exe" or p.suffix.lower() == ".cfg":
                                target_dir = p.parent
                                break
                    dest_key = target_dir / "licensekey.dat"
                    shutil.copy2(key_path, dest_key)
                    log.info(f"  Copied license key -> {dest_key}")
                    log.step_result("Install license key for LicenseServer", True, str(dest_key))
                else:
                    log.info("  No LicenseKeyPath configured or file missing; skipping license key install")

            # --- Post-extract: always deploy toolkit cfg templates when present ---
            # (runs even under ExtractOnly so BasePath trees get the known-good .cfg files)
            # Agentstore-like templates are applied during assembly from Content above.
            if (
                component.lower() in _CFG_TEMPLATE_COMPONENTS
                and component not in _AGENTSTORE_LIKE
                and not state.test_kd_component_stage_complete(st, component, "Configured")
            ):
                _deploy_cfg_templates(component, component_path, dry_run=dry_run)

            # --- Stage: Configure ---
            if extract_only:
                log.info(f"  [ExtractOnly] Skipping configuration for {component}")
            elif state.test_kd_component_stage_complete(st, component, "Configured"):
                log.info("  Skipping configuration (resume)")
            else:
                if component.lower() == "nifi":
                    # Apache NiFi: edit nifi.properties + bootstrap.conf + overwrite nifi.cmd
                    nifi_cfg = config.get("NiFi") or {}
                    props_file = None
                    for p in component_path.rglob("nifi.properties"):
                        props_file = p
                        break
                    boot_file = None
                    for p in component_path.rglob("bootstrap.conf"):
                        boot_file = p
                        break

                    if not props_file and not dry_run:
                        log.step_result(f"Configure {component}", False, "nifi.properties not found")
                    else:
                        if not dry_run and props_file:
                            # Prefer KD template (nifi/conf/nifi.properties) so placeholders are present
                            copied = _deploy_nifi_properties_template(component_path)
                            if copied:
                                props_file = copied
                                log.info(f"  Deployed nifi.properties template -> {props_file}")
                            updates = {}
                            key = nifi_cfg.get("SensitivePropsKey") or "ChangeMe-StrongPassword123!"
                            updates["nifi.sensitive.props.key"] = key
                            port = str(nifi_cfg.get("WebHttpsPort") or (config.get("Ports") or {}).get("NiFi") or "8443")
                            updates["nifi.web.https.port"] = port
                            # Bind to all interfaces so external clients can reach the UI
                            # (matches the firewall rule opened for the same port).
                            # Allow override via NiFi.WebHttpsHost when set.
                            host = str(nifi_cfg.get("WebHttpsHost") or "0.0.0.0").strip() or "0.0.0.0"
                            updates["nifi.web.https.host"] = host
                            # Build nifi.web.proxy.host for allowed Host headers (localhost + bind host + EXTRA_IP_SANS)
                            # EXTRA_IP_SANS from NiFi.ExternalIIPSAN only.
                            # Rule: if empty → EXTRA_IP_SANS = 127.0.0.1 else the given value.
                            # Legacy ExternalIpAddress is migrated once into ExternalIIPSAN if needed.
                            if not str(nifi_cfg.get("ExternalIIPSAN") or "").strip():
                                legacy = str(nifi_cfg.get("ExternalIpAddress") or "").strip()
                                if legacy:
                                    nifi_cfg["ExternalIIPSAN"] = legacy
                                nifi_cfg.pop("ExternalIpAddress", None)
                            extra_ip = str(nifi_cfg.get("ExternalIIPSAN") or "").strip()
                            if not extra_ip:
                                extra_ip = "127.0.0.1"
                            proxy_parts = [f"localhost:{port}", f"{host}:{port}"]
                            if extra_ip and extra_ip not in (host, "0.0.0.0"):
                                # Always include EXTRA_IP_SANS (default 127.0.0.1) unless it duplicates host
                                if f"{extra_ip}:{port}" not in proxy_parts:
                                    proxy_parts.append(f"{extra_ip}:{port}")
                            updates["nifi.web.proxy.host"] = ",".join(proxy_parts)
                            # Deploy generated SSL keystore/truststore and wire nifi.security.*
                            ssl_mat = _deploy_nifi_ssl_material(
                                component_path,
                                setup_path=Path(str(config.get("SetupPath") or "")) if config.get("SetupPath") else None,
                            )
                            if ssl_mat.get("ok"):
                                log.info(f"  {ssl_mat.get('detail')}")
                                updates.update(_nifi_security_property_updates(ssl_mat))
                                if not ssl_mat.get("keystore_pass") or not ssl_mat.get("truststore_pass"):
                                    log.warn(
                                        "  SSL PKCS12 files copied but passwords not found "
                                        "(check env/.idol-ssl-passwords.env or ssl-passwords.txt)"
                                    )
                            else:
                                log.warn(f"  NiFi SSL material not deployed: {ssl_mat.get('detail')}")
                            ok_props = _update_properties_file(props_file, updates)
                            if ok_props:
                                sec_note = ""
                                if ssl_mat.get("ok") and ssl_mat.get("keystore_pass"):
                                    sec_note = " + security keystore/truststore passwords"
                                log.info(
                                    f"  nifi.properties updated "
                                    f"(sensitive key + https host={host} port={port}"
                                    f" + proxy.host={updates['nifi.web.proxy.host']}"
                                    f"{sec_note})"
                                )
                            else:
                                log.warn(f"  Failed to update {props_file}")
                            ok_boot = True
                            if boot_file:
                                xms = nifi_cfg.get("HeapXms", "8g")
                                xmx = nifi_cfg.get("HeapXmx", "16g")
                                ok_boot = _update_properties_file(
                                    boot_file,
                                    {
                                        "java.arg.2": f"-Xms{xms}",
                                        "java.arg.3": f"-Xmx{xmx}",
                                    },
                                )
                                if ok_boot:
                                    log.info(f"  bootstrap.conf heap set to Xms={xms} Xmx={xmx}")
                                else:
                                    log.warn(f"  Failed to update {boot_file}")
                            # Overwrite nifi.cmd from toolkit (NSSM uses: /c nifi.cmd run)
                            nifi_cmd_dest = _deploy_nifi_cmd(component_path)
                            if nifi_cmd_dest:
                                log.info(f"  Deployed nifi.cmd -> {nifi_cmd_dest}")
                            # Fixed UI login from config (NiFi.Username / NiFi.Password)
                            creds = _set_nifi_single_user_credentials(
                                component_path,
                                str(nifi_cfg.get("Username") or ""),
                                str(nifi_cfg.get("Password") or ""),
                            )
                            if creds.get("Skipped"):
                                log.info(f"  {creds.get('Detail')}")
                            elif creds.get("Success"):
                                log.info(f"  {creds.get('Detail')}")
                            else:
                                log.warn(f"  NiFi credentials not applied: {creds.get('Detail')}")
                            if ok_props and ok_boot:
                                log.step_result(f"Configure {component}", True, str(props_file))
                            else:
                                log.step_result(
                                    f"Configure {component}", False,
                                    f"Could not write one or more config files under {component_path}",
                                )
                        else:
                            log.step_result(
                                f"Configure {component}", True,
                                "[DryRun] would edit nifi.properties + bootstrap.conf and overwrite nifi.cmd",
                            )
                        if not dry_run:
                            st = state.set_kd_component_stage(base_path, st, component, "Configured")
                elif component.lower() == "find":
                    # OpenText Find: copy toolkit config/cfg/find/config.json into
                    # <BasePath>/Find/home/config.json (defaultLogin admin/admin, etc.)
                    find_cfg = config.get("Find") or {}
                    home_name = str(find_cfg.get("HomeDir") or "home")
                    log.info(f"  Configuring Find (deploy config.json -> {home_name}/)")
                    deploy = _deploy_find_config_json(
                        component_path,
                        home_dir_name=home_name,
                        dry_run=dry_run,
                    )
                    log.step_result(
                        f"Configure {component}",
                        bool(deploy.get("Success")),
                        deploy.get("Detail") or "",
                    )
                    if deploy.get("Success") and not dry_run:
                        st = state.set_kd_component_stage(base_path, st, component, "Configured")
                else:
                    # Standard KD .cfg components:
                    # 1) Overwrite root cfg files from toolkit config/cfg/<component>/
                    # 2) Community: ensure aes.key + SecurityInfoKeys
                    # 3) Patch LicenseHost / [Server] Port / IndexPath from config JSON
                    log.info(f"  Configuring {component} (.cfg templates + runtime patches)")
                    if component not in _AGENTSTORE_LIKE:
                        # Agentstore-like templates already deployed in post-extract assembly
                        _deploy_cfg_templates(component, component_path, dry_run=dry_run)

                    if component.lower() == "community":
                        aes_result = _ensure_community_aes_key(component_path, dry_run=dry_run)
                        if not aes_result.get("Success"):
                            log.warn(f"  Community aes.key step: {aes_result.get('Detail')}")
                        else:
                            log.info(f"  Community aes.key: {aes_result.get('Detail')}")

                    cfg = _apply_standard_cfg_patches(
                        component, component_path, config, dry_run=dry_run
                    )
                    if cfg is None:
                        log.step_result(f"Configure {component}", False, "No .cfg file found")
                    else:
                        log.step_result(f"Configure {component}", True, str(cfg))
                        if not dry_run:
                            st = state.set_kd_component_stage(base_path, st, component, "Configured")

                    # idol.common.cfg only for LicenseServer
                    if component == "LicenseServer" and not dry_run:
                        for idol in component_path.rglob("idol.common.cfg"):
                            ini_config.update_kd_ini_file(idol, "License", "LicenseServerHost", "licenseserver")
                            ini_config.update_kd_ini_file(idol, "License", "Clients", "*.*.*.*")
                            log.info(f"  Updated idol.common.cfg: LicenseServerHost=licenseserver, Clients=*.*.*.* ({idol})")
                            log.step_result("Update idol.common.cfg (LicenseServer)", True, str(idol))

            # --- Stage: Service install ---
            if extract_only:
                log.info(f"  [ExtractOnly] Skipping service install for {component}")
            elif config.get("InstallService", True):
                # Resume must not skip registration when the Windows service is
                # actually missing (common after partial/failed runs). Verify
                # with sc.exe; only skip when state says done AND service exists.
                svc_name_check = service_manager.get_kd_service_name(component)
                svc_registered = False
                try:
                    import subprocess as _sp
                    q = _sp.run(
                        ["sc.exe", "query", svc_name_check],
                        capture_output=True,
                        text=True,
                    )
                    svc_registered = q.returncode == 0
                except Exception:
                    svc_registered = False

                skip_svc = (
                    state.test_kd_component_stage_complete(st, component, "ServiceInstalled")
                    and svc_registered
                )
                if skip_svc:
                    log.info(
                        f"  Skipping service install (resume) - {svc_name_check} is registered"
                    )
                    # Ensure firewall is open even when service install is skipped
                    if not dry_run:
                        _open_component_firewall(component, config, dry_run=False)
                else:
                    if (
                        state.test_kd_component_stage_complete(st, component, "ServiceInstalled")
                        and not svc_registered
                    ):
                        log.warn(
                            f"  State says ServiceInstalled but {svc_name_check} is NOT "
                            "registered - re-creating service"
                        )
                    if component.lower() == "nifi":
                        # Apache NiFi has no native Windows service installer (official Apache
                        # docs only cover Linux/macOS). Register via NSSM wrapping
                        # nifi.cmd run when NiFi.InstallService is true.
                        if not _nifi_install_service_enabled(config):
                            log.info(
                                "  Skipping NiFi Windows service (NiFi.InstallService is false). "
                                "Use bin\\nifi.cmd start, or nifi\\Setup-NiFi-Service.ps1 later."
                            )
                            if not dry_run:
                                _open_component_firewall(component, config, dry_run=False)
                            log.step_result(
                                f"Install service for {component}",
                                True,
                                "Skipped (NiFi.InstallService=false); use nifi.cmd or Setup-NiFi-Service.ps1 later",
                            )
                        else:
                            # Ensure toolkit nifi.cmd is present even if Configure was resumed/skipped
                            if not dry_run:
                                _deploy_nifi_cmd(component_path)
                            exe_result = discovery.find_kd_component_executable(component_path, component)
                            if not exe_result["Success"]:
                                log.step_result(f"Locate launcher for {component}", False, exe_result["Reason"])
                                failed.append(component)
                                continue
                            nifi_cmd = Path(exe_result["Executable"])
                            nifi_cfg = config.get("NiFi") or {}
                            nifi_port = (
                                nifi_cfg.get("WebHttpsPort")
                                or (config.get("Ports") or {}).get("NiFi")
                                or "8443"
                            )
                            svc_result = service_manager.install_kd_nifi_service(
                                component=component,
                                nifi_cmd_path=nifi_cmd,
                                start_mode=config.get("StartMode", "Auto"),
                                dry_run=dry_run,
                                port=nifi_port,
                            )
                            log.step_result(
                                f"Install service for {component}",
                                svc_result["Success"],
                                svc_result["Detail"],
                            )
                            if not svc_result["Success"]:
                                failed.append(component)
                                continue
                            if not dry_run:
                                st = state.set_kd_component_stage(base_path, st, component, "ServiceInstalled")
                    elif component.lower() == "find":
                        # OpenText Find is a Java .war - no native -install switch.
                        # Register via NSSM wrapping: java -jar find.war
                        find_cfg = config.get("Find") or {}
                        if find_cfg.get("InstallService", True) is False:
                            log.info(
                                "  Skipping Find Windows service (Find.InstallService is false)."
                            )
                            if not dry_run:
                                _open_component_firewall(component, config, dry_run=False)
                            log.step_result(
                                f"Install service for {component}",
                                True,
                                "Skipped (Find.InstallService=false)",
                            )
                        else:
                            exe_result = discovery.find_kd_component_executable(component_path, component)
                            if not exe_result["Success"]:
                                log.step_result(f"Locate find.war for {component}", False, exe_result["Reason"])
                                failed.append(component)
                                continue
                            find_war = Path(exe_result["Executable"])
                            ports = config.get("Ports") or {}
                            server_port = str(
                                find_cfg.get("ServerPort")
                                or ports.get("Find")
                                or "8080"
                            )
                            svc_result = service_manager.install_kd_find_service(
                                component=component,
                                find_war_path=find_war,
                                start_mode=config.get("StartMode", "Auto"),
                                server_port=server_port,
                                heap_xms=str(find_cfg.get("HeapXms") or "1g"),
                                heap_xmx=str(find_cfg.get("HeapXmx") or "2g"),
                                home_dir_name=str(find_cfg.get("HomeDir") or "home"),
                                dry_run=dry_run,
                            )
                            log.step_result(
                                f"Install service for {component}",
                                svc_result["Success"],
                                svc_result["Detail"],
                            )
                            if not svc_result["Success"]:
                                failed.append(component)
                                continue
                            if not dry_run:
                                st = state.set_kd_component_stage(base_path, st, component, "ServiceInstalled")
                    else:
                        exe_result = discovery.find_kd_component_executable(component_path, component)
                        if not exe_result["Success"]:
                            log.step_result(f"Locate executable for {component}", False, exe_result["Reason"])
                            failed.append(component)
                            continue
                        if len(exe_result["AllCandidates"]) > 1:
                            others = ", ".join(p.name for p in exe_result["AllCandidates"][1:4])
                            log.info(f"  Picked {exe_result['Executable'].name} for {component} (other candidates: {others})")

                        template = config.get("ServiceInstallArgsTemplate")
                        ports_map = config.get("Ports") or {}
                        # LicenseServer uses LicensePort; all others use Ports.<Component>
                        if component.lower() == "licenseserver":
                            comp_port = config.get("LicensePort") or ports_map.get("LicenseServer")
                        else:
                            comp_port = ports_map.get(component)
                        svc_result = service_manager.install_kd_service(
                            component=component,
                            executable_path=exe_result["Executable"],
                            start_mode=config.get("StartMode", "Auto"),
                            args_template=template,
                            dry_run=dry_run,
                            port=comp_port,
                        )
                        log.step_result(f"Install service for {component}", svc_result["Success"], svc_result["Detail"])
                        if not svc_result["Success"]:
                            failed.append(component)
                            continue
                        if not dry_run:
                            st = state.set_kd_component_stage(base_path, st, component, "ServiceInstalled")

            if not dry_run and not extract_only:
                st = state.set_kd_component_stage(base_path, st, component, "Complete")
            completed.append(component)
            _sync_progress()

        except Exception as e:
            log.error(f"Unexpected error processing {component}: {e}")
            failed.append(component)
            _sync_progress()

    # ------------------------------------------------------------------
    # Post-install service start - ONLY components listed in config
    # Order: LicenseServer first when present, then remaining Components.
    # Never require LicenseServer when it is not in Components (e.g. NiFi-only).
    # ------------------------------------------------------------------
    if extract_only:
        log.info("[ExtractOnly] Skipping service start phase")
    elif config.get("InstallService", True) and not dry_run:
        components = list(config.get("Components") or [])
        has_license = any(c.lower() == "licenseserver" for c in components)
        if has_license:
            ordered = [c for c in components if c.lower() == "licenseserver"] + [
                c for c in components if c.lower() != "licenseserver"
            ]
            log.info("=== Starting services (LicenseServer first, then remaining Components) ===")
        else:
            ordered = components
            log.info("=== Starting services (configured Components only) ===")
        log.info(f"  Start order: {', '.join(ordered) if ordered else '(none)'}")

        # Phase B starts after all components are processed
        starts_done = 0
        if clock is not None:
            clock.set_progress(units_per_phase, total_all)
            clock.set_task("starting services")

        for component in ordered:
            if _is_package_only_component(component):
                log.info(f"  Skipping start of {component} (package-only — not a Windows service)")
                starts_done += 1
                if clock is not None:
                    clock.set_progress(units_per_phase + starts_done, total_all)
                continue
            if component.lower() == "nifi" and not _nifi_install_service_enabled(config):
                log.info("  Skipping start of KD-NiFi (NiFi.InstallService is false)")
                starts_done += 1
                if clock is not None:
                    clock.set_progress(units_per_phase + starts_done, total_all)
                continue
            if component.lower() == "find":
                find_cfg = config.get("Find") or {}
                if find_cfg.get("InstallService", True) is False:
                    log.info("  Skipping start of KD-Find (Find.InstallService is false)")
                    starts_done += 1
                    if clock is not None:
                        clock.set_progress(units_per_phase + starts_done, total_all)
                    continue
            if clock is not None:
                clock.set_task(f"start {component}")
            svc_name = service_manager.get_kd_service_name(component)

            if component.lower() == "nifi":
                # Ensure external clients can reach NiFi HTTPS port
                _open_component_firewall(component, config, dry_run=False)
                # Start service, then require Flow Controller marker in nifi-app.log.
                # RUNNING alone is not treated as ready.
                nifi_home = Path(config["BasePath"]) / "NiFi"
                expected_log = nifi_home / "logs" / "nifi-app.log"
                log.info(
                    f"  Starting {svc_name} "
                    f"(require 'Flow Controller started successfully.' in "
                    f"{expected_log}, timeout 300s / 5 min)..."
                )
                nifi_result = service_manager.start_kd_nifi_and_wait_for_flow(
                    service_name=svc_name,
                    nifi_home=nifi_home,
                    timeout_seconds=300,
                )
                ok = bool(nifi_result.get("Success"))
                elapsed = nifi_result.get("ElapsedSeconds", 0)
                marker = nifi_result.get("MarkerFound")
                is_warn = bool(nifi_result.get("Warning"))
                log_path = nifi_result.get("LogPath") or str(expected_log)
                detail = nifi_result.get("Detail") or (
                    f"Status: {'Ready' if ok else 'Not ready'} "
                    f"(elapsed {elapsed}s, marker={'yes' if marker else 'no'}, "
                    f"log={log_path})"
                )
                # Marker missing / log missing / timeout → not ready (ok=False).
                # Marker seen but service not RUNNING → soft warning.
                log.step_result(
                    f"Start service {svc_name}",
                    ok,
                    detail,
                    warning=is_warn,
                )
                if not ok:
                    log.warn(
                        f"  NiFi is not fully ready. Do not assume the UI/API is available. "
                        f"Check {log_path}. Subsequent steps continue independently."
                    )
            else:
                # Longer timeouts: native KD binaries often need 60–90s to bind
                # ports and load indexes; Find (Java) needs longer for first JVM start.
                if component.lower() == "licenseserver":
                    timeout = 90
                elif component.lower() in (
                    "content", "community", "category", "agentstore",
                    "qmsagentstore", "answerbankagentstore", "conversationagentstore",
                    "qms", "statsserver", "answerserver",
                ):
                    timeout = 60
                elif component.lower() == "find":
                    timeout = 180
                else:
                    timeout = 45
                if component.lower() == "find":
                    # Ensure external clients can reach Find UI port (8080 by default)
                    _open_component_firewall(component, config, dry_run=False)
                log.info(f"  Starting {svc_name} (timeout {timeout}s)...")
                if component.lower() == "find":
                    log.info(
                        "  Find is a Java service (NSSM → java -jar find.war); "
                        "first start can take 1–3 minutes."
                    )
                start_info = service_manager.start_kd_service_detailed(svc_name, timeout)
                ok = bool(start_info.get("Success"))
                detail = start_info.get("Detail") or (
                    f"Status: {'Running' if ok else 'Failed / not yet Running'}"
                )
                # Start failures are soft (WARN): service may still come up later,
                # and Manage → Start / Create remains available. Do not mark the
                # whole Install as failed solely because a process is slow.
                log.step_result(
                    f"Start service {svc_name}",
                    ok,
                    detail,
                    warning=not ok,
                )
                if component.lower() == "find" and not ok:
                    log.warn(
                        "KD-Find did not reach Running within the timeout. "
                        "Check Java/JAVA_HOME, NSSM, and Find\\logs\\find-service-*.log. "
                        "You can start it later: Start-Service KD-Find "
                        "or .\\Manage-KDServices.ps1"
                    )
                if component.lower() == "licenseserver":
                    if not ok:
                        log.warn(
                            "LicenseServer did not reach Running; continuing with remaining "
                            "Components (check licensekey.dat and LicenseServer logs). "
                            "NiFi/Find do not require it to be up first."
                        )
                    else:
                        # Let LicenseServer finish binding before dependents start
                        log.info("  Waiting 5s for LicenseServer to settle before starting other components...")
                        time.sleep(5)
            starts_done += 1
            if clock is not None:
                clock.set_progress(units_per_phase + starts_done, total_all)
                clock.set_task("")
    elif dry_run and config.get("InstallService", True):
        components = list(config.get("Components") or [])
        has_license = any(c.lower() == "licenseserver" for c in components)
        order_note = "LicenseServer first, then remaining" if has_license else "configured Components only"
        log.info(f"[DryRun] Would start services ({order_note}): {', '.join(components)}")

    if failed and not force:
        log.warn(f"Some components failed: {', '.join(failed)}")

    return {
        "Completed": completed,
        "Failed": failed,
        "StateFile": str(state.get_kd_state_file_path(base_path)),
    }


def invoke_kd_uninstall(
    config: Dict[str, Any],
    dry_run: bool = False,
    non_interactive: bool = False,
) -> Dict[str, Any]:
    """
    Uninstall order is strict:
      Phase 1 – STOP the KD-* services for the Components listed in config
                (never touch services for components that aren't configured)
      Phase 2 – DELETE/unregister each service (stop again, then binary -remove / sc delete)
      Phase 3 – DELETE component folders (only after services are gone)
      Phase 4 – Optionally DELETE toolkit SSL folder (prompt when interactive)

    Only services corresponding to entries in config["Components"] are ever
    touched. A machine may have other KD-* services registered from a
    different install/config; those are left alone unless the config
    explicitly opts in via "CleanupLeftoverServices": true.
    """
    base_path = Path(config["BasePath"])
    completed: List[str] = []
    failed: List[str] = []
    components = list(reversed(config.get("Components", [])))
    cleanup_leftovers = bool(config.get("CleanupLeftoverServices", False))

    # Service name list is strictly derived from config["Components"] - the
    # installer only ever installs/manages/removes services that are part of
    # the Components list in the config JSON.
    svc_names_ordered: List[str] = []
    for component in components:
        if _is_package_only_component(component):
            continue  # no Windows service for package-only entries
        name = service_manager.get_kd_service_name(component)
        if name not in svc_names_ordered:
            svc_names_ordered.append(name)

    if cleanup_leftovers and config.get("InstallService", True) and not dry_run:
        try:
            for extra in service_manager.list_kd_services():
                if extra not in svc_names_ordered:
                    svc_names_ordered.append(extra)
                    log.info(f"  CleanupLeftoverServices=true: found additional service not in config: {extra}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Phase 1: for each service, check if it's currently running - if so,
    # stop it first, then continue. Never attempt to delete a running
    # service. (stop_kd_service_force() itself is a no-op / returns True
    # immediately if the service isn't registered or isn't running.)
    # ------------------------------------------------------------------
    if config.get("InstallService", True) and not dry_run:
        log.info("=== Phase 1: Checking/stopping KD-* services (Components in config only) ===")
        for svc_name in svc_names_ordered:
            log.info(f"  Checking whether {svc_name} is running...")
            ok = service_manager.stop_kd_service_force(svc_name, 45)
            log.step_result(
                f"Stop service {svc_name}",
                ok,
                "Stopped (or was not running)" if ok else "Still running after timeout (will retry before delete)",
            )
        # Let SCM and file handles settle
        time.sleep(5)
    elif dry_run and config.get("InstallService", True):
        log.info("[DryRun] Would check each configured service, STOP it if running, then continue with "
                 "DELETE services, then DELETE folders")

    # ------------------------------------------------------------------
    # Phase 2: DELETE services (stop again immediately before each delete)
    # ------------------------------------------------------------------
    if config.get("InstallService", True):
        log.info("=== Phase 2: Removing KD-* services ===")
        for component in components:
            component_path = base_path / component
            exe_path = None
            try:
                exe_result = discovery.find_kd_component_executable(component_path, component)
                if exe_result["Success"]:
                    exe_path = exe_result["Executable"]
            except Exception:
                pass
            template = config.get("ServiceUninstallArgsTemplate")
            # skip_stop=False => stop_kd_service_force runs again inside uninstall
            svc_result = service_manager.uninstall_kd_service(
                component=component,
                executable_path=exe_path,
                args_template=template,
                dry_run=dry_run,
                skip_stop=False,
            )
            log.step_result(
                f"Uninstall service for {component}",
                svc_result["Success"],
                svc_result["Detail"],
            )
            if not svc_result["Success"] and not dry_run:
                failed.append(component)

        # Any leftover KD-* services not mapped to a config component are left
        # alone by default (only Components in the config JSON are managed).
        # Set "CleanupLeftoverServices": true in the config to also sweep
        # these up during uninstall.
        if cleanup_leftovers and not dry_run:
            try:
                leftovers = service_manager.list_kd_services()
            except Exception:
                leftovers = []
            for svc_name in leftovers:
                if svc_name in svc_names_ordered:
                    continue
                log.info(f"  CleanupLeftoverServices=true: removing leftover service {svc_name}...")
                # Check first whether it's running; if so, stop it, then continue with removal.
                service_manager.stop_kd_service_force(svc_name, 30)
                try:
                    subprocess.run(["sc.exe", "delete", svc_name], capture_output=True, timeout=30)
                    time.sleep(2)
                except Exception:
                    pass
            time.sleep(3)

    # ------------------------------------------------------------------
    # Phase 3: DELETE component folders (services must already be gone)
    # Every name listed in config["Components"] is targeted - including
    # case-insensitive folder matches under BasePath.
    # ------------------------------------------------------------------
    log.info("=== Phase 3: Deleting component folders ===")
    components_delete = list(config.get("Components") or [])
    log.info(
        f"  Components to remove under {base_path}: "
        + (", ".join(components_delete) or "(none)")
    )
    for component in components_delete:
        log.info(f"=== Deleting files for: {component} ===")
        component_path = _resolve_component_folder(base_path, component)
        try:
            if component_path is not None and component_path.exists():
                if dry_run:
                    log.info(f"  [DryRun] Would DELETE folder: {component_path}")
                    log.step_result(f"Delete files for {component}", True, f"[DryRun] {component_path}")
                else:
                    # One more stop pass in case a process respawned
                    svc_name = service_manager.get_kd_service_name(component)
                    try:
                        service_manager.stop_kd_service_force(svc_name, 15)
                    except Exception as stop_err:
                        log.warn(f"  Pre-delete stop of {svc_name}: {stop_err}")
                    # NiFi / Java often hold file locks briefly after service stop
                    if component.lower() == "nifi" and os.name == "nt":
                        try:
                            subprocess.run(
                                ["taskkill", "/F", "/IM", "java.exe", "/T"],
                                capture_output=True,
                                timeout=30,
                            )
                            time.sleep(2)
                        except Exception:
                            pass
                    log.info(f"  Removing folder: {component_path}")
                    _force_rmtree(component_path)
                    if component_path.exists():
                        raise OSError(f"Folder still present after delete: {component_path}")
                    log.step_result(f"Delete files for {component}", True, str(component_path))
            else:
                log.info(f"  Folder not present (already removed): {base_path / component}")
                log.step_result(f"Delete files for {component}", True, "(folder not present)")
            if component not in failed and component not in completed:
                completed.append(component)
        except Exception as e:
            log.error(f"  Failed to delete {component}: {e}")
            log.step_result(f"Delete files for {component}", False, str(e))
            if component not in failed:
                failed.append(component)

    # Also remove IndexPath when it lives under BasePath (indexes are not a Component)
    index_path = config.get("IndexPath")
    if index_path and not dry_run:
        try:
            idx = Path(index_path)
            if idx.exists() and idx.is_dir():
                try:
                    base_resolved = base_path.resolve() if base_path.exists() else base_path
                    under_base = (
                        base_resolved in idx.resolve().parents
                        or idx.resolve() == base_resolved
                    )
                except Exception:
                    under_base = str(idx).lower().startswith(str(base_path).lower())
                if under_base:
                    log.info(f"  Removing IndexPath under BasePath: {idx}")
                    try:
                        _force_rmtree(idx)
                        log.step_result("Delete IndexPath", True, str(idx))
                    except Exception as e:
                        log.warn(f"  Could not delete IndexPath {idx}: {e}")
                        log.step_result("Delete IndexPath", False, str(e))
        except Exception as e:
            log.warn(f"  IndexPath cleanup skipped: {e}")

    # Second pass: any configured component folder still on disk → retry once
    if not dry_run:
        log.info("=== Phase 3b: Retry any remaining component folders ===")
        for component in components_delete:
            left = _resolve_component_folder(base_path, component)
            if left is None or not left.exists():
                continue
            log.warn(f"  Folder still present, retrying: {left}")
            try:
                svc_name = service_manager.get_kd_service_name(component)
                try:
                    service_manager.stop_kd_service_force(svc_name, 10)
                except Exception:
                    pass
                time.sleep(1)
                _force_rmtree(left)
                if left.exists():
                    log.error(f"  Still could not delete {left}")
                    log.step_result(f"Retry delete {component}", False, str(left))
                    if component not in failed:
                        failed.append(component)
                    if component in completed:
                        completed.remove(component)
                else:
                    log.info(f"  Retry deleted {left}")
                    log.step_result(f"Retry delete {component}", True, str(left))
                    if component in failed:
                        failed.remove(component)
                    if component not in completed:
                        completed.append(component)
            except Exception as e:
                log.error(f"  Retry failed for {component}: {e}")
                log.step_result(f"Retry delete {component}", False, str(e))
                if component not in failed:
                    failed.append(component)

    # Report what remains under BasePath for configured component names
    if base_path.is_dir() and not dry_run:
        remaining = []
        for component in components_delete:
            p = _resolve_component_folder(base_path, component)
            if p is not None and p.exists():
                remaining.append(str(p))
        if remaining:
            log.warn("  Component folders still present after Uninstall:")
            for r in remaining:
                log.warn(f"    - {r}")
        else:
            log.info("  All configured component folders removed from BasePath.")

    # Clean state file on full success
    state_file = state.get_kd_state_file_path(base_path)
    if not dry_run and state_file.exists() and not failed:
        state_file.unlink(missing_ok=True)
        log.info("Removed install state file.")

    # ------------------------------------------------------------------
    # Phase 4: DELETE toolkit SSL folder(s)
    # Generated certs/keys/passwords live next to the installer scripts.
    # Interactive: ask and confirm before deleting (default Yes).
    # Non-interactive: delete automatically (matches config/cleanup.json).
    # ------------------------------------------------------------------
    toolkit_root = Path(__file__).resolve().parent.parent
    ssl_candidates = [
        toolkit_root / "ssl",
        toolkit_root / "SSL",
        Path(r"C:\KD-Setup\idol-windows-setup\ssl"),
        Path(r"C:\KD-Setup\kd-win-setup-python\ssl"),
    ]
    cleanup_json = toolkit_root / "config" / "cleanup.json"
    if cleanup_json.is_file():
        try:
            import json as _json
            with open(cleanup_json, encoding="utf-8") as fh:
                cdata = _json.load(fh)
            for rel in cdata.get("ToolkitFolders") or []:
                if rel and str(rel).strip("\\/").lower() == "ssl":
                    ssl_candidates.append(toolkit_root / str(rel))
        except Exception:
            pass

    seen_ssl: set = set()
    existing_ssl: list = []
    for ssl_dir in ssl_candidates:
        try:
            key = str(ssl_dir.resolve()) if ssl_dir.exists() else str(ssl_dir)
        except Exception:
            key = str(ssl_dir)
        if key in seen_ssl:
            continue
        seen_ssl.add(key)
        if ssl_dir.exists():
            existing_ssl.append(ssl_dir)

    log.info("=== Phase 4: Toolkit SSL folder ===")
    if not existing_ssl:
        log.step_result("Delete toolkit SSL folder", True, "(not present)")
    else:
        delete_ssl = False
        if dry_run:
            for ssl_dir in existing_ssl:
                log.info(f"  [DryRun] Would DELETE SSL folder: {ssl_dir}")
            log.step_result(
                "Delete toolkit SSL folder",
                True,
                f"[DryRun] {len(existing_ssl)} folder(s) would be deleted",
            )
        elif non_interactive:
            log.info("Non-interactive uninstall: deleting toolkit SSL folder(s).")
            delete_ssl = True
        else:
            paths_list = ", ".join(str(p) for p in existing_ssl)
            try:
                print(
                    f"{_PROMPT_YELLOW}Delete toolkit SSL folder(s)?\n  {paths_list}\n"
                    f"This removes generated certificates, keys and passwords. (Y/N) [Y]: {_PROMPT_RESET}",
                    end="",
                    flush=True,
                )
                ans = input().strip()
            except EOFError:
                ans = "Y"
            if not ans or ans.upper() == "Y":
                try:
                    print(
                        f"{_PROMPT_YELLOW}Confirm deletion of SSL folder(s)? Type YES to proceed: {_PROMPT_RESET}",
                        end="",
                        flush=True,
                    )
                    confirm = input().strip()
                except EOFError:
                    confirm = ""
                if confirm.upper() == "YES":
                    delete_ssl = True
                else:
                    log.info("SSL folder deletion cancelled (confirmation not YES).")
            else:
                log.info("SSL folder deletion declined by user.")

        if delete_ssl:
            for ssl_dir in existing_ssl:
                try:
                    _force_rmtree(ssl_dir)
                    log.step_result("Delete toolkit SSL folder", True, str(ssl_dir))
                except Exception as e:
                    log.step_result(
                        "Delete toolkit SSL folder", False, f"{ssl_dir}: {e}"
                    )
        elif not dry_run:
            for ssl_dir in existing_ssl:
                log.step_result(
                    "Delete toolkit SSL folder",
                    True,
                    f"(kept) {ssl_dir}",
                )

    return {"Completed": completed, "Failed": failed}


def invoke_kd_install_nifi_service(
    config: Dict[str, Any],
    dry_run: bool = False,
    start_after: bool = True,
) -> Dict[str, Any]:
    """
    Register (or re-register) the KD-NiFi Windows service via NSSM.

    Expects NiFi already extracted under BasePath\\NiFi. Callers (menu 07 /
    InstallJavaServices, or its NiFi-only alias InstallNiFiService) should
    ensure the ZIP is present (download if needed) and extract via
    _ensure_component_extracted before invoking this helper. Does not
    install other KD components.
    """
    base_path = Path(config["BasePath"])
    component = "NiFi"
    component_path = base_path / component

    # Case-insensitive folder match
    if not component_path.is_dir():
        if base_path.is_dir():
            for child in base_path.iterdir():
                if child.is_dir() and child.name.lower() == "nifi":
                    component_path = child
                    break

    if not component_path.is_dir():
        detail = (
            f"NiFi folder not found under {base_path}. "
            f"Run Install or Extract-Only first, then re-run "
            f"07) Install Java Services (NiFi|Find)."
        )
        log.error(detail)
        log.step_result("Install NiFi service", False, detail)
        return {"Completed": [], "Failed": [component], "Detail": detail}

    if dry_run:
        log.info(f"[DryRun] Would overwrite nifi.cmd and register KD-NiFi under {component_path}")
        log.step_result("Install NiFi service", True, f"[DryRun] {component_path}")
        return {"Completed": [component], "Failed": [], "Detail": "[DryRun]"}

    log.info(f"=== Install NiFi Windows service under {component_path} ===")
    deployed = _deploy_nifi_cmd(component_path)
    if deployed:
        log.info(f"  Deployed nifi.cmd: {deployed}")

    # Always restore staged NAR connectors into NiFi/extensions before (re)registering
    # the service so autoload picks them up on the next Flow Controller start.
    # Source: <SetupPath>\nifi\nifi-connectors  →  Target: <BasePath>\NiFi\extensions
    # (same path NiFi uses via nifi.nar.library.autoload.directory=./extensions)
    base_path = Path(config.get("BasePath") or component_path.parent)
    nar_result = _copy_nifi_nar_extensions(base_path, dry_run=False)
    log.step_result(
        "Copy NiFi connector .nar files -> extensions",
        nar_result.get("Success", False),
        nar_result.get("Detail", ""),
    )

    exe_result = discovery.find_kd_component_executable(component_path, component)
    if not exe_result.get("Success"):
        detail = exe_result.get("Reason") or "nifi.cmd not found"
        log.step_result("Locate NiFi launcher", False, detail)
        return {"Completed": [], "Failed": [component], "Detail": detail}

    nifi_cmd = Path(exe_result["Executable"])
    nifi_cfg = config.get("NiFi") or {}
    nifi_port = (
        nifi_cfg.get("WebHttpsPort")
        or (config.get("Ports") or {}).get("NiFi")
        or "8443"
    )
    svc_result = service_manager.install_kd_nifi_service(
        component=component,
        nifi_cmd_path=nifi_cmd,
        start_mode=config.get("StartMode", "Auto"),
        dry_run=False,
        port=nifi_port,
    )
    log.step_result(
        "Install NiFi service",
        svc_result.get("Success", False),
        svc_result.get("Detail", ""),
    )
    if not svc_result.get("Success"):
        return {
            "Completed": [],
            "Failed": [component],
            "Detail": svc_result.get("Detail", "service install failed"),
        }

    if start_after:
        # Ensure external clients can reach NiFi HTTPS port (8443 by default)
        _open_component_firewall(component, config, dry_run=False)
        nifi_home = component_path
        log.info("  Starting KD-NiFi (waiting for Flow Controller)...")
        start_result = service_manager.start_kd_nifi_and_wait_for_flow(
            service_name=service_manager.get_kd_service_name(component),
            nifi_home=nifi_home,
            timeout_seconds=300,
        )
        ok = bool(start_result.get("Success"))
        is_warn = bool(start_result.get("Warning"))
        log.step_result(
            "Start service KD-NiFi",
            ok,
            start_result.get("Detail") or "",
            warning=is_warn,
        )
        if not ok:
            return {
                "Completed": [component],
                "Failed": [],
                "Detail": (
                    "Service registered but failed to start: "
                    + str(start_result.get("Detail") or "")
                ),
                "Started": False,
            }
        return {
            "Completed": [component],
            "Failed": [],
            "Detail": start_result.get("Detail") or "Service installed and started",
            "Started": True,
        }

    return {
        "Completed": [component],
        "Failed": [],
        "Detail": svc_result.get("Detail") or "Service installed (not started)",
        "Started": False,
    }


def invoke_kd_install_find_service(
    config: Dict[str, Any],
    dry_run: bool = False,
    start_after: bool = True,
) -> Dict[str, Any]:
    """
    Register (or re-register) the KD-Find Windows service via NSSM.

    Expects Find already extracted under BasePath\\Find. Callers (menu 07 /
    InstallJavaServices) should ensure the ZIP is present and extract via
    _ensure_component_extracted before invoking this helper. Does not
    install other KD components.
    """
    base_path = Path(config["BasePath"])
    component = "Find"
    component_path = base_path / component

    # Case-insensitive folder match
    if not component_path.is_dir():
        if base_path.is_dir():
            for child in base_path.iterdir():
                if child.is_dir() and child.name.lower() == "find":
                    component_path = child
                    break

    if not component_path.is_dir():
        detail = (
            f"Find folder not found under {base_path}. "
            f"Run Install or Extract-Only first, then re-run "
            f"07) Install Java Services (NiFi|Find)."
        )
        log.error(detail)
        log.step_result("Install Find service", False, detail)
        return {"Completed": [], "Failed": [component], "Detail": detail}

    if dry_run:
        log.info(f"[DryRun] Would deploy config.json and register KD-Find under {component_path}")
        log.step_result("Install Find service", True, f"[DryRun] {component_path}")
        return {"Completed": [component], "Failed": [], "Detail": "[DryRun]"}

    log.info(f"=== Install Find Windows service under {component_path} ===")

    find_cfg = config.get("Find") or {}
    home_name = str(find_cfg.get("HomeDir") or "home")
    deploy = _deploy_find_config_json(component_path, home_dir_name=home_name, dry_run=False)
    log.step_result(
        "Deploy Find config.json",
        bool(deploy.get("Success")),
        deploy.get("Detail") or "",
    )

    exe_result = discovery.find_kd_component_executable(component_path, component)
    if not exe_result.get("Success"):
        detail = exe_result.get("Reason") or "find.war not found"
        log.step_result("Locate Find launcher", False, detail)
        return {"Completed": [], "Failed": [component], "Detail": detail}

    find_war = Path(exe_result["Executable"])
    ports = config.get("Ports") or {}
    server_port = str(find_cfg.get("ServerPort") or ports.get("Find") or "8080")
    svc_result = service_manager.install_kd_find_service(
        component=component,
        find_war_path=find_war,
        start_mode=config.get("StartMode", "Auto"),
        server_port=server_port,
        heap_xms=str(find_cfg.get("HeapXms") or "1g"),
        heap_xmx=str(find_cfg.get("HeapXmx") or "2g"),
        home_dir_name=home_name,
        dry_run=False,
    )
    log.step_result(
        "Install Find service",
        svc_result.get("Success", False),
        svc_result.get("Detail", ""),
    )
    if not svc_result.get("Success"):
        return {
            "Completed": [],
            "Failed": [component],
            "Detail": svc_result.get("Detail", "service install failed"),
        }

    if start_after:
        # Ensure external clients can reach Find UI port (8080 by default)
        # the same way NiFi exposes 8443 before start
        _open_component_firewall(component, config, dry_run=False)
        svc_name = service_manager.get_kd_service_name(component)
        log.info(f"  Starting {svc_name}...")
        started = service_manager.start_kd_service(svc_name, timeout_seconds=180)
        log.step_result(f"Start service {svc_name}", started, "" if started else "Did not reach Running in time")
        return {
            "Completed": [component],
            "Failed": [],
            "Detail": svc_result.get("Detail") or "Service installed",
            "Started": started,
        }

    return {
        "Completed": [component],
        "Failed": [],
        "Detail": svc_result.get("Detail") or "Service installed (not started)",
        "Started": False,
    }


def invoke_kd_set_nifi_credentials(
    config: Dict[str, Any],
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Apply NiFi single-user UI credentials on an already-extracted BasePath\\NiFi tree.

    Username / password come from explicit args when provided, otherwise from
    config NiFi.Username / NiFi.Password. Does not start or stop the service;
    restart KD-NiFi afterwards if it is already running so the new login applies.
    """
    base_path = Path(config["BasePath"])
    component = "NiFi"
    component_path = base_path / component

    if not component_path.is_dir():
        if base_path.is_dir():
            for child in base_path.iterdir():
                if child.is_dir() and child.name.lower() == "nifi":
                    component_path = child
                    break

    if not component_path.is_dir():
        detail = (
            f"NiFi folder not found under {base_path}. "
            f"Run Install or Extract-Only first, then re-run SetNiFiCredentials."
        )
        log.error(detail)
        log.step_result("Set NiFi UI credentials", False, detail)
        return {"Completed": [], "Failed": [component], "Detail": detail}

    nifi_cfg = config.get("NiFi") or {}
    user = (username if username is not None else str(nifi_cfg.get("Username") or "")).strip()
    pwd = (password if password is not None else str(nifi_cfg.get("Password") or "")).strip()

    if not user or not pwd:
        detail = (
            "Username and password are required. Set NiFi.Username / NiFi.Password "
            "in the config JSON, or pass --nifi-username / --nifi-password."
        )
        log.error(detail)
        log.step_result("Set NiFi UI credentials", False, detail)
        return {"Completed": [], "Failed": [component], "Detail": detail}

    if dry_run:
        log.info(
            f"[DryRun] Would set NiFi single-user credentials for user '{user}' "
            f"under {component_path}"
        )
        log.step_result("Set NiFi UI credentials", True, f"[DryRun] user={user}")
        return {"Completed": [component], "Failed": [], "Detail": "[DryRun]"}

    log.info(f"=== Set NiFi UI credentials under {component_path} (user='{user}') ===")
    result = _set_nifi_single_user_credentials(component_path, user, pwd)
    ok = bool(result.get("Success")) and not result.get("Skipped")
    # Skipped only when user/pass empty - we already guard that above
    if result.get("Skipped"):
        ok = False
    log.step_result(
        "Set NiFi UI credentials",
        ok,
        result.get("Detail") or ("OK" if ok else "failed"),
    )
    if ok:
        log.info(
            "  Credentials updated in login-identity-providers.xml. "
            "If KD-NiFi is running, restart it for the new login to take effect:"
        )
        log.info(r"    .\Manage-KDServices.ps1 restart -Components NiFi")
        log.info(r"    or:  sc stop KD-NiFi & sc start KD-NiFi")
        return {"Completed": [component], "Failed": [], "Detail": result.get("Detail")}
    return {
        "Completed": [],
        "Failed": [component],
        "Detail": result.get("Detail") or "set-single-user-credentials failed",
    }



def invoke_kd_repair(
    config: Dict[str, Any],
    dry_run: bool = False,
    force: bool = False,
    non_interactive: bool = False,
    config_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    log.info("Repair mode: performing uninstall then fresh install...")
    un = invoke_kd_uninstall(
        config, dry_run=dry_run, non_interactive=non_interactive
    )
    if un["Failed"] and not force:
        log.warn("Uninstall phase had failures; aborting repair (use --force to continue).")
        return {
            "Completed": un["Completed"],
            "Failed": un["Failed"],
            "StateFile": str(state.get_kd_state_file_path(config["BasePath"])),
        }
    inst = invoke_kd_install(
        config,
        dry_run=dry_run,
        force=force,
        resume=False,
        non_interactive=non_interactive,
        config_path=config_path,
    )
    return {
        "Completed": inst["Completed"],
        "Failed": list(set(un["Failed"] + inst["Failed"])),
        "StateFile": inst["StateFile"],
    }


def invoke_kd_configure(config: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    base_path = Path(config["BasePath"])
    completed: List[str] = []
    failed: List[str] = []

    if not base_path.exists():
        log.error(f"BasePath does not exist: {base_path}. Cannot configure.")
        return {"Completed": [], "Failed": list(config.get("Components", []))}

    for component in config.get("Components", []):
        log.info(f"=== Configuring component: {component} ===")
        component_path = base_path / component
        if not component_path.exists():
            log.step_result(f"Configure {component}", False, "Component folder not found")
            failed.append(component)
            continue
        try:
            if component.lower() == "nifi":
                nifi_cfg = config.get("NiFi") or {}
                # Ensure extensions folder has staged connectors before property updates
                # so the next NiFi start loads them (nifi.nar.library.autoload.directory=./extensions)
                if not dry_run:
                    nar_result = _copy_nifi_nar_extensions(base_path, dry_run=False)
                    log.step_result(
                        "Copy NiFi connector .nar files -> extensions",
                        nar_result.get("Success", False),
                        nar_result.get("Detail", ""),
                    )
                props_file = next(component_path.rglob("nifi.properties"), None)
                boot_file = next(component_path.rglob("bootstrap.conf"), None)
                if not props_file:
                    log.step_result(f"Configure {component}", False, "nifi.properties not found")
                    failed.append(component)
                else:
                    if not dry_run:
                        # Prefer KD template (nifi/conf/nifi.properties) so placeholders are present
                        copied = _deploy_nifi_properties_template(component_path)
                        if copied:
                            props_file = copied
                            log.info(f"  Deployed nifi.properties template -> {props_file}")
                        port = str(
                            nifi_cfg.get("WebHttpsPort") or (config.get("Ports") or {}).get("NiFi") or "8443"
                        )
                        host = str(nifi_cfg.get("WebHttpsHost") or "0.0.0.0").strip() or "0.0.0.0"
                        # EXTRA_IP_SANS from NiFi.ExternalIIPSAN only.
                        # Rule: if empty → 127.0.0.1 else the given value.
                        if not str(nifi_cfg.get("ExternalIIPSAN") or "").strip():
                            legacy = str(nifi_cfg.get("ExternalIpAddress") or "").strip()
                            if legacy:
                                nifi_cfg["ExternalIIPSAN"] = legacy
                            nifi_cfg.pop("ExternalIpAddress", None)
                        extra_ip = str(nifi_cfg.get("ExternalIIPSAN") or "").strip()
                        if not extra_ip:
                            extra_ip = "127.0.0.1"
                        proxy_parts = [f"localhost:{port}", f"{host}:{port}"]
                        if extra_ip and extra_ip not in (host, "0.0.0.0"):
                            if f"{extra_ip}:{port}" not in proxy_parts:
                                proxy_parts.append(f"{extra_ip}:{port}")
                        updates = {
                            "nifi.sensitive.props.key": nifi_cfg.get("SensitivePropsKey") or "ChangeMe-StrongPassword123!",
                            "nifi.web.https.port": port,
                            "nifi.web.https.host": host,
                            "nifi.web.proxy.host": ",".join(proxy_parts),
                        }
                        # Deploy generated SSL keystore/truststore and wire nifi.security.*
                        ssl_mat = _deploy_nifi_ssl_material(
                            component_path,
                            setup_path=Path(str(config.get("SetupPath") or "")) if config.get("SetupPath") else None,
                        )
                        if ssl_mat.get("ok"):
                            log.info(f"  {ssl_mat.get('detail')}")
                            updates.update(_nifi_security_property_updates(ssl_mat))
                            if not ssl_mat.get("keystore_pass") or not ssl_mat.get("truststore_pass"):
                                log.warn(
                                    "  SSL PKCS12 files copied but passwords not found "
                                    "(check env/.idol-ssl-passwords.env or ssl-passwords.txt)"
                                )
                        else:
                            log.warn(f"  NiFi SSL material not deployed: {ssl_mat.get('detail')}")
                        _update_properties_file(props_file, updates)
                        sec_note = ""
                        if ssl_mat.get("ok") and ssl_mat.get("keystore_pass"):
                            sec_note = " + security keystore/truststore passwords"
                        log.info(
                            f"  nifi.properties updated "
                            f"(sensitive key + https host={host} port={port}"
                            f" + proxy.host={updates['nifi.web.proxy.host']}"
                            f"{sec_note})"
                        )
                        if boot_file:
                            xms = nifi_cfg.get("HeapXms", "8g")
                            xmx = nifi_cfg.get("HeapXmx", "16g")
                            _update_properties_file(
                                boot_file,
                                {"java.arg.2": f"-Xms{xms}", "java.arg.3": f"-Xmx{xmx}"},
                            )
                        nifi_cmd_dest = _deploy_nifi_cmd(component_path)
                        if nifi_cmd_dest:
                            log.info(f"  Deployed nifi.cmd -> {nifi_cmd_dest}")
                        creds = _set_nifi_single_user_credentials(
                            component_path,
                            str(nifi_cfg.get("Username") or ""),
                            str(nifi_cfg.get("Password") or ""),
                        )
                        if creds.get("Skipped"):
                            log.info(f"  {creds.get('Detail')}")
                        elif creds.get("Success"):
                            log.info(f"  {creds.get('Detail')}")
                        else:
                            log.warn(f"  NiFi credentials not applied: {creds.get('Detail')}")
                    log.step_result(f"Configure {component}", True, str(props_file))
                    completed.append(component)
            elif component.lower() == "find":
                find_cfg = config.get("Find") or {}
                home_name = str(find_cfg.get("HomeDir") or "home")
                deploy = _deploy_find_config_json(
                    component_path,
                    home_dir_name=home_name,
                    dry_run=dry_run,
                )
                log.step_result(
                    f"Configure {component}",
                    bool(deploy.get("Success")),
                    deploy.get("Detail") or "",
                )
                if deploy.get("Success"):
                    completed.append(component)
                else:
                    failed.append(component)
            else:
                # Overwrite toolkit cfg templates, then apply runtime patches
                log.info(f"  Configuring {component} (.cfg templates + runtime patches)")
                _deploy_cfg_templates(component, component_path, dry_run=dry_run)

                if component.lower() == "community":
                    aes_result = _ensure_community_aes_key(component_path, dry_run=dry_run)
                    if not aes_result.get("Success"):
                        log.warn(f"  Community aes.key step: {aes_result.get('Detail')}")
                    else:
                        log.info(f"  Community aes.key: {aes_result.get('Detail')}")

                cfg = _apply_standard_cfg_patches(
                    component, component_path, config, dry_run=dry_run
                )
                if cfg is None:
                    log.step_result(f"Configure {component}", False, "No .cfg file found")
                    failed.append(component)
                else:
                    log.step_result(f"Configure {component}", True, str(cfg))
                    completed.append(component)

                if component == "LicenseServer" and not dry_run:
                    for idol in component_path.rglob("idol.common.cfg"):
                        ini_config.update_kd_ini_file(idol, "License", "LicenseServerHost", "licenseserver")
                        ini_config.update_kd_ini_file(idol, "License", "Clients", "*.*.*.*")
                        log.step_result("Update idol.common.cfg (LicenseServer)", True, str(idol))

                    key_path = config.get("LicenseKeyPath")
                    if key_path and Path(key_path).is_file():
                        target_dir = component_path
                        versioned = next(
                            (d for d in component_path.iterdir() if d.is_dir() and "licenseserver" in d.name.lower() and "windows" in d.name.lower()),
                            None,
                        )
                        if versioned:
                            target_dir = versioned
                        dest_key = target_dir / "licensekey.dat"
                        shutil.copy2(key_path, dest_key)
                        log.info(f"  Copied license key -> {dest_key}")
        except Exception as e:
            log.error(f"Unexpected error configuring {component}: {e}")
            failed.append(component)

    return {"Completed": completed, "Failed": failed}


def invoke_kd_health_check(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Health-check only services that were supposed to be installed for Components."""
    results = []
    for component in config.get("Components", []):
        # Package-only (e.g. NiFiIngest): never a Windows service
        if _is_package_only_component(component):
            results.append({
                "Component": component,
                "Success": True,
                "Detail": "Skipped (package-only — NiFi connector NARs; no Windows service)",
            })
            continue
        if component.lower() == "nifi" and not _nifi_install_service_enabled(config):
            results.append({
                "Component": component,
                "Success": True,
                "Detail": "Skipped (NiFi.InstallService is false; no Windows service expected)",
            })
            continue
        if component.lower() == "find":
            find_cfg = config.get("Find") or {}
            if find_cfg.get("InstallService", True) is False:
                results.append({
                    "Component": component,
                    "Success": True,
                    "Detail": "Skipped (Find.InstallService is false; no Windows service expected)",
                })
                continue
        if not config.get("InstallService", True):
            results.append({
                "Component": component,
                "Success": True,
                "Detail": "Skipped (InstallService is false; no Windows service expected)",
            })
            continue
        try:
            h = service_manager.test_kd_service_healthy(component)
            results.append({"Component": component, "Success": h["Success"], "Detail": h["Detail"]})
        except Exception as e:
            results.append({"Component": component, "Success": False, "Detail": f"Health check error: {e}"})
    return results

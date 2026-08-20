"""
Idempotent Windows service install / uninstall / health-check for KD components.
Uses the component binary's own CLI switches (templated) + sc.exe as fallback.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .logging import log, status_line, status_line_clear, status_line_finish

DEFAULT_INSTALL_TEMPLATE = ["-install", "-servicename", "{ServiceName}", "-displayname", "{DisplayName}"]
DEFAULT_UNINSTALL_TEMPLATE = ["-remove", "-servicename", "{ServiceName}"]

# Entries that may appear in config Components but never get a Windows service
# (NiFiIngest = connector NAR package only).
_PACKAGE_ONLY_COMPONENTS = frozenset({"nifiingest"})


def is_package_only_component(component: str) -> bool:
    return (component or "").strip().lower() in _PACKAGE_ONLY_COMPONENTS



def get_kd_service_name(component: str) -> str:
    """
    Map a component name (or an already-qualified service name) to the
    Windows service name.

    Examples:
      Content      → KD-Content
      NiFi         → KD-NiFi
      KD-Content   → KD-Content   (idempotent)
      kd-nifi      → KD-nifi
    """
    name = (component or "").strip()
    if not name:
        return "KD-"
    if name[:3].upper() == "KD-":
        return "KD-" + name[3:]
    return f"KD-{name}"


def service_name_to_component(service_name: str) -> str:
    """Inverse of get_kd_service_name: KD-Content → Content, KD-NiFi → NiFi."""
    name = (service_name or "").strip()
    if name[:3].upper() == "KD-":
        return name[3:]
    return name


def expand_kd_args_template(template: List[str], values: Dict[str, str]) -> List[str]:
    result = []
    for token in template:
        val = token
        for k, v in values.items():
            val = val.replace(f"{{{k}}}", v)
        result.append(val)
    return result


def _run_captured(cmd: List[str], cwd: Optional[str] = None, timeout: int = 120) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
        )
        return {
            "ExitCode": proc.returncode,
            "StdOut": proc.stdout or "",
            "StdErr": proc.stderr or "",
        }
    except subprocess.TimeoutExpired:
        return {"ExitCode": -1, "StdOut": "", "StdErr": "Timeout"}
    except Exception as e:
        return {"ExitCode": -1, "StdOut": "", "StdErr": str(e)}


def _sc(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["sc.exe", *args], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args=["sc.exe", *args], returncode=-1, stdout="", stderr="Timeout")


def set_kd_service_display_name(component: str) -> None:
    """Force correct DisplayName (many OpenText binaries ignore -displayname)."""
    svc_name = get_kd_service_name(component)
    if component.lower() == "nifi":
        display = "OpenText KD Apache NiFi"
    elif component.lower() == "find":
        display = "OpenText KD Find"
    else:
        display = f"OpenText KD {component}"
    try:
        _sc("config", svc_name, f"DisplayName={display}")
        log.debug(f"DisplayName corrected for {svc_name} → '{display}'")
    except Exception as e:
        log.warn(f"Failed to set DisplayName for {svc_name}: {e}")


def _validate_tcp_port(port: Optional[str | int]) -> tuple[bool, str, Optional[int]]:
    """
    Validate a TCP port value before creating firewall rules.

    Returns:
      (ok, detail_or_error, port_int_or_None)

    Accepts int or digit-only string. Rejects empty, non-numeric, floats,
    and values outside 1..65535.
    """
    if port is None:
        return False, "port is None", None
    raw = str(port).strip()
    if not raw:
        return False, "port is empty", None
    # Reject floats / scientific notation / signs by requiring pure digits
    if not raw.isdigit():
        return False, f"port must be an integer (got {raw!r})", None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return False, f"port is not a valid integer (got {raw!r})", None
    if value < 1 or value > 65535:
        return False, f"port out of range 1-65535 (got {value})", None
    return True, "", value


def open_kd_firewall_ports(
    port: Optional[str | int] = None,
    component: str = "",
    *,
    also_icmp: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Open Windows Firewall inbound rules so external clients can reach the
    component service port (and optionally allow ICMPv4).

    Matches the operator pattern:
      New-NetFirewallRule -DisplayName "Temp-KD-9100" -Direction Inbound
        -Protocol TCP -LocalPort 9100 -Action Allow -Profile Any
      New-NetFirewallRule -DisplayName "Temp-ICMP" -Direction Inbound
        -Protocol ICMPv4 -Action Allow -Profile Any

    The port is validated (integer in 1..65535) before any rule is created.
    Rules are created idempotently (skipped when a rule with the same
    DisplayName already exists). Failures are logged but do not raise.
    """
    results: List[str] = []
    label = f" ({component})" if component else ""

    if port is None or str(port).strip() == "":
        return {
            "Success": True,
            "Skipped": True,
            "Detail": f"No port provided for firewall open{label}",
        }

    ok, err_msg, port_int = _validate_tcp_port(port)
    if not ok or port_int is None:
        detail = f"Invalid port for firewall open{label}: {err_msg}"
        log.warn(f"  {detail} — skipping firewall rule creation")
        return {"Success": False, "Skipped": True, "Detail": detail}

    port_str = str(port_int)
    display_tcp = f"Temp-KD-{port_str}"
    display_icmp = "Temp-ICMP"

    if dry_run:
        detail = f"[DryRun] Would open firewall TCP {port_str} as '{display_tcp}'"
        if also_icmp:
            detail += f" and ICMP as '{display_icmp}'"
        log.info(f"  {detail}")
        return {"Success": True, "Skipped": True, "Detail": detail}

    # Build a single PowerShell snippet that is idempotent and quiet on success.
    # port_str is already validated as digits-only in 1..65535.
    # IMPORTANT: use ${var} when a variable is followed by ':' — otherwise
    # PowerShell treats $tcpName:$port as an invalid drive-qualified reference
    # ("Variable reference is not valid. ':' was not followed by a valid
    # variable name character").
    ps_lines = [
        "$ErrorActionPreference = 'Stop'",
        f"$tcpName = '{display_tcp}'",
        f"$port = {port_str}",
        "if ($port -lt 1 -or $port -gt 65535) { throw \"Invalid port: ${port}\" }",
        "if (-not (Get-NetFirewallRule -DisplayName $tcpName -ErrorAction SilentlyContinue)) {",
        "  New-NetFirewallRule -DisplayName $tcpName -Direction Inbound "
        "-Protocol TCP -LocalPort $port -Action Allow -Profile Any | Out-Null",
        "  Write-Output \"CREATED_TCP:${tcpName}:${port}\"",
        "} else {",
        "  Write-Output \"EXISTS_TCP:${tcpName}:${port}\"",
        "}",
    ]
    if also_icmp:
        ps_lines.extend(
            [
                f"$icmpName = '{display_icmp}'",
                "if (-not (Get-NetFirewallRule -DisplayName $icmpName -ErrorAction SilentlyContinue)) {",
                "  New-NetFirewallRule -DisplayName $icmpName -Direction Inbound "
                "-Protocol ICMPv4 -Action Allow -Profile Any | Out-Null",
                "  Write-Output \"CREATED_ICMP:${icmpName}\"",
                "} else {",
                "  Write-Output \"EXISTS_ICMP:${icmpName}\"",
                "}",
            ]
        )

    ps_script = "; ".join(ps_lines)
    log.info(f"  Opening Windows Firewall for port {port_str}{label} (DisplayName={display_tcp})...")
    try:
        result = _run_captured(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-Command",
                ps_script,
            ],
            timeout=60,
        )
        out = (result.get("StdOut") or "").strip()
        err = (result.get("StdErr") or "").strip()
        code = result.get("ExitCode", -1)

        if code == 0:
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("CREATED_TCP:"):
                    log.info(f"  Firewall rule created: {display_tcp} (TCP {port_str} inbound Allow)")
                    results.append(f"created TCP {port_str}")
                elif line.startswith("EXISTS_TCP:"):
                    log.info(f"  Firewall rule already present: {display_tcp} (TCP {port_str})")
                    results.append(f"exists TCP {port_str}")
                elif line.startswith("CREATED_ICMP:"):
                    log.info(f"  Firewall rule created: {display_icmp} (ICMPv4 inbound Allow)")
                    results.append("created ICMP")
                elif line.startswith("EXISTS_ICMP:"):
                    log.info(f"  Firewall rule already present: {display_icmp}")
                    results.append("exists ICMP")
            detail = "; ".join(results) if results else f"opened TCP {port_str}"
            return {"Success": True, "Skipped": False, "Detail": detail}
        else:
            detail = f"Firewall open failed for port {port_str} (exit {code})"
            if err:
                detail += f": {err[:300]}"
            elif out:
                detail += f": {out[:300]}"
            log.warn(f"  {detail}")
            return {"Success": False, "Skipped": False, "Detail": detail}
    except Exception as e:
        detail = f"Exception opening firewall for port {port_str}: {e}"
        log.warn(f"  {detail}")
        return {"Success": False, "Skipped": False, "Detail": detail}


def stop_kd_service_force(service_name: str, timeout_seconds: int = 30) -> bool:
    """Aggressively stop a service and wait until Stopped (or kill process)."""
    # Check existence
    r = _sc("query", service_name)
    if r.returncode != 0:
        return True  # already gone

    log.info(f"Stopping service {service_name}...")
    _sc("stop", service_name)

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        r = _sc("query", service_name)
        if "STOPPED" in (r.stdout or "").upper() or r.returncode != 0:
            return True
        time.sleep(0.5)

    # Force-kill via taskkill if we can find the PID
    try:
        # sc queryex gives PID
        r = subprocess.run(
            ["sc.exe", "queryex", service_name],
            capture_output=True,
            text=True,
        )
        for line in (r.stdout or "").splitlines():
            if "PID" in line.upper():
                parts = line.split(":")
                if len(parts) > 1:
                    pid = parts[1].strip()
                    if pid.isdigit() and int(pid) > 0:
                        log.warn(f"Service {service_name} still running (PID {pid}); force-killing")
                        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                        time.sleep(2)
                        break
    except Exception:
        pass

    r = _sc("query", service_name)
    return "STOPPED" in (r.stdout or "").upper() or r.returncode != 0


def remove_kd_service_if_present(service_name: str, timeout_seconds: int = 30) -> Dict[str, Any]:
    """
    Check whether a service exists and is running; if running, stop it first,
    then delete it. If it exists but is already stopped, delete it directly
    (skips the redundant stop call). If it isn't present at all, this is a
    no-op success.

    Used consistently in both directions:
      - Install: clean up any stale/broken same-named service before
        registering a fresh one (instead of silently skipping).
      - Uninstall: final removal of each KD-* service.
    """
    r = _sc("query", service_name)
    if r.returncode != 0:
        return {"Success": True, "Detail": f"{service_name} not present; nothing to remove"}

    is_running = "RUNNING" in (r.stdout or "").upper()
    if is_running:
        log.info(f"  {service_name} is running - stopping before delete...")
        stop_kd_service_force(service_name, timeout_seconds)
    else:
        log.info(f"  {service_name} exists but is already stopped - deleting directly")

    _sc("delete", service_name)
    time.sleep(1)

    r2 = _sc("query", service_name)
    still_there = r2.returncode == 0
    return {
        "Success": not still_there,
        "Detail": (
            f"{service_name} still present (may require reboot)"
            if still_there
            else f"Removed {service_name}"
        ),
    }


def list_kd_services() -> List[str]:
    """Return all Windows service names that start with KD- (running or not)."""
    names: List[str] = []
    try:
        # sc query type= service state= all is verbose; use Get-Service via powershell for reliability
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Service -Name 'KD-*' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        for line in (proc.stdout or "").splitlines():
            name = line.strip()
            if name.startswith("KD-"):
                names.append(name)
    except Exception:
        pass
    # Fallback: try sc query for each known pattern is hard; also try WMIC-style via sc
    if not names:
        try:
            proc = subprocess.run(
                ["sc.exe", "query", "state=", "all"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            current = None
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if line.upper().startswith("SERVICE_NAME:"):
                    current = line.split(":", 1)[-1].strip()
                if current and current.startswith("KD-") and current not in names:
                    names.append(current)
                    current = None
        except Exception:
            pass
    return sorted(set(names))



def install_kd_service(
    component: str,
    executable_path: str | Path,
    start_mode: str = "Auto",
    args_template: Optional[List[str]] = None,
    dry_run: bool = False,
    port: Optional[str | int] = None,
) -> Dict[str, Any]:
    if is_package_only_component(component):
        return {
            "Success": True,
            "Skipped": True,
            "ServiceName": get_kd_service_name(component),
            "Detail": "Skipped (package-only — no Windows service for this component)",
        }
    svc_name = get_kd_service_name(component)
    exe = Path(executable_path)

    # Already exists? Clean it up first (stop if running, then delete) rather
    # than silently leaving a possibly-stale/broken registration in place -
    # this matters most for Repair / Force reinstall paths.
    r = _sc("query", svc_name)
    if r.returncode == 0:
        if dry_run:
            return {
                "Success": True,
                "Skipped": True,
                "ServiceName": svc_name,
                "Detail": "[DryRun] Service already exists; would stop-if-running and reinstall",
            }
        cleanup = remove_kd_service_if_present(svc_name)
        if not cleanup["Success"]:
            return {
                "Success": False,
                "Skipped": False,
                "ServiceName": svc_name,
                "Detail": f"Could not remove existing service before reinstall: {cleanup['Detail']}",
            }
        log.info(f"  {cleanup['Detail']}; proceeding with fresh install")

    template = args_template or DEFAULT_INSTALL_TEMPLATE
    values = {
        "ServiceName": svc_name,
        "DisplayName": f"OpenText KD {component}",
        "StartMode": start_mode,
    }
    args = expand_kd_args_template(template, values)
    cmd_line = f"{exe} {' '.join(args)}"

    if dry_run:
        if port is not None:
            open_kd_firewall_ports(port, component=component, dry_run=True)
        return {
            "Success": True,
            "Skipped": True,
            "ServiceName": svc_name,
            "Detail": f"[DryRun] Would run: {cmd_line}",
        }

    try:
        result = _run_captured([str(exe), *args], cwd=str(exe.parent))
        time.sleep(2)

        r = _sc("query", svc_name)
        if r.returncode == 0:
            set_kd_service_display_name(component)
            # Open firewall so external clients can reach this component
            if port is not None:
                fw = open_kd_firewall_ports(port, component=component, dry_run=False)
                if not fw.get("Success"):
                    log.warn(
                        f"  Service {svc_name} installed, but firewall open "
                        f"for port {port} had issues: {fw.get('Detail')}"
                    )
            return {
                "Success": True,
                "Skipped": False,
                "ServiceName": svc_name,
                "Detail": "Installed",
            }

        detail = f"Command: {cmd_line} | ExitCode: {result['ExitCode']}"
        if result["StdOut"].strip():
            detail += f" | StdOut: {result['StdOut'].strip()[:300]}"
        if result["StdErr"].strip():
            detail += f" | StdErr: {result['StdErr'].strip()[:300]}"
        return {
            "Success": False,
            "Skipped": False,
            "ServiceName": svc_name,
            "Detail": detail,
        }
    except Exception as e:
        return {
            "Success": False,
            "Skipped": False,
            "ServiceName": svc_name,
            "Detail": f"Exception during install: {e}",
        }


def _toolkit_nifi_setup_ps1() -> Optional[Path]:
    """Path to nifi/Setup-NiFi-Service.ps1 shipped with the toolkit."""
    root = Path(__file__).resolve().parent.parent
    candidate = root / "nifi" / "Setup-NiFi-Service.ps1"
    return candidate if candidate.is_file() else None


def install_kd_nifi_service(
    component: str,
    nifi_cmd_path: str | Path,
    start_mode: str = "Auto",
    dry_run: bool = False,
    port: Optional[str | int] = None,
) -> Dict[str, Any]:
    """
    Register Apache NiFi as a Windows service using NSSM.

    Apache NiFi is not a self-registering Win32 service binary (no -install
    switch like the KD IDOL components have), so we wrap nifi.cmd under
    NSSM. The preferred path is the toolkit helper
    ``nifi/Setup-NiFi-Service.ps1`` (same logic operators can run by hand).
    If that script is missing, falls back to inline NSSM configuration.

    NSSM layout (matches OpenText / operator NSSM editor):
      Application:  %COMSPEC% (cmd.exe)
      AppDirectory: <NIFI_HOME>\\bin
      AppParameters: /c nifi.cmd run

    Service name is always **KD-NiFi**. Legacy names ``KD-Apache-NiFi`` and
    ``ApacheNifiService`` are removed if present.

    When *port* is provided (typically NiFi WebHttpsPort / Ports.NiFi), an
    inbound firewall rule is opened so external clients can reach the UI.
    """
    svc_name = get_kd_service_name(component)  # KD-NiFi
    nifi_cmd = Path(nifi_cmd_path).resolve()
    bin_dir = nifi_cmd.parent
    nifi_home = bin_dir.parent
    display = "OpenText KD Apache NiFi"
    logs_dir = nifi_home / "logs"
    # Legacy names (old standalone helpers / early Setup-NiFi-Service.ps1)
    legacy_svcs = ("KD-Apache-NiFi", "ApacheNifiService")
    comspec = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"

    # AppDirectory is bin/, so use relative nifi.cmd (same as NSSM editor screenshot)
    app_params = "/c nifi.cmd run"
    launcher_label = "nifi.cmd"

    def _open_nifi_firewall() -> None:
        if port is not None:
            fw = open_kd_firewall_ports(port, component=component, dry_run=dry_run)
            if not fw.get("Success") and not dry_run:
                log.warn(
                    f"  Service {svc_name} installed, but firewall open "
                    f"for port {port} had issues: {fw.get('Detail')}"
                )

    # --- Preferred path: nifi/Setup-NiFi-Service.ps1 ---
    setup_ps1 = _toolkit_nifi_setup_ps1()
    if setup_ps1 is not None:
        if dry_run:
            _open_nifi_firewall()
            port_note = f" -Port {port}" if port is not None else ""
            return {
                "Success": True,
                "Skipped": True,
                "ServiceName": svc_name,
                "Detail": (
                    f"[DryRun] Would run Setup-NiFi-Service.ps1 "
                    f"-NifiHome \"{nifi_home}\" -ServiceName {svc_name} "
                    f"-NonInteractive{port_note}"
                ),
            }
        try:
            from . import prerequisites  # local import to avoid circularity

            logs_dir.mkdir(parents=True, exist_ok=True)
            nssm_result = prerequisites.ensure_nssm(dry_run=False)
            nssm_path = nssm_result.get("Path") if nssm_result.get("Success") else None

            ps_args = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-File", str(setup_ps1),
                "-NifiHome", str(nifi_home),
                "-ServiceName", svc_name,
                "-NonInteractive",
            ]
            if nssm_path:
                ps_args.extend(["-NssmPath", str(nssm_path)])
            if port is not None and str(port).strip():
                ps_args.extend(["-Port", str(port).strip()])

            log.info(f"  Invoking Setup-NiFi-Service.ps1 for {svc_name} ...")
            log.info(f"  Script: {setup_ps1}")
            result = _run_captured(ps_args, cwd=str(bin_dir))
            # Surface a short excerpt of script output for the step log
            combined = ((result["StdOut"] or "") + "\n" + (result["StdErr"] or "")).strip()
            for line in combined.splitlines()[-12:]:
                if line.strip():
                    log.info(f"    {line.rstrip()}")

            r = _sc("query", svc_name)
            if r.returncode == 0:
                set_kd_service_display_name(component)
                # Permanent JAVA_HOME / NIFI_HOME when missing (script sets AppEnvironmentExtra)
                java_home = (
                    os.environ.get("JAVA_HOME")
                    or os.environ.get("JDK_HOME")
                    or prerequisites.get_permanent_env_var("JAVA_HOME")
                )
                if java_home:
                    home_path = prerequisites.ensure_java_home_and_path(java_home)
                    if home_path.get("Success"):
                        log.info(f"  {home_path['Detail']}")
                env_nifi = prerequisites.set_permanent_env_var_if_missing("NIFI_HOME", str(nifi_home))
                if env_nifi.get("Success"):
                    log.info(f"  {env_nifi['Detail']}")

                _open_nifi_firewall()
                return {
                    "Success": True,
                    "Skipped": False,
                    "ServiceName": svc_name,
                    "Detail": f"Installed via Setup-NiFi-Service.ps1 -> {launcher_label} (run)",
                }

            detail = (
                f"Setup-NiFi-Service.ps1 exit {result['ExitCode']}; "
                f"sc query {svc_name} failed"
            )
            if combined:
                detail += f" | {combined[-400:]}"
            # Fall through to inline NSSM if the script did not leave a service
            log.warn(f"  {detail}; falling back to inline NSSM install")
        except Exception as e:
            log.warn(f"  Setup-NiFi-Service.ps1 failed ({e}); falling back to inline NSSM install")

    # --- Fallback: inline NSSM (same behaviour as Setup-NiFi-Service.ps1) ---
    for name in (svc_name, *legacy_svcs):
        r = _sc("query", name)
        if r.returncode == 0:
            if dry_run:
                log.info(f"[DryRun] Would stop-if-running and remove existing service {name}")
            else:
                log.info(f"  Removing existing service {name} before NSSM reinstall...")
                cleanup = remove_kd_service_if_present(name)
                if name == svc_name and not cleanup["Success"]:
                    return {
                        "Success": False,
                        "Skipped": False,
                        "ServiceName": svc_name,
                        "Detail": f"Could not remove existing service before reinstall: {cleanup['Detail']}",
                    }
                log.info(f"  {cleanup['Detail']}")

    if dry_run:
        _open_nifi_firewall()
        return {
            "Success": True,
            "Skipped": True,
            "ServiceName": svc_name,
            "Detail": f"[DryRun] Would install via NSSM: {comspec} {app_params}",
        }

    try:
        from . import prerequisites  # local import to avoid circularity

        logs_dir.mkdir(parents=True, exist_ok=True)

        nssm_result = prerequisites.ensure_nssm(dry_run=False)
        if not nssm_result["Success"] or not nssm_result.get("Path"):
            return {
                "Success": False,
                "Skipped": False,
                "ServiceName": svc_name,
                "Detail": f"NSSM not available: {nssm_result.get('Detail')}. "
                          f"Place nssm.exe under nifi\\nssm.exe or on PATH "
                          f"(https://nssm.cc/download). "
                          f"Or run nifi\\Setup-NiFi-Service.ps1 manually after installing NSSM.",
            }
        nssm = Path(nssm_result["Path"])
        log.info(f"  Using NSSM (inline fallback): {nssm}")

        inst = _run_captured([str(nssm), "install", svc_name, comspec], cwd=str(bin_dir))
        if inst["ExitCode"] != 0:
            detail = f"nssm install | ExitCode: {inst['ExitCode']}"
            if inst["StdOut"].strip():
                detail += f" | StdOut: {inst['StdOut'].strip()[:300]}"
            if inst["StdErr"].strip():
                detail += f" | StdErr: {inst['StdErr'].strip()[:300]}"
            r_check = _sc("query", svc_name)
            if r_check.returncode != 0:
                return {
                    "Success": False,
                    "Skipped": False,
                    "ServiceName": svc_name,
                    "Detail": detail,
                }
            log.warn(f"  nssm install non-zero but service is present; continuing configure ({detail})")

        settings = [
            ("Application", comspec),
            ("AppDirectory", str(bin_dir)),
            ("AppParameters", app_params),
            ("DisplayName", display),
            ("Description", "Apache NiFi Data Flow - OpenText Knowledge Discovery (NSSM)"),
            ("Start", "SERVICE_AUTO_START" if str(start_mode).lower() in ("auto", "automatic") else "SERVICE_DEMAND_START"),
            ("AppStdout", str(logs_dir / "nssm-stdout.log")),
            ("AppStderr", str(logs_dir / "nssm-stderr.log")),
            ("AppRotateFiles", "1"),
            ("AppRotateBytes", "10485760"),
            ("AppStopMethodSkip", "0"),
            ("AppStopMethodConsole", "20000"),
            ("AppKillProcessTree", "1"),
            ("AppRestartDelay", "10000"),
        ]
        for key, value in settings:
            r = _run_captured([str(nssm), "set", svc_name, key, value], cwd=str(bin_dir))
            if r["ExitCode"] not in (0,):
                log.warn(f"  nssm set {key} exit {r['ExitCode']}: {(r['StdErr'] or r['StdOut'] or '')[:200]}")

        java_home = (
            os.environ.get("JAVA_HOME")
            or os.environ.get("JDK_HOME")
            or prerequisites.get_permanent_env_var("JAVA_HOME")
        )
        env_pairs = [("NIFI_HOME", str(nifi_home))]
        if java_home:
            env_pairs.insert(0, ("JAVA_HOME", java_home))
        else:
            log.warn("  JAVA_HOME not set in this process — service may fail to start if Java is not on system PATH")

        env_extra = ";".join(f"{k}={v}" for k, v in env_pairs)
        r = _run_captured(
            [str(nssm), "set", svc_name, "AppEnvironmentExtra", *[f"{k}={v}" for k, v in env_pairs]],
            cwd=str(bin_dir),
        )
        if r["ExitCode"] == 0:
            log.info(f"  AppEnvironmentExtra {env_extra}")
        else:
            log.warn(f"  Could not set AppEnvironmentExtra: {(r['StdErr'] or r['StdOut'] or '')[:200]}")

        for name, value in env_pairs:
            if name == "JAVA_HOME":
                home_path = prerequisites.ensure_java_home_and_path(value)
                if home_path["Success"]:
                    log.info(f"  {home_path['Detail']}")
                else:
                    log.warn(f"  {home_path['Detail']}; set JAVA_HOME / PATH manually via "
                             f"System Properties > Environment Variables")
            else:
                env_result = prerequisites.set_permanent_env_var_if_missing(name, value)
                if env_result["Success"]:
                    log.info(f"  {env_result['Detail']}")
                else:
                    log.warn(f"  {env_result['Detail']}; set it manually via "
                             f"System Properties > Environment Variables")

        time.sleep(1)
        r = _sc("query", svc_name)
        if r.returncode == 0:
            set_kd_service_display_name(component)
            _open_nifi_firewall()
            return {
                "Success": True,
                "Skipped": False,
                "ServiceName": svc_name,
                "Detail": f"Installed via NSSM (inline) -> {launcher_label} (run)",
            }

        detail = f"NSSM install completed but sc query {svc_name} failed"
        if inst["StdOut"].strip():
            detail += f" | StdOut: {inst['StdOut'].strip()[:300]}"
        if inst["StdErr"].strip():
            detail += f" | StdErr: {inst['StdErr'].strip()[:300]}"
        return {
            "Success": False,
            "Skipped": False,
            "ServiceName": svc_name,
            "Detail": detail,
        }
    except Exception as e:
        return {
            "Success": False,
            "Skipped": False,
            "ServiceName": svc_name,
            "Detail": f"Exception during NiFi NSSM service install: {e}",
        }


def install_kd_find_service(
    component: str,
    find_war_path: str | Path,
    start_mode: str = "Auto",
    server_port: str = "8080",
    heap_xms: str = "1g",
    heap_xmx: str = "2g",
    home_dir_name: str = "home",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Register OpenText Find as a Windows service using NSSM.

    Find is a Java executable WAR (not a native Win32 binary), so it cannot
    use the component's native -install switch. We wrap java.exe under NSSM:

      Application:  <JAVA_HOME>\\bin\\java.exe  (or java.exe on PATH)
      AppDirectory: <FIND_HOME>  (folder containing find.war)
      AppParameters:
        -Xms{HeapXms} -Xmx{HeapXmx}
        -Didol.find.home=<FIND_HOME>\\home
        -Dserver.port={ServerPort}
        -jar find.war
        -uriEncoding utf-8

    Service name is always **KD-Find**.

    An inbound firewall rule for *server_port* is opened so external clients
    can reach the Find UI.
    """
    from . import prerequisites

    svc_name = get_kd_service_name(component)  # KD-Find
    find_war = Path(find_war_path).resolve()
    find_home = find_war.parent
    home_dir = find_home / (home_dir_name or "home")
    display = "OpenText KD Find"

    if dry_run:
        open_kd_firewall_ports(server_port, component=component, dry_run=True)
        return {
            "Success": True,
            "Skipped": True,
            "ServiceName": svc_name,
            "Detail": (
                f"[DryRun] Would install KD-Find via NSSM: java -jar {find_war.name} "
                f"(port={server_port}, home={home_dir})"
            ),
        }

    try:
        # Resolve java.exe
        java_exe: Optional[Path] = None
        java_home = (os.environ.get("JAVA_HOME") or os.environ.get("JDK_HOME") or "").strip().strip('"')
        if java_home:
            candidate = Path(java_home) / "bin" / "java.exe"
            if candidate.is_file():
                java_exe = candidate
        if java_exe is None:
            import shutil as _shutil
            which = _shutil.which("java") or _shutil.which("java.exe")
            if which:
                java_exe = Path(which)
        if java_exe is None or not java_exe.is_file():
            return {
                "Success": False,
                "Skipped": False,
                "ServiceName": svc_name,
                "Detail": (
                    "java.exe not found (set JAVA_HOME to a JDK install, or put java on PATH). "
                    "Find requires Java to run the .war."
                ),
            }

        # Ensure NSSM
        nssm_result = prerequisites.ensure_nssm(dry_run=False)
        if not nssm_result.get("Success"):
            return {
                "Success": False,
                "Skipped": False,
                "ServiceName": svc_name,
                "Detail": (
                    f"NSSM not available: {nssm_result.get('Detail', 'unknown')}. "
                    "Install NSSM (winget install NSSM.NSSM) or place nssm.exe on PATH."
                ),
            }
        nssm = Path(nssm_result["Path"])

        # Remove existing service if present
        cleanup = remove_kd_service_if_present(svc_name, timeout_seconds=30)
        if not cleanup.get("Success", True) and cleanup.get("Detail"):
            log.warn(f"  Pre-clean {svc_name}: {cleanup['Detail']}")

        # Ensure home directory exists (Find writes config/db here)
        home_dir.mkdir(parents=True, exist_ok=True)

        # Build AppParameters for java -jar
        # NSSM AppParameters is a single string of arguments after the Application
        app_params = (
            f"-Xms{heap_xms} -Xmx{heap_xmx} "
            f'-Didol.find.home="{home_dir}" '
            f"-Dserver.port={server_port} "
            f'-jar "{find_war}" '
            f"-uriEncoding utf-8"
        )

        log.info(f"  Installing {svc_name} via NSSM → {java_exe.name} -jar {find_war.name}")
        inst = _run_captured(
            [str(nssm), "install", svc_name, str(java_exe)],
            cwd=str(find_home),
        )
        if inst["ExitCode"] != 0:
            # If service already exists, nssm install may fail; try configure path
            r = _sc("query", svc_name)
            if r.returncode != 0:
                detail = f"nssm install | ExitCode: {inst['ExitCode']}"
                if inst["StdErr"].strip():
                    detail += f" | StdErr: {inst['StdErr'].strip()[:300]}"
                return {
                    "Success": False,
                    "Skipped": False,
                    "ServiceName": svc_name,
                    "Detail": detail,
                }
            log.warn("  nssm install non-zero but service is present; continuing configure")

        # Configure NSSM
        def _nssm_set(key: str, *values: str) -> None:
            r = _run_captured([str(nssm), "set", svc_name, key, *values], cwd=str(find_home))
            if r["ExitCode"] != 0:
                log.warn(f"  nssm set {key} failed: {(r['StdErr'] or r['StdOut'] or '')[:200]}")

        _nssm_set("Application", str(java_exe))
        _nssm_set("AppDirectory", str(find_home))
        _nssm_set("AppParameters", app_params)
        _nssm_set("DisplayName", display)
        _nssm_set("Description", "OpenText Knowledge Discovery Find (Java search UI)")
        _nssm_set("Start", "SERVICE_AUTO_START" if start_mode.lower() == "auto" else "SERVICE_DEMAND_START")
        _nssm_set("AppStdout", str(find_home / "logs" / "find-service-stdout.log"))
        _nssm_set("AppStderr", str(find_home / "logs" / "find-service-stderr.log"))
        (find_home / "logs").mkdir(parents=True, exist_ok=True)

        # Pass JAVA_HOME into the service environment
        if java_home:
            _nssm_set("AppEnvironmentExtra", f"JAVA_HOME={java_home}")
            env_result = prerequisites.set_permanent_env_var_if_missing("JAVA_HOME", java_home)
            if env_result.get("Success"):
                log.info(f"  {env_result['Detail']}")

        time.sleep(1)
        r = _sc("query", svc_name)
        if r.returncode == 0:
            set_kd_service_display_name(component)
            fw = open_kd_firewall_ports(server_port, component=component, dry_run=False)
            if not fw.get("Success"):
                log.warn(
                    f"  Service {svc_name} installed, but firewall open "
                    f"for port {server_port} had issues: {fw.get('Detail')}"
                )
            return {
                "Success": True,
                "Skipped": False,
                "ServiceName": svc_name,
                "Detail": (
                    f"Installed via NSSM → java -jar {find_war.name} "
                    f"(port={server_port}, home={home_dir})"
                ),
            }

        return {
            "Success": False,
            "Skipped": False,
            "ServiceName": svc_name,
            "Detail": f"NSSM install completed but sc query {svc_name} failed",
        }
    except Exception as e:
        return {
            "Success": False,
            "Skipped": False,
            "ServiceName": svc_name,
            "Detail": f"Exception during Find NSSM service install: {e}",
        }


def uninstall_kd_service(
    component: str,
    executable_path: Optional[str | Path] = None,
    args_template: Optional[List[str]] = None,
    dry_run: bool = False,
    skip_stop: bool = False,
) -> Dict[str, Any]:
    svc_name = get_kd_service_name(component)

    r = _sc("query", svc_name)
    if r.returncode != 0:
        # Also clear legacy NiFi service names if someone asks to uninstall NiFi
        if component.lower() == "nifi":
            cleaned = []
            for legacy_name in ("KD-Apache-NiFi", "ApacheNifiService"):
                legacy = remove_kd_service_if_present(legacy_name)
                if legacy.get("Success") and "not present" not in (legacy.get("Detail") or ""):
                    cleaned.append(legacy_name)
            if cleaned:
                return {
                    "Success": True,
                    "Detail": f"Service {svc_name} not present; cleaned legacy {', '.join(cleaned)}",
                }
        return {"Success": True, "Detail": f"Service {svc_name} not present; nothing to do"}

    if dry_run:
        return {"Success": True, "Detail": f"[DryRun] Would stop and remove service {svc_name}"}

    try:
        if not skip_stop:
            stopped = stop_kd_service_force(svc_name, 30)
            if not stopped:
                log.warn(f"Could not fully stop {svc_name} before removal; continuing")

        # NiFi is managed by NSSM — prefer nssm stop / nssm remove confirm
        if component.lower() == "nifi":
            try:
                from . import prerequisites
                nssm_result = prerequisites.ensure_nssm(dry_run=False)
                nssm = nssm_result.get("Path") if nssm_result.get("Success") else None
            except Exception:
                nssm = None
            nifi_names = [svc_name, "KD-Apache-NiFi", "ApacheNifiService"]
            if nssm:
                for name in nifi_names:
                    rq = _sc("query", name)
                    if rq.returncode != 0:
                        continue
                    log.info(f"  Stopping {name} via NSSM ({nssm})...")
                    _run_captured([str(nssm), "stop", name])
                    time.sleep(2)
                    log.info(f"  Removing {name} via NSSM remove confirm...")
                    _run_captured([str(nssm), "remove", name, "confirm"])
                    time.sleep(2)
            else:
                log.warn("  NSSM not available; falling back to sc stop/delete for NiFi services")
                for name in nifi_names:
                    remove_kd_service_if_present(name)

        # Prefer binary's own uninstall switch (KD components only — not NiFi/Find).
        # Short timeout: this is only meant to deregister the Windows service -
        # if the binary hasn't done that in ~25s it isn't going to, and
        # remove_kd_service_if_present() below is a guaranteed sc.exe-level
        # fallback regardless of whether this call succeeds or times out.
        # (OpenText binaries occasionally hang on -remove for an already-
        # stopped service - was previously eating the full 120s timeout.)
        if executable_path and Path(executable_path).is_file() and component.lower() not in ("nifi", "find"):
            template = args_template or DEFAULT_UNINSTALL_TEMPLATE
            args = expand_kd_args_template(template, {"ServiceName": svc_name})
            _run_captured([str(executable_path), *args], cwd=str(Path(executable_path).parent), timeout=25)
            time.sleep(2)

        # Final cleanup: check if it's running, stop if so, then delete
        # (no-op delete-only if it's already stopped by this point). Retry
        # once in case the binary's own uninstall switch left it lingering.
        cleanup = remove_kd_service_if_present(svc_name)
        if not cleanup["Success"]:
            cleanup = remove_kd_service_if_present(svc_name)

        r = _sc("query", svc_name)
        still_there = r.returncode == 0
        return {
            "Success": not still_there,
            "Detail": (
                f"Service {svc_name} still present (may require reboot)"
                if still_there
                else f"Removed {svc_name}"
            ),
        }
    except Exception as e:
        return {"Success": False, "Detail": f"Exception during uninstall: {e}"}


def test_kd_service_healthy(component: str) -> Dict[str, Any]:
    if is_package_only_component(component):
        return {
            "Success": True,
            "Detail": "Skipped (package-only — no Windows service for this component)",
        }
    svc_name = get_kd_service_name(component)
    r = _sc("query", svc_name)
    if r.returncode != 0:
        return {"Success": False, "Detail": f"Service {svc_name} not found"}
    running = "RUNNING" in (r.stdout or "").upper()
    return {
        "Success": running,
        "Detail": f"Status: {'Running' if running else 'Not running'}",
    }


def start_kd_service(service_name: str, timeout_seconds: int = 30) -> bool:
    """Start a Windows service and wait until it reports RUNNING."""
    result = start_kd_service_detailed(service_name, timeout_seconds)
    return bool(result.get("Success"))


def start_kd_service_detailed(
    service_name: str,
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    """
    Start a Windows service and wait until RUNNING.

    Returns Success/Detail/State/Win32ExitCode so callers can log diagnostics
    when the process exits immediately (missing license, bad .cfg, port in use).
    """
    r0 = _sc("query", service_name)
    if r0.returncode != 0:
        return {
            "Success": False,
            "State": "MISSING",
            "Win32ExitCode": None,
            "Detail": (
                f"{service_name} is not registered. "
                "Run Manage services → Create all services, or re-run Install."
            ),
        }

    out0 = (r0.stdout or "").upper()
    if "RUNNING" in out0:
        return {
            "Success": True,
            "State": "RUNNING",
            "Win32ExitCode": 0,
            "Detail": "Already Running",
        }

    start_result = _sc("start", service_name)
    start_out = ((start_result.stdout or "") + "\n" + (start_result.stderr or "")).strip()

    deadline = time.time() + timeout_seconds
    last_state = "UNKNOWN"
    win32_exit = None
    while time.time() < deadline:
        r = _sc("query", service_name)
        out = r.stdout or ""
        out_u = out.upper()
        for line in out.splitlines():
            if "STATE" in line.upper():
                last_state = line.split(":")[-1].strip() if ":" in line else line.strip()
            if "WIN32_EXIT_CODE" in line.upper():
                try:
                    win32_exit = int(line.split(":")[-1].strip().split()[0])
                except Exception:
                    pass
        if "RUNNING" in out_u:
            return {
                "Success": True,
                "State": "RUNNING",
                "Win32ExitCode": win32_exit if win32_exit is not None else 0,
                "Detail": "Running",
            }
        # Crashed back to STOPPED — no point waiting the full timeout
        if "STOPPED" in out_u and "START_PENDING" not in out_u:
            break
        time.sleep(1)

    detail_parts = [f"Status: {last_state or 'not Running'}"]
    if win32_exit is not None and win32_exit != 0:
        detail_parts.append(f"Win32ExitCode={win32_exit}")
    if start_result.returncode != 0 and start_out:
        detail_parts.append(f"sc start: {start_out[:200]}")
    detail_parts.append(
        "Check component log under BasePath, license key, ports, and .cfg. "
        "You can start later via Manage services."
    )
    return {
        "Success": False,
        "State": last_state,
        "Win32ExitCode": win32_exit,
        "Detail": " | ".join(detail_parts),
    }


# Success marker written by Apache NiFi when bootstrap finishes and the
# FlowController is up. Present in logs/nifi-app.log (and sometimes nifi-bootstrap.log).
NIFI_FLOW_STARTED_MARKER = "Flow Controller started successfully."


def resolve_nifi_home(nifi_home: Optional[str | Path] = None) -> Optional[Path]:
    """Best-effort NIFI_HOME for log tailing."""
    if nifi_home:
        p = Path(str(nifi_home))
        if p.is_dir():
            return p.resolve()
    for key in ("NIFI_HOME", "KD_NIFI_HOME"):
        val = os.environ.get(key)
        if val and Path(val).is_dir():
            return Path(val).resolve()
    try:
        from . import prerequisites

        val = prerequisites.get_permanent_env_var("NIFI_HOME")
        if val and Path(val).is_dir():
            return Path(val).resolve()
    except Exception:
        pass
    # Common KD layout defaults
    for candidate in (
        Path(r"C:\KnowledgeDiscovery\26.2\NiFi"),
        Path(r"C:\KnowledgeDiscovery\NiFi"),
        Path(r"D:\KnowledgeDiscovery\26.2\NiFi"),
    ):
        if candidate.is_dir():
            return candidate.resolve()
    return None


def _nifi_app_log_path(nifi_home: Path) -> Path:
    return nifi_home / "logs" / "nifi-app.log"


def _log_contains_marker(log_path: Path, marker: str, from_offset: int = 0) -> bool:
    """Return True if marker appears in the log at/after from_offset (byte offset)."""
    try:
        if not log_path.is_file():
            return False
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            if from_offset:
                fh.seek(max(0, from_offset))
            # Read remaining in chunks to avoid loading multi-GB logs
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                if marker in chunk:
                    return True
        return False
    except OSError:
        return False


def start_kd_nifi_and_wait_for_flow(
    service_name: str = "KD-NiFi",
    nifi_home: Optional[str | Path] = None,
    timeout_seconds: int = 300,
) -> Dict[str, Any]:
    """
    Start the NiFi Windows service, then verify readiness from the application
    log — **not** from service RUNNING alone.

    Verification rules:
      1. Resolve expected log: ``<NIFI_HOME>/logs/nifi-app.log``
      2. Start the service (if not already running).
      3. Within *timeout_seconds* (default 300 / 5 minutes):
         - wait for ``nifi-app.log`` to exist;
         - then monitor it for ``Flow Controller started successfully.``
      4. Success only when the marker is found.
      5. If the log never appears → failure (missing log file).
      6. If service is RUNNING but marker never appears → startup timeout
         (failure). Do **not** treat RUNNING as fully ready.
      7. All status/warning messages include the full log path.

    Returns dict with keys:
      Success, ElapsedSeconds, Detail, LogPath, MarkerFound, ServiceRunning,
      LogExists, Warning (optional).
    """
    home = resolve_nifi_home(nifi_home)
    log_path = _nifi_app_log_path(home) if home else None
    log_path_display = (
        str(log_path) if log_path else "(nifi-app.log path unknown — NIFI_HOME not resolved)"
    )
    marker = NIFI_FLOW_STARTED_MARKER

    if not home or log_path is None:
        detail = (
            "NiFi startup verification failed: NIFI_HOME could not be resolved, "
            "so nifi-app.log cannot be checked. Set NIFI_HOME or pass nifi_home."
        )
        log.error(f"  {detail}")
        return {
            "Success": False,
            "ElapsedSeconds": 0.0,
            "Detail": detail,
            "LogPath": None,
            "MarkerFound": False,
            "ServiceRunning": False,
            "LogExists": False,
        }

    # Record log size before start so we prefer a *new* occurrence of the marker
    start_offset = 0
    log_existed_at_start = log_path.is_file()
    if log_existed_at_start:
        try:
            start_offset = log_path.stat().st_size
        except OSError:
            start_offset = 0

    # Already up and marker already present (e.g. previous successful start)?
    r0 = _sc("query", service_name)
    already_running = r0.returncode == 0 and "RUNNING" in (r0.stdout or "").upper()
    if already_running and log_existed_at_start and _log_contains_marker(log_path, marker, 0):
        detail = (
            f"{service_name} already Running and log already contains "
            f"'{marker}' in {log_path_display}"
        )
        log.info(f"  {detail}")
        return {
            "Success": True,
            "ElapsedSeconds": 0.0,
            "Detail": detail,
            "LogPath": log_path_display,
            "MarkerFound": True,
            "ServiceRunning": True,
            "LogExists": True,
        }

    t0 = time.time()
    if not already_running:
        log.info(f"  Issuing sc start {service_name} ...")
        _sc("start", service_name)
    else:
        log.info(
            f"  {service_name} is already Running — verifying Flow Controller "
            f"via {log_path_display}"
        )

    log.info(f"  Expected log file: {log_path_display}")
    if not log_existed_at_start:
        log.info(
            f"  nifi-app.log not present yet at {log_path_display} — "
            f"will wait up to {timeout_seconds}s for it to appear"
        )
    log.info(f"  Waiting for: \"{marker}\"  (timeout {timeout_seconds}s)")
    log.info(
        "  Note: Windows service RUNNING alone does not mean NiFi is ready; "
        "the Flow Controller marker in nifi-app.log is required."
    )

    deadline = t0 + timeout_seconds
    marker_found = False
    log_seen = log_existed_at_start

    while time.time() < deadline:
        elapsed = time.time() - t0

        if not log_seen:
            if log_path.is_file():
                log_seen = True
                if start_offset <= 0:
                    start_offset = 0
                log.info(f"  nifi-app.log appeared: {log_path_display}")
            else:
                mins, secs = divmod(int(elapsed), 60)
                status_line(
                    f"  NiFi starting ... {mins:02d}:{secs:02d} elapsed "
                    f"(waiting for log file {log_path_display})"
                )
                time.sleep(1)
                continue

        if _log_contains_marker(log_path, marker, start_offset):
            marker_found = True
            break

        mins, secs = divmod(int(elapsed), 60)
        status_line(
            f"  NiFi starting ... {mins:02d}:{secs:02d} elapsed "
            f"(waiting for Flow Controller in {log_path.name})"
        )
        time.sleep(1)

    elapsed_total = time.time() - t0
    mins, secs = divmod(int(elapsed_total), 60)
    elapsed_str = f"{mins:02d}:{secs:02d}" if mins else f"{secs}s"
    elapsed_precise = f"{elapsed_total:.1f}s"

    r = _sc("query", service_name)
    running = r.returncode == 0 and "RUNNING" in (r.stdout or "").upper()
    log_exists_now = log_path.is_file()

    # --- Success: marker found ---
    if marker_found:
        detail = (
            f"Flow Controller started successfully after {elapsed_precise} "
            f"(wall {elapsed_str}). Verified in {log_path_display}"
        )
        if running:
            status_line_finish(
                f"  NiFi Flow Controller ready - total start time: "
                f"{elapsed_precise} ({elapsed_str})"
            )
            log.info(f"  [OK] {detail}")
            return {
                "Success": True,
                "ElapsedSeconds": round(elapsed_total, 1),
                "Detail": detail,
                "LogPath": log_path_display,
                "MarkerFound": True,
                "ServiceRunning": True,
                "LogExists": True,
            }

        detail_warn = (
            f"{detail} — however Windows service {service_name} is not RUNNING. "
            f"Check nssm/sc status and {log_path_display}."
        )
        status_line_finish(
            f"  NiFi Flow Controller marker seen after {elapsed_precise}, "
            f"but service is not RUNNING (WARNING)"
        )
        log.warn(f"  {detail_warn}")
        return {
            "Success": True,
            "ElapsedSeconds": round(elapsed_total, 1),
            "Detail": detail_warn,
            "LogPath": log_path_display,
            "MarkerFound": True,
            "ServiceRunning": False,
            "LogExists": True,
            "Warning": True,
        }

    # --- Failure: log file never appeared ---
    if not log_exists_now:
        detail = (
            f"NiFi startup verification failed after {elapsed_precise}: "
            f"log file is missing — {log_path_display}. "
            f"Service status: {'RUNNING' if running else 'not RUNNING'}. "
            f"NiFi may still be bootstrapping or failed before writing logs. "
            f"Check the service (nssm/sc), JAVA_HOME, and NIFI_HOME."
        )
        status_line_finish(
            f"  NiFi startup failed after {elapsed_precise}: nifi-app.log missing"
        )
        log.error(f"  {detail}")
        return {
            "Success": False,
            "ElapsedSeconds": round(elapsed_total, 1),
            "Detail": detail,
            "LogPath": log_path_display,
            "MarkerFound": False,
            "ServiceRunning": running,
            "LogExists": False,
        }

    # --- Failure: timeout — service RUNNING but marker not seen ---
    if running:
        detail = (
            f"Timeout after {elapsed_precise}: service is RUNNING but "
            f"'{marker}' was not seen in {log_path_display}. "
            f"Do not treat the service as fully ready. "
            f"NiFi may still be bootstrapping — check {log_path_display}."
        )
        status_line_finish(
            f"  NiFi startup timeout after {elapsed_precise}: "
            f"RUNNING but Flow Controller marker not in log"
        )
        log.warn(f"  {detail}")
        return {
            "Success": False,
            "ElapsedSeconds": round(elapsed_total, 1),
            "Detail": detail,
            "LogPath": log_path_display,
            "MarkerFound": False,
            "ServiceRunning": True,
            "LogExists": True,
            "Warning": True,
        }

    # --- Failure: no marker and service not RUNNING ---
    older_marker = _log_contains_marker(log_path, marker, 0)
    if older_marker:
        detail = (
            f"Timeout after {elapsed_precise}: service not RUNNING. "
            f"Log {log_path_display} contains '{marker}' from a previous start, "
            f"but no new marker was seen for this start. "
            f"NiFi may still be bootstrapping — check {log_path_display}."
        )
        status_line_finish(
            f"  NiFi service not RUNNING after {elapsed_precise} "
            f"(stale Flow Controller marker in log only)"
        )
        log.warn(f"  {detail}")
        return {
            "Success": False,
            "ElapsedSeconds": round(elapsed_total, 1),
            "Detail": detail,
            "LogPath": log_path_display,
            "MarkerFound": False,
            "ServiceRunning": False,
            "LogExists": True,
            "Warning": True,
        }

    detail = (
        f"Timeout after {elapsed_precise}: service not RUNNING and "
        f"'{marker}' not found in {log_path_display}. "
        f"NiFi may still be bootstrapping or failed to start — "
        f"check {log_path_display}."
    )
    status_line_finish(f"  NiFi failed to start within {elapsed_precise}")
    log.error(f"  {detail}")
    return {
        "Success": False,
        "ElapsedSeconds": round(elapsed_total, 1),
        "Detail": detail,
        "LogPath": log_path_display,
        "MarkerFound": False,
        "ServiceRunning": False,
        "LogExists": True,
    }

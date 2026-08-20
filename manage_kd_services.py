#!/usr/bin/env python3
"""
Manage Knowledge Discovery Windows services.

By default reads Components from config/my-config.json (or --config) and maps
each entry to service name KD-<Component> (e.g. Content → KD-Content,
NiFi → KD-NiFi).

With --all-deployed (or when Components is empty), discovers every Windows
service whose name starts with ``KD-`` that is actually registered on this
machine and operates on those — so you can start / stop / status any of the
deployed services even if they are not listed in the config JSON.

Actions:
  status     Show Running / Stopped / Missing for each service
  start      Start services (LicenseServer first, then the rest; NiFi waits
             for Flow Controller in nifi-app.log)
  stop       Stop services (others first, LicenseServer last)
  restart    stop then start
  delete     Stop (if running) and sc.exe delete — removes the service
             registration only (files under BasePath are left alone)
  uninstall  Full service uninstall via the component binary's -remove
             (when found) + sc delete. Still does not delete install folders
             (use Install-KD.ps1 -Mode Uninstall for that).
  create     Register Windows services for extracted components under BasePath.
             Standard components (Content, Community, …) use the component
             binary's native -install; NiFi uses NSSM + nifi.cmd; Find uses
             NSSM + java -jar find.war. Uses config Components; LicenseServer
             first. Re-creates if already exists.

Examples:
  python manage_kd_services.py status
  python manage_kd_services.py start --all-deployed
  python manage_kd_services.py start --components Content,NiFi
  python manage_kd_services.py start --components KD-Content,KD-Community
  python manage_kd_services.py stop --config config/my-config.json
  python manage_kd_services.py restart --all-deployed --non-interactive
  python manage_kd_services.py delete --components Content,Community
  python manage_kd_services.py uninstall --force
  python manage_kd_services.py create --non-interactive
  python manage_kd_services.py delete --all-deployed --force

Requires Administrator for start/stop/delete/uninstall (status works without).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kd.config import get_kd_config
from kd import discovery, service_manager
from kd.logging import log

# ANSI yellow for user-facing questions / input prompts
_PROMPT_YELLOW = "\033[33m"
_PROMPT_RESET = "\033[0m"


ACTIONS = ("status", "start", "stop", "restart", "delete", "uninstall", "create")


def _is_package_only(component: str) -> bool:
    """NiFiIngest and similar are NAR packages, not Windows services."""
    try:
        return service_manager.is_package_only_component(component)
    except Exception:
        return (component or "").strip().lower() in ("nifiingest",)


def _is_elevated() -> bool:
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _service_status(service_name: str) -> Dict[str, Any]:
    """Return {Name, Exists, State, Detail} using sc.exe query."""
    try:
        r = service_manager._sc("query", service_name)
    except FileNotFoundError:
        return {
            "Name": service_name,
            "Exists": False,
            "State": "Unavailable",
            "Detail": "sc.exe not found (Windows only)",
        }
    except Exception as e:
        return {
            "Name": service_name,
            "Exists": False,
            "State": "Error",
            "Detail": str(e),
        }
    if r.returncode != 0:
        return {
            "Name": service_name,
            "Exists": False,
            "State": "Missing",
            "Detail": "Service not found",
        }
    out = (r.stdout or "").upper()
    if "RUNNING" in out:
        state = "Running"
    elif "STOPPED" in out:
        state = "Stopped"
    elif "START_PENDING" in out:
        state = "StartPending"
    elif "STOP_PENDING" in out:
        state = "StopPending"
    else:
        state = "Unknown"
    return {
        "Name": service_name,
        "Exists": True,
        "State": state,
        "Detail": (r.stdout or "").strip().splitlines()[0] if r.stdout else state,
    }


def _normalize_component(token: str) -> str:
    """
    Accept either a component name (Content) or a full service name (KD-Content).
    Returns the component form used by the rest of the toolkit.
    """
    token = (token or "").strip()
    if not token:
        return token
    return service_manager.service_name_to_component(token)


def _ordered_components(components: Sequence[str], *, for_start: bool) -> List[str]:
    """
    Start: LicenseServer first, then others (stable order).
    Stop/delete/uninstall: others first, LicenseServer last.
    """
    comps = list(components)
    license_keys = [c for c in comps if c.lower() == "licenseserver"]
    rest = [c for c in comps if c.lower() != "licenseserver"]
    if for_start:
        return license_keys + rest
    return rest + license_keys


def _components_from_deployed_services() -> List[str]:
    """
    Discover every registered Windows service whose name starts with KD-
    and map them back to component names (KD-Content → Content).
    """
    services = service_manager.list_kd_services()
    components: List[str] = []
    seen = set()
    for svc in services:
        comp = service_manager.service_name_to_component(svc)
        key = comp.lower()
        if key in seen:
            continue
        seen.add(key)
        components.append(comp)
    return components


def _resolve_components(
    explicit: Optional[str],
    config_components: Sequence[str],
    *,
    all_deployed: bool,
) -> List[str]:
    """
    Resolve the component list for this run.

    Priority:
      1. --components LIST (comma-separated; accepts Content or KD-Content)
      2. --all-deployed → every KD-* service registered on this machine
      3. config Components
      4. fallback: every KD-* service registered on this machine
    """
    if explicit:
        return [
            _normalize_component(c)
            for c in explicit.split(",")
            if c.strip()
        ]

    if all_deployed:
        discovered = _components_from_deployed_services()
        if discovered:
            return discovered
        # Fall through to config if nothing is registered yet
        return list(config_components)

    if config_components:
        return list(config_components)

    # No config Components and no --components → operate on whatever is deployed
    return _components_from_deployed_services()


def _find_component_executable(base_path: Path, component: str) -> Optional[Path]:
    comp_dir = base_path / component
    if not comp_dir.is_dir():
        # Case-insensitive folder match (Windows usually is case-insensitive,
        # but path construction may differ in casing from service name).
        parent = base_path
        if parent.is_dir():
            for child in parent.iterdir():
                if child.is_dir() and child.name.lower() == component.lower():
                    comp_dir = child
                    break
        if not comp_dir.is_dir():
            return None
    result = discovery.find_kd_component_executable(comp_dir, component)
    if result.get("Success") and result.get("Executable"):
        return Path(result["Executable"])
    return None


def action_status(components: Sequence[str]) -> int:
    print()
    print(f"{'Component':<18} {'Service':<22} {'State':<14} Detail")
    print("-" * 72)
    for comp in components:
        if _is_package_only(comp):
            log.info(f"  {comp}: skipped (package-only — no Windows service)")
            continue
        svc = service_manager.get_kd_service_name(comp)
        st = _service_status(svc)
        print(f"{comp:<18} {svc:<22} {st['State']:<14} {st['Detail'][:40]}")
    print()
    return 0  # status is informational; non-running is not a hard failure


def action_start(
    components: Sequence[str],
    dry_run: bool,
    base_path: Optional[Path] = None,
    config: Optional[Dict[str, Any]] = None,
) -> int:
    ordered = _ordered_components(components, for_start=True)
    failed: List[str] = []
    config = config or {}
    for comp in ordered:
        if _is_package_only(comp):
            log.info(f"  {comp}: skipped (package-only — no Windows service)")
            continue
        svc = service_manager.get_kd_service_name(comp)
        st = _service_status(svc)
        if not st["Exists"]:
            log.warn(f"{svc} not installed — skip start")
            failed.append(comp)
            continue
        if dry_run:
            log.info(f"[DryRun] Would start {svc}")
            continue

        if comp.lower() == "nifi":
            # Ensure external clients can reach NiFi HTTPS port (8443 by default)
            ports = config.get("Ports") or {}
            nifi_cfg = config.get("NiFi") or {}
            nifi_port = nifi_cfg.get("WebHttpsPort") or ports.get("NiFi") or "8443"
            service_manager.open_kd_firewall_ports(nifi_port, component=comp, dry_run=False)
            nifi_home = (base_path / "NiFi") if base_path else None
            log.info(
                f"Starting {svc} (waiting for Flow Controller in nifi-app.log)..."
            )
            result = service_manager.start_kd_nifi_and_wait_for_flow(
                service_name=svc,
                nifi_home=nifi_home,
                timeout_seconds=300,
            )
            if result.get("Success"):
                elapsed = result.get("ElapsedSeconds", 0)
                marker = "yes" if result.get("MarkerFound") else "no"
                svc_run = "yes" if result.get("ServiceRunning", True) else "no"
                msg = (
                    f"  {svc} → ready (elapsed {elapsed}s, "
                    f"Flow Controller marker={marker}, service RUNNING={svc_run})"
                )
                if result.get("Warning"):
                    # Marker seen / soft timeout — orange WARNING, not ERROR
                    log.warn(msg + "  [WARNING]")
                else:
                    log.info(msg)
            else:
                log.error(f"  {svc} failed: {result.get('Detail')}")
                failed.append(comp)
            continue

        if st["State"] == "Running":
            log.info(f"{svc} already Running")
            continue

        if comp.lower() == "find":
            # Ensure external clients can reach Find UI port (8080 by default)
            # the same way NiFi exposes 8443 before start
            ports = config.get("Ports") or {}
            find_cfg = config.get("Find") or {}
            find_port = find_cfg.get("ServerPort") or ports.get("Find") or "8080"
            service_manager.open_kd_firewall_ports(find_port, component=comp, dry_run=False)

        timeout = 180 if comp.lower() == "find" else 45
        log.info(f"Starting {svc}...")
        ok = service_manager.start_kd_service(svc, timeout_seconds=timeout)
        if ok:
            log.info(f"  {svc} → Running")
        else:
            log.error(f"  {svc} failed to reach Running")
            failed.append(comp)
    return 1 if failed else 0


def action_stop(components: Sequence[str], dry_run: bool) -> int:
    ordered = _ordered_components(components, for_start=False)
    failed: List[str] = []
    for comp in ordered:
        if _is_package_only(comp):
            log.info(f"  {comp}: skipped (package-only — no Windows service)")
            continue
        svc = service_manager.get_kd_service_name(comp)
        st = _service_status(svc)
        if not st["Exists"]:
            log.info(f"{svc} not present — nothing to stop")
            continue
        if st["State"] == "Stopped":
            log.info(f"{svc} already Stopped")
            continue
        if dry_run:
            log.info(f"[DryRun] Would stop {svc}")
            continue
        ok = service_manager.stop_kd_service_force(svc, timeout_seconds=45)
        if ok:
            log.info(f"  {svc} → Stopped")
        else:
            log.error(f"  {svc} could not be stopped")
            failed.append(comp)
    return 1 if failed else 0


def action_restart(
    components: Sequence[str],
    dry_run: bool,
    base_path: Optional[Path] = None,
    config: Optional[Dict[str, Any]] = None,
) -> int:
    rc = action_stop(components, dry_run)
    rc2 = action_start(components, dry_run, base_path=base_path, config=config)
    return rc or rc2


def action_delete(components: Sequence[str], dry_run: bool) -> int:
    """Stop-if-running + sc delete only (no binary -remove, folders kept)."""
    ordered = _ordered_components(components, for_start=False)
    failed: List[str] = []
    for comp in ordered:
        if _is_package_only(comp):
            log.info(f"  {comp}: skipped (package-only — no Windows service)")
            continue
        svc = service_manager.get_kd_service_name(comp)
        st = _service_status(svc)
        if not st["Exists"]:
            log.info(f"{svc} not present — nothing to delete")
            continue
        if dry_run:
            log.info(f"[DryRun] Would stop-if-running and delete {svc}")
            continue
        result = service_manager.remove_kd_service_if_present(svc, timeout_seconds=45)
        if result.get("Success"):
            log.info(f"  {result.get('Detail', f'Deleted {svc}')}")
        else:
            log.error(f"  {result.get('Detail', f'Failed to delete {svc}')}")
            failed.append(comp)
    return 1 if failed else 0


def action_uninstall(
    components: Sequence[str],
    base_path: Path,
    uninstall_template: Optional[List[str]],
    dry_run: bool,
) -> int:
    """
    Full service uninstall: component binary -remove (when found) + sc delete.
    Does not remove BasePath folders (use Install-KD Uninstall mode for that).
    """
    ordered = _ordered_components(components, for_start=False)
    failed: List[str] = []
    for comp in ordered:
        if _is_package_only(comp):
            log.info(f"  {comp}: skipped (package-only — no Windows service)")
            continue
        svc = service_manager.get_kd_service_name(comp)
        exe = _find_component_executable(base_path, comp)
        if dry_run:
            log.info(
                f"[DryRun] Would uninstall {svc}"
                + (f" via {exe}" if exe else " (sc delete only; exe not found)")
            )
            continue
        result = service_manager.uninstall_kd_service(
            comp,
            executable_path=exe,
            args_template=uninstall_template,
            dry_run=False,
            skip_stop=False,
        )
        if result.get("Success"):
            log.info(f"  {result.get('Detail', f'Removed {svc}')}")
        else:
            log.error(f"  {result.get('Detail', f'Failed to uninstall {svc}')}")
            failed.append(comp)
    return 1 if failed else 0


def action_create(
    components: Sequence[str],
    base_path: Path,
    *,
    install_template: Optional[List[str]],
    start_mode: str,
    nifi_install_service: bool,
    find_install_service: bool = True,
    find_cfg: Optional[Dict[str, Any]] = None,
    ports: Optional[Dict[str, Any]] = None,
    license_port: Optional[str] = None,
    nifi_port: Optional[str] = None,
    dry_run: bool,
) -> int:
    """
    Create / re-register Windows services for already-extracted components
    under BasePath. Does not extract ZIPs or edit .cfg files — use Install
    or Configure for that.

    Order: LicenseServer first, then remaining (same as start).
    NiFi uses install_kd_nifi_service (NSSM + nifi.cmd);
    Find uses install_kd_find_service (NSSM + java -jar find.war);
    every other component uses install_kd_service (native -install).

    When ports / license_port / nifi_port are supplied, inbound firewall
    rules are opened for each component so external clients can connect.
    """
    ordered = _ordered_components(components, for_start=True)
    if not ordered:
        log.error("No components to create services for")
        return 1

    if not base_path.is_dir():
        log.error(f"BasePath does not exist: {base_path}")
        log.error("  Extract components first (Install / Extract only), then re-run create.")
        return 1

    log.info(f"Creating services under BasePath={base_path}")
    log.info(f"  Order: {', '.join(ordered)}")
    failed: List[str] = []
    find_cfg = find_cfg or {}
    ports = ports or {}

    for comp in ordered:
        svc = service_manager.get_kd_service_name(comp)
        # Package-only (NiFiIngest): not a Windows service
        if _is_package_only(comp):
            log.info(f"  {comp}: skipped (package-only — NiFi connector NARs, not a service)")
            continue
        comp_dir = base_path / comp
        if not comp_dir.is_dir():
            # case-insensitive
            if base_path.is_dir():
                for child in base_path.iterdir():
                    if child.is_dir() and child.name.lower() == comp.lower():
                        comp_dir = child
                        break
        if not comp_dir.is_dir():
            log.warn(f"  {comp}: folder not found under {base_path} — skip create")
            failed.append(comp)
            continue

        if comp.lower() == "nifi":
            if not nifi_install_service:
                log.info(f"  {svc}: skipped (NiFi.InstallService is false)")
                continue
            exe = _find_component_executable(base_path, comp)
            if exe is None:
                # Prefer bin/nifi.cmd
                nifi_cmd = comp_dir / "bin" / "nifi.cmd"
                if nifi_cmd.is_file():
                    exe = nifi_cmd
            if exe is None:
                log.error(f"  {svc}: nifi.cmd not found under {comp_dir}")
                failed.append(comp)
                continue
            log.info(f"  Creating {svc} via NSSM (nifi.cmd) ...")
            result = service_manager.install_kd_nifi_service(
                component=comp,
                nifi_cmd_path=exe,
                start_mode=start_mode,
                dry_run=dry_run,
                port=nifi_port or ports.get("NiFi") or "8443",
            )
        elif comp.lower() == "find":
            if not find_install_service:
                log.info(f"  {svc}: skipped (Find.InstallService is false)")
                continue
            exe = _find_component_executable(base_path, comp)
            if exe is None:
                # Prefer find.war at component root
                war = comp_dir / "find.war"
                if war.is_file():
                    exe = war
            if exe is None:
                log.error(f"  {svc}: find.war not found under {comp_dir}")
                failed.append(comp)
                continue
            port = str(
                find_cfg.get("ServerPort")
                or ports.get("Find")
                or "8080"
            )
            log.info(f"  Creating {svc} via NSSM (java -jar {exe.name}) ...")
            result = service_manager.install_kd_find_service(
                component=comp,
                find_war_path=exe,
                start_mode=start_mode,
                server_port=port,
                heap_xms=str(find_cfg.get("HeapXms") or "1g"),
                heap_xmx=str(find_cfg.get("HeapXmx") or "2g"),
                home_dir_name=str(find_cfg.get("HomeDir") or "home"),
                dry_run=dry_run,
            )
        else:
            exe = _find_component_executable(base_path, comp)
            if exe is None:
                log.error(f"  {svc}: component executable not found under {comp_dir}")
                failed.append(comp)
                continue
            if comp.lower() == "licenseserver":
                comp_port = license_port or ports.get("LicenseServer")
            else:
                comp_port = ports.get(comp)
            log.info(f"  Creating {svc} via native -install ({exe.name}) ...")
            result = service_manager.install_kd_service(
                component=comp,
                executable_path=exe,
                start_mode=start_mode,
                args_template=install_template,
                dry_run=dry_run,
                port=comp_port,
            )

        if result.get("Success"):
            detail = result.get("Detail") or "OK"
            log.info(f"  {svc}: {detail}")
        else:
            log.error(f"  {svc}: {result.get('Detail') or 'create failed'}")
            failed.append(comp)

    if failed:
        log.warn(f"Create finished with failures: {', '.join(failed)}")
        return 1
    log.info("All requested services created successfully.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="manage_kd_services.py",
        description=(
            "Start / stop / delete / uninstall KD Windows services. "
            "Targets config Components by default, or every deployed KD-* "
            "service with --all-deployed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python manage_kd_services.py status
  python manage_kd_services.py start --all-deployed
  python manage_kd_services.py stop
  python manage_kd_services.py start --config config/my-config.json
  python manage_kd_services.py restart --components Content,NiFi
  python manage_kd_services.py start --components KD-Content,KD-Community
  python manage_kd_services.py delete --force
  python manage_kd_services.py uninstall --non-interactive
  python manage_kd_services.py create --non-interactive
  python manage_kd_services.py delete --all-deployed --force

service names:
  Each component becomes KD-<Name>  (NiFi → KD-NiFi).
  --components accepts either form: Content  or  KD-Content.

discovery:
  --all-deployed  operate on every Windows service named KD-* that is
                  registered on this machine (not limited to config Components).
  If Components is empty and --components is omitted, discovery is used
  automatically so any deployed service can still be started/stopped.

order:
  start     : LicenseServer first, then remaining order
  stop/delete/uninstall : remaining first, LicenseServer last
  create   : LicenseServer first, then remaining (native -install; NiFi via NSSM)
""",
    )
    p.add_argument(
        "action",
        choices=ACTIONS,
        help="Operation to perform",
    )
    p.add_argument(
        "--config",
        dest="config_path",
        metavar="PATH",
        help="JSON config (default: config/my-config.json or config/default-config.json)",
    )
    p.add_argument(
        "--components",
        metavar="LIST",
        help=(
            "Override target list (comma-separated). Accepts component names "
            "or full service names, e.g. Content,NiFi or KD-Content,KD-NiFi"
        ),
    )
    p.add_argument(
        "--all-deployed",
        "-a",
        action="store_true",
        help=(
            "Operate on every KD-* Windows service registered on this machine "
            "(ignores config Components unless none are found)"
        ),
    )
    p.add_argument("--dry-run", action="store_true", help="Print actions only")
    p.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation for delete/uninstall",
    )
    p.add_argument(
        "--non-interactive",
        action="store_true",
        help="No prompts (implies --force for destructive actions)",
    )
    return p


def _resolve_config_path(explicit: Optional[str]) -> Path:
    root = Path(__file__).resolve().parent
    if explicit:
        return Path(explicit)
    for rel in ("config/my-config.json", "config/default-config.json"):
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return root / "config/default-config.json"


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    action = args.action

    needs_admin = action in ("start", "stop", "restart", "delete", "uninstall", "create")
    if needs_admin and sys.platform == "win32" and not _is_elevated():
        print(
            "ERROR: This action requires Administrator privileges.\n"
            "Right-click PowerShell → Run as administrator, then re-run.\n"
            "Or use:  .\\Manage-KDServices.ps1 " + action,
            file=sys.stderr,
        )
        return 1

    config_path = _resolve_config_path(args.config_path)
    config: Dict[str, Any] = {}
    try:
        config = get_kd_config(config_path)
    except Exception as e:
        # Config is optional when --all-deployed or --components is given
        if not args.all_deployed and not args.components:
            print(f"FATAL: cannot load config ({config_path}): {e}", file=sys.stderr)
            return 1
        print(f"WARNING: cannot load config ({config_path}): {e}", file=sys.stderr)
        print("         Continuing with discovered / explicit service list only.", file=sys.stderr)

    components = _resolve_components(
        args.components,
        list(config.get("Components") or []),
        all_deployed=bool(args.all_deployed),
    )

    if not components:
        print(
            "FATAL: No services to manage.\n"
            "  - Set Components in the config JSON, or\n"
            "  - Pass --components Content,NiFi, or\n"
            "  - Pass --all-deployed (requires at least one KD-* service installed).",
            file=sys.stderr,
        )
        return 1

    base_path = Path(config.get("BasePath") or ".")
    uninstall_template = config.get("ServiceUninstallArgsTemplate")

    # Prefer BasePath\logs when it exists; otherwise temp (avoids failing on
    # a not-yet-created install path during status checks).
    log_dir = base_path / "logs"
    if not log_dir.parent.is_dir():
        log_dir = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp")
    try:
        log.initialize(log_dir)
    except Exception:
        try:
            log.initialize(Path(os.environ.get("TEMP") or "/tmp"))
        except Exception:
            pass

    source = (
        "explicit --components"
        if args.components
        else ("all deployed KD-* services" if args.all_deployed else "config Components")
    )
    # If we fell back to discovery because config Components was empty:
    if not args.components and not args.all_deployed and not (config.get("Components") or []):
        source = "all deployed KD-* services (config Components empty)"

    print("=" * 60)
    print(f"  KD Service Manager  |  action={action}")
    print(f"  Config : {config_path}")
    print(f"  BasePath: {base_path}")
    print(f"  Source : {source}")
    print(f"  Targets ({len(components)}): {', '.join(components)}")
    print("=" * 60)

    destructive = action in ("delete", "uninstall")
    auto_yes = args.force or args.non_interactive or args.dry_run
    if destructive and not auto_yes:
        print()
        print(f"This will {action} these Windows services:")
        for c in components:
            print(f"  - {service_manager.get_kd_service_name(c)}")
        try:
            print(f"{_PROMPT_YELLOW}Type YES to continue: {_PROMPT_RESET}", end="", flush=True)
            answer = input().strip()
        except EOFError:
            answer = ""
        if answer != "YES":
            print("Aborted.")
            return 0

    if action == "status":
        return action_status(components)
    if action == "start":
        return action_start(components, args.dry_run, base_path=base_path, config=config)
    if action == "stop":
        return action_stop(components, args.dry_run)
    if action == "restart":
        return action_restart(components, args.dry_run, base_path=base_path, config=config)
    if action == "delete":
        return action_delete(components, args.dry_run)
    if action == "uninstall":
        return action_uninstall(
            components,
            base_path=base_path,
            uninstall_template=uninstall_template,
            dry_run=args.dry_run,
        )
    if action == "create":
        # Prefer config Components (folders must already exist under BasePath).
        # --all-deployed is ignored for create — you cannot create from SCM alone.
        create_list = list(config.get("Components") or [])
        if args.components:
            create_list = [
                _normalize_component(c)
                for c in args.components.split(",")
                if c.strip()
            ]
        if not create_list:
            print(
                "FATAL: create requires Components in the config JSON "
                "or --components Content,NiFi,...",
                file=sys.stderr,
            )
            return 1
        nifi_cfg = config.get("NiFi") or {}
        nifi_install = bool(nifi_cfg.get("InstallService", True))
        find_cfg = config.get("Find") or {}
        # Merge Ports.Find into find_cfg if ServerPort not set
        ports = config.get("Ports") or {}
        if "ServerPort" not in find_cfg and ports.get("Find"):
            find_cfg = dict(find_cfg)
            find_cfg["ServerPort"] = ports["Find"]
        find_install = bool(find_cfg.get("InstallService", True))
        if config.get("InstallService") is False:
            # Global switch still allows explicit create via this tool
            pass
        nifi_port = str(
            nifi_cfg.get("WebHttpsPort") or ports.get("NiFi") or "8443"
        )
        return action_create(
            create_list,
            base_path=base_path,
            install_template=config.get("ServiceInstallArgsTemplate"),
            start_mode=str(config.get("StartMode") or "Auto"),
            nifi_install_service=nifi_install,
            find_install_service=find_install,
            find_cfg=find_cfg,
            ports=ports,
            license_port=str(config.get("LicensePort") or ports.get("LicenseServer") or ""),
            nifi_port=nifi_port,
            dry_run=args.dry_run,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())

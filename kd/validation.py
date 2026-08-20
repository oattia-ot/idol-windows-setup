"""
Pre-flight environment validation.
"""

from __future__ import annotations

import hashlib
import platform
import re
import socket
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .logging import log

# ANSI yellow for user-facing questions / input prompts
_PROMPT_YELLOW = "\033[33m"
_PROMPT_RESET = "\033[0m"

# Optional rich console for colored SHA-256 report (same stack as kd.logging)
try:
    from rich.console import Console
    from rich.text import Text

    _rich_console = Console(highlight=False)
    _HAS_RICH = True
except ImportError:
    _rich_console = None
    _HAS_RICH = False

# ANSI fallbacks (Windows Terminal / modern consoles)
_ANSI_BLUE = "\033[94m"
_ANSI_CYAN = "\033[96m"
_ANSI_PURPLE = "\033[95m"  # bright magenta / purple (periwinkle-style)
_ANSI_BOLD = "\033[1m"
_ANSI_RESET = "\033[0m"
_ANSI_DIM = "\033[2m"

# PowerShell command shown to the operator so they can independently
# verify ZIP file integrity on the machine where the ZIPs were downloaded.
# Run inside the ZipPath folder (folder that contains the downloaded ZIPs).
# - Runs on top-level files in the current folder only
# - Skips sha256.txt itself
# - Calculates the SHA-256 checksum for each file
# - Writes the checksum and filename to sha256.txt
# - Uses UTF-8 encoding
_PS_SHA256_CMD = (
    "Get-ChildItem -File |\n"
    "    Where-Object { $_.Name -ne \"sha256.txt\" } |\n"
    "    Get-FileHash -Algorithm SHA256 |\n"
    "    ForEach-Object { \"$($_.Hash) $($_.Path | Split-Path -Leaf)\" } |\n"
    "    Out-File -Encoding UTF8 sha256.txt"
)

_PS_SHA256_HINT_LINES = (
    "PowerShell command to verify ZIP file integrity",
    "",
    "Run this command on the machine where you downloaded the ZIP files.",
    "Before running the command:",
    "  1. Open the folder containing the downloaded ZIP files.",
    "  2. Make sure all ZIP files are in this folder.",
    "  3. Run the command from this folder.",
    "  4. The command calculates the SHA-256 checksum for each ZIP file.",
    "  5. Compare the generated checksums with the SHA-256 checksums",
    "     provided with the original uploaded ZIP files.",
    "  6. Matching checksums confirm that the downloaded files are",
    "     identical to the uploaded files.",
    "",
    "The command:",
    "  # - Runs on top-level files in the current folder only",
    "  # - Skips sha256.txt itself",
    "  # - Calculates the SHA-256 checksum for each file",
    "  # - Writes the checksum and filename to sha256.txt",
    "  # - Uses UTF-8 encoding",
)


def test_kd_admin_rights() -> Dict[str, Any]:
    is_admin = False
    detail = "Unable to determine elevation status"
    try:
        import ctypes
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        detail = "Running elevated" if is_admin else "Script must be run as Administrator"
    except Exception:
        # Non-Windows or restricted
        detail = "Admin check only meaningful on Windows"
        is_admin = platform.system() != "Windows"  # allow non-Windows for dry-run testing
    return {
        "Name": "Administrator privileges",
        "Pass": is_admin,
        "Detail": detail,
    }


def test_kd_operating_system() -> Dict[str, Any]:
    is_win64 = platform.system() == "Windows" and platform.machine().endswith("64")
    detail = f"{platform.system()} {platform.release()} ({platform.machine()})"
    return {
        "Name": "Operating system (Windows x64)",
        "Pass": is_win64 or platform.system() != "Windows",  # allow non-Windows for development
        "Detail": detail,
    }


def test_kd_powershell_version() -> Dict[str, Any]:
    # Python equivalent: just check Python version
    ver = sys.version_info
    pass_ = ver >= (3, 8)
    return {
        "Name": "Python version",
        "Pass": pass_,
        "Detail": f"Current: {ver.major}.{ver.minor}.{ver.micro}, Required: >= 3.8",
    }


def test_kd_memory(minimum_gb: int = 4) -> Dict[str, Any]:
    total_gb = 0.0
    try:
        import psutil
        total_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except ImportError:
        # Fallback rough estimate via ctypes on Windows
        try:
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            total_gb = round(stat.ullTotalPhys / (1024 ** 3), 1)
        except Exception:
            return {
                "Name": "System memory",
                "Pass": True,  # don't block if we can't query
                "Detail": "Unable to query memory (skipped)",
            }
    pass_ = total_gb >= minimum_gb
    return {
        "Name": "System memory",
        "Pass": pass_,
        "Detail": f"Total: {total_gb}GB, Required: {minimum_gb}GB",
    }


def test_kd_disk_space(path: str, minimum_gb: int = 10) -> Dict[str, Any]:
    try:
        import shutil
        # Use the drive of the path even if the folder does not exist yet
        p = Path(path)
        # On Windows Path.drive works; on Linux we just use /
        target = p.drive + "\\" if p.drive else "/"
        usage = shutil.disk_usage(target if Path(target).exists() else str(p.parent or "/"))
        free_gb = round(usage.free / (1024 ** 3), 1)
        pass_ = free_gb >= minimum_gb
        return {
            "Name": "Disk space",
            "Pass": pass_,
            "Detail": f"Free: {free_gb}GB, Required: {minimum_gb}GB",
        }
    except Exception as e:
        return {
            "Name": "Disk space",
            "Pass": False,
            "Detail": f"Could not determine free space: {e}",
        }


def test_kd_port_available(port: int) -> Dict[str, Any]:
    in_use = False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
    except OSError:
        in_use = True
    return {
        "Name": f"Port {port} available",
        "Pass": not in_use,
        "Detail": f"Port {port} is {'already in use' if in_use else 'free'}",
    }


def test_kd_zip_source_reachable(zip_path: str) -> Dict[str, Any]:
    p = Path(zip_path)
    exists = p.is_dir()
    has_zips = False
    if exists:
        has_zips = any(p.glob("*.zip"))
    if not exists:
        detail = f"Path not found: {zip_path}"
    elif not has_zips:
        detail = f"No .zip files found in {zip_path}"
    else:
        detail = f"Found packages in {zip_path}"
    return {
        "Name": "ZIP package source",
        "Pass": exists and has_zips,
        "Detail": detail,
    }


def test_kd_package_versions(zip_path: str, base_path: str = "") -> Dict[str, Any]:
    """
    Parse major.minor from KD ZIP names and warn on mismatches / BasePath drift.

    Pass is True when ZipPath is empty/missing (skipped) or when a consensus
    version exists. Version mismatches are reported as Warning=True but still
    Pass so install can continue with operator awareness.
    """
    from . import discovery

    if not zip_path:
        return {
            "Name": "KD package version (ZIP names)",
            "Pass": True,
            "Warning": False,
            "Detail": "ZipPath not set — skipped version scan",
            "Analysis": None,
        }

    analysis = discovery.analyze_kd_zip_versions(zip_path)
    warnings = list(analysis.get("Warnings") or [])
    mm = analysis.get("MajorMinor")
    suggested = analysis.get("SuggestedBasePath")

    if mm and base_path:
        norm_base = str(base_path).replace("/", "\\").rstrip("\\")
        expected_suffix = f"\\{mm}"
        if re.search(r"(?i)KnowledgeDiscovery\\\d+\.\d+$", norm_base) and not norm_base.lower().endswith(
            expected_suffix.lower()
        ):
            warnings.append(
                f"BasePath is '{base_path}' but ZIP packages consensus version is {mm}. "
                f"Suggested BasePath: {suggested}"
            )

    if not analysis.get("Success"):
        # No parseable versions: soft warning, do not fail the install
        detail = "; ".join(warnings) if warnings else (analysis.get("Detail") or "No version info")
        return {
            "Name": "KD package version (ZIP names)",
            "Pass": True,
            "Warning": bool(warnings),
            "Detail": detail,
            "Analysis": analysis,
        }

    detail_parts = [analysis.get("Detail") or f"Consensus {mm}"]
    if suggested:
        detail_parts.append(f"Suggested BasePath: {suggested}")
    if warnings:
        detail_parts.extend(warnings)

    return {
        "Name": "KD package version (ZIP names)",
        "Pass": True,
        "Warning": bool(warnings),
        "Detail": " | ".join(detail_parts),
        "Analysis": analysis,
    }


def test_kd_environment(config: Dict[str, Any], required_ports: List[int] | None = None) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    results.append(test_kd_admin_rights())
    results.append(test_kd_operating_system())
    results.append(test_kd_powershell_version())  # actually Python version
    results.append(test_kd_memory(4))
    results.append(test_kd_disk_space(config.get("BasePath", "C:\\"), 10))
    results.append(test_kd_zip_source_reachable(config.get("ZipPath", "")))
    results.append(
        test_kd_package_versions(
            config.get("ZipPath", ""),
            base_path=str(config.get("BasePath") or ""),
        )
    )
    if required_ports:
        for p in required_ports:
            results.append(test_kd_port_available(int(p)))
    failed = [r for r in results if not r["Pass"]]
    return {
        "AllPassed": len(failed) == 0,
        "Results": results,
        "Failed": failed,
    }


def compute_file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """
    Compute the SHA-256 hex digest of a file using buffered reads.
    Suitable for multi-GB OpenText component ZIP packages.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def generate_zip_sha_report(
    zip_path: str | Path,
) -> Dict[str, Any]:
    """
    Generate SHA-256 hashes for every .zip under ZipPath.

    Returns a dict with:
      Success, Hashes (list of {FileName, Path, SizeMB, SHA256}), Detail
    """
    zip_dir = Path(zip_path) if zip_path else Path()
    if not zip_dir.is_dir():
        return {
            "Success": False,
            "Hashes": [],
            "Detail": f"ZipPath not found or not a directory: {zip_path}",
        }

    zips = sorted(zip_dir.glob("*.zip"))
    if not zips:
        return {
            "Success": True,
            "Hashes": [],
            "Detail": f"No .zip files found in {zip_dir}",
        }

    hashes: List[Dict[str, Any]] = []
    log.info(f"Computing SHA-256 for {len(zips)} ZIP package(s) in {zip_dir} ...")
    for z in zips:
        try:
            size_mb = z.stat().st_size / (1024 * 1024)
            log.info(f"  Hashing {z.name} ({size_mb:.1f} MB) ...")
            sha = compute_file_sha256(z)
            entry = {
                "FileName": z.name,
                "Path": str(z.resolve()),
                "SizeMB": round(size_mb, 2),
                "SHA256": sha,
            }
            hashes.append(entry)
            # Color the SHA in the live log line when rich is available
            if _HAS_RICH:
                log.info(f"    {z.name}: [bold cyan]{sha}[/bold cyan]")
            else:
                log.info(f"    {z.name}: {sha}")
        except Exception as e:
            log.error(f"  Failed to hash {z.name}: {e}")
            hashes.append(
                {
                    "FileName": z.name,
                    "Path": str(z),
                    "SizeMB": 0.0,
                    "SHA256": None,
                    "Error": str(e),
                }
            )

    ok = all(h.get("SHA256") for h in hashes)
    return {
        "Success": ok,
        "Hashes": hashes,
        "Detail": f"Hashed {len(hashes)} package(s)" + ("" if ok else " (some failures)"),
    }


def _print_blue(msg: str) -> None:
    """Print an informational line in blue (rich or ANSI)."""
    if _HAS_RICH and _rich_console is not None:
        _rich_console.print(f"[bold blue]{msg}[/bold blue]")
    else:
        print(f"{_ANSI_BLUE}{_ANSI_BOLD}{msg}{_ANSI_RESET}")


def _print_purple(msg: str) -> None:
    """Print a line in purple / bright magenta (rich or ANSI)."""
    if _HAS_RICH and _rich_console is not None:
        _rich_console.print(f"[bold magenta]{msg}[/bold magenta]")
    else:
        print(f"{_ANSI_PURPLE}{_ANSI_BOLD}{msg}{_ANSI_RESET}")


def _print_powershell_sha_hint() -> None:
    """
    Show purple instructions + the PowerShell command the operator can run
    inside ZipPath to independently generate a sha256.txt for comparison.
    """
    if _HAS_RICH and _rich_console is not None:
        _rich_console.print()
        for line in _PS_SHA256_HINT_LINES:
            if line == "":
                _rich_console.print()
            else:
                _rich_console.print(f"[bold magenta]  {line}[/bold magenta]")
        _rich_console.print(f"[bold magenta]{_PS_SHA256_CMD}[/bold magenta]")
        _rich_console.print()
    else:
        print()
        for line in _PS_SHA256_HINT_LINES:
            if line == "":
                print()
            else:
                print(f"{_ANSI_PURPLE}{_ANSI_BOLD}  {line}{_ANSI_RESET}")
        for line in _PS_SHA256_CMD.splitlines():
            print(f"{_ANSI_PURPLE}{_ANSI_BOLD}{line}{_ANSI_RESET}")
        print()


def _print_sha_report(zip_dir: Path, hashes: List[Dict[str, Any]]) -> None:
    """
    Pretty-print the SHA-256 report.

    - Headers / info lines → blue
    - Filename → bold white/default
    - Size → dim
    - SHA-256 value → bold cyan (highly visible)
    - Errors → bold red
    """
    sep = "=" * 72

    if _HAS_RICH and _rich_console is not None:
        _rich_console.print()
        _rich_console.print(f"[bold blue]{sep}[/bold blue]")
        _rich_console.print(
            "[bold blue]  ZIP SHA-256 VERIFICATION (before extraction)[/bold blue]"
        )
        _rich_console.print(f"[bold blue]{sep}[/bold blue]")
        _rich_console.print(
            f"[blue]  Source folder: [bold]{zip_dir}[/bold][/blue]"
        )
        _rich_console.print()

        for h in hashes:
            name = h.get("FileName", "?")
            size = h.get("SizeMB", 0)
            sha = h.get("SHA256")
            err = h.get("Error")

            _rich_console.print(f"  [bold]{name}[/bold]")
            _rich_console.print(f"    [dim]Size   : {size:.2f} MB[/dim]")
            if sha:
                # SHA-256 code in bold cyan for easy visual scanning / copy
                _rich_console.print(
                    f"    [blue]SHA256 :[/blue] [bold cyan]{sha}[/bold cyan]"
                )
            else:
                _rich_console.print(
                    f"    [bold red]SHA256 : ERROR — {err or 'unknown'}[/bold red]"
                )
            _rich_console.print()

        _rich_console.print(f"[bold blue]{sep}[/bold blue]")
        _rich_console.print(
            "[blue]  Compare these hashes with the values published by the download[/blue]"
        )
        _rich_console.print(
            "[blue]  source (OpenText / vendor portal) to confirm package integrity.[/blue]"
        )
        _rich_console.print(f"[bold blue]{sep}[/bold blue]")
        _rich_console.print()
    else:
        # ANSI fallback
        print()
        print(f"{_ANSI_BLUE}{_ANSI_BOLD}{sep}{_ANSI_RESET}")
        print(
            f"{_ANSI_BLUE}{_ANSI_BOLD}  ZIP SHA-256 VERIFICATION (before extraction){_ANSI_RESET}"
        )
        print(f"{_ANSI_BLUE}{_ANSI_BOLD}{sep}{_ANSI_RESET}")
        print(f"{_ANSI_BLUE}  Source folder: {_ANSI_BOLD}{zip_dir}{_ANSI_RESET}")
        print()

        for h in hashes:
            name = h.get("FileName", "?")
            size = h.get("SizeMB", 0)
            sha = h.get("SHA256")
            err = h.get("Error")

            print(f"  {_ANSI_BOLD}{name}{_ANSI_RESET}")
            print(f"    {_ANSI_DIM}Size   : {size:.2f} MB{_ANSI_RESET}")
            if sha:
                print(
                    f"    {_ANSI_BLUE}SHA256 :{_ANSI_RESET} "
                    f"{_ANSI_CYAN}{_ANSI_BOLD}{sha}{_ANSI_RESET}"
                )
            else:
                print(
                    f"    {_ANSI_BOLD}\033[91mSHA256 : ERROR — {err or 'unknown'}"
                    f"{_ANSI_RESET}"
                )
            print()

        print(f"{_ANSI_BLUE}{_ANSI_BOLD}{sep}{_ANSI_RESET}")
        print(
            f"{_ANSI_BLUE}  Compare these hashes with the values published by the download{_ANSI_RESET}"
        )
        print(
            f"{_ANSI_BLUE}  source (OpenText / vendor portal) to confirm package integrity.{_ANSI_RESET}"
        )
        print(f"{_ANSI_BLUE}{_ANSI_BOLD}{sep}{_ANSI_RESET}")
        print()


def load_sha256_txt(zip_dir: Path) -> Dict[str, Any]:
    """
    Load expected hashes from ``sha256.txt`` in *zip_dir*.

    Accepted line formats (case-insensitive hash):
      HASH  filename
      HASH *filename          (GNU coreutils style)
      HASH  full\\path\\file   (basename is used for matching)

    Returns:
      {
        "Found": bool,
        "Path": Path | None,
        "Expected": { filename_lower: hash_lower },
        "Detail": str,
      }
    """
    candidates = [
        zip_dir / "sha256.txt",
        zip_dir / "SHA256.txt",
        zip_dir / "sha256sums.txt",
    ]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return {
            "Found": False,
            "Path": None,
            "Expected": {},
            "Detail": f"No sha256.txt found in {zip_dir}",
        }

    expected: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as e:
        return {
            "Found": True,
            "Path": path,
            "Expected": {},
            "Detail": f"Could not read {path.name}: {e}",
        }

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Support "HASH  name" and "HASH *name"
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        digest, name = parts[0].strip(), parts[1].strip()
        if name.startswith("*"):
            name = name[1:]
        # Use basename only so full paths still match our FileName keys
        name = Path(name).name
        if len(digest) == 64 and all(c in "0123456789abcdefABCDEF" for c in digest):
            expected[name.lower()] = digest.lower()

    return {
        "Found": True,
        "Path": path,
        "Expected": expected,
        "Detail": f"Loaded {len(expected)} expected hash(es) from {path.name}",
    }


def compare_zip_hashes(
    hashes: List[Dict[str, Any]],
    expected: Dict[str, str],
) -> Dict[str, Any]:
    """
    Compare computed hashes against the expected map from sha256.txt.

    Returns:
      {
        "AllMatch": bool,
        "Matches": [ {FileName, SHA256} ],
        "Mismatches": [ {FileName, Computed, Expected} ],
        "MissingInFile": [ {FileName, Computed} ],   # ZIP present, not in sha256.txt
        "ExtraInFile": [ {FileName, Expected} ],     # listed in sha256.txt, no ZIP
      }
    """
    matches: List[Dict[str, Any]] = []
    mismatches: List[Dict[str, Any]] = []
    missing_in_file: List[Dict[str, Any]] = []

    seen_lower: set = set()
    for h in hashes:
        name = h.get("FileName") or "?"
        key = name.lower()
        seen_lower.add(key)
        computed = (h.get("SHA256") or "").lower()
        exp = expected.get(key)

        if exp is None:
            missing_in_file.append({"FileName": name, "Computed": computed or None})
        elif not computed:
            mismatches.append(
                {
                    "FileName": name,
                    "Computed": None,
                    "Expected": exp,
                    "Error": h.get("Error") or "hash failed",
                }
            )
        elif computed == exp:
            matches.append({"FileName": name, "SHA256": computed})
        else:
            mismatches.append(
                {
                    "FileName": name,
                    "Computed": computed,
                    "Expected": exp,
                }
            )

    extra_in_file = [
        {"FileName": name, "Expected": dig}
        for name, dig in expected.items()
        if name not in seen_lower
    ]

    all_match = (
        len(mismatches) == 0
        and len(missing_in_file) == 0
        and len(extra_in_file) == 0
        and len(matches) > 0
    )
    # Soft all-match: every computed ZIP that appears in the file matches,
    # and there are no hard mismatches (extras / missing-in-file are warnings)
    strict_ok = len(mismatches) == 0 and len(matches) + len(missing_in_file) == len(hashes)

    return {
        "AllMatch": all_match,
        "StrictOk": strict_ok and len(mismatches) == 0,
        "Matches": matches,
        "Mismatches": mismatches,
        "MissingInFile": missing_in_file,
        "ExtraInFile": extra_in_file,
    }


def _print_comparison_report(
    zip_dir: Path,
    sha_file: Optional[Path],
    comparison: Dict[str, Any],
) -> None:
    """Colored report of MATCH / MISMATCH / missing / extra entries."""
    sep = "=" * 72
    mismatches = comparison.get("Mismatches") or []
    missing = comparison.get("MissingInFile") or []
    extra = comparison.get("ExtraInFile") or []
    matches = comparison.get("Matches") or []

    def _rich(msg: str) -> None:
        if _HAS_RICH and _rich_console is not None:
            _rich_console.print(msg)
        else:
            # strip simple rich tags for plain fallback is imperfect; use ANSI paths below
            print(msg)

    if _HAS_RICH and _rich_console is not None:
        _rich_console.print()
        _rich_console.print(f"[bold blue]{sep}[/bold blue]")
        _rich_console.print(
            "[bold blue]  SHA-256 COMPARISON vs sha256.txt[/bold blue]"
        )
        _rich_console.print(f"[bold blue]{sep}[/bold blue]")
        if sha_file:
            _rich_console.print(f"[blue]  Reference file: [bold]{sha_file}[/bold][/blue]")
        _rich_console.print(
            f"[blue]  Matches: [bold green]{len(matches)}[/bold green]  |  "
            f"Mismatches: [bold red]{len(mismatches)}[/bold red]  |  "
            f"Missing in file: [bold yellow]{len(missing)}[/bold yellow]  |  "
            f"Extra in file: [bold yellow]{len(extra)}[/bold yellow][/blue]"
        )
        _rich_console.print()

        if matches:
            _rich_console.print("[bold green]  ✓ MATCHED[/bold green]")
            for m in matches:
                _rich_console.print(
                    f"    [green]{m['FileName']}[/green]  "
                    f"[dim]{m['SHA256']}[/dim]"
                )
            _rich_console.print()

        if mismatches:
            _rich_console.print("[bold red]  ✗ DIFFERENT HASHES[/bold red]")
            for m in mismatches:
                _rich_console.print(f"    [bold red]{m['FileName']}[/bold red]")
                _rich_console.print(
                    f"      [red]Computed :[/red] [bold cyan]{m.get('Computed') or '(failed)'}[/bold cyan]"
                )
                _rich_console.print(
                    f"      [red]Expected :[/red] [bold cyan]{m.get('Expected') or '(none)'}[/bold cyan]"
                )
            _rich_console.print()

        if missing:
            _rich_console.print(
                "[bold yellow]  ! ZIP present but NOT listed in sha256.txt[/bold yellow]"
            )
            for m in missing:
                _rich_console.print(
                    f"    [yellow]{m['FileName']}[/yellow]  "
                    f"[dim]computed={m.get('Computed') or '?'}[/dim]"
                )
            _rich_console.print()

        if extra:
            _rich_console.print(
                "[bold yellow]  ! Listed in sha256.txt but ZIP not found[/bold yellow]"
            )
            for m in extra:
                _rich_console.print(
                    f"    [yellow]{m['FileName']}[/yellow]  "
                    f"[dim]expected={m.get('Expected')}[/dim]"
                )
            _rich_console.print()

        _rich_console.print(f"[bold blue]{sep}[/bold blue]")
        _rich_console.print()
    else:
        print()
        print(f"{_ANSI_BLUE}{_ANSI_BOLD}{sep}{_ANSI_RESET}")
        print(f"{_ANSI_BLUE}{_ANSI_BOLD}  SHA-256 COMPARISON vs sha256.txt{_ANSI_RESET}")
        print(f"{_ANSI_BLUE}{_ANSI_BOLD}{sep}{_ANSI_RESET}")
        if sha_file:
            print(f"{_ANSI_BLUE}  Reference file: {_ANSI_BOLD}{sha_file}{_ANSI_RESET}")
        print(
            f"{_ANSI_BLUE}  Matches: {len(matches)}  |  Mismatches: {len(mismatches)}  |  "
            f"Missing in file: {len(missing)}  |  Extra in file: {len(extra)}{_ANSI_RESET}"
        )
        print()
        if matches:
            print(f"  {_ANSI_BOLD}\033[92mMATCHED{_ANSI_RESET}")
            for m in matches:
                print(f"    {m['FileName']}  {m['SHA256']}")
            print()
        if mismatches:
            print(f"  {_ANSI_BOLD}\033[91mDIFFERENT HASHES{_ANSI_RESET}")
            for m in mismatches:
                print(f"    {m['FileName']}")
                print(f"      Computed : {m.get('Computed') or '(failed)'}")
                print(f"      Expected : {m.get('Expected') or '(none)'}")
            print()
        if missing:
            print(f"  {_ANSI_BOLD}\033[93mZIP present but NOT listed in sha256.txt{_ANSI_RESET}")
            for m in missing:
                print(f"    {m['FileName']}  computed={m.get('Computed') or '?'}")
            print()
        if extra:
            print(f"  {_ANSI_BOLD}\033[93mListed in sha256.txt but ZIP not found{_ANSI_RESET}")
            for m in extra:
                print(f"    {m['FileName']}  expected={m.get('Expected')}")
            print()
        print(f"{_ANSI_BLUE}{_ANSI_BOLD}{sep}{_ANSI_RESET}")
        print()


def confirm_zip_hashes(
    zip_path: str | Path,
    *,
    non_interactive: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Before any ZIP extraction:

    1. Compute SHA-256 for every ``*.zip`` under ZipPath.
    2. Load ``sha256.txt`` from the same folder (if present).
    3. Compare computed hashes to the file contents.
    4. If all identical → continue.
    5. If any differ → list every file with a different code; interactive mode
       asks the operator whether to continue anyway or abort.

    Non-interactive: continues only when there are no hard mismatches
    (different hash values). Missing sha256.txt is a warning, not a hard fail,
    unless the operator is interactive and declines.

    Returns:
      {
        "Success": bool,
        "Aborted": bool,
        "Hashes": list,
        "Comparison": dict | None,
        "Detail": str,
      }
    """
    zip_dir = Path(zip_path) if zip_path else Path()
    report = generate_zip_sha_report(zip_dir)
    hashes = report.get("Hashes") or []

    if not hashes:
        log.info("No ZIP packages present — skipping SHA-256 confirmation.")
        return {
            "Success": True,
            "Aborted": False,
            "Hashes": [],
            "Comparison": None,
            "Detail": report.get("Detail") or "No ZIPs to verify",
        }

    # Colored computed-hash report
    _print_sha_report(zip_dir.resolve(), hashes)

    # Purple tip: how to (re)generate sha256.txt
    _print_powershell_sha_hint()

    # Load reference file and compare
    loaded = load_sha256_txt(zip_dir)
    comparison: Optional[Dict[str, Any]] = None

    if not loaded["Found"]:
        log.warn(
            f"No sha256.txt found in {zip_dir}. "
            "Place a downloaded/generated sha256.txt next to the ZIP packages "
            "for automatic comparison."
        )
        _print_blue(
            "  No sha256.txt found — cannot auto-compare. "
            "Generate one with the purple command above, then re-run."
        )
    else:
        log.info(loaded["Detail"])
        comparison = compare_zip_hashes(hashes, loaded["Expected"])
        _print_comparison_report(zip_dir, loaded.get("Path"), comparison)

        if comparison["Mismatches"]:
            log.error(
                f"{len(comparison['Mismatches'])} ZIP(s) have a DIFFERENT SHA-256 "
                "than listed in sha256.txt:"
            )
            for m in comparison["Mismatches"]:
                log.error(
                    f"  {m['FileName']}: computed={m.get('Computed')} "
                    f"expected={m.get('Expected')}"
                )
        if comparison.get("AllMatch"):
            log.info("All ZIP SHA-256 hashes match sha256.txt — integrity OK.")
        elif comparison.get("StrictOk") and not comparison["Mismatches"]:
            log.info(
                "All listed ZIPs match sha256.txt "
                f"({len(comparison.get('MissingInFile') or [])} ZIP(s) not in file)."
            )

    if dry_run:
        log.info("[DryRun] ZIP SHA-256 comparison finished; skipping confirmation.")
        return {
            "Success": True,
            "Aborted": False,
            "Hashes": hashes,
            "Comparison": comparison,
            "Detail": "Dry-run: comparison done, confirmation skipped",
        }

    # Non-interactive: hard-fail only on actual hash mismatches
    if non_interactive:
        if comparison and comparison.get("Mismatches"):
            log.error(
                "Non-interactive mode: hash mismatches detected — aborting setup."
            )
            return {
                "Success": False,
                "Aborted": True,
                "Hashes": hashes,
                "Comparison": comparison,
                "Detail": "Non-interactive: mismatched SHA-256 vs sha256.txt",
            }
        log.info(
            "Non-interactive mode: continuing "
            + (
                "(all hashes match sha256.txt)."
                if comparison and comparison.get("AllMatch")
                else "(no hard mismatches, or no sha256.txt)."
            )
        )
        return {
            "Success": True,
            "Aborted": False,
            "Hashes": hashes,
            "Comparison": comparison,
            "Detail": "Non-interactive: comparison done",
        }

    # Interactive gate
    has_mismatches = bool(comparison and comparison.get("Mismatches"))
    all_match = bool(comparison and comparison.get("AllMatch"))

    if all_match:
        _print_blue(
            "All SHA-256 hashes are identical to sha256.txt. "
            "Type YES (or Y) to continue setup."
        )
    elif has_mismatches:
        _print_blue(
            "WARNING: one or more files have DIFFERENT SHA-256 codes (listed above)."
        )
        _print_blue(
            "Type YES (or Y) to continue anyway, or anything else to abort."
        )
    else:
        _print_blue(
            "Type YES (or Y) to confirm the hashes and continue setup."
        )
        _print_blue(
            "Anything else will abort the install before any extraction begins."
        )

    try:
        print(f"{_PROMPT_YELLOW}Confirm ZIP hashes / continue? [yes/no]: {_PROMPT_RESET}", end="", flush=True)
        answer = input().strip().lower()
    except EOFError:
        answer = "no"

    if answer in ("y", "yes"):
        if has_mismatches:
            log.warn(
                "Operator continued despite SHA-256 mismatches. Proceeding with setup."
            )
        else:
            log.info("Operator confirmed ZIP SHA-256 hashes. Continuing with setup.")
        return {
            "Success": True,
            "Aborted": False,
            "Hashes": hashes,
            "Comparison": comparison,
            "Detail": (
                "Operator continued despite mismatches"
                if has_mismatches
                else "Operator confirmed hashes"
            ),
        }

    log.error("Operator declined ZIP hash confirmation — aborting setup.")
    return {
        "Success": False,
        "Aborted": True,
        "Hashes": hashes,
        "Comparison": comparison,
        "Detail": "Operator aborted: ZIP hashes not confirmed",
    }

"""
Locates component ZIP packages and the correct main service executable.
Strong ranking + exclusion list tuned for real OpenText KD package layouts.
Also extracts Knowledge Discovery major.minor version from ZIP file names
(e.g. Content_26.3.1_WINDOWS_X86_64.zip → 26.3) for BasePath suggestions.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Packages that use independent versioning (not KD major.minor)
_NON_KD_VERSION_NAMES = re.compile(
    r"(?i)(?:^|[^a-z])(?:nifi|nssm|jdk|jre|temurin|adoptium|vc_redist|vcredist)(?:[^a-z]|$)"
)

# OpenText KD-style: Content_26.3.1_WINDOWS_X86_64.zip / Community_26.3.0_...
_KD_UNDERSCORE_VER = re.compile(
    r"(?i)(?:^|_)(?:content|community|category|agentstore|licenseserver|view|qms|"
    r"answerserver|statsserver|dah|dih|cfs|eduction|keyview|"
    r"answerbankagentstore|conversationagentstore|qmsagentstore)"
    r"[^0-9]*_(\d+)\.(\d+)(?:\.(\d+))?_"
)

# Generic OpenText component_MAJOR.MINOR.PATCH_PLATFORM.zip
_GENERIC_UNDERSCORE_VER = re.compile(r"(?i)_(\d+)\.(\d+)(?:\.(\d+))?_(?:windows|linux|win64|x86)")

# Find-style: find-26.2.0.zip
_FIND_DASH_VER = re.compile(r"(?i)(?:^|[^0-9])find[_-](\d+)\.(\d+)(?:\.(\d+))?")

# Fallback: any _MAJOR.MINOR.PATCH_ token in a WINDOWS package name
_FALLBACK_VER = re.compile(r"(?i)(?:^|_)(\d{2})\.(\d+)(?:\.(\d+))?(?:_|$)")


def parse_kd_version_from_filename(filename: str) -> Optional[Dict[str, Any]]:
    """
    Extract Knowledge Discovery version parts from a package file name.

    Examples:
      Content_26.3.1_WINDOWS_X86_64.zip  → major=26, minor=3, patch=1, major_minor="26.3"
      find-26.2.0.zip                    → major=26, minor=2, patch=0, major_minor="26.2"
      nifi-2.10.0-bin.zip                → None (not a KD product version)
    """
    name = Path(filename).name
    if not name.lower().endswith(".zip"):
        return None
    if _NON_KD_VERSION_NAMES.search(name):
        return None

    m = _KD_UNDERSCORE_VER.search(name)
    if not m:
        m = _GENERIC_UNDERSCORE_VER.search(name)
    if not m:
        m = _FIND_DASH_VER.search(name)
    if not m:
        # Only accept fallback when name looks like an OpenText Windows package
        if re.search(r"(?i)windows|win64|x86_64", name) and not re.search(r"(?i)linux", name):
            m = _FALLBACK_VER.search(name)
    if not m:
        return None

    major = int(m.group(1))
    minor = int(m.group(2))
    patch = int(m.group(3)) if m.lastindex and m.lastindex >= 3 and m.group(3) is not None else None
    # KD product versions are typically 12.x–30.x range; reject absurd values
    if major < 10 or major > 99:
        return None
    return {
        "Major": major,
        "Minor": minor,
        "Patch": patch,
        "MajorMinor": f"{major}.{minor}",
        "Full": f"{major}.{minor}.{patch}" if patch is not None else f"{major}.{minor}",
        "FileName": name,
    }


def suggest_base_path(major_minor: str, root: str = r"C:\KnowledgeDiscovery") -> str:
    """Build BasePath like C:\\KnowledgeDiscovery\\26.3 from major.minor."""
    root = str(root).rstrip("\\/")
    return f"{root}\\{major_minor}"


def analyze_kd_zip_versions(zip_directory: str | Path) -> Dict[str, Any]:
    """
    Scan ZipPath for KD package versions and detect consensus / mismatches.

    Returns:
      Success, MajorMinor (consensus), SuggestedBasePath, Packages (list),
      Mismatches (list of {FileName, MajorMinor}), Warnings (list of str),
      Unversioned (KD-looking names without a parseable version).
    """
    zip_dir = Path(zip_directory)
    empty: Dict[str, Any] = {
        "Success": False,
        "MajorMinor": None,
        "SuggestedBasePath": None,
        "Packages": [],
        "Mismatches": [],
        "Unversioned": [],
        "Warnings": [],
        "Detail": f"ZipPath not found or not a directory: {zip_dir}",
    }
    if not zip_dir.is_dir():
        return empty

    packages: List[Dict[str, Any]] = []
    unversioned: List[str] = []
    try:
        zips = [p for p in zip_dir.iterdir() if p.is_file() and p.suffix.lower() == ".zip"]
    except Exception as e:
        empty["Detail"] = f"Cannot list ZipPath: {e}"
        return empty

    for p in zips:
        if _NON_KD_VERSION_NAMES.search(p.name):
            continue
        # Skip obvious non-KD archives
        if re.search(r"(?i)nssm|jdk|jre|temurin|vcredist|vc_redist", p.name):
            continue
        parsed = parse_kd_version_from_filename(p.name)
        if parsed:
            packages.append(parsed)
        elif re.search(
            r"(?i)content|community|category|agentstore|licenseserver|view|qms|"
            r"answerserver|statsserver|find|windows",
            p.name,
        ):
            unversioned.append(p.name)

    if not packages:
        return {
            "Success": False,
            "MajorMinor": None,
            "SuggestedBasePath": None,
            "Packages": [],
            "Mismatches": [],
            "Unversioned": unversioned,
            "Warnings": (
                [
                    "No Knowledge Discovery version could be parsed from ZIP names in ZipPath. "
                    "Expected names like Content_26.3.1_WINDOWS_X86_64.zip."
                ]
                if zips
                else ["No .zip files found in ZipPath."]
            ),
            "Detail": "No parseable KD package versions",
        }

    counts = Counter(p["MajorMinor"] for p in packages)
    consensus, _ = counts.most_common(1)[0]
    mismatches = [
        {"FileName": p["FileName"], "MajorMinor": p["MajorMinor"], "Full": p["Full"]}
        for p in packages
        if p["MajorMinor"] != consensus
    ]

    warnings: List[str] = []
    if mismatches:
        details = ", ".join(f"{m['FileName']} ({m['MajorMinor']})" for m in mismatches)
        warnings.append(
            f"ZIP version mismatch: consensus is {consensus}, but these packages differ: {details}. "
            f"BasePath should use major.minor from the package set (e.g. C:\\KnowledgeDiscovery\\{consensus})."
        )
    if unversioned:
        warnings.append(
            "Some KD-looking ZIP names have no parseable major.minor version: "
            + ", ".join(unversioned[:8])
            + ("…" if len(unversioned) > 8 else "")
        )
    if len(counts) > 1:
        summary = ", ".join(f"{v}×{c}" for v, c in counts.most_common())
        warnings.append(f"Multiple KD versions present in ZipPath: {summary}. Prefer a single major.minor set.")

    return {
        "Success": True,
        "MajorMinor": consensus,
        "SuggestedBasePath": suggest_base_path(consensus),
        "Packages": packages,
        "Mismatches": mismatches,
        "Unversioned": unversioned,
        "Warnings": warnings,
        "Detail": f"Consensus KD version {consensus} from {len(packages)} package(s)",
        "Counts": dict(counts),
    }


def apply_versioned_base_path(
    config: Dict[str, Any],
    analysis: Optional[Dict[str, Any]] = None,
    *,
    only_if_default: bool = True,
) -> Dict[str, Any]:
    """
    Update config BasePath (and IndexPath only when already present) to
    <root>\\{major.minor} when analysis found a consensus version, where
    <root> is whichever drive/prefix the user's current BasePath already
    used (e.g. D:\\KnowledgeDiscovery) — not always C:\\KnowledgeDiscovery.
    This preserves a deliberately-chosen install drive instead of silently
    moving it back to C:\\.

    IndexPath is left unset when the config does not already contain one,
    so the default relative ./index layout from the OpenText templates is
    preserved.

    If only_if_default is True, only rewrite paths that still look like the
    stock template (…\\KnowledgeDiscovery\\26.2 or missing version leaf),
    on any drive.
    """
    result = {
        "Changed": False,
        "BasePath": config.get("BasePath"),
        "IndexPath": config.get("IndexPath"),
        "MajorMinor": None,
        "Detail": "",
    }
    if analysis is None:
        zp = config.get("ZipPath") or ""
        analysis = analyze_kd_zip_versions(zp)
    if not analysis.get("Success") or not analysis.get("MajorMinor"):
        result["Detail"] = analysis.get("Detail") or "No consensus version"
        return result

    mm = analysis["MajorMinor"]
    result["MajorMinor"] = mm
    current = str(config.get("BasePath") or "")

    def _is_default_kd_path(path: str) -> bool:
        if not path:
            return True
        norm = path.replace("/", "\\").rstrip("\\")
        # Stock template or any KnowledgeDiscovery\\<digits>.<digits>, on any drive
        if re.search(r"(?i)KnowledgeDiscovery\\26\.2$", norm):
            return True
        if re.search(r"(?i)KnowledgeDiscovery\\\d+\.\d+$", norm):
            return True
        return False

    def _existing_kd_root(path: str) -> Optional[str]:
        """
        If path already follows the '<root>\\KnowledgeDiscovery\\<ver>' shape
        (on whatever drive), return '<root>\\KnowledgeDiscovery' so we can
        keep bumping just the version leaf on that same drive/prefix instead
        of falling back to the hardcoded C:\\KnowledgeDiscovery default.
        """
        if not path:
            return None
        norm = path.replace("/", "\\").rstrip("\\")
        m = re.match(r"(?i)^(.*\\KnowledgeDiscovery)\\\d+\.\d+$", norm)
        return m.group(1) if m else None

    if only_if_default and current and not _is_default_kd_path(current):
        result["Detail"] = f"BasePath left unchanged (custom): {current}"
        return result

    # Prefer the drive/root the user's current BasePath already used; only
    # fall back to the hardcoded C:\\KnowledgeDiscovery default when there's
    # no existing versioned path to infer a drive from (e.g. BasePath empty).
    existing_root = _existing_kd_root(current)
    suggested = (
        suggest_base_path(mm, root=existing_root)
        if existing_root
        else (analysis.get("SuggestedBasePath") or suggest_base_path(mm))
    )

    if current.replace("/", "\\").rstrip("\\").lower() == suggested.replace("/", "\\").rstrip("\\").lower():
        result["Detail"] = f"BasePath already matches package version {mm}"
        return result

    config["BasePath"] = suggested
    # Only rewrite IndexPath when the user already had one configured.
    # When IndexPath is absent we leave it unset so the toolkit keeps the
    # default relative ./index folders from the OpenText templates.
    old_index = str(config.get("IndexPath") or "").strip()
    if old_index and (
        _is_default_kd_path(str(Path(old_index).parent))
        or (current and old_index.replace("/", "\\").lower().startswith(current.replace("/", "\\").rstrip("\\").lower()))
    ):
        config["IndexPath"] = suggested + "\\Indexes"

    result["Changed"] = True
    result["BasePath"] = config["BasePath"]
    result["IndexPath"] = config.get("IndexPath")
    result["Detail"] = f"BasePath set to {suggested} from ZIP version {mm}"
    return result


def test_kd_windows_executable(exe_path: str | Path) -> bool:
    """PE header check (MZ)."""
    path = Path(exe_path)
    if not path.is_file():
        return False
    try:
        with open(path, "rb") as f:
            header = f.read(2)
        return header == b"MZ"
    except Exception:
        return False


def find_kd_component_zip(zip_directory: str | Path, component: str) -> Dict[str, Any]:
    """
    Find the best Windows ZIP for a component.
    Agentstore-like components (Agentstore, QMSAgentStore, AnswerBankAgentStore,
    ConversationAgentStore) re-use the Content package.
    NiFi is special-cased: looks for Apache NiFi bin ZIPs (*nifi*-bin*.zip).
    """
    zip_dir = Path(zip_directory)

    # --- Apache NiFi (optional component) ---
    if component.lower() == "nifi":
        # Prefer official bin distributions: nifi-2.10.0-bin.zip etc.
        # Exclude NiFiIngest_*.zip — that package only supplies connector NARs,
        # not the Apache NiFi runtime.
        candidates = list(zip_dir.glob("*nifi*-bin*.zip")) + list(zip_dir.glob("*nifi*.zip"))
        candidates = [c for c in candidates if "nifiingest" not in c.name.lower()]
        # de-dupe while preserving order
        seen = set()
        unique = []
        for c in candidates:
            if c.name not in seen:
                seen.add(c.name)
                unique.append(c)
        candidates = unique
        if not candidates:
            return {
                "Success": False,
                "Reason": f"No NiFi binary ZIP found in {zip_dir} (expected *nifi*-bin*.zip or *nifi*.zip)",
                "Zip": None,
            }
        # Prefer higher version-looking names and those containing -bin
        def nifi_score(p: Path) -> tuple:
            name = p.name.lower()
            s = 0
            if "-bin" in name:
                s -= 1000
            # crude version preference
            m = re.search(r"nifi-(\d+)\.(\d+)\.(\d+)", name)
            if m:
                s -= int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
            return (s, -p.stat().st_size)
        ranked = sorted(candidates, key=nifi_score)
        return {"Success": True, "Reason": "OK (NiFi binary)", "Zip": ranked[0]}

    # --- OpenText Find (optional Java component; sample: find-26.2.0.zip / find-xx.x.x.zip) ---
    if component.lower() == "find":
        candidates = list(zip_dir.glob("*find*.zip"))
        # de-dupe
        seen = set()
        unique = []
        for c in candidates:
            if c.name not in seen:
                seen.add(c.name)
                unique.append(c)
        candidates = unique
        if not candidates:
            return {
                "Success": False,
                "Reason": f"No Find ZIP found in {zip_dir} (expected *find*.zip e.g. find-26.2.0.zip)",
                "Zip": None,
            }
        def find_score(p: Path) -> tuple:
            name = p.name.lower()
            s = 0
            m = re.search(r"find-(\d+)\.(\d+)\.(\d+)", name)
            if m:
                s -= int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
            if re.search(r"(?i)linux", name):
                s += 5000
            return (s, -p.stat().st_size)
        ranked = sorted(candidates, key=find_score)
        return {"Success": True, "Reason": "OK (Find binary)", "Zip": ranked[0]}

    search_component = (
        "Content"
        if component
        in (
            "Agentstore",
            "QMSAgentStore",
            "AnswerBankAgentStore",
            "ConversationAgentStore",
        )
        else component
    )

    candidates = list(zip_dir.glob(f"*{search_component}*.zip"))
    if not candidates:
        return {
            "Success": False,
            "Reason": f"No file matching *{search_component}*.zip found in {zip_dir} (needed for {component})",
            "Zip": None,
        }

    # Prefer explicitly Windows-marked packages
    windows_marked = [
        c
        for c in candidates
        if re.search(r"(?i)windows|win64|x64", c.name) and not re.search(r"(?i)linux", c.name)
    ]
    if windows_marked:
        return {"Success": True, "Reason": "OK", "Zip": windows_marked[0]}

    linux_marked = [c for c in candidates if re.search(r"(?i)linux", c.name)]
    safe = [c for c in candidates if c not in linux_marked]
    if safe:
        return {"Success": True, "Reason": "OK (platform unmarked)", "Zip": safe[0]}

    return {
        "Success": False,
        "Reason": f"Only Linux packages found for {search_component}",
        "Zip": None,
    }


def find_kd_component_executable(component_path: str | Path, component: str) -> Dict[str, Any]:
    """
    Locate the main service binary, applying a strong exclusion list and ranking.
    NiFi is special: returns the nifi.cmd launcher (not a PE executable).
    """
    root = Path(component_path)

    # --- Apache NiFi ---
    if component.lower() == "nifi":
        # Prefer bin/nifi.cmd (standard layout after stripping top-level folder)
        candidates = list(root.rglob("nifi.cmd"))
        if not candidates:
            return {
                "Success": False,
                "Reason": "nifi.cmd not found under component path (is the ZIP extracted correctly?)",
                "Executable": None,
                "AllCandidates": [],
            }
        # Prefer the one closest to root / under a bin/ folder
        def nifi_cmd_score(p: Path) -> tuple:
            parts = [x.lower() for x in p.parts]
            s = 0
            if "bin" in parts:
                s -= 100
            try:
                s += len(p.relative_to(root).parts) * 10
            except ValueError:
                s += 50
            return (s,)
        ranked = sorted(candidates, key=nifi_cmd_score)
        return {
            "Success": True,
            "Reason": "OK (NiFi launcher)",
            "Executable": ranked[0],
            "AllCandidates": ranked,
        }

    # --- OpenText Find (Java executable .war) ---
    if component.lower() == "find":
        candidates = list(root.rglob("find.war")) + list(root.rglob("*.war"))
        # Prefer exact find.war
        exact = [c for c in candidates if c.name.lower() == "find.war"]
        if exact:
            candidates = exact
        if not candidates:
            return {
                "Success": False,
                "Reason": "find.war not found under component path (is the ZIP extracted correctly?)",
                "Executable": None,
                "AllCandidates": [],
            }
        def find_war_score(p: Path) -> tuple:
            s = 0
            if p.name.lower() == "find.war":
                s -= 1000
            try:
                s += len(p.relative_to(root).parts) * 10
            except ValueError:
                s += 50
            return (s,)
        ranked = sorted(candidates, key=find_war_score)
        return {
            "Success": True,
            "Reason": "OK (Find Java war)",
            "Executable": ranked[0],
            "AllCandidates": ranked,
        }

    exclude_re = re.compile(
        r"(?i)uninstall|setup|remove|delete|java|jre|jdk|"
        r"vc_?redist|vcruntime|msvcp|msvcr|dotnet|ndp\d|"
        r"prereq|redist|crashpad|directx|dxsetup|langfiles|jpn|cha|"
        r"tools|nistrdstool|autpassword|makeda|kvoop|extract|filter|lua"
    )
    path_exclude_re = re.compile(r"(?i)[\\/](langfiles|tools|jpn|cha|passwordlib|filters)[\\/]")

    candidates: List[Path] = []
    for exe in root.rglob("*.exe"):
        if exclude_re.search(exe.name):
            continue
        if path_exclude_re.search(str(exe)):
            continue
        candidates.append(exe)

    if not candidates:
        return {
            "Success": False,
            "Reason": f"No suitable .exe found for {component} after exclusions",
            "Executable": None,
            "AllCandidates": [],
        }

    comp_lower = component.lower()

    def score(p: Path) -> tuple:
        name = p.stem.lower()
        full = str(p).lower()
        s = 0

        # Extremely strong bonus for the real main binary
        if (
            name == comp_lower
            or name == f"{comp_lower}server"
            or name == f"idol{comp_lower}"
            or name in ("cfs", "content", "community", "category")
            # Agentstore-like components run agentstore.exe (copied from content.exe)
            or (
                comp_lower
                in (
                    "agentstore",
                    "qmsagentstore",
                    "answerbankagentstore",
                    "conversationagentstore",
                )
                and name == "agentstore"
            )
            or (
                comp_lower == "qms"
                and name in ("qms", "querymanipulation", "querymanipulationserver")
            )
            or (
                comp_lower == "answerserver"
                and name in ("answerserver", "answer")
            )
        ):
            s -= 15000
        elif comp_lower in name and not path_exclude_re.search(full):
            s -= 5000

        # Heavy penalties
        if path_exclude_re.search(full):
            s += 12000
        if re.search(r"(?i)^(makeda|autpassword|nistrdstool|passwordlib|kvoop|extract|filter|lua)", name):
            s += 10000
        if re.search(r"(?i)linux|linux_x86_64", full):
            s += 6000

        # Depth penalty
        try:
            depth = len(p.relative_to(root).parts)
            if depth > 2:
                s += 3000
        except ValueError:
            s += 3000

        # Prefer larger files as tie-breaker (main binaries tend to be bigger)
        return (s, -p.stat().st_size)

    ranked = sorted(candidates, key=score)
    best = ranked[0]

    if re.search(r"(?i)linux|linux_x86_64", str(best)):
        return {
            "Success": False,
            "Reason": "Only Linux binary found",
            "Executable": None,
            "AllCandidates": ranked,
        }

    if not test_kd_windows_executable(best):
        return {
            "Success": False,
            "Reason": "Not a valid Windows PE executable",
            "Executable": None,
            "AllCandidates": ranked,
        }

    return {
        "Success": True,
        "Reason": "OK",
        "Executable": best,
        "AllCandidates": ranked,
    }

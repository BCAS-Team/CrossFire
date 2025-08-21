from __future__ import annotations
import argparse
import concurrent.futures as _fut
import hashlib
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

__version__ = "2.1.0"

# ----------------------------
# OS & Architecture Detection
# ----------------------------
OS_NAME = platform.system()
ARCH = platform.architecture()[0]

try:
    import distro  # type: ignore
    DISTRO_NAME = distro.id() or "linux"
    DISTRO_VERSION = distro.version() or ""
except Exception:
    if OS_NAME == "Darwin":
        DISTRO_NAME = "macOS"
        DISTRO_VERSION = platform.mac_ver()[0]
    elif OS_NAME == "Windows":
        DISTRO_NAME = "Windows"
        DISTRO_VERSION = platform.version()
    else:
        DISTRO_NAME = OS_NAME.lower()
        DISTRO_VERSION = ""

# ----------------------------
# Logging
# ----------------------------
class Colors:
    INFO = "\033[94m"
    SUCCESS = "\033[92m"
    WARNING = "\033[93m"
    ERROR = "\033[91m"
    MUTED = "\033[90m"
    RESET = "\033[0m"

@dataclass
class LogCfg:
    quiet: bool = False
    verbose: bool = False
    json_mode: bool = False

LOG = LogCfg()
_lock = threading.Lock()

def _out_json(level: str, msg: str, **extra):
    payload = {"level": level, "msg": msg, "ts": time.time(), **extra}
    with _lock:
        print(json.dumps(payload, ensure_ascii=False))

def _fmt(msg: str, typ: str) -> str:
    # Disable colors on Windows if not supported
    if OS_NAME == "Windows" and not os.environ.get("FORCE_COLOR"):
        return f"[CrossFire] {msg}"
    color = getattr(Colors, typ, Colors.INFO)
    return f"{color}[CrossFire]{Colors.RESET} {msg}"

def cprint(msg: str, typ: str = "INFO", **extra) -> None:
    if LOG.quiet and typ != "ERROR":
        return
    if LOG.json_mode:
        _out_json(typ.lower(), msg, **extra)
        return
    with _lock:
        print(_fmt(msg, typ))

# ----------------------------
# Secure subprocess helpers
# ----------------------------
@dataclass
class RunResult:
    ok: bool
    code: int
    out: str
    err: str

def _split_cmd(cmd: str | List[str]) -> List[str]:
    if isinstance(cmd, list):
        return cmd
    import shlex
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()

def run_command(
    cmd: str | List[str],
    *,
    timeout: int = 600,
    retries: int = 1,
    backoff: float = 1.5,
    env: Optional[Dict[str, str]] = None,
    shell: bool = False,
) -> RunResult:
    last = RunResult(False, -1, "", "")
    for attempt in range(retries + 1):
        try:
            proc_env = os.environ.copy()
            if env:
                proc_env.update(env)

            if LOG.verbose:
                cmd_str = ' '.join(cmd if isinstance(cmd, list) else [cmd])
                cprint(f"Running: {cmd_str}", "MUTED")

            p = subprocess.run(
                cmd if shell else _split_cmd(cmd),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=proc_env,
                shell=shell,
                creationflags=subprocess.CREATE_NO_WINDOW if OS_NAME == "Windows" else 0,
            )
            last = RunResult(p.returncode == 0, p.returncode, p.stdout, p.stderr)
            if last.ok or attempt == retries:
                return last

        except subprocess.TimeoutExpired as e:
            last = RunResult(False, -9, (e.stdout or "") if isinstance(e.stdout, str) else "", "Command timed out")
        except Exception as e:
            last = RunResult(False, -1, "", str(e))

        if attempt < retries:
            if LOG.verbose:
                cprint(f"Command failed (rc={last.code}). Retrying in {backoff ** (attempt + 1):.1f}s...", "WARNING")
            time.sleep(backoff ** (attempt + 1))
    return last

# ----------------------------
# PATH management + Launcher
# ----------------------------
def _get_shell_rc_file() -> str:
    shell = os.environ.get("SHELL", "")
    home = os.path.expanduser("~")
    if shell.endswith("zsh") or os.path.exists(os.path.join(home, ".zshrc")):
        return os.path.join(home, ".zshrc")
    elif shell.endswith("bash") or os.path.exists(os.path.join(home, ".bashrc")):
        return os.path.join(home, ".bashrc")
    elif os.path.exists(os.path.join(home, ".profile")):
        return os.path.join(home, ".profile")
    else:
        return os.path.join(home, ".profile")

def _append_to_shell(shell_file: str, line: str) -> bool:
    try:
        content = ""
        if os.path.exists(shell_file):
            with open(shell_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        marker = "# CrossFire CLI"
        if marker in content and line.strip() in content:
            return True
        os.makedirs(os.path.dirname(shell_file), exist_ok=True)
        with open(shell_file, "a", encoding="utf-8") as f:
            f.write(f"\n{marker}\n{line}\n")
        return True
    except Exception as e:
        if LOG.verbose:
            cprint(f"Failed to edit {shell_file}: {e}", "WARNING")
        return False

def add_to_path_safely() -> None:
    script_dir = os.path.dirname(os.path.realpath(__file__))
    if not script_dir:
        return
    parts = []
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if p and not p.lower().endswith("crossfire.py"):
            parts.append(p)
    if script_dir not in parts:
        parts.insert(0, script_dir)
    os.environ["PATH"] = os.pathsep.join(parts)

    if OS_NAME == "Windows":
        cprint("PATH updated for current session.", "SUCCESS")
        cprint("Note: Run 'crossfire' from this directory or add to PATH manually.", "INFO")
        return

    try:
        if OS_NAME in ("Darwin", "Linux"):
            rc_file = _get_shell_rc_file()
            export_line = f'export PATH="{script_dir}:$PATH"'
            if _append_to_shell(rc_file, export_line):
                cprint(f"PATH updated in {os.path.basename(rc_file)}.", "SUCCESS")
            else:
                cprint("PATH updated for current session only.", "WARNING")
        else:
            cprint("PATH updated for current session only.", "INFO")
    except Exception as e:
        cprint("PATH updated for current session only.", "INFO")
        if LOG.verbose:
            cprint(f"PATH persistence error: {e}", "MUTED")

def install_launcher() -> Optional[str]:
    target_name = "crossfire"
    script_path = os.path.abspath(__file__)
    installed_path: Optional[str] = None
    try:
        if OS_NAME in ("Linux", "Darwin"):
            candidates = [os.path.expanduser("~/.local/bin"), "/usr/local/bin"]
            for bin_dir in candidates:
                try:
                    os.makedirs(bin_dir, exist_ok=True)
                    launcher_path = os.path.join(bin_dir, target_name)
                    launcher_content = f"""#!/bin/bash
# CrossFire launcher
exec "{sys.executable}" "{script_path}" "$@"
"""
                    with open(launcher_path, "w", encoding="utf-8") as f:
                        f.write(launcher_content)
                    current_mode = os.stat(launcher_path).st_mode
                    os.chmod(launcher_path, current_mode | stat.S_IEXEC)
                    installed_path = launcher_path
                    cprint(f"Launcher installed: {launcher_path}", "SUCCESS")
                    break
                except Exception as e:
                    if LOG.verbose:
                        cprint(f"Install attempt in {bin_dir} failed: {e}", "WARNING")
                    continue
        elif OS_NAME == "Windows":
            python_exe = sys.executable or "python"
            script_dir = os.path.dirname(script_path)
            bat_path = os.path.join(script_dir, f"{target_name}.bat")
            try:
                batch_content = f"""@echo off
REM CrossFire launcher
"{python_exe}" "{script_path}" %*
"""
                with open(bat_path, "w", encoding="utf-8") as f:
                    f.write(batch_content)
                installed_path = bat_path
                cprint(f"Launcher created: {target_name}.bat", "SUCCESS")
                cprint("You can now run 'crossfire' from this directory.", "INFO")
            except Exception as e:
                if LOG.verbose:
                    cprint(f"Launcher creation failed: {e}", "WARNING")
        else:
            cprint(f"Unsupported OS: {OS_NAME}", "WARNING")
    except Exception as e:
        cprint(f"Launcher installation failed: {e}", "ERROR")
    return installed_path

# ----------------------------
# Package Managers (status/update)
# ----------------------------
def _get_python_commands() -> List[List[str]]:
    candidates = []
    if sys.executable:
        candidates.append([sys.executable, "-m", "pip"])
    for exe_name in ("python3", "python", "py"):
        exe_path = shutil.which(exe_name)
        if exe_path and exe_path != sys.executable:
            candidates.append([exe_path, "-m", "pip"])
    return candidates

PACKAGE_MANAGERS: Dict[str, Dict[str, any]] = {
    # Status/update layer (not the same as install handlers below)
    "Python": {
        "manager": _get_python_commands(),
        "update_cmd": None,
        "check_cmd": None,
    },
    "NodeJS": {
        "manager": [["npm"]],
        "update_cmd": ["npm", "install", "-g", "npm@latest"],
        "check_cmd": ["npm", "--version"],
    },
    "Homebrew": {
        "manager": [["brew"]],
        "update_cmd": ["brew", "update"],
        "check_cmd": ["brew", "--version"],
    },
    "APT": {
        "manager": [["apt"]],
        "update_cmd": ["sudo", "apt", "update", "&&", "sudo", "apt", "upgrade", "-y"],
        "check_cmd": ["apt", "--version"],
    },
    "DNF": {
        "manager": [["dnf"]],
        "update_cmd": ["sudo", "dnf", "makecache", "--refresh"],
        "check_cmd": ["dnf", "--version"],
    },
    "YUM": {
        "manager": [["yum"]],
        "update_cmd": ["sudo", "yum", "makecache"],
        "check_cmd": ["yum", "--version"],
    },
    "Pacman": {
        "manager": [["pacman"]],
        "update_cmd": ["sudo", "pacman", "-Sy"],
        "check_cmd": ["pacman", "--version"],
    },
    "Zypper": {
        "manager": [["zypper"]],
        "update_cmd": ["sudo", "zypper", "refresh"],
        "check_cmd": ["zypper", "--version"],
    },
    "APK": {
        "manager": [["apk"]],
        "update_cmd": ["sudo", "apk", "update"],
        "check_cmd": ["apk", "--version"],
    },
    "Chocolatey": {
        "manager": [["choco"]],
        "update_cmd": ["choco", "upgrade", "chocolatey", "-y"],
        "check_cmd": ["choco", "version"],
    },
    "Winget": {
        "manager": [["winget"]],
        "update_cmd": ["winget", "source", "update"],
        "check_cmd": ["winget", "--version"],
    },
    "Snap": {
        "manager": [["snap"]],
        "update_cmd": ["sudo", "snap", "refresh"],
        "check_cmd": ["snap", "--version"],
    },
    "Flatpak": {
        "manager": [["flatpak"]],
        "update_cmd": ["flatpak", "update", "-y"],
        "check_cmd": ["flatpak", "--version"],
    },
}

_installed_cache: Dict[str, bool] = {}

def is_installed(manager_name: str) -> bool:
    if manager_name in _installed_cache:
        return _installed_cache[manager_name]
    meta = PACKAGE_MANAGERS.get(manager_name, {})
    manager_cmds = meta.get("manager", [])
    if manager_name == "Python":
        manager_cmds = _get_python_commands()
    installed = False
    for cmd_list in manager_cmds:
        if cmd_list and shutil.which(cmd_list[0]):
            check_cmd = meta.get("check_cmd")
            if check_cmd:
                result = run_command(check_cmd, timeout=10)
                if result.ok:
                    installed = True
                    break
            else:
                installed = True
                break
    _installed_cache[manager_name] = installed
    return installed

def list_managers_status() -> Dict[str, str]:
    result = {}
    names = list(PACKAGE_MANAGERS.keys())
    with _fut.ThreadPoolExecutor(max_workers=min(8, len(names))) as executor:
        future_to_manager = {executor.submit(is_installed, name): name for name in names}
        for future in _fut.as_completed(future_to_manager):
            manager = future_to_manager[future]
            try:
                is_avail = future.result()
                result[manager] = "Installed" if is_avail else "Not Installed"
            except Exception as e:
                result[manager] = f"Error: {str(e)}"
                if LOG.verbose:
                    cprint(f"Error checking {manager}: {e}", "WARNING")
    return result

def _update_manager(name: str) -> Tuple[str, bool, str]:
    try:
        meta = PACKAGE_MANAGERS.get(name, {})
        if not meta:
            return (name, False, "Unknown manager")
        if not is_installed(name):
            return (name, False, "Manager not installed")
        if name == "Python":
            python_cmds = _get_python_commands()
            for cmd_template in python_cmds:
                if shutil.which(cmd_template[0]):
                    update_cmd = cmd_template[:-1] + ["install", "--upgrade", "pip"]
                    result = run_command(update_cmd, timeout=300)
                    if result.ok:
                        return (name, True, "Updated successfully")
                    else:
                        if LOG.verbose:
                            cprint(f"Pip update attempt failed: {result.err}", "WARNING")
            return (name, False, "No working Python found for pip update")

        update_cmd = meta.get("update_cmd")
        if not update_cmd:
            return (name, False, "No update command defined")

        # Special case: some update_cmds embed '&&'; run via shell=True
        use_shell = any(tok in update_cmd for tok in ("&&", ";"))
        result = run_command(update_cmd, timeout=900, shell=use_shell)
        status_msg = result.out.strip() if result.ok else result.err.strip()
        return (name, result.ok, status_msg or ("Updated successfully" if result.ok else "Update failed"))
    except Exception as e:
        return (name, False, f"Exception: {str(e)}")

def _update_all_managers() -> Dict[str, Dict[str, str]]:
    results = {}
    installed_managers = [name for name, status in list_managers_status().items() if status == "Installed"]
    if not installed_managers:
        cprint("No package managers found to update.", "WARNING")
        return results

    cprint(f"Updating {len(installed_managers)} package managers...", "INFO")
    max_workers = min(4, len(installed_managers))
    with _fut.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_manager = {executor.submit(_update_manager, name): name for name in installed_managers}
        for future in _fut.as_completed(future_to_manager):
            manager_name = future_to_manager[future]
            try:
                name, success, message = future.result()
                results[name] = {"ok": str(success).lower(), "msg": message}
                status = "SUCCESS" if success else "ERROR"
                cprint(f"{name}: {'✓' if success else '✗'} {message}", status)
            except Exception as e:
                results[manager_name] = {"ok": "false", "msg": f"Exception: {str(e)}"}
                cprint(f"{manager_name}: ✗ Exception: {e}", "ERROR")
    return results

# ----------------------------
# Self-update functionality
# ----------------------------
ALLOWED_UPDATE_HOSTS = {"github.com", "raw.githubusercontent.com", "githubusercontent.com"}
MAX_UPDATE_SIZE = 10 * 1024 * 1024  # 10MB
DEFAULT_UPDATE_URL = "https://raw.githubusercontent.com/BCAS-Team/CrossFire/main/CrossFireL/crossfire.py"

def _validate_update_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if not any(allowed in host for allowed in ALLOWED_UPDATE_HOSTS):
        raise ValueError(f"Update host '{host}' not in allowlist: {sorted(ALLOWED_UPDATE_HOSTS)}")

def download_update(url: str, timeout: int = 60) -> bytes:
    _validate_update_url(url)
    try:
        req = urllib.request.Request(url)
        req.add_header('User-Agent', f'CrossFire/{__version__}')
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content_length = response.headers.get('Content-Length')
            if content_length and int(content_length) > MAX_UPDATE_SIZE:
                raise ValueError(f"Update file too large: {content_length} bytes")
            data = b""
            chunk_size = 8192
            while len(data) < MAX_UPDATE_SIZE:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                data += chunk
            if len(data) >= MAX_UPDATE_SIZE:
                raise ValueError("Update file exceeds size limit")
            return data
    except Exception as e:
        raise Exception(f"Download failed: {str(e)}")

def cross_update(url: str = DEFAULT_UPDATE_URL, *, verify_sha256: Optional[str] = None) -> bool:
    try:
        cprint(f"Downloading update from: {url}", "INFO")
        update_data = download_update(url)
        if verify_sha256:
            actual_hash = hashlib.sha256(update_data).hexdigest().lower()
            expected_hash = verify_sha256.lower()
            if actual_hash != expected_hash:
                cprint(f"SHA256 mismatch: expected {expected_hash}, got {actual_hash}", "ERROR")
                return False
            cprint("SHA256 verification passed", "SUCCESS")

        current_file = os.path.abspath(__file__)
        backup_file = current_file + ".backup"
        try:
            shutil.copy2(current_file, backup_file)
        except Exception as e:
            cprint(f"Failed to create backup: {e}", "WARNING")

        try:
            with tempfile.NamedTemporaryFile(mode='wb', delete=False, dir=os.path.dirname(current_file)) as tmp_file:
                tmp_file.write(update_data)
                temp_path = tmp_file.name
            if OS_NAME == "Windows":
                try:
                    os.replace(temp_path, current_file)
                except OSError:
                    os.remove(current_file)
                    os.rename(temp_path, current_file)
            else:
                os.replace(temp_path, current_file)
            if OS_NAME != "Windows":
                st = os.stat(current_file)
                os.chmod(current_file, st.st_mode | stat.S_IEXEC)
            cprint(f"Update successful! Backup saved as: {os.path.basename(backup_file)}", "SUCCESS")
            return True
        except Exception as e:
            if os.path.exists(backup_file):
                try:
                    shutil.copy2(backup_file, current_file)
                    cprint("Update failed, restored from backup", "WARNING")
                except Exception:
                    pass
            cprint(f"Update failed: {e}", "ERROR")
            return False
    except Exception as e:
        cprint(f"Update error: {e}", "ERROR")
        return False

# ----------------------------
# Package INSTALLATION (new)
# ----------------------------
# Install command builders per manager (subset from index.py, install only)
def _pip_install(pkg: str) -> List[str]:
    return [sys.executable, "-m", "pip", "install", pkg]

def _npm_install(pkg: str) -> List[str]:
    return ["npm", "install", "-g", pkg]

def _apt_install(pkg: str) -> List[str]:
    return ["sudo", "apt", "install", "-y", pkg]

def _dnf_install(pkg: str) -> List[str]:
    return ["sudo", "dnf", "install", "-y", pkg]

def _yum_install(pkg: str) -> List[str]:
    return ["sudo", "yum", "install", "-y", pkg]

def _pacman_install(pkg: str) -> List[str]:
    return ["sudo", "pacman", "-S", "--noconfirm", pkg]

def _zypper_install(pkg: str) -> List[str]:
    return ["sudo", "zypper", "--non-interactive", "install", pkg]

def _apk_install(pkg: str) -> List[str]:
    return ["sudo", "apk", "add", pkg]

def _brew_install(pkg: str) -> List[str]:
    return ["brew", "install", pkg]

def _choco_install(pkg: str) -> List[str]:
    return ["choco", "install", "-y", pkg]

def _winget_install(pkg: str) -> List[str]:
    # Rely on silent + agreements to keep UX smooth
    return ["winget", "install", "--silent", "--accept-package-agreements", "--accept-source-agreements", pkg]

def _snap_install(pkg: str) -> List[str]:
    return ["sudo", "snap", "install", pkg]

def _flatpak_install(pkg: str) -> List[str]:
    # Flatpak usually needs a remote/app-id; we still expose as best-effort
    return ["flatpak", "install", "-y", pkg]

# Exposed install handlers
MANAGER_INSTALL_HANDLERS: Dict[str, callable] = {
    "pip": _pip_install,
    "npm": _npm_install,
    "apt": _apt_install,
    "dnf": _dnf_install,
    "yum": _yum_install,
    "pacman": _pacman_install,
    "zypper": _zypper_install,
    "apk": _apk_install,
    "brew": _brew_install,
    "choco": _choco_install,
    "winget": _winget_install,
    "snap": _snap_install,
    "flatpak": _flatpak_install,
}

def _os_type() -> str:
    s = platform.system().lower()
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "macos"
    if s == "linux":
        return "linux"
    if s in ("freebsd", "openbsd", "netbsd"):
        return "bsd"
    if s.startswith("sunos"):
        return "solaris"
    return "unknown"

def _detect_installed_installers() -> Dict[str, bool]:
    """Which installation managers from MANAGER_INSTALL_HANDLERS are available."""
    available = {}
    for name, fn in MANAGER_INSTALL_HANDLERS.items():
        exe = {
            "pip": sys.executable.split(os.sep)[-1] if sys.executable else "python",
            "brew": "brew",
            "apt": "apt",
            "dnf": "dnf",
            "yum": "yum",
            "pacman": "pacman",
            "zypper": "zypper",
            "apk": "apk",
            "npm": "npm",
            "choco": "choco",
            "winget": "winget",
            "snap": "snap",
            "flatpak": "flatpak",
        }.get(name, name)
        # For pip we check via python -m pip presence by trying to run a version query quickly
        if name == "pip":
            if sys.executable:
                available[name] = True
            else:
                available[name] = shutil.which("python") is not None or shutil.which("python3") is not None
        else:
            available[name] = shutil.which(exe) is not None
    return available

def _looks_like_python_pkg(pkg: str) -> bool:
    # Heuristics: version specifiers, extras, underscores, typical python naming
    special = any(x in pkg for x in ["==", ">=", "<=", "~=", "!=", "[", "]"])
    pyish = pkg.lower().startswith(("py", "django", "flask", "numpy", "pandas", "pytest"))
    has_dash_underscore = "-" in pkg or "_" in pkg
    return special or pyish or has_dash_underscore

def _looks_like_npm_pkg(pkg: str) -> bool:
    # Heuristics: @scope/name, or typical js libs
    return pkg.startswith("@") or pkg.lower() in ("express", "react", "vue", "angular", "typescript", "eslint")

def _system_manager_priority() -> List[str]:
    ot = _os_type()
    if ot == "macos":
        return ["brew", "snap", "flatpak"]
    if ot == "windows":
        return ["winget", "choco"]
    # linux flavors
    for cand in ("apt", "dnf", "yum", "pacman", "zypper", "apk"):
        if shutil.which(cand):
            # prefer native first, then snap/flatpak
            return [cand, "snap", "flatpak"]
    return ["snap", "flatpak"]

def _ordered_install_manager_candidates(pkg: str, installed: Dict[str, bool]) -> List[str]:
    # Respect heuristics first
    prefs: List[str] = []

    if _looks_like_python_pkg(pkg) and installed.get("pip"):
        prefs.append("pip")
    if _looks_like_npm_pkg(pkg) and installed.get("npm"):
        prefs.append("npm")

    # Add system managers based on OS
    for m in _system_manager_priority():
        if installed.get(m):
            prefs.append(m)

    # As a wider fallback, include any other installed handlers not already listed
    for m, ok in installed.items():
        if ok and m not in prefs:
            prefs.append(m)

    # Deduplicate, preserve order
    seen = set()
    ordered = []
    for m in prefs:
        if m not in seen:
            seen.add(m)
            ordered.append(m)
    return ordered

def _manager_human(name: str) -> str:
    # Pretty titles for output
    mapping = {
        "pip": "Python (pip)",
        "npm": "Node.js (npm)",
        "apt": "APT",
        "dnf": "DNF",
        "yum": "YUM",
        "pacman": "Pacman",
        "zypper": "Zypper",
        "apk": "APK",
        "brew": "Homebrew",
        "choco": "Chocolatey",
        "winget": "Winget",
        "snap": "Snap",
        "flatpak": "Flatpak",
    }
    return mapping.get(name, name)

def install_package(pkg: str, preferred_manager: Optional[str] = None) -> Tuple[bool, List[Tuple[str, RunResult]]]:
    """
    Try to install `pkg`. If preferred_manager is set, try that first (if available),
    then fall back to auto-ordered candidates. Returns (success, attempts).
    Each attempt is (manager_name, RunResult).
    """
    cprint(f"Preparing to install: {pkg}", "INFO")

    installed = _detect_installed_installers()
    if not any(installed.values()):
        cprint("No supported package managers are available on this system.", "ERROR")
        return (False, [])

    attempts: List[Tuple[str, RunResult]] = []

    # Build candidate order
    candidates = _ordered_install_manager_candidates(pkg, installed)

    # Respect explicit --manager first if valid
    if preferred_manager:
        pm = preferred_manager.lower()
        if pm in MANAGER_INSTALL_HANDLERS and installed.get(pm):
            candidates = [pm] + [m for m in candidates if m != pm]
        else:
            cprint(f"--manager '{preferred_manager}' not available; ignoring preference.", "WARNING")

    # Friendly preview
    cprint("Will try managers in this order:", "MUTED")
    for m in candidates:
        cprint(f"  • {_manager_human(m)}", "MUTED")

    # Attempt installs in order
    for m in candidates:
        cmd_builder = MANAGER_INSTALL_HANDLERS.get(m)
        if not cmd_builder:
            continue
        cmd = cmd_builder(pkg)
        cprint(f"→ Attempting via {_manager_human(m)} …", "INFO")
        res = run_command(cmd, timeout=1800, retries=0)
        attempts.append((m, res))
        if res.ok:
            cprint(f"✓ Installed '{pkg}' via {_manager_human(m)}", "SUCCESS")
            return (True, attempts)
        else:
            # Special case: some managers succeed with informative stdout but non-zero rc is rare; we trust rc
            err_snip = (res.err or res.out).strip()
            if err_snip:
                err_snip = err_snip.splitlines()[-1][:180]
                cprint(f"{_manager_human(m)} failed: {err_snip}", "WARNING")

    cprint(f"✗ Failed to install '{pkg}' with all available managers.", "ERROR")
    cprint("Tip: specify a manager explicitly, e.g.  crossfire --install PACKAGE --manager pip", "MUTED")
    return (False, attempts)

# ----------------------------
# CLI
# ----------------------------
def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CrossFire — Universal Package Manager CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  crossfire                          # Show status
  crossfire --list-managers          # List all supported package managers
  crossfire -um Python               # Update Python/pip
  crossfire -um ALL                  # Update all managers
  crossfire -cu                      # Self-update from default URL
  crossfire -cu <url> --sha256 <h>   # Self-update with verification

  crossfire --install                # AUTO: pick best manager to install 
  crossfire -i curl                  # AUTO: pick best manager to install 'curl'
  crossfire -i express --manager npm # Force npm for 'express'
        """,
    )

    # Output control
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    parser.add_argument("-q", "--quiet", action="store_true", help="Quiet mode (errors only)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    # Informational / update actions
    parser.add_argument("--list-managers", action="store_true", help="List all supported package managers and their status")
    parser.add_argument("-um", "--update-manager", metavar="NAME", help="Update specific manager or 'ALL' for all managers")

    # Self-update
    parser.add_argument("-cu", "--crossupdate", nargs="?", const=DEFAULT_UPDATE_URL, metavar="URL",
                        help="Self-update from URL (default: GitHub)")
    parser.add_argument("--sha256", metavar="HASH", help="Expected SHA256 hash for update verification")

    # NEW: install packages
    parser.add_argument("-i", "--install", metavar="PKG", help="Install a package by name")
    parser.add_argument("--manager", metavar="NAME", help="Preferred manager to use (pip, npm, apt, dnf, yum, pacman, zypper, apk, brew, choco, winget, snap, flatpak)")

    return parser

def show_default_status() -> int:
    cprint(f"CrossFire v{__version__} — {OS_NAME}/{DISTRO_NAME} {DISTRO_VERSION}", "INFO")
    status_info = list_managers_status()

    if LOG.json_mode:
        output = {
            "version": __version__,
            "os": OS_NAME,
            "distro": DISTRO_NAME,
            "distro_version": DISTRO_VERSION,
            "managers": status_info,
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        installed_count = sum(1 for status in status_info.values() if status == "Installed")
        total_count = len(status_info)
        cprint(f"Package Managers: {installed_count}/{total_count} installed", "INFO")
        for manager, status in sorted(status_info.items()):
            if status == "Installed":
                cprint(f"  ✓ {manager}: {status}", "SUCCESS")
            elif status == "Not Installed":
                cprint(f"  ✗ {manager}: {status}", "MUTED")
            else:
                cprint(f"  ? {manager}: {status}", "WARNING")
    return 0

def main(argv: Optional[List[str]] = None) -> int:
    # Initialize PATH and launcher first
    add_to_path_safely()
    install_launcher()

    # Parse arguments
    parser = create_parser()
    args = parser.parse_args(argv)

    # Configure logging
    LOG.quiet = args.quiet
    LOG.verbose = args.verbose
    LOG.json_mode = args.json

    # Self-update
    if args.crossupdate is not None:
        url = args.crossupdate or DEFAULT_UPDATE_URL
        success = cross_update(url, verify_sha256=args.sha256)
        return 0 if success else 1

    # List managers
    if args.list_managers:
        status_info = list_managers_status()
        if LOG.json_mode:
            print(json.dumps(status_info, indent=2, ensure_ascii=False))
        else:
            cprint("Package Manager Status:", "INFO")
            for manager, status in sorted(status_info.items()):
                if status == "Installed":
                    cprint(f"  ✓ {manager}: {status}", "SUCCESS")
                elif status == "Not Installed":
                    cprint(f"  ✗ {manager}: {status}", "MUTED")
                else:
                    cprint(f"  ? {manager}: {status}", "WARNING")
        return 0

    # Update manager(s)
    if args.update_manager:
        target = args.update_manager.upper()
        if target == "ALL":
            results = _update_all_managers()
            if LOG.json_mode:
                print(json.dumps(results, indent=2, ensure_ascii=False))
            any_success = any(r.get("ok") == "true" for r in results.values())
            return 0 if any_success else 1
        else:
            manager_name = None
            for name in PACKAGE_MANAGERS.keys():
                if name.upper() == target:
                    manager_name = name
                    break
            if not manager_name:
                available = ", ".join(sorted(PACKAGE_MANAGERS.keys()))
                cprint(f"Unknown manager '{args.update_manager}'. Available: {available}", "ERROR")
                return 1
            name, success, message = _update_manager(manager_name)
            if LOG.json_mode:
                result = {"manager": name, "ok": success, "message": message}
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                status_icon = "✓" if success else "✗"
                status_type = "SUCCESS" if success else "ERROR"
                cprint(f"{status_icon} {name}: {message}", status_type)
            return 0 if success else 1

    # Install package (new feature)
    if args.install:
        pkg = args.install.strip()
        if not pkg:
            cprint("No package name provided.", "ERROR")
            return 2
        preferred = args.manager.strip() if args.manager else None
        ok, attempts = install_package(pkg, preferred_manager=preferred)
        if LOG.json_mode:
            # Summarize attempts for JSON output
            out = {
                "package": pkg,
                "ok": ok,
                "attempts": [{
                    "manager": m,
                    "ok": r.ok,
                    "code": r.code,
                    "stdout": (r.out or "").strip()[-1000:],  # last 1000 chars
                    "stderr": (r.err or "").strip()[-1000:],
                } for (m, r) in attempts],
            }
            print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0 if ok else 1

    # Default action
    return show_default_status()

if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        cprint("\nInterrupted by user", "ERROR")
        sys.exit(130)
    except Exception as e:
        cprint(f"Unexpected error: {e}", "ERROR")
        if LOG.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)

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

__version__ = "2.2.0"

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
    """ANSI color codes for console output."""
    INFO = "\033[94m"
    SUCCESS = "\033[92m"
    WARNING = "\033[93m"
    ERROR = "\033[91m"
    MUTED = "\033[90m"
    RESET = "\033[0m"

@dataclass
class LogCfg:
    """Configuration for logging."""
    quiet: bool = False
    verbose: bool = False
    json_mode: bool = False

LOG = LogCfg()
_lock = threading.Lock()

def _out_json(level: str, msg: str, **extra):
    """Outputs a JSON payload for machine parsing."""
    payload = {"level": level, "msg": msg, "ts": time.time(), **extra}
    with _lock:
        print(json.dumps(payload, ensure_ascii=False))

def _fmt(msg: str, typ: str) -> str:
    """Formats a message with colors and a prefix."""
    # Disable colors on Windows if not supported
    if OS_NAME == "Windows" and not os.environ.get("FORCE_COLOR"):
        return f"[CrossFire] {msg}"
    color = getattr(Colors, typ, Colors.INFO)
    return f"{color}[CrossFire]{Colors.RESET} {msg}"

def cprint(msg: str, typ: str = "INFO", **extra) -> None:
    """Prints a log message based on configuration."""
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
    """Result of a command execution."""
    ok: bool
    code: int
    out: str
    err: str

def _split_cmd(cmd: str | List[str]) -> List[str]:
    """Splits a command string into a list of arguments."""
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
    """Runs a command with retries and timeout."""
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
    """Detects the user's shell profile file."""
    shell = os.environ.get("SHELL", "")
    home = os.path.expanduser("~")
    
    # Check for existing RC files in order of preference
    candidates = []
    
    # Shell-specific files based on current shell
    if shell.endswith("zsh"):
        candidates.extend([".zshrc", ".zprofile"])
    elif shell.endswith("bash"):
        candidates.extend([".bashrc", ".bash_profile"])
    elif shell.endswith("fish"):
        candidates.append(".config/fish/config.fish")
    
    # Common fallbacks
    candidates.extend([".profile", ".bashrc", ".zshrc"])
    
    # Return the first existing file, or create .profile as fallback
    for candidate in candidates:
        rc_path = os.path.join(home, candidate)
        if os.path.exists(rc_path):
            return rc_path
    
    # Default to .profile if nothing exists
    return os.path.join(home, ".profile")

def _check_path_already_added(script_dir: str) -> bool:
    """Check if the script directory is already in PATH or shell config."""
    # Check current PATH
    current_path = os.environ.get("PATH", "")
    if script_dir in current_path.split(os.pathsep):
        return True
    
    # Check shell RC files for export statements
    home = os.path.expanduser("~")
    rc_files = [".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile"]
    
    for rc_file in rc_files:
        rc_path = os.path.join(home, rc_file)
        if os.path.exists(rc_path):
            try:
                with open(rc_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    if script_dir in content and "export PATH" in content:
                        return True
            except Exception:
                continue
    
    return False

def add_to_path_safely() -> bool:
    """Adds the script directory to the system PATH."""
    script_dir = os.path.dirname(os.path.realpath(__file__))
    if not script_dir:
        cprint("Cannot determine script directory", "ERROR")
        return False
    
    if OS_NAME == "Windows":
        cprint("For permanent PATH update on Windows, you must add the script directory manually.", "WARNING")
        cprint(f"Add this directory to your PATH: {script_dir}", "INFO")
        return False

    try:
        # Check if already added
        if _check_path_already_added(script_dir):
            cprint(f"✅ PATH already contains CrossFire directory ({script_dir})", "SUCCESS")
            return True
            
        rc_file = _get_shell_rc_file()
        export_line = f'export PATH="{script_dir}:$PATH"'
        
        # Ensure the RC file's directory exists (for fish config, etc.)
        os.makedirs(os.path.dirname(rc_file), exist_ok=True)
        
        # Add the export line
        with open(rc_file, "a", encoding="utf-8") as f:
            f.write(f"\n# CrossFire CLI\n{export_line}\n")
        
        cprint(f"✅ Added CrossFire to PATH in {os.path.basename(rc_file)}", "SUCCESS")
        cprint("💡 Restart your terminal or run: source ~/.profile", "INFO")
        return True
        
    except Exception as e:
        cprint(f"✗ Failed to update PATH: {e}", "ERROR")
        cprint("You may need to add the following directory to your PATH manually:", "INFO")
        cprint(f"  {script_dir}", "MUTED")
        return False

def install_launcher() -> Optional[str]:
    """Installs a system-wide launcher for 'crossfire'."""
    target_name = "crossfire"
    script_path = os.path.abspath(__file__)
    installed_path: Optional[str] = None
    
    try:
        if OS_NAME in ("Linux", "Darwin"):
            # Prioritize user-local installation
            home_bin = os.path.expanduser("~/.local/bin")
            candidates = [home_bin, "/usr/local/bin"]
            
            for bin_dir in candidates:
                try:
                    launcher_path = os.path.join(bin_dir, target_name)
                    
                    # Check if launcher already exists and is working
                    if os.path.exists(launcher_path):
                        # Verify it's working
                        try:
                            result = run_command([launcher_path, "--help"], timeout=5)
                            if result.ok or "CrossFire" in result.out:
                                cprint(f"✅ Working launcher found at {launcher_path}", "SUCCESS")
                                installed_path = launcher_path
                                break
                        except Exception:
                            # Remove broken launcher
                            cprint(f"Removing broken launcher at {launcher_path}", "WARNING")
                            os.remove(launcher_path)
                    
                    # Create directory if it doesn't exist
                    os.makedirs(bin_dir, exist_ok=True)
                    
                    # Create launcher script
                    launcher_content = f"""#!/bin/bash
# CrossFire launcher - auto-generated
exec "{sys.executable}" "{script_path}" "$@"
"""
                    
                    with open(launcher_path, "w", encoding="utf-8") as f:
                        f.write(launcher_content)
                    
                    # Make executable
                    current_mode = os.stat(launcher_path).st_mode
                    os.chmod(launcher_path, current_mode | stat.S_IEXEC | stat.S_IREAD | stat.S_IWRITE)
                    
                    # Test the launcher
                    test_result = run_command([launcher_path, "--help"], timeout=10)
                    if test_result.ok or "CrossFire" in (test_result.out + test_result.err):
                        installed_path = launcher_path
                        cprint(f"✅ Launcher installed and tested: {launcher_path}", "SUCCESS")
                        
                        # Ensure ~/.local/bin is in PATH for user installations
                        if bin_dir == home_bin:
                            current_path = os.environ.get("PATH", "")
                            if home_bin not in current_path:
                                cprint(f"💡 Note: {home_bin} should be in your PATH", "INFO")
                        break
                    else:
                        cprint(f"Launcher test failed for {launcher_path}", "WARNING")
                        if os.path.exists(launcher_path):
                            os.remove(launcher_path)
                            
                except PermissionError:
                    if LOG.verbose:
                        cprint(f"Permission denied for {bin_dir}, trying next location", "MUTED")
                    continue
                except Exception as e:
                    if LOG.verbose:
                        cprint(f"Install attempt in {bin_dir} failed: {e}", "WARNING")
                    continue
                    
        elif OS_NAME == "Windows":
            python_exe = sys.executable or "python"
            script_dir = os.path.dirname(script_path)
            bat_path = os.path.join(script_dir, f"{target_name}.bat")
            
            if os.path.exists(bat_path):
                cprint(f"✅ Launcher already exists: {target_name}.bat", "SUCCESS")
                return bat_path
            
            try:
                batch_content = f"""@echo off
REM CrossFire launcher
"{python_exe}" "{script_path}" %*
"""
                with open(bat_path, "w", encoding="utf-8") as f:
                    f.write(batch_content)
                installed_path = bat_path
                cprint(f"✅ Launcher created: {target_name}.bat", "SUCCESS")
            except Exception as e:
                if LOG.verbose:
                    cprint(f"Launcher creation failed: {e}", "WARNING")
        else:
            cprint(f"Unsupported OS: {OS_NAME}", "WARNING")
            
    except Exception as e:
        cprint(f"✗ Launcher installation failed: {e}", "ERROR")
        
    return installed_path

# ----------------------------
# Package Managers (status/update)
# ----------------------------
def _get_python_commands() -> List[List[str]]:
    """Generates possible pip command combinations."""
    candidates = []
    if sys.executable:
        candidates.append([sys.executable, "-m", "pip"])
    for exe_name in ("python3", "python", "py"):
        exe_path = shutil.which(exe_name)
        if exe_path and exe_path != sys.executable:
            candidates.append([exe_path, "-m", "pip"])
    return candidates

PACKAGE_MANAGERS: Dict[str, Dict[str, any]] = {
    "Python": { "manager": _get_python_commands(), "update_cmd": None, "check_cmd": None },
    "NodeJS": { "manager": [["npm"]], "update_cmd": ["npm", "install", "-g", "npm@latest"], "check_cmd": ["npm", "--version"] },
    "Homebrew": { "manager": [["brew"]], "update_cmd": ["brew", "update"], "check_cmd": ["brew", "--version"] },
    "APT": { "manager": [["apt"]], "update_cmd": ["sudo", "apt", "update", "&&", "sudo", "apt", "upgrade", "-y"], "check_cmd": ["apt", "--version"] },
    "DNF": { "manager": [["dnf"]], "update_cmd": ["sudo", "dnf", "makecache", "--refresh"], "check_cmd": ["dnf", "--version"] },
    "YUM": { "manager": [["yum"]], "update_cmd": ["sudo", "yum", "makecache"], "check_cmd": ["yum", "--version"] },
    "Pacman": { "manager": [["pacman"]], "update_cmd": ["sudo", "pacman", "-Sy"], "check_cmd": ["pacman", "--version"] },
    "Zypper": { "manager": [["zypper"]], "update_cmd": ["sudo", "zypper", "refresh"], "check_cmd": ["zypper", "--version"] },
    "APK": { "manager": [["apk"]], "update_cmd": ["sudo", "apk", "update"], "check_cmd": ["apk", "--version"] },
    "Chocolatey": { "manager": [["choco"]], "update_cmd": ["choco", "upgrade", "chocolatey", "-y"], "check_cmd": ["choco", "version"] },
    "Winget": { "manager": [["winget"]], "update_cmd": ["winget", "source", "update"], "check_cmd": ["winget", "--version"] },
    "Snap": { "manager": [["snap"]], "update_cmd": ["sudo", "snap", "refresh"], "check_cmd": ["snap", "--version"] },
    "Flatpak": { "manager": [["flatpak"]], "update_cmd": ["flatpak", "update", "-y"], "check_cmd": ["flatpak", "--version"] },
}

_installed_cache: Dict[str, bool] = {}

def is_installed(manager_name: str) -> bool:
    """Checks if a given package manager is installed and available."""
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
    """Returns a dictionary of all managers and their installed status."""
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
    return result

def _update_manager(name: str) -> Tuple[str, bool, str]:
    """Updates a single package manager."""
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
            return (name, False, "No working Python found for pip update")

        update_cmd = meta.get("update_cmd")
        if not update_cmd:
            return (name, False, "No update command defined")

        use_shell = any(tok in update_cmd for tok in ("&&", ";"))
        result = run_command(update_cmd, timeout=900, shell=use_shell)
        status_msg = result.out.strip() if result.ok else result.err.strip()
        return (name, result.ok, status_msg or ("Updated successfully" if result.ok else "Update failed"))
    except Exception as e:
        return (name, False, f"Exception: {str(e)}")

def _update_all_managers() -> Dict[str, Dict[str, str]]:
    """Updates all installed package managers concurrently."""
    results = {}
    installed_managers = [name for name, status in list_managers_status().items() if status == "Installed"]
    if not installed_managers:
        cprint("No package managers found to update.", "WARNING")
        return results

    cprint(f"🔄 Updating {len(installed_managers)} package managers...", "INFO")
    max_workers = min(4, len(installed_managers))
    with _fut.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_manager = {executor.submit(_update_manager, name): name for name in installed_managers}
        for future in _fut.as_completed(future_to_manager):
            manager_name = future_to_manager[future]
            try:
                name, success, message = future.result()
                results[name] = {"ok": str(success).lower(), "msg": message}
                status_icon = "✅" if success else "❌"
                cprint(f"{status_icon} {name}: {message}", "SUCCESS" if success else "ERROR")
            except Exception as e:
                results[manager_name] = {"ok": "false", "msg": f"Exception: {str(e)}"}
                cprint(f"❌ {manager_name}: Exception: {e}", "ERROR")
    return results

# ----------------------------
# Self-update functionality
# ----------------------------
ALLOWED_UPDATE_HOSTS = {"github.com", "raw.githubusercontent.com", "githubusercontent.com"}
MAX_UPDATE_SIZE = 10 * 1024 * 1024  # 10MB
DEFAULT_UPDATE_URL = "https://raw.githubusercontent.com/BCAS-Team/CrossFire/main/CrossFireL/crossfire.py"

def _validate_update_url(url: str) -> None:
    """Checks if a URL is from an allowed host."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if not any(allowed in host for allowed in ALLOWED_UPDATE_HOSTS):
        raise ValueError(f"Update host '{host}' not in allowlist: {sorted(ALLOWED_UPDATE_HOSTS)}")

def _single_thread_download(url: str, timeout: int) -> bytes:
    """Downloads a file using a single thread (fallback)."""
    try:
        req = urllib.request.Request(url)
        req.add_header('User-Agent', f'CrossFire/{__version__}')
        with urllib.request.urlopen(req, timeout=timeout) as response:
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
        raise Exception(f"Single-threaded download failed: {str(e)}")

def download_update(url: str, timeout: int = 60) -> bytes:
    """Downloads a file using concurrent connections to increase speed."""
    _validate_update_url(url)
    try:
        # Step 1: Get file size with a HEAD request
        head_req = urllib.request.Request(url, method='HEAD')
        head_req.add_header('User-Agent', f'CrossFire/{__version__}')
        with urllib.request.urlopen(head_req, timeout=5) as response:
            content_length = int(response.headers.get('Content-Length', 0))
            if content_length > MAX_UPDATE_SIZE:
                raise ValueError(f"Update file too large: {content_length} bytes")
        
        # Fallback to single-thread download if file size is unknown
        if content_length == 0:
            cprint("Warning: Could not determine file size. Falling back to single-threaded download.", "WARNING")
            return _single_thread_download(url, timeout)

        # Step 2: Determine number of chunks and size
        num_chunks = min(4, (os.cpu_count() or 1) * 2) # Use up to 4 threads or 2x CPU cores
        chunk_size = content_length // num_chunks
        cprint(f"🚀 Downloading {content_length / (1024*1024):.2f} MB in {num_chunks} parallel chunks...", "INFO")

        chunks_data = [None] * num_chunks
        results_queue = _fut.Queue()
        
        # Step 3: Define the download worker function
        def _download_chunk(start: int, end: int, index: int):
            try:
                req = urllib.request.Request(url)
                req.add_header('User-Agent', f'CrossFire/{__version__}')
                req.add_header('Range', f'bytes={start}-{end}')
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    data = response.read()
                    results_queue.put((index, data))
            except Exception as e:
                results_queue.put((index, e))

        # Step 4: Submit tasks to the thread pool
        with _fut.ThreadPoolExecutor(max_workers=num_chunks) as executor:
            for i in range(num_chunks):
                start = i * chunk_size
                end = start + chunk_size - 1
                if i == num_chunks - 1:
                    end = content_length - 1
                executor.submit(_download_chunk, start, end, i)

        # Step 5: Collect results and join chunks
        for _ in range(num_chunks):
            index, data = results_queue.get()
            if isinstance(data, Exception):
                raise Exception(f"Chunk {index} download failed: {data}")
            chunks_data[index] = data

        return b"".join(chunks_data)
        
    except Exception as e:
        raise Exception(f"Concurrent download failed: {str(e)}")

def cross_update(url: str = DEFAULT_UPDATE_URL, *, verify_sha256: Optional[str] = None) -> bool:
    """Performs a self-update of the script."""
    try:
        cprint(f"🔄 Downloading update from: {url}", "INFO")
        update_data = download_update(url)
        if verify_sha256:
            actual_hash = hashlib.sha256(update_data).hexdigest().lower()
            expected_hash = verify_sha256.lower()
            if actual_hash != expected_hash:
                cprint(f"❌ SHA256 mismatch: expected {expected_hash}, got {actual_hash}", "ERROR")
                return False
            cprint("✅ SHA256 verification passed", "SUCCESS")

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
            cprint(f"✅ Update successful! Backup saved as: {os.path.basename(backup_file)}", "SUCCESS")
            return True
        except Exception as e:
            if os.path.exists(backup_file):
                try:
                    shutil.copy2(backup_file, current_file)
                    cprint("Update failed, restored from backup", "WARNING")
                except Exception:
                    pass
            cprint(f"❌ Update failed: {e}", "ERROR")
            return False
    except Exception as e:
        cprint(f"❌ Update error: {e}", "ERROR")
        return False

# ----------------------------
# Package INSTALLATION
# ----------------------------
# Install command builders per manager
def _pip_install(pkg: str) -> List[str]: return [sys.executable, "-m", "pip", "install", pkg]
def _npm_install(pkg: str) -> List[str]: return ["npm", "install", "-g", pkg]
def _apt_install(pkg: str) -> List[str]: return ["sudo", "apt", "install", "-y", pkg]
def _dnf_install(pkg: str) -> List[str]: return ["sudo", "dnf", "install", "-y", pkg]
def _yum_install(pkg: str) -> List[str]: return ["sudo", "yum", "install", "-y", pkg]
def _pacman_install(pkg: str) -> List[str]: return ["sudo", "pacman", "-S", "--noconfirm", pkg]
def _zypper_install(pkg: str) -> List[str]: return ["sudo", "zypper", "--non-interactive", "install", pkg]
def _apk_install(pkg: str) -> List[str]: return ["sudo", "apk", "add", pkg]
def _brew_install(pkg: str) -> List[str]: return ["brew", "install", pkg]
def _choco_install(pkg: str) -> List[str]: return ["choco", "install", "-y", pkg]
def _winget_install(pkg: str) -> List[str]: return ["winget", "install", "--silent", "--accept-package-agreements", "--accept-source-agreements", pkg]
def _snap_install(pkg: str) -> List[str]: return ["sudo", "snap", "install", pkg]
def _flatpak_install(pkg: str) -> List[str]: return ["flatpak", "install", "-y", pkg]

# Exposed install handlers
MANAGER_INSTALL_HANDLERS: Dict[str, callable] = {
    "pip": _pip_install, "npm": _npm_install, "apt": _apt_install, "dnf": _dnf_install, "yum": _yum_install,
    "pacman": _pacman_install, "zypper": _zypper_install, "apk": _apk_install, "brew": _brew_install,
    "choco": _choco_install, "winget": _winget_install, "snap": _snap_install, "flatpak": _flatpak_install,
}

def _os_type() -> str:
    """Returns a simplified OS name for heuristics."""
    s = platform.system().lower()
    if s.startswith("win"): return "windows"
    if s == "darwin": return "macos"
    if s == "linux": return "linux"
    return "unknown"

def _detect_installed_installers() -> Dict[str, bool]:
    """Determines which installation managers are available."""
    available = {}
    for name, fn in MANAGER_INSTALL_HANDLERS.items():
        exe = {
            "pip": sys.executable.split(os.sep)[-1] if sys.executable else "python",
        }.get(name, name)
        if name == "pip":
            available[name] = sys.executable is not None or shutil.which("python") is not None or shutil.which("python3") is not None
        else:
            available[name] = shutil.which(exe) is not None
    return available

def _looks_like_python_pkg(pkg: str) -> bool:
    """Heuristics for Python packages."""
    return any(x in pkg for x in ["==", ">=", "<=", "~=", "!=", "[", "]"]) or pkg.lower().startswith(("py", "django", "flask", "numpy", "pandas"))

def _looks_like_npm_pkg(pkg: str) -> bool:
    """Heuristics for NPM packages."""
    return pkg.startswith("@") or pkg.lower() in ("express", "react", "vue", "angular", "typescript", "eslint")

def _system_manager_priority() -> List[str]:
    """Returns a prioritized list of system package managers for the current OS."""
    ot = _os_type()
    if ot == "macos": return ["brew", "snap", "flatpak"]
    if ot == "windows": return ["winget", "choco"]
    for cand in ("apt", "dnf", "yum", "pacman", "zypper", "apk"):
        if shutil.which(cand):
            return [cand, "snap", "flatpak"]
    return ["snap", "flatpak"]

def _ordered_install_manager_candidates(pkg: str, installed: Dict[str, bool]) -> List[str]:
    """Generates a prioritized list of managers to try for a given package."""
    prefs: List[str] = []
    if _looks_like_python_pkg(pkg) and installed.get("pip"): prefs.append("pip")
    if _looks_like_npm_pkg(pkg) and installed.get("npm"): prefs.append("npm")
    for m in _system_manager_priority():
        if installed.get(m): prefs.append(m)
    for m, ok in installed.items():
        if ok and m not in prefs: prefs.append(m)
    return list(dict.fromkeys(prefs)) # Deduplicate while preserving order

def _manager_human(name: str) -> str:
    """Returns a human-readable name for a manager."""
    return {
        "pip": "Python (pip)", "npm": "Node.js (npm)", "apt": "APT", "dnf": "DNF", "yum": "YUM",
        "pacman": "Pacman", "zypper": "Zypper", "apk": "APK", "brew": "Homebrew",
        "choco": "Chocolatey", "winget": "Winget", "snap": "Snap", "flatpak": "Flatpak",
    }.get(name, name)

def install_package(pkg: str, preferred_manager: Optional[str] = None) -> Tuple[bool, List[Tuple[str, RunResult]]]:
    """Tries to install a package using available managers."""
    cprint(f"📦 Preparing to install: {pkg}", "INFO")
    installed = _detect_installed_installers()
    if not any(installed.values()):
        cprint("❌ No supported package managers are available on this system.", "ERROR")
        return (False, [])
    attempts: List[Tuple[str, RunResult]] = []
    candidates = _ordered_install_manager_candidates(pkg, installed)

    if preferred_manager:
        pm = preferred_manager.lower()
        if pm in MANAGER_INSTALL_HANDLERS and installed.get(pm):
            candidates = [pm] + [m for m in candidates if m != pm]
        else:
            cprint(f"Warning: --manager '{preferred_manager}' not available; ignoring preference.", "WARNING")

    cprint("Will try managers in this order:", "MUTED")
    for m in candidates:
        cprint(f"  • {_manager_human(m)}", "MUTED")

    for m in candidates:
        cmd_builder = MANAGER_INSTALL_HANDLERS.get(m)
        if not cmd_builder: continue
        cmd = cmd_builder(pkg)
        cprint(f"→ Attempting via {_manager_human(m)}...", "INFO")
        res = run_command(cmd, timeout=1800, retries=0)
        attempts.append((m, res))
        if res.ok:
            cprint(f"✅ Installed '{pkg}' via {_manager_human(m)}", "SUCCESS")
            return (True, attempts)
        else:
            err_snip = (res.err or res.out).strip()
            if err_snip:
                err_snip = err_snip.splitlines()[-1][:180]
                cprint(f"❌ {_manager_human(m)} failed: {err_snip}", "WARNING")

    cprint(f"❌ Failed to install '{pkg}' with all available managers.", "ERROR")
    return (False, attempts)

# ----------------------------
# CLI
# ----------------------------
def create_parser() -> argparse.ArgumentParser:
    """Creates the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="CrossFire — Universal Package Manager CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  crossfire --list-managers           # List all supported package managers
  crossfire -um <NAME>                # Update a specific manager or 'ALL'
  crossfire -cu [URL]                 # Self-update from a URL
  crossfire --install <PKG>           # AUTO: install a package with the best manager
  crossfire --setup                   # One-time setup: install launcher & update PATH

Examples:
  crossfire --setup
  crossfire -i curl
  crossfire -i numpy --manager pip
  crossfire -um ALL
        """,
    )
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    parser.add_argument("-q", "--quiet", action="store_true", help="Quiet mode (errors only)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--list-managers", action="store_true", help="List all supported managers and their status")
    parser.add_argument("-um", "--update-manager", metavar="NAME", help="Update specific manager or 'ALL' for all managers")
    parser.add_argument("-cu", "--crossupdate", nargs="?", const=DEFAULT_UPDATE_URL, metavar="URL",
                         help="Self-update from URL (default: GitHub)")
    parser.add_argument("--sha256", metavar="HASH", help="Expected SHA256 hash for update verification")
    parser.add_argument("-i", "--install", metavar="PKG", help="Install a package by name")
    parser.add_argument("--manager", metavar="NAME", help="Preferred manager to use (pip, npm, apt, brew, etc.)")
    parser.add_argument("--setup", action="store_true", help="Performs initial setup: installs launcher and adds to PATH")
    parser.add_argument("--test-setup", action="store_true", help="Test if CrossFire setup is working correctly")
    return parser

def test_setup() -> int:
    """Test if CrossFire is properly set up and accessible."""
    script_dir = os.path.dirname(os.path.realpath(__file__))
    
    cprint("🔍 Testing CrossFire setup...", "INFO")
    
    # Test 1: Check if script directory is in PATH
    current_path = os.environ.get("PATH", "").split(os.pathsep)
    path_ok = any(script_dir in path_part for path_part in current_path)
    
    if path_ok:
        cprint("✅ Script directory is in PATH", "SUCCESS")
    else:
        cprint("❌ Script directory not found in PATH", "ERROR")
        cprint(f"   Expected: {script_dir}", "MUTED")
    
    # Test 2: Check for launcher scripts
    launcher_found = False
    if OS_NAME in ("Linux", "Darwin"):
        possible_launchers = [
            os.path.expanduser("~/.local/bin/crossfire"),
            "/usr/local/bin/crossfire"
        ]
        for launcher in possible_launchers:
            if os.path.exists(launcher) and os.access(launcher, os.X_OK):
                cprint(f"✅ Found executable launcher: {launcher}", "SUCCESS")
                launcher_found = True
                break
    elif OS_NAME == "Windows":
        bat_launcher = os.path.join(script_dir, "crossfire.bat")
        if os.path.exists(bat_launcher):
            cprint(f"✅ Found batch launcher: {bat_launcher}", "SUCCESS")
            launcher_found = True
    
    if not launcher_found:
        cprint("❌ No working launcher found", "ERROR")
    
    # Test 3: Try to execute crossfire command
    crossfire_works = False
    try:
        # Test if 'crossfire' command works
        result = run_command(["crossfire", "--help"], timeout=10)
        if result.ok or "CrossFire" in (result.out + result.err):
            cprint("✅ 'crossfire' command works from PATH", "SUCCESS")
            crossfire_works = True
        else:
            cprint("❌ 'crossfire' command failed", "ERROR")
    except Exception as e:
        cprint(f"❌ Error testing crossfire command: {e}", "ERROR")
    
    # Summary
    setup_ok = path_ok or launcher_found or crossfire_works
    if setup_ok:
        cprint("\n🎉 CrossFire setup appears to be working!", "SUCCESS")
        if not crossfire_works:
            cprint("💡 You may need to restart your terminal or run: source ~/.profile", "INFO")
        return 0
    else:
        cprint("\n❌ CrossFire setup has issues. Try running: python crossfire.py --setup", "ERROR")
        return 1

def show_default_status() -> int:
    """Shows the tool's default status message and available managers."""
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
        installed_managers = sorted([m for m, s in status_info.items() if s == "Installed"])
        not_installed = sorted([m for m, s in status_info.items() if s != "Installed"])
        
        cprint("\n✅ Installed Managers:", "SUCCESS")
        if installed_managers:
            for manager in installed_managers:
                cprint(f"  - {manager}", "SUCCESS")
        else:
            cprint("  (None found)", "WARNING")
            
        cprint("\n❌ Not Installed Managers:", "MUTED")
        if not_installed:
            for manager in not_installed:
                cprint(f"  - {manager}", "MUTED")
        
    # Check if setup is needed
    script_dir = os.path.dirname(os.path.realpath(__file__))
    if not _check_path_already_added(script_dir):
        cprint("\n💡 To install the 'crossfire' command globally, run:", "INFO")
        cprint(f"   python {os.path.basename(__file__)} --setup", "MUTED")
    else:
        cprint("\n✅ CrossFire is set up. Use 'crossfire --help' for usage.", "SUCCESS")
    
    return 0

def run(argv: Optional[List[str]] = None) -> int:
    """Main execution entry point."""
    parser = create_parser()
    args = parser.parse_args(argv)

    LOG.quiet = args.quiet
    LOG.verbose = args.verbose
    LOG.json_mode = args.json
    
    # Handle the setup command separately and early for better performance
    if args.setup:
        cprint("⚙️ Running one-time setup...", "INFO")
        path_added = add_to_path_safely()
        launcher_installed = install_launcher()
        
        if path_added or launcher_installed:
            cprint(f"\n🎉 Setup complete!", "SUCCESS")
            cprint("You can now run 'crossfire' from any directory.", "INFO")
            cprint("You may need to restart your terminal or run: source ~/.profile", "MUTED")
            
            # Test the setup
            cprint("\nTesting setup...", "INFO")
            return test_setup()
        else:
            cprint("\n❌ Setup encountered issues. Try running with sudo or check permissions.", "ERROR")
            return 1
    
    if args.test_setup:
        return test_setup()

    if args.crossupdate is not None:
        url = args.crossupdate or DEFAULT_UPDATE_URL
        success = cross_update(url, verify_sha256=args.sha256)
        return 0 if success else 1
    
    if args.list_managers:
        status_info = list_managers_status()
        if LOG.json_mode:
            print(json.dumps(status_info, indent=2, ensure_ascii=False))
        else:
            cprint("Package Manager Status:", "INFO")
            for manager, status in sorted(status_info.items()):
                status_icon = "✅" if status == "Installed" else "❌" if status == "Not Installed" else "❓"
                cprint(f" {status_icon} {manager}: {status}", "SUCCESS" if status == "Installed" else "MUTED" if status == "Not Installed" else "WARNING")
        return 0

    if args.update_manager:
        target = args.update_manager.upper()
        if target == "ALL":
            results = _update_all_managers()
        else:
            name, ok, msg = _update_manager(target)
            results = {name: {"ok": str(ok).lower(), "msg": msg}}
            status_icon = "✅" if ok else "❌"
            cprint(f"{status_icon} {name}: {msg}", "SUCCESS" if ok else "ERROR")
        if LOG.json_mode:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0 if all(r.get("ok") == "true" for r in results.values()) else 1
    
    if args.install:
        pkg = args.install
        success, attempts = install_package(pkg, preferred_manager=args.manager)
        if LOG.json_mode:
            output = {"package": pkg, "success": success, "attempts": [{"manager": m, "result": {"ok": r.ok, "code": r.code, "out": r.out, "err": r.err}} for m, r in attempts]}
            print(json.dumps(output, indent=2, ensure_ascii=False))
        return 0 if success else 1
    
    return show_default_status()

if __name__ == "__main__":
    try:
        sys.exit(run())
    except KeyboardInterrupt:
        cprint("\nOperation cancelled by user.", "WARNING")
        sys.exit(1)
    except Exception as e:
        cprint(f"An unexpected error occurred: {e}", "ERROR")
        if LOG.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)

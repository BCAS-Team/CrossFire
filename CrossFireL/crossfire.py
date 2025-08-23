#From Github Update 
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
import queue

__version__ = "3.0.0 (Stable Branch)"

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
    # Check for color support more reliably
    supports_color = (
        hasattr(sys.stdout, 'isatty') and sys.stdout.isatty() and
        os.environ.get('TERM', '').lower() != 'dumb' and
        (OS_NAME != "Windows" or os.environ.get("FORCE_COLOR") or 
         os.environ.get("ANSICON") or "ANSI" in os.environ.get("TERM", ""))
    )
    
    if not supports_color:
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

            # Fix: Use proper shell handling for complex commands
            if shell or (isinstance(cmd, str) and any(op in cmd for op in ['&&', '||', ';', '|', '>', '<'])):
                shell = True
                cmd_to_run = cmd if isinstance(cmd, str) else ' '.join(cmd)
            else:
                cmd_to_run = _split_cmd(cmd)

            creation_flags = 0
            if OS_NAME == "Windows":
                creation_flags = subprocess.CREATE_NO_WINDOW

            p = subprocess.run(
                cmd_to_run,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=proc_env,
                shell=shell,
                creationflags=creation_flags,
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
    
    # Check for specific shells first
    if shell.endswith("zsh") or os.path.exists(os.path.join(home, ".zshrc")):
        return os.path.join(home, ".zshrc")
    elif shell.endswith("bash"):
        # Prefer .bashrc on Linux, .bash_profile on macOS
        if OS_NAME == "Darwin" and os.path.exists(os.path.join(home, ".bash_profile")):
            return os.path.join(home, ".bash_profile")
        elif os.path.exists(os.path.join(home, ".bashrc")):
            return os.path.join(home, ".bashrc")
        else:
            return os.path.join(home, ".bashrc")
    elif shell.endswith("fish"):
        fish_config_dir = os.path.join(home, ".config", "fish")
        os.makedirs(fish_config_dir, exist_ok=True)
        return os.path.join(fish_config_dir, "config.fish")
    elif os.path.exists(os.path.join(home, ".profile")):
        return os.path.join(home, ".profile")
    else:
        return os.path.join(home, ".profile")

def add_to_path_safely() -> None:
    """Adds the script directory to the system PATH."""
    script_dir = os.path.dirname(os.path.realpath(__file__))
    if not script_dir:
        return
    
    if OS_NAME == "Windows":
        cprint("For permanent PATH update on Windows, you must add the script directory manually.", "WARNING")
        cprint(f"Add this to your PATH: {script_dir}", "INFO")
        return

    try:
        rc_file = _get_shell_rc_file()
        export_line = f'export PATH="{script_dir}:$PATH"'
        
        # Handle fish shell differently
        if rc_file.endswith("config.fish"):
            export_line = f'set -gx PATH "{script_dir}" $PATH'
        
        # Check if the line is already in the file to avoid duplicates
        if os.path.exists(rc_file):
            with open(rc_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                if script_dir in content or export_line in content:
                    cprint(f"✅ PATH is already configured in {os.path.basename(rc_file)}.", "SUCCESS")
                    return
        
        with open(rc_file, "a", encoding="utf-8") as f:
            f.write(f"\n# CrossFire CLI\n{export_line}\n")
        cprint(f"✅ PATH updated in {os.path.basename(rc_file)}.", "SUCCESS")
        cprint("Note: You may need to restart your terminal or run 'source ~/.bashrc' (or equivalent) for changes to take effect.", "INFO")
    except Exception as e:
        cprint(f"✗ Failed to update PATH: {e}", "ERROR")

def install_launcher() -> Optional[str]:
    """Installs a system-wide launcher for 'crossfire'."""
    target_name = "crossfire"
    script_path = os.path.abspath(__file__)
    installed_path: Optional[str] = None
    
    try:
        if OS_NAME in ("Linux", "Darwin"):
            # Try user bin directory first, then system-wide
            candidates = [
                os.path.expanduser("~/.local/bin"),
                "/usr/local/bin",
                os.path.expanduser("~/bin")  # Some distros use this
            ]
            
            for bin_dir in candidates:
                try:
                    launcher_path = os.path.join(bin_dir, target_name)
                    
                    # Skip if already exists and is working
                    if os.path.exists(launcher_path):
                        test_result = run_command([launcher_path, "--version"], timeout=5)
                        if test_result.ok:
                            cprint(f"✅ Working launcher already exists at {launcher_path}.", "SUCCESS")
                            installed_path = launcher_path
                            break
                        else:
                            cprint(f"Removing broken launcher at {launcher_path}", "WARNING")
                            os.remove(launcher_path)
                    
                    # Create directory if it doesn't exist
                    os.makedirs(bin_dir, exist_ok=True)
                    
                    # Create launcher script
                    launcher_content = f"""#!/bin/bash
# CrossFire launcher
exec "{sys.executable}" "{script_path}" "$@"
"""
                    with open(launcher_path, "w", encoding="utf-8") as f:
                        f.write(launcher_content)
                    
                    # Make executable
                    current_mode = os.stat(launcher_path).st_mode
                    os.chmod(launcher_path, current_mode | stat.S_IEXEC | stat.S_IXUSR | stat.S_IXGRP)
                    
                    # Test the launcher
                    test_result = run_command([launcher_path, "--version"], timeout=5)
                    if test_result.ok:
                        installed_path = launcher_path
                        cprint(f"✅ Launcher installed: {launcher_path}", "SUCCESS")
                        break
                    else:
                        cprint(f"Launcher test failed at {launcher_path}, trying next location", "WARNING")
                        os.remove(launcher_path)
                        
                except PermissionError:
                    if LOG.verbose:
                        cprint(f"Permission denied for {bin_dir}, trying next location", "WARNING")
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
    
    # Try common Python executables
    for exe_name in ("python3", "python", "py"):
        exe_path = shutil.which(exe_name)
        if exe_path and exe_path != sys.executable:
            candidates.append([exe_path, "-m", "pip"])
    
    # Also try direct pip commands
    for pip_name in ("pip3", "pip"):
        pip_path = shutil.which(pip_name)
        if pip_path:
            candidates.append([pip_path])
    
    return candidates

PACKAGE_MANAGERS: Dict[str, Dict[str, any]] = {
    "Python": { "manager": _get_python_commands(), "update_cmd": None, "check_cmd": None },
    "NodeJS": { "manager": [["npm"]], "update_cmd": ["npm", "install", "-g", "npm@latest"], "check_cmd": ["npm", "--version"] },
    "Homebrew": { "manager": [["brew"]], "update_cmd": ["brew", "update"], "check_cmd": ["brew", "--version"] },
    "APT": { "manager": [["apt"]], "update_cmd": "sudo apt update && sudo apt upgrade -y", "check_cmd": ["apt", "--version"] },
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
                # For Python, test if pip actually works
                if manager_name == "Python":
                    test_cmd = cmd_list + ["--version"]
                    result = run_command(test_cmd, timeout=10)
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
    
    # Use threading for I/O bound operations
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
                    update_cmd = cmd_template + ["install", "--upgrade", "pip"]
                    result = run_command(update_cmd, timeout=300)
                    if result.ok:
                        return (name, True, "Updated successfully")
            return (name, False, "No working Python found for pip update")

        update_cmd = meta.get("update_cmd")
        if not update_cmd:
            return (name, False, "No update command defined")

        # Handle shell commands properly
        use_shell = isinstance(update_cmd, str) or any(op in str(update_cmd) for op in ("&&", ";", "||"))
        result = run_command(update_cmd, timeout=900, shell=use_shell)
        status_msg = result.out.strip() if result.ok else result.err.strip()
        return (name, result.ok, status_msg or ("Updated successfully" if result.ok else "Update failed"))
        
    except Exception as e:
        return (name, False, f"Exception: {str(e)}")

def _update_all_managers() -> Dict[str, Dict[str, str]]:
    """Updates all installed package managers concurrently."""
    results = {}
    status_info = list_managers_status()
    installed_managers = [name for name, status in status_info.items() if status == "Installed"]
    
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
        
        try:
            with urllib.request.urlopen(head_req, timeout=5) as response:
                content_length = int(response.headers.get('Content-Length', 0))
                if content_length > MAX_UPDATE_SIZE:
                    raise ValueError(f"Update file too large: {content_length} bytes")
        except:
            # HEAD request failed, fallback to single-thread
            cprint("HEAD request failed, falling back to single-threaded download.", "WARNING")
            return _single_thread_download(url, timeout)
        
        # Fallback to single-thread download if file size is unknown or small
        if content_length == 0 or content_length < 1024 * 100:  # Less than 100KB
            return _single_thread_download(url, timeout)

        # Step 2: Determine number of chunks and size
        num_chunks = min(4, max(2, (os.cpu_count() or 1)))  # 2-4 chunks
        chunk_size = content_length // num_chunks
        cprint(f"🚀 Downloading {content_length / (1024*1024):.2f} MB in {num_chunks} parallel chunks...", "INFO")

        chunks_data = [None] * num_chunks
        results_queue = queue.Queue()
        
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
        cprint(f"Concurrent download failed: {str(e)}, trying single-threaded fallback", "WARNING")
        return _single_thread_download(url, timeout)

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
            # Use a more robust update process
            with tempfile.NamedTemporaryFile(mode='wb', delete=False, dir=os.path.dirname(current_file), suffix='.tmp') as tmp_file:
                tmp_file.write(update_data)
                temp_path = tmp_file.name
            
            # Make the temp file executable before moving (Unix-like systems)
            if OS_NAME != "Windows":
                st = os.stat(current_file)
                os.chmod(temp_path, st.st_mode | stat.S_IEXEC)
            
            # Atomic move
            if OS_NAME == "Windows":
                try:
                    os.replace(temp_path, current_file)
                except OSError:
                    os.remove(current_file)
                    os.rename(temp_path, current_file)
            else:
                os.replace(temp_path, current_file)
                
            # Verify the update worked
            try:
                result = run_command([sys.executable, current_file, "--version"], timeout=5)
                if not result.ok:
                    raise Exception("Updated script verification failed")
            except Exception as e:
                cprint(f"Update verification failed: {e}", "WARNING")
            
            cprint(f"✅ Update successful! Backup saved as: {os.path.basename(backup_file)}", "SUCCESS")
            return True
            
        except Exception as e:
            # Restore from backup
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
# Package Search & Information
# ----------------------------
def _search_pip(query: str) -> List[Dict[str, str]]:
    """Search Python packages via pip."""
    try:
        python_cmds = _get_python_commands()
        for cmd in python_cmds:
            if shutil.which(cmd[0]):
                search_cmd = cmd + ["search", query]
                result = run_command(search_cmd, timeout=30)
                if result.ok:
                    packages = []
                    lines = result.out.strip().split('\n')
                    for line in lines[:10]:  # Limit to first 10 results
                        if ' - ' in line:
                            name, desc = line.split(' - ', 1)
                            packages.append({"name": name.strip(), "description": desc.strip()})
                    return packages
                break
        # Fallback to PyPI API search if pip search fails
        return _search_pypi_api(query)
    except Exception:
        return _search_pypi_api(query)

def _search_pypi_api(query: str) -> List[Dict[str, str]]:
    """Search Python packages via PyPI API."""
    try:
        import json
        import urllib.request
        url = f"https://pypi.org/pypi/{query}/json"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            return [{
                "name": data["info"]["name"],
                "description": data["info"]["summary"] or "No description available",
                "version": data["info"]["version"]
            }]
    except Exception:
        return []

def _search_npm(query: str) -> List[Dict[str, str]]:
    """Search Node.js packages via npm."""
    try:
        search_cmd = ["npm", "search", query, "--json", "--long"]
        result = run_command(search_cmd, timeout=30)
        if result.ok:
            import json
            data = json.loads(result.out)
            packages = []
            for pkg in list(data.items())[:10]:  # Limit results
                name, info = pkg
                packages.append({
                    "name": name,
                    "description": info.get("description", "No description available"),
                    "version": info.get("version", "unknown")
                })
            return packages
    except Exception:
        pass
    return []

def _search_brew(query: str) -> List[Dict[str, str]]:
    """Search Homebrew packages."""
    try:
        search_cmd = ["brew", "search", query]
        result = run_command(search_cmd, timeout=20)
        if result.ok:
            packages = []
            for line in result.out.strip().split('\n')[:10]:
                if line.strip():
                    packages.append({
                        "name": line.strip(),
                        "description": "Homebrew package"
                    })
            return packages
    except Exception:
        pass
    return []

def _search_apt(query: str) -> List[Dict[str, str]]:
    """Search APT packages."""
    try:
        search_cmd = ["apt", "search", query]
        result = run_command(search_cmd, timeout=30)
        if result.ok:
            packages = []
            lines = result.out.strip().split('\n')
            for line in lines[:20]:  # Limit results
                if '/' in line and ' - ' in line:
                    parts = line.split(' - ', 1)
                    if len(parts) == 2:
                        name_part = parts[0].split('/')[0]
                        packages.append({
                            "name": name_part,
                            "description": parts[1].strip()
                        })
            return packages[:10]
    except Exception:
        pass
    return []

SEARCH_HANDLERS = {
    "pip": _search_pip,
    "npm": _search_npm,
    "brew": _search_brew,
    "apt": _search_apt,
}

def search_packages(query: str, manager: Optional[str] = None) -> Dict[str, List[Dict[str, str]]]:
    """Search for packages across available managers."""
    cprint(f"🔍 Searching for: {query}", "INFO")
    
    installed = _detect_installed_installers()
    results = {}
    
    if manager:
        # Search specific manager
        if manager in SEARCH_HANDLERS and installed.get(manager):
            results[manager] = SEARCH_HANDLERS[manager](query)
        else:
            cprint(f"Manager '{manager}' not available or not supported for search", "WARNING")
    else:
        # Search all available managers
        for mgr_name, search_fn in SEARCH_HANDLERS.items():
            if installed.get(mgr_name):
                try:
                    results[mgr_name] = search_fn(query)
                except Exception as e:
                    if LOG.verbose:
                        cprint(f"Search failed for {mgr_name}: {e}", "WARNING")
    
    return results

# ----------------------------
# Package Information
# ----------------------------
def _get_pip_info(package: str) -> Dict[str, str]:
    """Get detailed information about a Python package."""
    try:
        python_cmds = _get_python_commands()
        for cmd in python_cmds:
            if shutil.which(cmd[0]):
                info_cmd = cmd + ["show", package]
                result = run_command(info_cmd, timeout=15)
                if result.ok:
                    info = {}
                    for line in result.out.strip().split('\n'):
                        if ':' in line:
                            key, value = line.split(':', 1)
                            info[key.strip()] = value.strip()
                    return info
                break
    except Exception:
        pass
    return {}

def _get_npm_info(package: str) -> Dict[str, str]:
    """Get detailed information about an npm package."""
    try:
        info_cmd = ["npm", "info", package, "--json"]
        result = run_command(info_cmd, timeout=15)
        if result.ok:
            import json
            data = json.loads(result.out)
            return {
                "Name": data.get("name", ""),
                "Version": data.get("version", ""),
                "Description": data.get("description", ""),
                "Homepage": data.get("homepage", ""),
                "License": data.get("license", ""),
                "Author": str(data.get("author", "")),
            }
    except Exception:
        pass
    return {}

def _get_brew_info(package: str) -> Dict[str, str]:
    """Get detailed information about a Homebrew package."""
    try:
        info_cmd = ["brew", "info", package, "--json"]
        result = run_command(info_cmd, timeout=15)
        if result.ok:
            import json
            data = json.loads(result.out)
            if data:
                pkg = data[0]
                return {
                    "Name": pkg.get("name", ""),
                    "Version": pkg.get("versions", {}).get("stable", ""),
                    "Description": pkg.get("desc", ""),
                    "Homepage": pkg.get("homepage", ""),
                    "License": pkg.get("license", ""),
                }
    except Exception:
        pass
    return {}

def _get_apt_info(package: str) -> Dict[str, str]:
    """Get detailed information about an APT package."""
    try:
        info_cmd = ["apt", "show", package]
        result = run_command(info_cmd, timeout=15)
        if result.ok:
            info = {}
            for line in result.out.strip().split('\n'):
                if ':' in line:
                    key, value = line.split(':', 1)
                    info[key.strip()] = value.strip()
            return info
    except Exception:
        pass
    return {}

INFO_HANDLERS = {
    "pip": _get_pip_info,
    "npm": _get_npm_info,
    "brew": _get_brew_info,
    "apt": _get_apt_info,
}

def get_package_info(package: str, manager: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """Get detailed information about a package."""
    installed = _detect_installed_installers()
    results = {}
    
    if manager:
        if manager in INFO_HANDLERS and installed.get(manager):
            results[manager] = INFO_HANDLERS[manager](package)
        else:
            cprint(f"Manager '{manager}' not available or not supported for info", "WARNING")
    else:
        # Try to detect package type and use appropriate manager
        candidates = []
        if _looks_like_python_pkg(package):
            candidates = ["pip"]
        elif _looks_like_npm_pkg(package):
            candidates = ["npm"]
        else:
            candidates = ["pip", "npm", "brew", "apt"]
        
        for mgr_name in candidates:
            if mgr_name in INFO_HANDLERS and installed.get(mgr_name):
                try:
                    info = INFO_HANDLERS[mgr_name](package)
                    if info:  # Only add if we got actual info
                        results[mgr_name] = info
                        break  # Stop after first successful lookup
                except Exception as e:
                    if LOG.verbose:
                        cprint(f"Info lookup failed for {mgr_name}: {e}", "WARNING")
    
    return results

# ----------------------------
# Bulk Package Operations
# ----------------------------
def install_from_file(filepath: str, manager: Optional[str] = None) -> Dict[str, bool]:
    """Install packages from a requirements file."""
    if not os.path.exists(filepath):
        cprint(f"❌ File not found: {filepath}", "ERROR")
        return {}
    
    cprint(f"📦 Installing packages from: {filepath}", "INFO")
    results = {}
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        packages = []
        for line in lines:
            line = line.strip()
            # Skip comments and empty lines
            if line and not line.startswith('#'):
                # Handle different file formats
                if '==' in line or '>=' in line or '<=' in line:
                    # Python requirements.txt format
                    packages.append(line)
                else:
                    # Simple package list
                    packages.append(line.split()[0])  # Take first word only
        
        cprint(f"Found {len(packages)} packages to install", "INFO")
        
        for pkg in packages:
            if pkg.strip():
                cprint(f"Installing: {pkg}", "INFO")
                success, _ = install_package(pkg.strip(), manager)
                results[pkg.strip()] = success
                
    except Exception as e:
        cprint(f"❌ Error reading file {filepath}: {e}", "ERROR")
    
    # Summary
    successful = sum(1 for success in results.values() if success)
    total = len(results)
    cprint(f"📊 Installation complete: {successful}/{total} packages installed successfully", 
           "SUCCESS" if successful == total else "WARNING")
    
    return results

def export_installed_packages(manager: str, output_file: Optional[str] = None) -> bool:
    """Export installed packages to a file."""
    if not is_installed(manager):
        cprint(f"❌ Manager '{manager}' is not installed", "ERROR")
        return False
    
    try:
        packages = []
        
        if manager.lower() == "pip":
            python_cmds = _get_python_commands()
            for cmd in python_cmds:
                if shutil.which(cmd[0]):
                    list_cmd = cmd + ["list", "--format=freeze"]
                    result = run_command(list_cmd, timeout=30)
                    if result.ok:
                        packages = result.out.strip().split('\n')
                        break
                        
        elif manager.lower() == "npm":
            list_cmd = ["npm", "list", "-g", "--depth=0", "--parseable"]
            result = run_command(list_cmd, timeout=30)
            if result.ok:
                for line in result.out.strip().split('\n'):
                    if line and '/node_modules/' in line:
                        pkg_name = line.split('/node_modules/')[-1]
                        if pkg_name and pkg_name != 'npm':
                            packages.append(pkg_name)
                            
        elif manager.lower() == "brew":
            list_cmd = ["brew", "list", "--formula"]
            result = run_command(list_cmd, timeout=30)
            if result.ok:
                packages = result.out.strip().split('\n')
        
        if not packages:
            cprint(f"❌ No packages found or export not supported for {manager}", "WARNING")
            return False
        
        # Generate output
        if output_file:
            with open(output_file, 'w', encoding='utf-8') as f:
                for pkg in packages:
                    if pkg.strip():
                        f.write(f"{pkg.strip()}\n")
            cprint(f"✅ Exported {len(packages)} packages to: {output_file}", "SUCCESS")
        else:
            cprint(f"📦 Installed packages ({manager}):", "INFO")
            for pkg in packages:
                if pkg.strip():
                    cprint(f"  - {pkg.strip()}", "MUTED")
        
        return True
        
    except Exception as e:
        cprint(f"❌ Export failed: {e}", "ERROR")
        return False

# ----------------------------
# Package Removal
# ----------------------------
def _pip_remove(pkg: str) -> List[str]:
    """Generate pip uninstall command."""
    python_cmds = _get_python_commands()
    for cmd in python_cmds:
        if shutil.which(cmd[0]):
            return cmd + ["uninstall", "-y", pkg]
    return [sys.executable, "-m", "pip", "uninstall", "-y", pkg]

def _npm_remove(pkg: str) -> List[str]:
    return ["npm", "uninstall", "-g", pkg]

def _brew_remove(pkg: str) -> List[str]:
    return ["brew", "uninstall", pkg]

def _apt_remove(pkg: str) -> List[str]:
    return ["sudo", "apt", "remove", "-y", pkg]

def _dnf_remove(pkg: str) -> List[str]:
    return ["sudo", "dnf", "remove", "-y", pkg]

def _yum_remove(pkg: str) -> List[str]:
    return ["sudo", "yum", "remove", "-y", pkg]

def _pacman_remove(pkg: str) -> List[str]:
    return ["sudo", "pacman", "-R", "--noconfirm", pkg]

def _snap_remove(pkg: str) -> List[str]:
    return ["sudo", "snap", "remove", pkg]

def _flatpak_remove(pkg: str) -> List[str]:
    return ["flatpak", "uninstall", "-y", pkg]

MANAGER_REMOVE_HANDLERS = {
    "pip": _pip_remove,
    "npm": _npm_remove,
    "brew": _brew_remove,
    "apt": _apt_remove,
    "dnf": _dnf_remove,
    "yum": _yum_remove,
    "pacman": _pacman_remove,
    "snap": _snap_remove,
    "flatpak": _flatpak_remove,
}

def remove_package(pkg: str, manager: Optional[str] = None) -> Tuple[bool, List[Tuple[str, RunResult]]]:
    """Remove a package using available managers."""
    cprint(f"🗑️ Preparing to remove: {pkg}", "INFO")
    installed = _detect_installed_installers()
    
    if not any(installed.values()):
        cprint("❌ No supported package managers are available on this system.", "ERROR")
        return (False, [])
    
    attempts: List[Tuple[str, RunResult]] = []
    
    if manager:
        if manager.lower() in MANAGER_REMOVE_HANDLERS and installed.get(manager.lower()):
            candidates = [manager.lower()]
        else:
            cprint(f"❌ Manager '{manager}' not available for package removal", "ERROR")
            return (False, [])
    else:
        # Try managers in order of likelihood
        candidates = _ordered_install_manager_candidates(pkg, installed)
        # Filter to only those that support removal
        candidates = [m for m in candidates if m in MANAGER_REMOVE_HANDLERS]

    if not candidates:
        cprint("❌ No package managers available for package removal.", "ERROR")
        return (False, [])

    for mgr in candidates:
        cmd_builder = MANAGER_REMOVE_HANDLERS.get(mgr)
        if not cmd_builder:
            continue
            
        try:
            cmd = cmd_builder(pkg)
            cprint(f"→ Attempting removal via {_manager_human(mgr)}...", "INFO")
            
            res = run_command(cmd, timeout=600, retries=0)
            attempts.append((mgr, res))
            
            if res.ok:
                cprint(f"✅ Removed '{pkg}' via {_manager_human(mgr)}", "SUCCESS")
                return (True, attempts)
            else:
                err_msg = (res.err or res.out).strip()
                if err_msg:
                    error_lines = err_msg.splitlines()
                    relevant_error = error_lines[-1] if error_lines else "Unknown error"
                    if len(relevant_error) > 180:
                        relevant_error = relevant_error[:177] + "..."
                    cprint(f"❌ {_manager_human(mgr)} failed: {relevant_error}", "WARNING")
                else:
                    cprint(f"❌ {_manager_human(mgr)} failed with no error message", "WARNING")
                    
        except Exception as e:
            err_result = RunResult(False, -1, "", str(e))
            attempts.append((mgr, err_result))
            cprint(f"❌ {_manager_human(mgr)} failed with exception: {str(e)}", "WARNING")

    cprint(f"❌ Failed to remove '{pkg}' with all available managers.", "ERROR")
    return (False, attempts)

# ----------------------------
# System Cleanup
# ----------------------------
def cleanup_system() -> Dict[str, Dict[str, str]]:
    """Clean up package manager caches and temporary files."""
    cprint("🧹 Starting system cleanup...", "INFO")
    results = {}
    installed = _detect_installed_installers()
    
    cleanup_commands = {
        "pip": [sys.executable, "-m", "pip", "cache", "purge"],
        "npm": ["npm", "cache", "clean", "--force"],
        "brew": ["brew", "cleanup", "--prune=all"],
        "apt": "sudo apt autoremove -y && sudo apt autoclean",
        "dnf": ["sudo", "dnf", "clean", "all"],
        "yum": ["sudo", "yum", "clean", "all"],
        "pacman": ["sudo", "pacman", "-Sc", "--noconfirm"],
    }
    
    for manager, cmd in cleanup_commands.items():
        if installed.get(manager):
            try:
                cprint(f"→ Cleaning {_manager_human(manager)}...", "INFO")
                use_shell = isinstance(cmd, str)
                result = run_command(cmd, timeout=300, shell=use_shell)
                
                if result.ok:
                    results[manager] = {"ok": "true", "msg": "Cleanup successful"}
                    cprint(f"✅ {_manager_human(manager)}: Cleanup successful", "SUCCESS")
                else:
                    results[manager] = {"ok": "false", "msg": result.err or "Cleanup failed"}
                    cprint(f"❌ {_manager_human(manager)}: Cleanup failed", "WARNING")
                    
            except Exception as e:
                results[manager] = {"ok": "false", "msg": f"Exception: {e}"}
                cprint(f"❌ {_manager_human(manager)}: Exception during cleanup: {e}", "WARNING")
    
    successful = sum(1 for r in results.values() if r.get("ok") == "true")
    total = len(results)
    cprint(f"📊 Cleanup complete: {successful}/{total} managers cleaned successfully", 
           "SUCCESS" if successful > 0 else "WARNING")
    
    return results
# Install command builders per manager
def _pip_install(pkg: str) -> List[str]: 
    # Use the first working Python/pip combination
    python_cmds = _get_python_commands()
    for cmd in python_cmds:
        if shutil.which(cmd[0]):
            return cmd + ["install", pkg]
    return [sys.executable, "-m", "pip", "install", pkg]

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
        if name == "pip":
            # Check if any Python/pip combination works
            python_cmds = _get_python_commands()
            available[name] = any(shutil.which(cmd[0]) for cmd in python_cmds if cmd)
        else:
            available[name] = shutil.which(name) is not None
    return available

def _looks_like_python_pkg(pkg: str) -> bool:
    """Heuristics for Python packages."""
    python_indicators = ["==", ">=", "<=", "~=", "!=", "[", "]"]
    python_common = ["py", "django", "flask", "numpy", "pandas", "requests", "boto3", "tensorflow", "torch"]
    
    # Check for version specifiers
    if any(indicator in pkg for indicator in python_indicators):
        return True
    
    # Check for common Python package prefixes/names
    pkg_lower = pkg.lower()
    if any(pkg_lower.startswith(prefix) for prefix in python_common):
        return True
    
    return False

def _looks_like_npm_pkg(pkg: str) -> bool:
    """Heuristics for NPM packages."""
    npm_indicators = pkg.startswith("@")
    npm_common = ["express", "react", "vue", "angular", "typescript", "eslint", "webpack", "lodash", "axios"]
    
    if npm_indicators:
        return True
    
    pkg_lower = pkg.lower()
    if pkg_lower in npm_common:
        return True
    
    return False

def _system_manager_priority() -> List[str]:
    """Returns a prioritized list of system package managers for the current OS."""
    ot = _os_type()
    
    if ot == "macos": 
        return ["brew", "snap", "flatpak"]
    elif ot == "windows": 
        return ["winget", "choco"]
    elif ot == "linux":
        # Detect Linux distribution and prioritize accordingly
        try:
            # Check for specific package managers in order of preference
            linux_managers = [
                ("apt", ["apt", "apt-get"]),
                ("dnf", ["dnf"]),
                ("yum", ["yum"]),
                ("pacman", ["pacman"]),
                ("zypper", ["zypper"]),
                ("apk", ["apk"])
            ]
            
            for manager, commands in linux_managers:
                if any(shutil.which(cmd) for cmd in commands):
                    return [manager, "snap", "flatpak"]
        except:
            pass
        
        # Fallback to universal package managers
        return ["snap", "flatpak"]
    
    return ["snap", "flatpak"]

def _ordered_install_manager_candidates(pkg: str, installed: Dict[str, bool]) -> List[str]:
    """Generates a prioritized list of managers to try for a given package."""
    prefs: List[str] = []
    
    # Prioritize based on package type heuristics
    if _looks_like_python_pkg(pkg) and installed.get("pip"):
        prefs.append("pip")
    if _looks_like_npm_pkg(pkg) and installed.get("npm"):
        prefs.append("npm")
    
    # Add system package managers in priority order
    for manager in _system_manager_priority():
        if installed.get(manager) and manager not in prefs:
            prefs.append(manager)
    
    # Add any remaining installed managers
    for manager, is_installed in installed.items():
        if is_installed and manager not in prefs:
            prefs.append(manager)
    
    return prefs

def _manager_human(name: str) -> str:
    """Returns a human-readable name for a manager."""
    names = {
        "pip": "Python (pip)", "npm": "Node.js (npm)", "apt": "APT", "dnf": "DNF", "yum": "YUM",
        "pacman": "Pacman", "zypper": "Zypper", "apk": "APK", "brew": "Homebrew",
        "choco": "Chocolatey", "winget": "Winget", "snap": "Snap", "flatpak": "Flatpak",
    }
    return names.get(name, name.title())

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
            # Move preferred manager to front
            candidates = [pm] + [m for m in candidates if m != pm]
        else:
            available_managers = [m for m, avail in installed.items() if avail]
            cprint(f"Warning: --manager '{preferred_manager}' not available. Available: {', '.join(available_managers)}", "WARNING")

    if not candidates:
        cprint("❌ No package managers available for installation.", "ERROR")
        return (False, [])

    cprint("Will try managers in this order:", "MUTED")
    for m in candidates:
        cprint(f"  • {_manager_human(m)}", "MUTED")

    for manager in candidates:
        cmd_builder = MANAGER_INSTALL_HANDLERS.get(manager)
        if not cmd_builder: 
            continue
            
        try:
            cmd = cmd_builder(pkg)
            cprint(f"→ Attempting via {_manager_human(manager)}...", "INFO")
            
            # Use longer timeout for installations
            res = run_command(cmd, timeout=1800, retries=0)
            attempts.append((manager, res))
            
            if res.ok:
                cprint(f"✅ Installed '{pkg}' via {_manager_human(manager)}", "SUCCESS")
                return (True, attempts)
            else:
                # Show more helpful error messages
                err_msg = (res.err or res.out).strip()
                if err_msg:
                    # Get the last few lines of error output
                    error_lines = err_msg.splitlines()
                    relevant_error = error_lines[-1] if error_lines else "Unknown error"
                    if len(relevant_error) > 180:
                        relevant_error = relevant_error[:177] + "..."
                    cprint(f"❌ {_manager_human(manager)} failed: {relevant_error}", "WARNING")
                else:
                    cprint(f"❌ {_manager_human(manager)} failed with no error message", "WARNING")
                    
        except Exception as e:
            err_result = RunResult(False, -1, "", str(e))
            attempts.append((manager, err_result))
            cprint(f"❌ {_manager_human(manager)} failed with exception: {str(e)}", "WARNING")

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
    parser.add_argument("--version", action="version", version=f"CrossFire {__version__}")
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
    parser.add_argument("-s", "--search", metavar="QUERY", help="Search for packages")
    parser.add_argument("--info", metavar="PKG", help="Get detailed information about a package")
    parser.add_argument("-r", "--remove", metavar="PKG", help="Remove/uninstall a package")
    parser.add_argument("--install-from", metavar="FILE", help="Install packages from requirements file")
    parser.add_argument("--export", metavar="MANAGER", help="Export installed packages list")
    parser.add_argument("-o", "--output", metavar="FILE", help="Output file for export command")
    parser.add_argument("--cleanup", action="store_true", help="Clean package manager caches and temporary files")
    parser.add_argument("--health-check", action="store_true", help="Run comprehensive system health check")
    parser.add_argument("--config", action="store_true", help="Show and manage CrossFire configuration")
    parser.add_argument("--stats", action="store_true", help="Show package manager statistics")
    parser.add_argument("--setup", action="store_true", help="Performs initial setup: installs launcher and adds to PATH")
    return parser

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
                status = status_info[manager]
                cprint(f"  - {manager} ({status})", "MUTED")
        
        cprint("\nTo install the 'crossfire' command globally, run: crossfire --setup", "INFO")
        cprint("For help with commands, run: crossfire --help", "INFO")
    
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
        add_to_path_safely()
        installed_path = install_launcher()
        
        if installed_path:
            cprint(f"\n✅ Setup complete! You can now run 'crossfire' from any directory.", "SUCCESS")
            cprint("You may need to restart your terminal or source your shell profile.", "INFO")
        else:
            cprint("⚠️ Setup completed with some issues. The launcher may not have been installed correctly.", "WARNING")
        return 0

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
                if status == "Installed":
                    status_icon = "✅"
                    color = "SUCCESS"
                elif status == "Not Installed":
                    status_icon = "❌"
                    color = "MUTED"
                else:
                    status_icon = "❓"
                    color = "WARNING"
                cprint(f" {status_icon} {manager}: {status}", color)
        return 0

    if args.update_manager:
        target = args.update_manager.upper()
        if target == "ALL":
            results = _update_all_managers()
        else:
            # Convert target back to proper case for lookup
            proper_name = None
            for name in PACKAGE_MANAGERS.keys():
                if name.upper() == target:
                    proper_name = name
                    break
            
            if not proper_name:
                cprint(f"❌ Unknown package manager: {args.update_manager}", "ERROR")
                return 1
                
            name, ok, msg = _update_manager(proper_name)
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
            output = {
                "package": pkg, 
                "success": success, 
                "attempts": [
                    {
                        "manager": m, 
                        "result": {
                            "ok": r.ok, 
                            "code": r.code, 
                            "stdout": r.out, 
                            "stderr": r.err
                        }
                    } for m, r in attempts
                ]
            }
            print(json.dumps(output, indent=2, ensure_ascii=False))
        return 0 if success else 1
    
    # No specific command given, show status
    return show_default_status()

# ----------------------------
# Health Check & Statistics  
# ----------------------------
def get_system_stats() -> Dict[str, any]:
    """Collect comprehensive system and package manager statistics."""
    stats = {
        "system": {
            "os": OS_NAME,
            "distro": DISTRO_NAME,
            "distro_version": DISTRO_VERSION,
            "architecture": ARCH,
            "python_version": platform.python_version(),
        },
        "managers": {},
        "disk_usage": {},
    }
    
    # Package manager statistics
    status_info = list_managers_status()
    for manager, status in status_info.items():
        stats["managers"][manager] = {"status": status}
        
        if status == "Installed":
            # Get package counts where possible
            try:
                if manager == "Python":
                    python_cmds = _get_python_commands()
                    for cmd in python_cmds:
                        if shutil.which(cmd[0]):
                            result = run_command(cmd + ["list"], timeout=15)
                            if result.ok:
                                count = len([line for line in result.out.split('\n') if line.strip()])
                                stats["managers"][manager]["package_count"] = count - 2  # Remove headers
                                break
                elif manager == "NodeJS":
                    result = run_command(["npm", "list", "-g", "--depth=0"], timeout=15)
                    if result.ok:
                        count = len([line for line in result.out.split('\n') if 'node_modules' in line])
                        stats["managers"][manager]["package_count"] = count
                elif manager == "Homebrew":
                    result = run_command(["brew", "list"], timeout=15)
                    if result.ok:
                        stats["managers"][manager]["package_count"] = len(result.out.split('\n')) - 1
            except Exception:
                pass
    
    # Disk usage for package manager caches
    cache_dirs = {
        "pip": "~/.cache/pip",
        "npm": "~/.npm",
        "brew": "/usr/local/var/homebrew",
        "apt": "/var/cache/apt",
    }
    
    for manager, cache_dir in cache_dirs.items():
        try:
            expanded_dir = os.path.expanduser(cache_dir)
            if os.path.exists(expanded_dir):
                if OS_NAME != "Windows":
                    result = run_command(["du", "-sh", expanded_dir], timeout=10)
                    if result.ok:
                        size = result.out.split('\t')[0]
                        stats["disk_usage"][manager] = size
        except Exception:
            pass
    
    return stats

def health_check() -> Dict[str, any]:
    """Run comprehensive health check on system and package managers."""
    cprint("🏥 Running system health check...", "INFO")
    
    health = {
        "overall_status": "healthy",
        "issues": [],
        "recommendations": [],
        "manager_health": {}
    }
    
    # Check each package manager
    status_info = list_managers_status()
    for manager, status in status_info.items():
        manager_health = {"status": status, "issues": [], "working": False}
        
        if status == "Installed":
            # Test basic functionality
            try:
                if manager == "Python":
                    python_cmds = _get_python_commands()
                    for cmd in python_cmds:
                        if shutil.which(cmd[0]):
                            result = run_command(cmd + ["--version"], timeout=5)
                            if result.ok:
                                manager_health["working"] = True
                                manager_health["version"] = result.out.strip()
                                break
                elif manager == "NodeJS":
                    result = run_command(["npm", "--version"], timeout=5)
                    if result.ok:
                        manager_health["working"] = True
                        manager_health["version"] = result.out.strip()
                elif manager == "Homebrew":
                    result = run_command(["brew", "--version"], timeout=5)
                    if result.ok:
                        manager_health["working"] = True
                        manager_health["version"] = result.out.split('\n')[0]
                # Add more manager-specific health checks...
                
                if not manager_health["working"]:
                    manager_health["issues"].append("Manager installed but not responding to version check")
                    
            except Exception as e:
                manager_health["issues"].append(f"Health check failed: {e}")
        
        health["manager_health"][manager] = manager_health
    
    # Check for common issues
    working_managers = sum(1 for mh in health["manager_health"].values() 
                          if mh["status"] == "Installed" and mh["working"])
    
    if working_managers == 0:
        health["overall_status"] = "critical"
        health["issues"].append("No working package managers found")
        health["recommendations"].append("Install at least one package manager (pip, npm, apt, brew, etc.)")
    elif working_managers < 2:
        health["overall_status"] = "warning"
        health["recommendations"].append("Consider installing additional package managers for better coverage")
    
    # Check Python specifically
    if not sys.executable:
        health["issues"].append("Python executable path not found")
        health["overall_status"] = "warning"
    
    # Check disk space for cache directories
    try:
        if shutil.which("df"):
            result = run_command(["df", "-h", os.path.expanduser("~")], timeout=5)
            if result.ok:
                lines = result.out.strip().split('\n')
                if len(lines) > 1:
                    usage_line = lines[1].split()
                    if len(usage_line) >= 5:
                        usage_percent = usage_line[4].rstrip('%')
                        if usage_percent.isdigit() and int(usage_percent) > 90:
                            health["issues"].append(f"Low disk space: {usage_percent}% used")
                            health["recommendations"].append("Consider running 'crossfire --cleanup' to free space")
                            if health["overall_status"] == "healthy":
                                health["overall_status"] = "warning"
    except Exception:
        pass
    
    return health

# ----------------------------
# Configuration Management
# ----------------------------
def get_config_path() -> str:
    """Get the path to CrossFire configuration file."""
    if OS_NAME == "Windows":
        config_dir = os.path.expanduser("~/AppData/Local/CrossFire")
    else:
        config_dir = os.path.expanduser("~/.config/crossfire")
    
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, "config.json")

def load_config() -> Dict[str, any]:
    """Load CrossFire configuration."""
    config_path = get_config_path()
    default_config = {
        "preferred_managers": {},
        "auto_cleanup": False,
        "timeout": 600,
        "max_retries": 1,
        "color_output": True,
        "update_check_interval": 86400,  # 24 hours
    }
    
    try:
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                # Merge with defaults
                for key, value in default_config.items():
                    config.setdefault(key, value)
                return config
    except Exception as e:
        if LOG.verbose:
            cprint(f"Config load failed: {e}", "WARNING")
    
    return default_config

def save_config(config: Dict[str, any]) -> bool:
    """Save CrossFire configuration."""
    try:
        config_path = get_config_path()
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        cprint(f"Config save failed: {e}", "ERROR")
        return False

def show_config() -> Dict[str, any]:
    """Display current configuration."""
    config = load_config()
    config_path = get_config_path()
    
    cprint("🔧 CrossFire Configuration", "INFO")
    cprint(f"Config file: {config_path}", "MUTED")
    cprint("", "INFO")
    
    if LOG.json_mode:
        print(json.dumps(config, indent=2, ensure_ascii=False))
    else:
        for key, value in config.items():
            cprint(f"  {key}: {value}", "INFO")
    
    return config

def run(argv: Optional[List[str]] = None) -> int:
    """Main execution entry point."""
    parser = create_parser()
    args = parser.parse_args(argv)

    LOG.quiet = args.quiet
    LOG.verbose = args.verbose
    LOG.json_mode = args.json
    
    # Load configuration
    config = load_config()
    
    # Handle the setup command separately and early for better performance
    if args.setup:
        cprint("⚙️ Running one-time setup...", "INFO")
        add_to_path_safely()
        installed_path = install_launcher()
        
        if installed_path:
            cprint(f"\n✅ Setup complete! You can now run 'crossfire' from any directory.", "SUCCESS")
            cprint("You may need to restart your terminal or source your shell profile.", "INFO")
        else:
            cprint("⚠️ Setup completed with some issues. The launcher may not have been installed correctly.", "WARNING")
        return 0

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
                if status == "Installed":
                    status_icon = "✅"
                    color = "SUCCESS"
                elif status == "Not Installed":
                    status_icon = "❌"
                    color = "MUTED"
                else:
                    status_icon = "❓"
                    color = "WARNING"
                cprint(f" {status_icon} {manager}: {status}", color)
        return 0

    if args.search:
        results = search_packages(args.search, args.manager)
        if LOG.json_mode:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            if not results:
                cprint("No packages found.", "WARNING")
                return 1
            
            for manager, packages in results.items():
                if packages:
                    cprint(f"\n📦 {_manager_human(manager)} Results:", "SUCCESS")
                    for pkg in packages[:10]:  # Show top 10
                        name = pkg.get("name", "")
                        desc = pkg.get("description", "")
                        version = pkg.get("version", "")
                        version_str = f" ({version})" if version else ""
                        cprint(f"  • {name}{version_str}", "INFO")
                        if desc:
                            cprint(f"    {desc[:100]}{'...' if len(desc) > 100 else ''}", "MUTED")
        return 0
    
    if args.info:
        results = get_package_info(args.info, args.manager)
        if LOG.json_mode:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            if not results:
                cprint(f"No information found for package: {args.info}", "WARNING")
                return 1
                
            for manager, info in results.items():
                if info:
                    cprint(f"\n📋 {_manager_human(manager)} - {args.info}", "SUCCESS")
                    for key, value in info.items():
                        if value:
                            cprint(f"  {key}: {value}", "INFO")
        return 0
    
    if args.remove:
        success, attempts = remove_package(args.remove, args.manager)
        
        if LOG.json_mode:
            output = {
                "package": args.remove, 
                "success": success, 
                "attempts": [
                    {
                        "manager": m, 
                        "result": {
                            "ok": r.ok, 
                            "code": r.code, 
                            "stdout": r.out, 
                            "stderr": r.err
                        }
                    } for m, r in attempts
                ]
            }
            print(json.dumps(output, indent=2, ensure_ascii=False))
        return 0 if success else 1
    
    if args.install_from:
        results = install_from_file(args.install_from, args.manager)
        if LOG.json_mode:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        success_count = sum(1 for success in results.values() if success)
        return 0 if success_count > 0 else 1
    
    if args.export:
        output_file = args.output
        success = export_installed_packages(args.export, output_file)
        return 0 if success else 1
    
    if args.cleanup:
        results = cleanup_system()
        if LOG.json_mode:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0 if any(r.get("ok") == "true" for r in results.values()) else 1
    
    if args.health_check:
        health = health_check()
        if LOG.json_mode:
            print(json.dumps(health, indent=2, ensure_ascii=False))
        else:
            status_color = {
                "healthy": "SUCCESS",
                "warning": "WARNING", 
                "critical": "ERROR"
            }.get(health["overall_status"], "INFO")
            
            status_icon = {
                "healthy": "✅",
                "warning": "⚠️", 
                "critical": "❌"
            }.get(health["overall_status"], "❓")
            
            cprint(f"{status_icon} Overall Status: {health['overall_status'].upper()}", status_color)
            
            if health["issues"]:
                cprint("\n❌ Issues Found:", "ERROR")
                for issue in health["issues"]:
                    cprint(f"  • {issue}", "ERROR")
            
            if health["recommendations"]:
                cprint("\n💡 Recommendations:", "WARNING")
                for rec in health["recommendations"]:
                    cprint(f"  • {rec}", "WARNING")
            
            cprint("\n📊 Manager Health:", "INFO")
            for manager, mh in health["manager_health"].items():
                if mh["status"] == "Installed":
                    icon = "✅" if mh["working"] else "❌"
                    version = f" ({mh.get('version', '')})" if mh.get('version') else ""
                    cprint(f"  {icon} {manager}{version}", "SUCCESS" if mh["working"] else "ERROR")
                    if mh["issues"]:
                        for issue in mh["issues"]:
                            cprint(f"    ⚠️  {issue}", "WARNING")
        
        return 0 if health["overall_status"] != "critical" else 1
    
    if args.stats:
        stats = get_system_stats()
        if LOG.json_mode:
            print(json.dumps(stats, indent=2, ensure_ascii=False))
        else:
            cprint("📊 System Statistics", "INFO")
            cprint(f"\n🖥️  System: {stats['system']['os']} {stats['system']['distro']} {stats['system']['distro_version']}", "INFO")
            cprint(f"🐍 Python: {stats['system']['python_version']}", "INFO")
            cprint(f"🏗️  Architecture: {stats['system']['architecture']}", "INFO")
            
            cprint("\n📦 Package Managers:", "INFO")
            for manager, info in stats["managers"].items():
                status = info["status"]
                if status == "Installed":
                    count = info.get("package_count", "unknown")
                    cprint(f"  ✅ {manager}: {count} packages", "SUCCESS")
                else:
                    cprint(f"  ❌ {manager}: {status}", "MUTED")
            
            if stats["disk_usage"]:
                cprint("\n💾 Cache Usage:", "INFO")
                for manager, usage in stats["disk_usage"].items():
                    cprint(f"  📁 {manager}: {usage}", "INFO")
        return 0
    
    if args.config:
        show_config()
        return 0

    if args.update_manager:
        target = args.update_manager.upper()
        if target == "ALL":
            results = _update_all_managers()
        else:
            # Convert target back to proper case for lookup
            proper_name = None
            for name in PACKAGE_MANAGERS.keys():
                if name.upper() == target:
                    proper_name = name
                    break
            
            if not proper_name:
                cprint(f"❌ Unknown package manager: {args.update_manager}", "ERROR")
                return 1
                
            name, ok, msg = _update_manager(proper_name)
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
            output = {
                "package": pkg, 
                "success": success, 
                "attempts": [
                    {
                        "manager": m, 
                        "result": {
                            "ok": r.ok, 
                            "code": r.code, 
                            "stdout": r.out, 
                            "stderr": r.err
                        }
                    } for m, r in attempts
                ]
            }
            print(json.dumps(output, indent=2, ensure_ascii=False))
        return 0 if success else 1
    
    # No specific command given, show status
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


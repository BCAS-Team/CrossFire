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
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple, Any, Union
import queue
import re
from datetime import datetime, timedelta
import sqlite3
from pathlib import Path
import requests
import xml.etree.ElementTree as ET

__version__ = "3.2.0f2 (Production)"

# ----------------------------
# Configuration & Constants
# ----------------------------
DEFAULT_UPDATE_URL = "https://raw.githubusercontent.com/crossfire-pm/crossfire-launcher/main/crossfire.py"
CROSSFIRE_DIR = Path.home() / ".crossfire"
CROSSFIRE_DB = CROSSFIRE_DIR / "packages.db"
CROSSFIRE_CACHE = CROSSFIRE_DIR / "cache"

# Ensure CrossFire directory exists
CROSSFIRE_DIR.mkdir(exist_ok=True)
CROSSFIRE_CACHE.mkdir(exist_ok=True)

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
# Logging System
# ----------------------------
class Colors:
    """ANSI color codes for console output."""
    INFO = "\033[94m"
    SUCCESS = "\033[92m"
    WARNING = "\033[93m"
    ERROR = "\033[91m"
    MUTED = "\033[90m"
    BOLD = "\033[1m"
    CYAN = "\033[96m"
    RESET = "\033[0m"

class Logger:
    def __init__(self):
        self.quiet = False
        self.verbose = False
        self.json_mode = False

    def cprint(self, text, color="INFO"):
        if self.json_mode:
            return
        if self.quiet and color in ["INFO", "WARNING"]:
            return
        if self.quiet and color in ["SUCCESS"]:
            return
        if not sys.stdout.isatty():
            sys.stdout.write(f"{text}\n")
            return
        
        color_code = getattr(Colors, color.upper(), Colors.INFO)
        print(f"{color_code}{text}{Colors.RESET}")

LOG = Logger()
cprint = LOG.cprint

# ============================================================================
# Database System for Package Tracking
# ============================================================================
class PackageDB:
    def __init__(self, db_path: Path = CROSSFIRE_DB):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """Initialize the SQLite database for package tracking."""
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS installed_packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                version TEXT,
                manager TEXT NOT NULL,
                install_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                install_command TEXT,
                UNIQUE(name, manager)
            )
        ''')
        conn.commit()
        conn.close()
    
    def add_package(self, name: str, version: str, manager: str, command: str = ""):
        """Record a successfully installed package."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute('''
                INSERT OR REPLACE INTO installed_packages 
                (name, version, manager, install_command) 
                VALUES (?, ?, ?, ?)
            ''', (name, version or "unknown", manager, command))
            conn.commit()
        finally:
            conn.close()
    
    def remove_package(self, name: str, manager: str = None):
        """Remove a package record."""
        conn = sqlite3.connect(self.db_path)
        try:
            if manager:
                conn.execute('DELETE FROM installed_packages WHERE name = ? AND manager = ?', 
                           (name, manager))
            else:
                conn.execute('DELETE FROM installed_packages WHERE name = ?', (name,))
            conn.commit()
        finally:
            conn.close()
    
    def get_installed_packages(self, manager: str = None) -> List[Dict]:
        """Get list of installed packages."""
        conn = sqlite3.connect(self.db_path)
        try:
            if manager:
                cursor = conn.execute('''
                    SELECT name, version, manager, install_date 
                    FROM installed_packages 
                    WHERE manager = ? 
                    ORDER BY install_date DESC
                ''', (manager,))
            else:
                cursor = conn.execute('''
                    SELECT name, version, manager, install_date 
                    FROM installed_packages 
                    ORDER BY install_date DESC
                ''')
            
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()
    
    def is_installed(self, name: str, manager: str = None) -> bool:
        """Check if a package is recorded as installed."""
        conn = sqlite3.connect(self.db_path)
        try:
            if manager:
                cursor = conn.execute(
                    'SELECT COUNT(*) FROM installed_packages WHERE name = ? AND manager = ?',
                    (name, manager)
                )
            else:
                cursor = conn.execute(
                    'SELECT COUNT(*) FROM installed_packages WHERE name = ?',
                    (name,)
                )
            return cursor.fetchone()[0] > 0
        finally:
            conn.close()

package_db = PackageDB()

# ============================================================================
# Progress/ETA System
# ============================================================================
class ProgressBar:
    def __init__(self, total, description, unit):
        self.total = total
        self.description = description
        self.unit = unit
        self.current = 0
        self.start_time = time.time()
        self.lock = threading.Lock()
        self.bar_length = 50
        self.terminal_width = shutil.get_terminal_size((80, 20)).columns

    def update(self, step=1):
        with self.lock:
            self.current = min(self.current + step, self.total)
            self._draw_bar()

    def _draw_bar(self):
        if LOG.json_mode or not sys.stdout.isatty():
            return
        
        progress = self.current / self.total if self.total > 0 else 0
        percent = progress * 100
        filled_length = int(self.bar_length * progress)
        bar = '█' * filled_length + '-' * (self.bar_length - filled_length)
        
        elapsed = time.time() - self.start_time
        eta_str = "N/A"
        speed_str = ""
        
        if progress > 0 and elapsed > 0:
            remaining = (elapsed / progress) - elapsed if progress < 1 else 0
            if remaining > 3600:
                eta_str = f"{remaining/3600:.1f}h"
            elif remaining > 60:
                eta_str = f"{remaining/60:.1f}m"
            else:
                eta_str = f"{remaining:.1f}s"
            
            # Calculate speed
            if self.unit == "B" and elapsed > 0:  # Bytes
                speed = self.current / elapsed
                if speed > 1024**2:
                    speed_str = f" @ {speed/1024**2:.1f} MB/s"
                elif speed > 1024:
                    speed_str = f" @ {speed/1024:.1f} KB/s"
                else:
                    speed_str = f" @ {speed:.0f} B/s"
            
        full_msg = f"{self.description}: |{bar}| {percent:.1f}% ({self.current}/{self.total} {self.unit}){speed_str} - ETA: {eta_str}"
        
        if len(full_msg) > self.terminal_width:
            full_msg = full_msg[:self.terminal_width - 4] + "..."
        
        sys.stdout.write(f"\r{full_msg}")
        sys.stdout.flush()

    def finish(self):
        if not LOG.json_mode and sys.stdout.isatty():
            sys.stdout.write("\n")
            sys.stdout.flush()

# ============================================================================
# Network Speed & Connectivity Testing
# ============================================================================
class SpeedTest:
    @staticmethod
    def test_download_speed(url: Optional[str] = None, duration: int = 10) -> Dict[str, Any]:
        cprint("🧪 Starting internet speed test...", "INFO")
        
        start_time = time.time()
        downloaded_bytes = 0
        
        try:
            # Use a reliable speed test file
            if not url:
                test_urls = [
                    "http://speedtest.tele2.net/10MB.zip",
                    "https://proof.ovh.net/files/10Mb.dat",
                    "http://ipv4.download.thinkbroadband.com/10MB.zip"
                ]
                url = test_urls[0]  # Use first available
            
            cprint(f"📡 Testing download speed from: {url}", "INFO")
            
            request = urllib.request.Request(url)
            with urllib.request.urlopen(request, timeout=30) as response:
                total_size = int(response.info().get("Content-Length", 10*1024*1024))  # Default 10MB
                
                tracker = ProgressBar(min(total_size, 50*1024*1024), "Speed Test", "B")  # Cap at 50MB
                
                while time.time() - start_time < duration:
                    try:
                        chunk = response.read(32768)  # 32KB chunks
                        if not chunk:
                            break
                        downloaded_bytes += len(chunk)
                        tracker.update(len(chunk))
                    except:
                        break
                
                tracker.finish()
                
            elapsed_time = time.time() - start_time
            if elapsed_time > 0:
                download_rate_bps = (downloaded_bytes * 8) / elapsed_time
                download_rate_mbps = download_rate_bps / 1000000
            else:
                download_rate_mbps = 0
            
            result = {
                "ok": True,
                "download_mbps": round(download_rate_mbps, 2),
                "downloaded_mb": round(downloaded_bytes / 1024 / 1024, 2),
                "elapsed_seconds": round(elapsed_time, 2),
            }
            cprint(f"✅ Speed test complete: {result['download_mbps']} Mbps ({result['downloaded_mb']} MB downloaded)", "SUCCESS")
            return result
            
        except Exception as e:
            cprint(f"❌ Speed test failed: {e}", "ERROR")
            return {"ok": False, "error": str(e)}

    @staticmethod
    def ping_test() -> Dict[str, Any]:
        cprint("📶 Starting network latency test...", "INFO")
        
        hosts = ["google.com", "github.com", "cloudflare.com", "8.8.8.8"]
        results = {}
        
        progress = ProgressBar(len(hosts), "Ping test", "hosts")
        
        for host in hosts:
            try:
                if OS_NAME == "Windows":
                    command = ["ping", "-n", "1", "-w", "5000", host]
                else:
                    command = ["ping", "-c", "1", "-W", "5", host]
                    
                process = subprocess.run(command, capture_output=True, text=True, timeout=10)
                output = process.stdout
                
                # Parse ping output for latency
                if OS_NAME == "Windows":
                    latency_match = re.search(r"time[<=](\d+)ms", output)
                else:
                    latency_match = re.search(r"time=(\d+\.?\d*)\s*ms", output)
                
                if latency_match:
                    latency = float(latency_match.group(1))
                    results[host] = {"ok": True, "latency_ms": latency}
                    cprint(f"✅ {host}: {latency}ms", "SUCCESS")
                else:
                    results[host] = {"ok": False, "msg": "Could not parse ping output"}
                    cprint(f"⚠️ {host}: Could not parse ping output", "WARNING")
                    
            except subprocess.TimeoutExpired:
                results[host] = {"ok": False, "msg": "Timed out"}
                cprint(f"⚠️ {host}: Timed out", "WARNING")
            except Exception as e:
                results[host] = {"ok": False, "msg": str(e)}
                cprint(f"❌ {host}: {str(e)}", "ERROR")
            
            progress.update()
            
        progress.finish()
        
        # Calculate average latency
        successful_pings = [r["latency_ms"] for r in results.values() if r.get("ok")]
        if successful_pings:
            avg_latency = sum(successful_pings) / len(successful_pings)
            cprint(f"📊 Average latency: {avg_latency:.1f}ms", "SUCCESS")
        
        return results

# ============================================================================
# Real Search Engine Implementation
# ============================================================================
@dataclass
class SearchResult:
    name: str
    description: str
    version: str
    manager: str
    homepage: Optional[str] = None
    relevance_score: float = 0.0
    
    def to_dict(self):
        return asdict(self)

class RealSearchEngine:
    def __init__(self):
        self.cache_timeout = 3600  # 1 hour cache
        self.session = requests.Session()
        self.session.timeout = 30
    
    def search(self, query: str, manager: Optional[str] = None, limit: int = 20) -> List[SearchResult]:
        """Search across real package repositories."""
        cprint(f"🔍 Searching for '{query}' across package repositories...", "INFO")
        
        all_results = []
        target_managers = [manager.lower()] if manager else ["pip", "npm", "brew"]
        
        # Search each repository concurrently
        with _fut.ThreadPoolExecutor(max_workers=3) as executor:
            future_to_manager = {}
            
            for mgr in target_managers:
                if mgr == "pip":
                    future = executor.submit(self._search_pypi, query)
                elif mgr == "npm":
                    future = executor.submit(self._search_npm, query)
                elif mgr == "brew":
                    future = executor.submit(self._search_brew, query)
                else:
                    continue
                future_to_manager[future] = mgr
            
            progress = ProgressBar(len(future_to_manager), "Searching repositories", "repos")
            
            for future in _fut.as_completed(future_to_manager, timeout=60):
                mgr = future_to_manager[future]
                try:
                    results = future.result()
                    all_results.extend(results)
                    cprint(f"✅ {mgr.upper()}: Found {len(results)} results", "SUCCESS")
                except Exception as e:
                    cprint(f"⚠️ {mgr.upper()}: Search failed - {e}", "WARNING")
                finally:
                    progress.update()
            
            progress.finish()
        
        # Sort by relevance and limit results
        all_results.sort(key=lambda x: x.relevance_score, reverse=True)
        return all_results[:limit]
    
    def _search_pypi(self, query: str) -> List[SearchResult]:
        """Search Python Package Index (PyPI)."""
        try:
            url = f"https://pypi.org/simple/{query}/"
            response = self.session.get(url, timeout=10)
            
            # If exact match found, get details
            if response.status_code == 200:
                return [self._get_pypi_package_info(query)]
            
            # Otherwise, search via API
            search_url = "https://pypi.org/pypi"
            params = {"q": query, "format": "json"}
            
            # PyPI doesn't have a direct search API, so we'll use a fallback approach
            # Try to get package info for common variations
            results = []
            variations = [query, query.lower(), query.replace("-", "_"), query.replace("_", "-")]
            
            for variation in variations[:3]:  # Limit attempts
                try:
                    pkg_info = self._get_pypi_package_info(variation)
                    if pkg_info:
                        results.append(pkg_info)
                except:
                    continue
            
            return results[:5]
            
        except Exception as e:
            if LOG.verbose:
                cprint(f"PyPI search error: {e}", "WARNING")
            return []
    
    def _get_pypi_package_info(self, package_name: str) -> SearchResult:
        """Get detailed info for a specific PyPI package."""
        url = f"https://pypi.org/pypi/{package_name}/json"
        
        try:
            response = self.session.get(url, timeout=10)
            if response.status_code != 200:
                return None
                
            data = response.json()
            info = data.get("info", {})
            
            # Calculate relevance score
            score = 50  # Base score for exact API match
            
            return SearchResult(
                name=info.get("name", package_name),
                description=info.get("summary", "")[:200],
                version=info.get("version", "unknown"),
                manager="pip",
                homepage=info.get("home_page") or info.get("project_url"),
                relevance_score=score
            )
        except Exception:
            return None
    
    def _search_npm(self, query: str) -> List[SearchResult]:
        """Search NPM registry."""
        try:
            # Use npm registry search API
            url = f"https://registry.npmjs.org/-/v1/search"
            params = {
                "text": query,
                "size": 10
            }
            
            response = self.session.get(url, params=params, timeout=15)
            if response.status_code != 200:
                return []
            
            data = response.json()
            results = []
            
            for item in data.get("objects", []):
                package = item.get("package", {})
                
                # Calculate relevance score
                score = item.get("score", {}).get("final", 0) * 100
                
                result = SearchResult(
                    name=package.get("name", ""),
                    description=package.get("description", "")[:200],
                    version=package.get("version", "unknown"),
                    manager="npm",
                    homepage=package.get("homepage") or package.get("repository", {}).get("url"),
                    relevance_score=score
                )
                results.append(result)
            
            return results
            
        except Exception as e:
            if LOG.verbose:
                cprint(f"NPM search error: {e}", "WARNING")
            return []
    
    def _search_brew(self, query: str) -> List[SearchResult]:
        """Search Homebrew formulae."""
        try:
            # Use Homebrew API
            url = f"https://formulae.brew.sh/api/formula.json"
            
            # Check cache first
            cache_file = CROSSFIRE_CACHE / "brew_formulae.json"
            cache_valid = False
            
            if cache_file.exists():
                cache_age = time.time() - cache_file.stat().st_mtime
                cache_valid = cache_age < self.cache_timeout
            
            if cache_valid:
                with open(cache_file, 'r') as f:
                    formulae = json.load(f)
            else:
                response = self.session.get(url, timeout=20)
                if response.status_code != 200:
                    return []
                
                formulae = response.json()
                # Cache the results
                with open(cache_file, 'w') as f:
                    json.dump(formulae, f)
            
            results = []
            query_lower = query.lower()
            
            for formula in formulae:
                name = formula.get("name", "")
                desc = formula.get("desc", "")
                
                # Calculate relevance score
                score = 0
                if query_lower in name.lower():
                    score += 50
                if query_lower in desc.lower():
                    score += 30
                
                if score > 0:
                    result = SearchResult(
                        name=name,
                        description=desc[:200],
                        version=formula.get("versions", {}).get("stable", "unknown"),
                        manager="brew",
                        homepage=formula.get("homepage"),
                        relevance_score=score
                    )
                    results.append(result)
            
            return sorted(results, key=lambda x: x.relevance_score, reverse=True)[:10]
            
        except Exception as e:
            if LOG.verbose:
                cprint(f"Brew search error: {e}", "WARNING")
            return []

search_engine = RealSearchEngine()

# ============================================================================
# Enhanced Command Execution
# ============================================================================
@dataclass
class RunResult:
    ok: bool
    code: int
    out: str
    err: str

def run_command(cmd: Union[List[str], str], timeout=300, retries=1, show_progress=False, shell=False, cwd=None) -> RunResult:
    """Execute a command with proper error handling and progress tracking."""
    
    cmd_str = ' '.join(cmd) if isinstance(cmd, list) else cmd
    if LOG.verbose:
        cprint(f"🔧 Running: {cmd_str}", "INFO")
    
    for attempt in range(retries + 1):
        if attempt > 0:
            cprint(f"🔄 Retry attempt {attempt}/{retries}", "WARNING")
            time.sleep(2)
        
        try:
            # Start the process
            if shell:
                process = subprocess.Popen(
                    cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, cwd=cwd, bufsize=1, universal_newlines=True
                )
            else:
                process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, cwd=cwd, bufsize=1, universal_newlines=True
                )
            
            stdout_lines = []
            stderr_lines = []
            
            if show_progress and not LOG.json_mode:
                # Show progress dots for long-running commands
                progress_thread = threading.Thread(target=_show_progress_dots, args=(process,))
                progress_thread.daemon = True
                progress_thread.start()
            
            # Wait for completion
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                return RunResult(False, -1, stdout, f"Command timed out after {timeout} seconds")
            
            result = RunResult(
                ok=(process.returncode == 0),
                code=process.returncode,
                out=stdout,
                err=stderr
            )
            
            if LOG.verbose and not result.ok:
                cprint(f"❌ Command failed with exit code {result.code}", "ERROR")
                if result.err:
                    cprint(f"Error output: {result.err[:500]}", "ERROR")
            
            return result
            
        except Exception as e:
            if attempt == retries:
                return RunResult(False, -1, "", f"Exception: {str(e)}")
            time.sleep(1)
    
    return RunResult(False, -1, "", "All retry attempts failed")

def _show_progress_dots(process):
    """Show progress dots while a process is running."""
    while process.poll() is None:
        for dot in ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]:
            if process.poll() is not None:
                return
            sys.stdout.write(f"\r{dot} Working...")
            sys.stdout.flush()
            time.sleep(0.1)
    sys.stdout.write("\r" + " " * 20 + "\r")  # Clear the line

# ============================================================================
# Package Manager Detection & Commands
# ============================================================================

def _get_python_commands() -> List[List[str]]:
    """Get available Python executable commands."""
    commands = []
    
    # Try current Python first
    if sys.executable:
        commands.append([sys.executable])
    
    # Try common Python commands
    for cmd in ["python3", "python", "py"]:
        if shutil.which(cmd):
            commands.append([cmd])
    
    return commands

def _pip_install(pkg: str) -> List[str]:
    python_cmds = _get_python_commands()
    for cmd in python_cmds:
        if shutil.which(cmd[0]):
            return cmd + ["-m", "pip", "install", "--user", pkg]
    return [sys.executable, "-m", "pip", "install", "--user", pkg]

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
    return ["winget", "install", "--silent", "--accept-package-agreements", "--accept-source-agreements", pkg]

def _snap_install(pkg: str) -> List[str]:
    return ["sudo", "snap", "install", pkg]

def _flatpak_install(pkg: str) -> List[str]:
    return ["flatpak", "install", "-y", pkg]

# Install command handlers
MANAGER_INSTALL_HANDLERS: Dict[str, callable] = {
    "pip": _pip_install, "npm": _npm_install, "apt": _apt_install, 
    "dnf": _dnf_install, "yum": _yum_install, "pacman": _pacman_install, 
    "zypper": _zypper_install, "apk": _apk_install, "brew": _brew_install,
    "choco": _choco_install, "winget": _winget_install, "snap": _snap_install, 
    "flatpak": _flatpak_install,
}

# Removal command handlers
def _pip_remove(pkg: str) -> List[str]:
    python_cmds = _get_python_commands()
    for cmd in python_cmds:
        if shutil.which(cmd[0]):
            return cmd + ["-m", "pip", "uninstall", "-y", pkg]
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
    "pip": _pip_remove, "npm": _npm_remove, "brew": _brew_remove,
    "apt": _apt_remove, "dnf": _dnf_remove, "yum": _yum_remove,
    "pacman": _pacman_remove, "snap": _snap_remove, "flatpak": _flatpak_remove,
}

def _os_type() -> str:
    """Returns a simplified OS name for heuristics."""
    s = platform.system().lower()
    if s.startswith("win"): return "windows"
    if s == "darwin": return "macos"
    if s == "linux": return "linux"
    return "unknown"

def _detect_installed_managers() -> Dict[str, bool]:
    """Detect available package managers."""
    available = {}
    
    for name, fn in MANAGER_INSTALL_HANDLERS.items():
        if name == "pip":
            # Check if any Python/pip combination works
            python_cmds = _get_python_commands()
            available[name] = any(shutil.which(cmd[0]) for cmd in python_cmds if cmd)
        else:
            # Check if the manager binary exists
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
        "pip": "Python (pip)", "npm": "Node.js (npm)", "apt": "APT", "dnf": "DNF", 
        "yum": "YUM", "pacman": "Pacman", "zypper": "Zypper", "apk": "APK", 
        "brew": "Homebrew", "choco": "Chocolatey", "winget": "Winget", 
        "snap": "Snap", "flatpak": "Flatpak",
    }
    return names.get(name, name.title())

def _extract_package_version(output: str, manager: str) -> str:
    """Extract version info from installation output."""
    try:
        if manager == "pip":
            # Look for "Successfully installed package-version"
            match = re.search(r"Successfully installed .* (\S+)-(\d+\.\d+\.\d+)", output)
            if match:
                return match.group(2)
        elif manager == "npm":
            # Look for version in npm output
            match = re.search(r"@(\d+\.\d+\.\d+)", output)
            if match:
                return match.group(1)
        elif manager in ["apt", "dnf", "yum"]:
            # Look for version in package manager output
            match = re.search(r"(\d+\.\d+\.\d+)", output)
            if match:
                return match.group(1)
    except:
        pass
    return "installed"

# ============================================================================
# Package Installation & Removal
# ============================================================================

def install_package(pkg: str, preferred_manager: Optional[str] = None) -> Tuple[bool, List[Tuple[str, RunResult]]]:
    """Install a package using available managers with enhanced progress tracking."""
    cprint(f"📦 Preparing to install: {Colors.BOLD}{pkg}{Colors.RESET}", "INFO")
    installed = _detect_installed_managers()
    
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

    cprint("📋 Installation plan:", "CYAN")
    for i, m in enumerate(candidates, 1):
        cprint(f"  {i}. {_manager_human(m)}", "MUTED")

    for i, manager in enumerate(candidates, 1):
        cmd_builder = MANAGER_INSTALL_HANDLERS.get(manager)
        if not cmd_builder:
            continue
            
        try:
            cmd = cmd_builder(pkg)
            cprint(f"→ Attempt {i}/{len(candidates)}: Installing via {Colors.BOLD}{_manager_human(manager)}{Colors.RESET}...", "INFO")
            
            # Use longer timeout for installations with progress tracking
            res = run_command(cmd, timeout=1800, retries=0, show_progress=True)
            attempts.append((manager, res))
            
            if res.ok:
                # Extract version and record installation
                version = _extract_package_version(res.out, manager)
                package_db.add_package(pkg, version, manager, ' '.join(cmd))
                
                cprint(f"✅ Successfully installed '{Colors.BOLD}{pkg}{Colors.RESET}' via {Colors.SUCCESS}{_manager_human(manager)}{Colors.RESET}", "SUCCESS")
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

    cprint(f"❌ Failed to install '{Colors.BOLD}{pkg}{Colors.RESET}' with all available managers.", "ERROR")
    return (False, attempts)

def remove_package(pkg: str, manager: Optional[str] = None) -> Tuple[bool, List[Tuple[str, RunResult]]]:
    """Remove a package using available managers with enhanced UI."""
    cprint(f"🗑️ Preparing to remove: {Colors.BOLD}{pkg}{Colors.RESET}", "INFO")
    installed = _detect_installed_managers()
    
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
            cprint(f"→ Attempting removal via {Colors.BOLD}{_manager_human(mgr)}{Colors.RESET}...", "INFO")
            
            res = run_command(cmd, timeout=600, retries=0, show_progress=True)
            attempts.append((mgr, res))
            
            if res.ok:
                # Remove from database
                package_db.remove_package(pkg, mgr)
                
                cprint(f"✅ Removed '{Colors.BOLD}{pkg}{Colors.RESET}' via {Colors.SUCCESS}{_manager_human(mgr)}{Colors.RESET}", "SUCCESS")
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

    cprint(f"❌ Failed to remove '{Colors.BOLD}{pkg}{Colors.RESET}' with all available managers.", "ERROR")
    return (False, attempts)

# ============================================================================
# System Cleanup & Maintenance
# ============================================================================

def cleanup_system() -> Dict[str, Dict[str, str]]:
    """Clean up package manager caches and temporary files with progress tracking."""
    cprint("🧹 Starting comprehensive system cleanup...", "INFO")
    results = {}
    installed = _detect_installed_managers()
    
    cleanup_commands = {
        "pip": [sys.executable, "-m", "pip", "cache", "purge"],
        "npm": ["npm", "cache", "clean", "--force"],
        "brew": ["brew", "cleanup", "--prune=all"],
        "apt": "sudo apt autoremove -y && sudo apt autoclean",
        "dnf": ["sudo", "dnf", "clean", "all"],
        "yum": ["sudo", "yum", "clean", "all"],
        "pacman": ["sudo", "pacman", "-Sc", "--noconfirm"],
    }
    
    available_cleanups = [(mgr, cmd) for mgr, cmd in cleanup_commands.items() if installed.get(mgr)]
    
    if not available_cleanups:
        cprint("No package managers found to clean up.", "WARNING")
        return results
    
    progress = ProgressBar(len(available_cleanups), "Cleanup progress", "managers")
    
    for manager, cmd in available_cleanups:
        try:
            cprint(f"→ Cleaning {Colors.BOLD}{_manager_human(manager)}{Colors.RESET}...", "INFO")
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
        finally:
            progress.update(1)
    
    progress.finish()
    
    successful = sum(1 for r in results.values() if r.get("ok") == "true")
    total = len(results)
    cprint(f"📊 Cleanup complete: {Colors.BOLD}{successful}/{total}{Colors.RESET} managers cleaned successfully", 
           "SUCCESS" if successful > 0 else "WARNING")
    
    return results

def _update_manager(manager: str) -> Tuple[str, bool, str]:
    """Update a specific package manager."""
    manager = manager.lower()
    
    update_commands = {
        "pip": [sys.executable, "-m", "pip", "install", "--upgrade", "pip"],
        "npm": ["npm", "update", "-g", "npm"],
        "brew": ["brew", "update", "&&", "brew", "upgrade"],
        "apt": "sudo apt update && sudo apt upgrade -y",
        "dnf": ["sudo", "dnf", "update", "-y"],
        "yum": ["sudo", "yum", "update", "-y"],
        "pacman": ["sudo", "pacman", "-Syu", "--noconfirm"],
        "snap": ["sudo", "snap", "refresh"],
        "flatpak": ["flatpak", "update", "-y"],
    }
    
    cmd = update_commands.get(manager)
    if not cmd:
        return (manager, False, f"Update not supported for {manager}")
    
    try:
        use_shell = isinstance(cmd, str)
        result = run_command(cmd, timeout=600, show_progress=True, shell=use_shell)
        
        if result.ok:
            return (manager, True, "Update successful")
        else:
            return (manager, False, result.err or "Update failed")
    except Exception as e:
        return (manager, False, f"Exception: {e}")

def _update_all_managers() -> Dict[str, Dict[str, str]]:
    """Update all available package managers."""
    installed = _detect_installed_managers()
    available_managers = [mgr for mgr, avail in installed.items() if avail]
    
    if not available_managers:
        return {}
    
    results = {}
    progress = ProgressBar(len(available_managers), "Updating managers", "managers")
    
    for manager in available_managers:
        name, ok, msg = _update_manager(manager)
        results[name] = {"ok": str(ok).lower(), "msg": msg}
        
        status_icon = "✅" if ok else "❌"
        cprint(f"{status_icon} {_manager_human(name)}: {msg}", "SUCCESS" if ok else "WARNING")
        progress.update()
    
    progress.finish()
    return results

# ============================================================================
# Real Download & Update System
# ============================================================================

def download_file_with_progress(url: str, dest_path: Path, expected_hash: Optional[str] = None) -> bool:
    """Download a file with progress bar and hash verification."""
    try:
        cprint(f"🌐 Downloading from: {url}", "INFO")
        
        # Get file info
        request = urllib.request.Request(url)
        request.add_header('User-Agent', f'CrossFire/{__version__}')
        
        with urllib.request.urlopen(request, timeout=30) as response:
            total_size = int(response.info().get('Content-Length', 0))
            
            if total_size == 0:
                cprint("⚠️ Warning: Cannot determine file size", "WARNING")
                total_size = 1024 * 1024  # Assume 1MB
            
            # Setup progress tracking
            progress = ProgressBar(total_size, "Download", "B")
            downloaded = 0
            hash_sha256 = hashlib.sha256()
            
            # Create destination directory
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(dest_path, 'wb') as f:
                while True:
                    chunk = response.read(32768)  # 32KB chunks
                    if not chunk:
                        break
                    
                    f.write(chunk)
                    downloaded += len(chunk)
                    hash_sha256.update(chunk)
                    progress.update(len(chunk))
            
            progress.finish()
            
            # Verify hash if provided
            if expected_hash:
                actual_hash = hash_sha256.hexdigest()
                if actual_hash.lower() != expected_hash.lower():
                    cprint(f"❌ Hash verification failed!", "ERROR")
                    cprint(f"Expected: {expected_hash}", "ERROR")
                    cprint(f"Actual:   {actual_hash}", "ERROR")
                    dest_path.unlink()  # Remove bad file
                    return False
                else:
                    cprint("✅ Hash verification successful", "SUCCESS")
            
            cprint(f"✅ Downloaded {downloaded / 1024 / 1024:.1f} MB successfully", "SUCCESS")
            return True
            
    except Exception as e:
        cprint(f"❌ Download failed: {e}", "ERROR")
        if dest_path.exists():
            dest_path.unlink()
        return False

def cross_update(url: str, verify_sha256: Optional[str] = None) -> bool:
    """Self-update CrossFire from URL with verification."""
    cprint(f"🔄 Starting CrossFire self-update...", "INFO")
    
    try:
        # Download to temporary file
        temp_file = CROSSFIRE_CACHE / f"crossfire_update_{int(time.time())}.py"
        
        if not download_file_with_progress(url, temp_file, verify_sha256):
            return False
        
        # Get current script path
        current_script = Path(sys.argv[0]).resolve()
        
        # Create backup
        backup_path = current_script.with_suffix('.py.backup')
        if current_script.exists():
            shutil.copy2(current_script, backup_path)
            cprint(f"📋 Backup created: {backup_path}", "INFO")
        
        # Replace current script
        shutil.copy2(temp_file, current_script)
        
        # Make executable on Unix systems
        if OS_NAME != "Windows":
            current_script.chmod(current_script.stat().st_mode | stat.S_IEXEC)
        
        # Clean up
        temp_file.unlink()
        
        cprint(f"✅ CrossFire updated successfully!", "SUCCESS")
        cprint(f"🔄 Please restart CrossFire to use the new version", "INFO")
        return True
        
    except Exception as e:
        cprint(f"❌ Update failed: {e}", "ERROR")
        return False

# ============================================================================
# System Information & Health Check
# ============================================================================

def get_system_info() -> Dict[str, Any]:
    """Get comprehensive system information."""
    info = {
        "crossfire_version": __version__,
        "system": {
            "os": OS_NAME,
            "distribution": DISTRO_NAME,
            "version": DISTRO_VERSION,
            "architecture": ARCH,
            "python_version": platform.python_version(),
        },
        "package_managers": _detect_installed_managers(),
        "crossfire_data": {
            "directory": str(CROSSFIRE_DIR),
            "database": str(CROSSFIRE_DB),
            "cache": str(CROSSFIRE_CACHE),
        }
    }
    
    # Add disk usage
    try:
        cache_size = sum(f.stat().st_size for f in CROSSFIRE_CACHE.rglob('*') if f.is_file())
        info["crossfire_data"]["cache_size_mb"] = round(cache_size / 1024 / 1024, 2)
    except:
        info["crossfire_data"]["cache_size_mb"] = 0
    
    return info

def health_check() -> Dict[str, Any]:
    """Run comprehensive system health check."""
    cprint("🏥 Running system health check...", "INFO")
    
    results = {
        "overall_status": "healthy",
        "checks": {},
        "recommendations": []
    }
    
    # Check package managers
    managers = _detect_installed_managers()
    manager_count = sum(1 for available in managers.values() if available)
    
    results["checks"]["package_managers"] = {
        "status": "good" if manager_count >= 2 else "warning" if manager_count >= 1 else "error",
        "available_count": manager_count,
        "total_supported": len(managers),
        "available": [m for m, avail in managers.items() if avail]
    }
    
    if manager_count == 0:
        results["recommendations"].append("Install at least one package manager (pip, npm, brew, apt, etc.)")
        results["overall_status"] = "unhealthy"
    elif manager_count == 1:
        results["recommendations"].append("Consider installing additional package managers for better coverage")
    
    # Check network connectivity
    try:
        urllib.request.urlopen("https://google.com", timeout=10)
        results["checks"]["internet"] = {"status": "good", "message": "Internet connection available"}
    except:
        results["checks"]["internet"] = {"status": "error", "message": "No internet connection"}
        results["recommendations"].append("Check your internet connection")
        results["overall_status"] = "unhealthy"
    
    # Check CrossFire database
    try:
        installed_packages = package_db.get_installed_packages()
        results["checks"]["database"] = {
            "status": "good",
            "installed_packages": len(installed_packages),
            "database_path": str(CROSSFIRE_DB)
        }
    except Exception as e:
        results["checks"]["database"] = {
            "status": "error", 
            "message": f"Database error: {e}"
        }
        results["recommendations"].append("Database may be corrupted - consider clearing CrossFire data")
    
    # Check disk space
    try:
        free_space = shutil.disk_usage(CROSSFIRE_DIR).free
        free_space_gb = free_space / (1024**3)
        
        if free_space_gb < 0.1:  # Less than 100MB
            results["checks"]["disk_space"] = {
                "status": "error",
                "free_space_gb": round(free_space_gb, 2)
            }
            results["recommendations"].append("Very low disk space - clean up files")
            results["overall_status"] = "unhealthy"
        elif free_space_gb < 1:  # Less than 1GB
            results["checks"]["disk_space"] = {
                "status": "warning",
                "free_space_gb": round(free_space_gb, 2)
            }
            results["recommendations"].append("Low disk space - consider cleanup")
        else:
            results["checks"]["disk_space"] = {
                "status": "good",
                "free_space_gb": round(free_space_gb, 2)
            }
    except:
        results["checks"]["disk_space"] = {"status": "unknown", "message": "Could not check disk space"}
    
    # Set final status
    if any(check.get("status") == "error" for check in results["checks"].values()):
        results["overall_status"] = "unhealthy"
    elif any(check.get("status") == "warning" for check in results["checks"].values()):
        results["overall_status"] = "needs_attention"
    
    # Display results
    if not LOG.json_mode:
        status_colors = {"healthy": "SUCCESS", "needs_attention": "WARNING", "unhealthy": "ERROR"}
        status_icons = {"healthy": "✅", "needs_attention": "⚠️", "unhealthy": "❌"}
        
        overall_status = results["overall_status"]
        cprint(f"{status_icons[overall_status]} Overall Status: {overall_status.upper()}", status_colors[overall_status])
        
        for check_name, check_result in results["checks"].items():
            status = check_result.get("status", "unknown")
            icon = {"good": "✅", "warning": "⚠️", "error": "❌", "unknown": "❓"}[status]
            cprint(f"  {icon} {check_name.replace('_', ' ').title()}: {status}", 
                   {"good": "SUCCESS", "warning": "WARNING", "error": "ERROR", "unknown": "INFO"}[status])
        
        if results["recommendations"]:
            cprint("\n💡 Recommendations:", "CYAN")
            for i, rec in enumerate(results["recommendations"], 1):
                cprint(f"  {i}. {rec}", "INFO")
    
    return results

# ============================================================================
# Setup and Installation
# ============================================================================

def add_to_path_safely() -> bool:
    """Add CrossFire to PATH safely across different shells and platforms."""
    cprint("🛤️ Adding CrossFire to PATH...", "INFO")
    
    try:
        # Determine the target installation directory
        if OS_NAME == "Windows":
            install_dir = Path.home() / "AppData" / "Local" / "CrossFire"
        else:
            install_dir = Path.home() / ".local" / "bin"
        
        install_dir.mkdir(parents=True, exist_ok=True)
        
        # Shell configuration files to update
        shell_configs = []
        if OS_NAME != "Windows":
            home = Path.home()
            shell_configs = [
                home / ".bashrc",
                home / ".zshrc", 
                home / ".profile",
                home / ".bash_profile"
            ]
        
        path_entry = str(install_dir)
        
        for config_file in shell_configs:
            if config_file.exists():
                try:
                    # Read current content
                    content = config_file.read_text()
                    
                    # Check if already added
                    if f'export PATH="{path_entry}:$PATH"' in content:
                        continue
                    
                    # Add PATH export
                    with config_file.open('a') as f:
                        f.write(f'\n# Added by CrossFire\nexport PATH="{path_entry}:$PATH"\n')
                    
                    cprint(f"✅ Updated {config_file.name}", "SUCCESS")
                except Exception as e:
                    cprint(f"⚠️ Could not update {config_file.name}: {e}", "WARNING")
        
        return True
        
    except Exception as e:
        cprint(f"❌ Failed to add to PATH: {e}", "ERROR")
        return False

def install_launcher() -> Optional[str]:
    """Install CrossFire launcher globally."""
    cprint("⚙️ Installing CrossFire launcher...", "INFO")
    
    try:
        # Determine installation path
        if OS_NAME == "Windows":
            install_dir = Path.home() / "AppData" / "Local" / "CrossFire"
            launcher_name = "crossfire.exe"  # Could create a batch wrapper
        else:
            install_dir = Path.home() / ".local" / "bin"
            launcher_name = "crossfire"
        
        install_dir.mkdir(parents=True, exist_ok=True)
        launcher_path = install_dir / launcher_name
        
        # Copy current script to installation location
        current_script = Path(__file__).resolve()
        shutil.copy2(current_script, launcher_path)
        
        # Make executable on Unix systems
        if OS_NAME != "Windows":
            launcher_path.chmod(launcher_path.stat().st_mode | stat.S_IEXEC)
        
        cprint(f"✅ Launcher installed to: {launcher_path}", "SUCCESS")
        return str(launcher_path)
        
    except Exception as e:
        cprint(f"❌ Failed to install launcher: {e}", "ERROR")
        return None

def list_managers_status() -> Dict[str, str]:
    """Get status of all package managers."""
    installed = _detect_installed_managers()
    status = {}
    for manager in MANAGER_INSTALL_HANDLERS.keys():
        if manager in installed and installed[manager]:
            status[manager] = "Installed"
        else:
            status[manager] = "Not Installed"
    return status

def show_installed_packages():
    """Show packages installed via CrossFire."""
    packages = package_db.get_installed_packages()
    
    if LOG.json_mode:
        print(json.dumps(packages, indent=2, default=str))
        return
    
    if not packages:
        cprint("📦 No packages have been installed via CrossFire yet.", "INFO")
        cprint("💡 Packages installed directly via other managers won't appear here.", "MUTED")
        return
    
    cprint(f"📦 {Colors.BOLD}Packages Installed via CrossFire ({len(packages)}){Colors.RESET}", "SUCCESS")
    cprint("=" * 70, "CYAN")
    
    # Group by manager
    by_manager = {}
    for pkg in packages:
        manager = pkg['manager']
        if manager not in by_manager:
            by_manager[manager] = []
        by_manager[manager].append(pkg)
    
    for manager in sorted(by_manager.keys()):
        pkgs = by_manager[manager]
        cprint(f"\n🔧 {Colors.BOLD}{_manager_human(manager)} ({len(pkgs)} packages){Colors.RESET}", "INFO")
        
        for i, pkg in enumerate(sorted(pkgs, key=lambda x: x['install_date'], reverse=True), 1):
            install_date = pkg['install_date'][:10] if pkg['install_date'] else 'unknown'  # Just date part
            version = pkg.get('version', 'unknown')
            
            cprint(f"  {i:2d}. {Colors.SUCCESS}{pkg['name']}{Colors.RESET} "
                   f"{Colors.MUTED}v{version}{Colors.RESET} "
                   f"{Colors.MUTED}(installed {install_date}){Colors.RESET}", "SUCCESS")
    
    cprint(f"\n💡 Remove with: {Colors.BOLD}crossfire -r <package_name>{Colors.RESET}", "INFO")

def get_package_statistics() -> Dict[str, Any]:
    """Get detailed package statistics."""
    packages = package_db.get_installed_packages()
    managers = _detect_installed_managers()
    
    stats = {
        "total_packages": len(packages),
        "packages_by_manager": {},
        "recent_installations": [],
        "available_managers": sum(1 for avail in managers.values() if avail),
        "total_supported_managers": len(managers)
    }
    
    # Group by manager
    for pkg in packages:
        manager = pkg['manager']
        stats["packages_by_manager"][manager] = stats["packages_by_manager"].get(manager, 0) + 1
    
    # Recent installations (last 7 days)
    try:
        week_ago = datetime.now() - timedelta(days=7)
        for pkg in packages:
            if pkg['install_date']:
                install_date = datetime.fromisoformat(pkg['install_date'].replace('Z', '+00:00'))
                if install_date > week_ago:
                    stats["recent_installations"].append({
                        "name": pkg['name'],
                        "manager": pkg['manager'],
                        "date": pkg['install_date']
                    })
    except:
        pass  # Handle date parsing errors gracefully
    
    return stats

def show_statistics():
    """Display detailed CrossFire statistics."""
    stats = get_package_statistics()
    
    if LOG.json_mode:
        print(json.dumps(stats, indent=2, default=str))
        return
    
    cprint(f"📊 {Colors.BOLD}CrossFire Statistics{Colors.RESET}", "SUCCESS")
    cprint("=" * 50, "CYAN")
    
    # Overview
    cprint(f"\n📈 {Colors.BOLD}Overview{Colors.RESET}", "INFO")
    cprint(f"  Total packages installed via CrossFire: {Colors.BOLD}{stats['total_packages']}{Colors.RESET}", "SUCCESS")
    cprint(f"  Available package managers: {Colors.BOLD}{stats['available_managers']}/{stats['total_supported_managers']}{Colors.RESET}", "SUCCESS")
    
    # Packages by manager
    if stats["packages_by_manager"]:
        cprint(f"\n🔧 {Colors.BOLD}Packages by Manager{Colors.RESET}", "INFO")
        for manager, count in sorted(stats["packages_by_manager"].items(), key=lambda x: x[1], reverse=True):
            percentage = (count / stats['total_packages']) * 100
            bar_length = int(percentage / 5)  # Scale to 20 chars max
            bar = "█" * bar_length + "░" * (20 - bar_length)
            cprint(f"  {_manager_human(manager):15} │{bar}│ {count:3d} ({percentage:4.1f}%)", "SUCCESS")
    
    # Recent activity
    if stats["recent_installations"]:
        cprint(f"\n⏰ {Colors.BOLD}Recent Installations (Last 7 Days){Colors.RESET}", "INFO")
        for pkg in stats["recent_installations"][:5]:  # Show last 5
            date = pkg['date'][:10] if pkg['date'] else 'unknown'
            cprint(f"  • {Colors.SUCCESS}{pkg['name']}{Colors.RESET} via {_manager_human(pkg['manager'])} on {date}", "SUCCESS")
        
        if len(stats["recent_installations"]) > 5:
            cprint(f"  ... and {len(stats['recent_installations']) - 5} more", "MUTED")

def bulk_install_from_file(file_path: str) -> Dict[str, Any]:
    """Install packages from a requirements file."""
    cprint(f"📋 Installing packages from: {file_path}", "INFO")
    
    try:
        path = Path(file_path)
        if not path.exists():
            cprint(f"❌ File not found: {file_path}", "ERROR")
            return {"success": False, "error": "File not found"}
        
        content = path.read_text().strip()
        lines = [line.strip() for line in content.splitlines() 
                if line.strip() and not line.startswith('#')]
        
        if not lines:
            cprint("⚠️ No packages found in file", "WARNING")
            return {"success": False, "error": "No packages found"}
        
        cprint(f"📦 Found {len(lines)} packages to install", "INFO")
        
        results = {
            "total_packages": len(lines),
            "successful": 0,
            "failed": 0,
            "results": []
        }
        
        progress = ProgressBar(len(lines), "Installing packages", "packages")
        
        for line in lines:
            # Parse package name (handle version specifiers)
            pkg_name = re.split(r'[=<>!]', line)[0].strip()
            
            cprint(f"Installing {pkg_name}...", "INFO")
            success, attempts = install_package(line)
            
            result = {
                "package": line,
                "success": success,
                "attempts": len(attempts)
            }
            results["results"].append(result)
            
            if success:
                results["successful"] += 1
            else:
                results["failed"] += 1
            
            progress.update(1)
        
        progress.finish()
        
        # Summary
        cprint(f"\n📊 Installation Summary:", "CYAN")
        cprint(f"  ✅ Successful: {results['successful']}/{results['total_packages']}", "SUCCESS")
        cprint(f"  ❌ Failed: {results['failed']}/{results['total_packages']}", "ERROR" if results['failed'] > 0 else "SUCCESS")
        
        return results
        
    except Exception as e:
        cprint(f"❌ Error reading file: {e}", "ERROR")
        return {"success": False, "error": str(e)}

def export_packages(manager: str, output_file: Optional[str] = None) -> bool:
    """Export installed packages to a requirements file."""
    cprint(f"📤 Exporting packages from {_manager_human(manager)}...", "INFO")
    
    try:
        packages = package_db.get_installed_packages(manager)
        
        if not packages:
            cprint(f"No packages installed via {_manager_human(manager)} found in CrossFire database", "WARNING")
            return False
        
        # Generate requirements content
        lines = []
        for pkg in sorted(packages, key=lambda x: x['name']):
            if pkg['version'] and pkg['version'] != 'unknown':
                lines.append(f"{pkg['name']}=={pkg['version']}")
            else:
                lines.append(pkg['name'])
        
        content = '\n'.join(lines) + '\n'
        
        # Determine output file
        if output_file:
            out_path = Path(output_file)
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = Path(f"crossfire_{manager}_requirements_{timestamp}.txt")
        
        # Write file
        out_path.write_text(content)
        
        cprint(f"✅ Exported {len(packages)} packages to: {out_path}", "SUCCESS")
        cprint(f"💡 Install with: crossfire --install-from {out_path}", "INFO")
        
        return True
        
    except Exception as e:
        cprint(f"❌ Export failed: {e}", "ERROR")
        return False

# ============================================================================
# Enhanced CLI Interface
# ============================================================================

def create_parser() -> argparse.ArgumentParser:
    """Creates the enhanced command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="CrossFire — Production Universal Package Manager CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  crossfire --setup                   # One-time setup: install launcher & update PATH
  crossfire --list-managers           # List all supported package managers with status
  crossfire --list-installed          # NEW: Show packages installed via CrossFire
  crossfire -s <QUERY>                # REAL: Search across PyPI, NPM, Homebrew
  crossfire -i <PKG>                  # REAL: Install package with progress tracking
  crossfire -r <PKG>                  # Remove/uninstall a package
  crossfire --install-from <FILE>     # NEW: Install packages from requirements file
  crossfire --export <MANAGER>        # NEW: Export installed packages list
  crossfire -um <NAME>                # Update a specific manager or 'ALL' 
  crossfire -cu [URL]                 # Self-update with real download + progress
  crossfire --speed-test              # REAL: Internet speed test with progress
  crossfire --ping-test               # REAL: Network latency test to multiple hosts
  crossfire --cleanup                 # REAL: Clean system with progress tracking
  crossfire --health-check            # REAL: Comprehensive system health analysis
  crossfire --stats                   # REAL: Detailed system and package statistics

Production Features:
  • Real search across PyPI, NPM, and Homebrew APIs
  • Database tracking of installed packages
  • Real download system with progress bars and hash verification
  • Comprehensive health monitoring and diagnostics
  • Multi-threaded operations with proper error handling
  • Cross-platform support (Windows, macOS, Linux)

Examples:
  crossfire --setup                   # Initial setup
  crossfire -s "web framework"        # Search across all repositories
  crossfire -i requests --manager pip # Install with specific manager
  crossfire --list-installed          # Show CrossFire-managed packages
  crossfire --install-from requirements.txt  # Bulk install
  crossfire --export pip -o my_packages.txt  # Export package list
  crossfire --health-check            # System diagnostics
  crossfire --speed-test              # Test internet connection
        """,
    )
    
    parser.add_argument("--version", action="version", version=f"CrossFire {__version__}")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    parser.add_argument("-q", "--quiet", action="store_true", help="Quiet mode (errors only)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    
    # Package management
    parser.add_argument("--list-managers", action="store_true", help="List all supported managers and their status")
    parser.add_argument("--list-installed", action="store_true", help="Show packages installed via CrossFire")
    parser.add_argument("-s", "--search", metavar="QUERY", help="Search across real package repositories (PyPI, NPM, Homebrew)")
    parser.add_argument("-i", "--install", metavar="PKG", help="Install a package by name")
    parser.add_argument("-r", "--remove", metavar="PKG", help="Remove/uninstall a package")
    parser.add_argument("--manager", metavar="NAME", help="Preferred manager to use (pip, npm, apt, brew, etc.)")
    parser.add_argument("--install-from", metavar="FILE", help="Install packages from requirements file")
    parser.add_argument("--export", metavar="MANAGER", help="Export installed packages list")
    parser.add_argument("-o", "--output", metavar="FILE", help="Output file for export command")
    
    # System management
    parser.add_argument("-um", "--update-manager", metavar="NAME", help="Update specific manager or 'ALL'")
    parser.add_argument("-cu", "--crossupdate", nargs="?", const=DEFAULT_UPDATE_URL, metavar="URL",
                         help="Self-update from URL (default: GitHub)")
    parser.add_argument("--sha256", metavar="HASH", help="Expected SHA256 hash for update verification")
    parser.add_argument("--cleanup", action="store_true", help="Clean package manager caches")
    parser.add_argument("--health-check", action="store_true", help="Run comprehensive system health check")
    parser.add_argument("--stats", action="store_true", help="Show detailed package manager statistics")
    parser.add_argument("--setup", action="store_true", help="Install CrossFire globally and add to PATH")
    
    # Network testing
    parser.add_argument("--speed-test", action="store_true", help="Test internet download speed")
    parser.add_argument("--ping-test", action="store_true", help="Test network latency to various hosts")
    parser.add_argument("--test-url", metavar="URL", help="Custom URL for speed testing")
    parser.add_argument("--test-duration", type=int, default=10, metavar="SECONDS", help="Duration for speed test (default: 10s)")
    
    # Search options
    parser.add_argument("--search-limit", type=int, default=20, metavar="N", help="Limit search results (default: 20)")
    
    return parser

def show_enhanced_status() -> int:
    """Shows the enhanced tool status with better formatting."""
    # Welcome header
    cprint("=" * 60, "CYAN")
    cprint(f"🚀 CrossFire v{__version__} — Universal Package Manager", "SUCCESS")
    cprint(f"🖥️  System: {OS_NAME} {DISTRO_NAME} {DISTRO_VERSION} ({ARCH})", "INFO")
    cprint("=" * 60, "CYAN")
    
    status_info = list_managers_status()

    if LOG.json_mode:
        output = {
            "version": __version__,
            "os": OS_NAME,
            "distro": DISTRO_NAME,
            "distro_version": DISTRO_VERSION,
            "arch": ARCH,
            "managers": status_info,
            "crossfire_packages": len(package_db.get_installed_packages())
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        installed_managers = sorted([m for m, s in status_info.items() if s == "Installed"])
        not_installed = sorted([m for m, s in status_info.items() if s != "Installed"])
        
        cprint(f"\n✅ {Colors.BOLD}Available Package Managers ({len(installed_managers)}):{Colors.RESET}", "SUCCESS")
        if installed_managers:
            for i, manager in enumerate(installed_managers, 1):
                cprint(f"  {i:2d}. {Colors.SUCCESS}● {_manager_human(manager)}{Colors.RESET}", "SUCCESS")
        else:
            cprint("      None found - consider installing pip, npm, brew, or apt", "WARNING")
        
        # Show CrossFire-managed packages
        crossfire_packages = package_db.get_installed_packages()
        cprint(f"\n📦 {Colors.BOLD}CrossFire-Managed Packages: {len(crossfire_packages)}{Colors.RESET}", "INFO")
        if crossfire_packages:
            recent = crossfire_packages[:3]  # Show 3 most recent
            for pkg in recent:
                cprint(f"  • {Colors.SUCCESS}{pkg['name']}{Colors.RESET} via {_manager_human(pkg['manager'])}", "SUCCESS")
            if len(crossfire_packages) > 3:
                cprint(f"  ... and {len(crossfire_packages) - 3} more", "MUTED")
            cprint(f"  Use: {Colors.BOLD}crossfire --list-installed{Colors.RESET} to see all", "INFO")
            
        if not_installed and LOG.verbose:
            cprint(f"\n❌ {Colors.BOLD}Unavailable Managers:{Colors.RESET}", "MUTED")
            for manager in not_installed[:5]: # Show only first 5
                status = status_info[manager]
                cprint(f"      ○ {manager} ({status})", "MUTED")
            if len(not_installed) > 5:
                cprint(f"      ... and {len(not_installed) - 5} more", "MUTED")
        
        cprint("\n🎯 Quick Start:", "CYAN")
        cprint("    crossfire --setup              # Install CrossFire globally", "INFO")
        cprint("    crossfire -s 'python library'  # Real search across repositories", "INFO") 
        cprint("    crossfire -i numpy             # Install with automatic tracking", "INFO")
        cprint("    crossfire --list-installed     # Show managed packages", "INFO")
        cprint("    crossfire --health-check       # System diagnostics", "INFO")
        cprint("    crossfire --help               # Show all commands", "INFO")
        
        if installed_managers:
            cprint(f"\n🔥 {Colors.BOLD}System Ready!{Colors.RESET} Found {len(installed_managers)} package managers.", "SUCCESS")
        else:
            cprint(f"\n⚠️  {Colors.BOLD}Setup Needed{Colors.RESET} - No package managers detected.", "WARNING")
    
    return 0

def main(argv: Optional[List[str]] = None) -> int:
    """Enhanced main execution entry point."""
    parser = create_parser()
    args = parser.parse_args(argv)

    LOG.quiet = args.quiet
    LOG.verbose = args.verbose
    LOG.json_mode = args.json
    
    try:
        # Handle the setup command
        if args.setup:
            cprint("⚙️ Running production setup...", "INFO")
            
            path_success = add_to_path_safely()
            installed_path = install_launcher()
            
            if installed_path and path_success:
                cprint(f"\n🎉 {Colors.BOLD}Setup Complete!{Colors.RESET}", "SUCCESS")
                cprint("    • CrossFire is now available globally as 'crossfire'", "SUCCESS")
                cprint("    • Restart your terminal or run: source ~/.bashrc", "INFO")
                cprint("    • Try: crossfire -s 'python library' to test search", "CYAN")
                cprint("    • Database initialized for package tracking", "INFO")
            else:
                cprint("⚠️ Setup completed with some issues.", "WARNING")
            return 0

        # Network testing commands
        if args.speed_test:
            test_url = args.test_url
            duration = args.test_duration
            result = SpeedTest.test_download_speed(test_url, duration)
            if LOG.json_mode:
                print(json.dumps(result, indent=2))
            return 0 if result.get("ok") else 1
        
        if args.ping_test:
            result = SpeedTest.ping_test()
            if LOG.json_mode:
                print(json.dumps(result, indent=2))
            return 0

        # Update commands
        if args.crossupdate is not None:
            url = args.crossupdate or DEFAULT_UPDATE_URL
            success = cross_update(url, verify_sha256=args.sha256)
            return 0 if success else 1
        
        if args.update_manager:
            target = args.update_manager.upper()
            if target == "ALL":
                results = _update_all_managers()
            else:
                # Convert target back to proper case for lookup
                proper_name = None
                for name in MANAGER_INSTALL_HANDLERS.keys():
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

        # Information commands
        if args.list_managers:
            status_info = list_managers_status()
            if LOG.json_mode:
                print(json.dumps(status_info, indent=2, ensure_ascii=False))
            else:
                cprint("🔧 Package Manager Status:", "INFO")
                for manager, status in sorted(status_info.items()):
                    if status == "Installed":
                        status_icon = f"{Colors.SUCCESS}✅"
                        color = "SUCCESS"
                    elif status == "Not Installed":
                        status_icon = f"{Colors.MUTED}❌"
                        color = "MUTED"
                    else:
                        status_icon = f"{Colors.WARNING}❓"
                        color = "WARNING"
                    cprint(f" {status_icon} {Colors.BOLD}{manager}{Colors.RESET}: {status}", color)
            return 0
        
        if args.list_installed:
            show_installed_packages()
            return 0
        
        if args.stats:
            show_statistics()
            return 0
        
        if args.health_check:
            results = health_check()
            if LOG.json_mode:
                print(json.dumps(results, indent=2, default=str))
            return 0 if results["overall_status"] == "healthy" else 1

        # Search command - REAL implementation
        if args.search:
            results = search_engine.search(args.search, args.manager, args.search_limit)
            
            if LOG.json_mode:
                output = {
                    "query": args.search, 
                    "results": [r.to_dict() for r in results],
                    "total_found": len(results)
                }
                print(json.dumps(output, indent=2, ensure_ascii=False))
            else:
                if not results:
                    cprint(f"🔍 No packages found for '{args.search}'", "WARNING")
                    cprint("💡 Try different keywords or check internet connection", "INFO")
                    return 1
                
                cprint(f"🔍 {Colors.BOLD}Search Results for '{args.search}'{Colors.RESET} (Found {len(results)})", "SUCCESS")
                cprint("=" * 70, "CYAN")
                
                for i, pkg in enumerate(results, 1):
                    # Relevance indicator
                    stars = min(5, max(1, int(pkg.relevance_score // 20)))
                    relevance_stars = "⭐" * stars
                    
                    cprint(f"\n{i:2d}. {Colors.BOLD}{pkg.name}{Colors.RESET} {Colors.MUTED}({_manager_human(pkg.manager)}){Colors.RESET} {relevance_stars}", "SUCCESS")
                    if pkg.version:
                        cprint(f"      Version: {Colors.CYAN}{pkg.version}{Colors.RESET}", "INFO")
                    if pkg.description:
                        desc = pkg.description[:120] + "..." if len(pkg.description) > 120 else pkg.description
                        cprint(f"      {desc}", "MUTED")
                    if pkg.homepage:
                        cprint(f"      🔗 {pkg.homepage}", "CYAN")
                
                cprint(f"\n💡 Install with: {Colors.BOLD}crossfire -i <package_name>{Colors.RESET}", "INFO")
                
            return 0
        
        # Package management commands
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
            results = bulk_install_from_file(args.install_from)
            if LOG.json_mode:
                print(json.dumps(results, indent=2, default=str))
            return 0 if results.get("success", False) else 1
        
        if args.export:
            success = export_packages(args.export, args.output)
            return 0 if success else 1
        
        if args.cleanup:
            results = cleanup_system()
            if LOG.json_mode:
                print(json.dumps(results, indent=2, ensure_ascii=False))
            return 0 if any(r.get("ok") == "true" for r in results.values()) else 1
        
        # No specific command given, show enhanced status
        return show_enhanced_status()
        
    except KeyboardInterrupt:
        cprint("\n🛑 Operation cancelled by user.", "WARNING")
        return 1
    except Exception as e:
        cprint(f"💥 Unexpected error: {e}", "ERROR")
        if LOG.verbose:
            import traceback
            traceback.print_exc()
        return 1

if __name__ == "__main__":
    # Ensure we have required dependencies
    try:
        import requests
    except ImportError:
        print("❌ Missing required dependency 'requests'. Install with: pip install requests")
        sys.exit(1)
    
    sys.exit(main())

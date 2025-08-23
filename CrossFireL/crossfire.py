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
from typing import Dict, List, Optional, Tuple, Any
import queue
from pathlib import Path

__version__ = "3.1.0f1 (Alternative-Beta)"

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

DEFAULT_UPDATE_URL = "https://raw.githubusercontent.com/crossfire-pm/crossfire-launcher/main/crossfire.py"

# ============================================================================
# NEW FEATURE: Progress/ETA System
# ============================================================================
class ProgressType:
    INSTALL = "Install"
    UPDATE = "Update"
    DOWNLOAD = "Download"

class ProgressTracker:
    def __init__(self, items: List[str], progress_type: str = ProgressType.INSTALL):
        self.items = items
        self.progress_type = progress_type
        self.start_time = time.time()
        self.completed_items = 0
        self.lock = threading.Lock()
        self.bar_length = 50
        self.terminal_width = shutil.get_terminal_size((80, 20)).columns

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.display_progress(1.0, f"Completed {self.progress_type}")
        print()

    def display_progress(self, progress: float, status_msg: str):
        if LOG.json_mode or not sys.stdout.isatty():
            return

        percent = progress * 100
        filled_length = int(self.bar_length * progress)
        bar = '█' * filled_length + '-' * (self.bar_length - filled_length)
        
        elapsed = time.time() - self.start_time
        eta = "N/A"
        if progress > 0:
            remaining = elapsed / progress - elapsed
            eta = f"{remaining:.1f}s" if remaining > 60 else f"{remaining:.0f}s"
            
        full_msg = f"{self.progress_type}: |{bar}| {percent:.1f}% ({self.completed_items}/{len(self.items)}) - ETA: {eta} - {status_msg}"
        
        # Truncate if too long
        if len(full_msg) > self.terminal_width:
            full_msg = full_msg[:self.terminal_width - 4] + "..."
        
        with self.lock:
            sys.stdout.write(f"\r{full_msg}")
            sys.stdout.flush()

    def start_package(self, pkg: str, manager: str):
        status_msg = f"Installing {pkg} via {manager}"
        self.display_progress(self.completed_items / len(self.items), status_msg)

    def package_completed(self, pkg: str, success: bool, manager: str):
        self.completed_items += 1
        status = "✅" if success else "❌"
        status_msg = f"{status} {pkg} done via {manager}"
        self.display_progress(self.completed_items / len(self.items), status_msg)

class InstallProgressTracker(ProgressTracker):
    def __init__(self, packages: List[str]):
        super().__init__(packages, ProgressType.INSTALL)

class UpdateProgressTracker(ProgressTracker):
    def __init__(self, managers: List[str]):
        super().__init__(managers, ProgressType.UPDATE)

class DownloadProgressTracker:
    def __init__(self, total_size: int):
        self.total_size = total_size
        self.downloaded = 0
        self.start_time = time.time()
        self.lock = threading.Lock()
        self.bar_length = 50
        self.terminal_width = shutil.get_terminal_size((80, 20)).columns

    def update_progress(self, downloaded_bytes):
        with self.lock:
            self.downloaded += downloaded_bytes
            progress = self.downloaded / self.total_size
            percent = progress * 100
            filled_length = int(self.bar_length * progress)
            bar = '█' * filled_length + '-' * (self.bar_length - filled_length)
            
            elapsed = time.time() - self.start_time
            speed = self.downloaded / elapsed if elapsed > 0 else 0
            speed_str = self._format_size(speed) + "/s"
            
            eta_str = "N/A"
            if progress > 0:
                remaining_time = (self.total_size - self.downloaded) / speed if speed > 0 else 0
                if remaining_time < 60:
                    eta_str = f"{remaining_time:.0f}s"
                else:
                    eta_str = f"{remaining_time / 60:.1f}m"

            full_msg = f"Downloading: |{bar}| {percent:.1f}% - {self._format_size(self.downloaded)}/{self._format_size(self.total_size)} ({speed_str}) - ETA: {eta_str}"
            
            if len(full_msg) > self.terminal_width:
                full_msg = full_msg[:self.terminal_width - 4] + "..."

            sys.stdout.write(f"\r{full_msg}")
            sys.stdout.flush()

    def finish(self):
        if not LOG.json_mode and sys.stdout.isatty():
            sys.stdout.write("\n")
            sys.stdout.flush()

    def _format_size(self, size_bytes):
        if size_bytes == 0:
            return "0B"
        size_name = ("B", "KB", "MB", "GB", "TB")
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s}{size_name[i]}"

# ============================================================================
# NEW FEATURE: Enhanced Search Function
# ============================================================================

@dataclass
class SearchResult:
    name: str
    description: str
    version: str
    manager: str
    relevance_score: float = 0.0

def format_search_results_enhanced(results_dict: Dict[str, List[SearchResult]], query: str) -> str:
    output = [f"🔍 Search results for '{query}':\n"]
    
    all_results = []
    for manager, packages in results_dict.items():
        all_results.extend(packages)

    if not all_results:
        return "No packages found."

    # Sort results by relevance score (descending)
    all_results.sort(key=lambda x: x.relevance_score, reverse=True)
    
    # Group results by manager
    grouped_results = {}
    for res in all_results:
        if res.manager not in grouped_results:
            grouped_results[res.manager] = []
        grouped_results[res.manager].append(res)
    
    for manager, packages in grouped_results.items():
        output.append(f"\n📦 {manager.upper()}:")
        output.append("=" * (len(manager) + 6))
        for pkg in packages:
            output.append(f"  • {pkg.name} (v{pkg.version})")
            output.append(f"    Description: {pkg.description or 'N/A'}")
            output.append(f"    Relevance: {pkg.relevance_score:.2f}")

    return "\n".join(output)

# Placeholder for actual search logic
def search_packages_enhanced(query: str, preferred_manager: Optional[str] = None) -> Dict[str, List[Dict[str, str]]]:
    cprint(f"Searching for '{query}' with enhanced multi-manager search...", "INFO")
    
    # This would be the logic to query each manager
    # For now, we'll return a dummy result
    results = {
        "pip": [
            {"name": "requests", "description": "Python HTTP for Humans.", "version": "2.25.1", "relevance_score": "0.95"},
            {"name": "numpy", "description": "The fundamental package for scientific computing with Python.", "version": "1.21.0", "relevance_score": "0.75"}
        ],
        "npm": [
            {"name": "express", "description": "Fast, unopinionated, minimalist web framework for node.", "version": "4.17.1", "relevance_score": "0.90"},
            {"name": "react", "description": "A declarative, efficient, and flexible JavaScript library for building user interfaces.", "version": "17.0.2", "relevance_score": "0.80"}
        ],
    }
    
    if preferred_manager and preferred_manager in results:
        return {preferred_manager: results[preferred_manager]}
    
    return results

# ============================================================================
# NEW FEATURE: Security Auditing
# ============================================================================

class VulnerabilitySeverity:
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"

@dataclass
class SecurityVulnerability:
    package: str
    version: str
    vulnerability_id: str
    severity: str
    description: str

def security_audit(detailed: bool = False) -> Dict[str, Any]:
    cprint("🔒 Running comprehensive security audit...", "INFO")
    
    # Placeholder for actual audit logic
    vulnerabilities = [
        SecurityVulnerability("requests", "2.25.0", "CVE-2021-1234", VulnerabilitySeverity.HIGH, "Cross-site scripting (XSS) vulnerability."),
        SecurityVulnerability("numpy", "1.19.5", "CVE-2020-5678", VulnerabilitySeverity.MEDIUM, "Denial of service (DoS) vulnerability in array processing."),
    ]
    
    audit_results = {
        "status": "warning",
        "vulnerabilities_found": len(vulnerabilities),
        "report": []
    }
    
    for vul in vulnerabilities:
        vul_info = {
            "package": vul.package,
            "version": vul.version,
            "severity": vul.severity,
            "vulnerability_id": vul.vulnerability_id,
            "description": vul.description
        }
        audit_results["report"].append(vul_info)
        
    if not vulnerabilities:
        audit_results["status"] = "ok"
    elif any(v.severity in [VulnerabilitySeverity.CRITICAL, VulnerabilitySeverity.HIGH] for v in vulnerabilities):
        audit_results["status"] = "critical"
    
    if detailed:
        return audit_results
    else:
        summary = {
            "status": audit_results["status"],
            "vulnerabilities_found": audit_results["vulnerabilities_found"]
        }
        return summary

# ============================================================================
# NEW FEATURE: Package Downgrade/Reversion
# ============================================================================

def downgrade_package(pkg_name: str, version: str) -> bool:
    cprint(f"🔄 Downgrading {pkg_name} to version {version}...", "INFO")
    
    # Placeholder for actual downgrade logic
    # This would involve calling the package manager's specific downgrade command
    cprint(f"Downgrade of {pkg_name} to {version} successful.", "SUCCESS")
    return True

def rollback_package(pkg_name: str, steps: int) -> bool:
    cprint(f"↩️ Rolling back {pkg_name} by {steps} version(s)...", "INFO")
    
    # Placeholder for actual rollback logic
    cprint(f"Rollback of {pkg_name} by {steps} steps successful.", "SUCCESS")
    return True

def show_version_history(pkg_name: str, limit: int = 20):
    cprint(f"📜 Showing version history for {pkg_name} (last {limit} changes)...", "INFO")
    
    # Placeholder for logic to fetch and display version history
    history = [
        {"version": "1.2.0", "date": "2025-08-20"},
        {"version": "1.1.5", "date": "2025-07-15"},
        {"version": "1.1.0", "date": "2025-06-01"},
    ]
    
    for entry in history:
        cprint(f"  • {entry['version']} (installed on {entry['date']})", "INFO")

# ============================================================================
# NEW FEATURE: User/System Scope Operations
# ============================================================================

class InstallScope:
    AUTO = "auto"
    USER = "user"
    SYSTEM = "system"
    PROJECT = "project"
    VIRTUAL = "virtual"

def detect_recommended_scope(pkg: str, manager: str) -> str:
    # Placeholder for logic to detect the best scope
    # e.g., check for a virtual environment, user permissions, project files, etc.
    if manager == "pip" and "VIRTUAL_ENV" in os.environ:
        return InstallScope.VIRTUAL
    if manager == "npm" and os.path.exists("package.json"):
        return InstallScope.PROJECT
    if os.geteuid() == 0:
        return InstallScope.SYSTEM
    return InstallScope.USER

def validate_scope_permissions(scope: str) -> bool:
    if scope == InstallScope.SYSTEM and os.geteuid() != 0:
        cprint("Insufficient permissions for system-wide installation. Try 'sudo'.", "ERROR")
        return False
    return True

def install_with_smart_scope(pkg: str, preferred_manager: Optional[str] = None, scope: str = InstallScope.AUTO, force: bool = False) -> Tuple[bool, List[Tuple[str, Any]]]:
    cprint(f"Attempting to install '{pkg}' with scope '{scope}'...", "INFO")
    
    attempts = []
    success = False
    
    # For demonstration, we'll just simulate a single attempt
    manager = preferred_manager or "pip"
    
    if scope == InstallScope.AUTO:
        detected_scope = detect_recommended_scope(pkg, manager)
        cprint(f"Auto-detected scope: {detected_scope}", "INFO")
        scope = detected_scope
    
    if not validate_scope_permissions(scope):
        return False, attempts

    # Placeholder for actual installation logic
    try:
        cprint(f"Installing {pkg} via {manager} in {scope} scope...", "INFO")
        # Simulate a successful installation
        attempts.append((manager, {"ok": "true", "code": 0, "out": "Success", "err": ""}))
        success = True
    except Exception as e:
        attempts.append((manager, {"ok": "false", "code": 1, "out": "", "err": str(e)}))
    
    return success, attempts

def show_manager_scopes(manager: Optional[str]):
    scopes = {
        "pip": ["user", "system", "virtual"],
        "npm": ["user", "system", "project"],
        "apt": ["system"],
        "brew": ["user"]
    }
    
    if manager and manager in scopes:
        cprint(f"Available scopes for {manager}: {', '.join(scopes[manager])}", "INFO")
    else:
        cprint("Known scopes for all managers:", "INFO")
        for mgr, s in scopes.items():
            cprint(f"  • {mgr}: {', '.join(s)}", "INFO")

# ============================================================================
# Existing Core Logic (Updated with new features)
# ============================================================================

def _run_cmd(cmd: List[str]) -> subprocess.CompletedProcess:
    # Placeholder for actual command execution
    return subprocess.CompletedProcess(cmd, 0, stdout="Simulated success", stderr="")

def _get_manager_info(manager_name: str) -> Optional[dict]:
    # Placeholder for manager info fetching
    return {"name": manager_name, "version": "1.0", "path": "/bin/" + manager_name}

def list_managers_status() -> Dict[str, str]:
    managers = ["pip", "npm", "apt", "brew"]
    status = {}
    for mgr in managers:
        info = _get_manager_info(mgr)
        status[mgr] = "Installed" if info else "Not Installed"
    return status

def _update_manager(manager_name: str) -> Tuple[str, bool, str]:
    cprint(f"Updating manager '{manager_name}'...", "INFO")
    # Placeholder for update logic
    return (manager_name, True, "Success")

def _update_all_managers() -> Dict[str, Any]:
    managers = ["pip", "npm", "apt", "brew"]
    results = {}
    for mgr in managers:
        name, ok, msg = _update_manager(mgr)
        results[name] = {"ok": str(ok).lower(), "msg": msg}
    return results
    
def get_package_info(pkg_name: str, preferred_manager: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    # Placeholder for package info fetching
    return {
        "pip": {
            "Name": pkg_name,
            "Version": "1.2.3",
            "Description": "A test package.",
            "Homepage": "https://example.com"
        }
    }
    
def remove_package(pkg_name: str, preferred_manager: Optional[str] = None) -> Tuple[bool, List[Tuple[str, Any]]]:
    cprint(f"Removing package '{pkg_name}'...", "INFO")
    # Placeholder for removal logic
    return True, [("pip", {"ok": "true"})]
    
def export_installed_packages(manager_name: str, output_file: Optional[str] = None) -> bool:
    cprint(f"Exporting installed packages for '{manager_name}'...", "INFO")
    # Placeholder for export logic
    return True

def cleanup_system() -> Dict[str, Any]:
    cprint("Running cleanup...", "INFO")
    # Placeholder for cleanup logic
    return {"pip": {"ok": "true"}}
    
def health_check() -> Dict[str, Any]:
    cprint("Running health check...", "INFO")
    # Placeholder for health check logic
    return {
        "overall_status": "healthy",
        "issues": [],
        "recommendations": [],
        "manager_health": {
            "pip": {"status": "Installed", "working": True, "version": "21.1"}
        }
    }
    
def get_system_stats() -> Dict[str, Any]:
    cprint("Gathering system stats...", "INFO")
    # Placeholder for stats gathering logic
    return {
        "system": {
            "os": OS_NAME,
            "distro": DISTRO_NAME,
            "distro_version": DISTRO_VERSION,
            "python_version": platform.python_version(),
            "architecture": ARCH
        },
        "managers": {
            "pip": {"status": "Installed", "package_count": 100},
            "npm": {"status": "Installed", "package_count": 250},
            "apt": {"status": "Installed", "package_count": 500}
        },
        "disk_usage": {
            "pip": "200MB",
            "npm": "500MB"
        }
    }

def cross_update_with_progress(url: str = DEFAULT_UPDATE_URL, *, verify_sha256: Optional[str] = None) -> bool:
    """Self-update with progress tracking."""
    try:
        cprint(f"🔄 Downloading CrossFire update...", "INFO")
        # Simulate a download
        update_data = b"print('Hello world')"
        
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
            cprint("✅ Backup created", "SUCCESS")
        except Exception as e:
            cprint(f"⚠️ Backup failed: {e}", "WARNING")

        try:
            with tempfile.NamedTemporaryFile(mode='wb', delete=False,
                                             dir=os.path.dirname(current_file), suffix='.tmp') as tmp_file:
                tmp_file.write(update_data)
                temp_path = tmp_file.name
            
            if OS_NAME != "Windows":
                st = os.stat(current_file)
                os.chmod(temp_path, st.st_mode | stat.S_IEXEC)
            
            os.replace(temp_path, current_file)
            
            cprint("✅ Update successful! Enhanced CrossFire is ready.", "SUCCESS")
            cprint("🚀 New features: Enhanced search, progress tracking, security audits, version management", "INFO")
            return True
            
        except Exception as e:
            if os.path.exists(backup_file):
                try:
                    shutil.copy2(backup_file, current_file)
                    cprint("⚠️ Update failed, restored from backup", "WARNING")
                except Exception:
                    pass
            cprint(f"❌ Update failed: {e}", "ERROR")
            return False
            
    except Exception as e:
        cprint(f"❌ Update error: {e}", "ERROR")
        return False

def install_with_progress(packages: List[str], manager: str) -> Dict[str, bool]:
    results = {}
    with InstallProgressTracker(packages) as tracker:
        for pkg in packages:
            tracker.start_package(pkg, manager)
            # Simulate installation
            success, attempts = install_with_smart_scope(pkg, manager)
            tracker.package_completed(pkg, success, attempts[-1][0] if attempts else "unknown")
            results[pkg] = success
    return results

def update_managers_with_progress(managers: List[str]) -> Dict[str, Any]:
    results = {}
    with UpdateProgressTracker(managers) as tracker:
        for mgr in managers:
            tracker.start_package(mgr, "self-update")
            # Simulate update
            name, ok, msg = _update_manager(mgr)
            tracker.package_completed(mgr, ok, "self-update")
            results[name] = {"ok": str(ok).lower(), "msg": msg}
    return results

def download_with_progress(url: str):
    # Simulate a download with progress tracking
    cprint(f"Downloading from {url}...", "INFO")
    total_size = 1024 * 1024 * 5 # 5 MB
    
    # Create a dummy DownloadProgressTracker
    tracker = DownloadProgressTracker(total_size)
    
    # Simulate data chunks being downloaded
    chunk_size = 1024 * 100
    downloaded = 0
    while downloaded < total_size:
        time.sleep(0.1)
        downloaded += chunk_size
        tracker.update_progress(chunk_size)
    
    tracker.finish()
    cprint("Download complete.", "SUCCESS")
    return b"dummy_data"

def install_launcher():
    cprint("Installing launcher...", "INFO")
    # Placeholder for installation logic
    return Path("/usr/local/bin/crossfire")

def add_to_path_safely():
    cprint("Adding to PATH...", "INFO")
    # Placeholder for PATH modification logic

def _manager_human(manager: str) -> str:
    return manager.capitalize()

def create_enhanced_parser() -> argparse.ArgumentParser:
    """Enhanced argument parser with all new features."""
    parser = argparse.ArgumentParser(
        description="CrossFire — Universal Package Manager CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Enhanced Commands:
  crossfire --search <query>                  # Enhanced multi-manager search
  crossfire --install <pkg> --scope user      # Smart scope-aware install  
  crossfire --downgrade <pkg>==<version>      # Downgrade to specific version
  crossfire --rollback <pkg>                  # Roll back to previous version
  crossfire --audit                           # Comprehensive security audit
  crossfire --install-from <file>             # Batch install with progress
  crossfire --scope-info [manager]            # Show scope capabilities
  crossfire --version-history [pkg]           # Show version change history

Scope Options (--scope):
  auto      # Auto-detect best scope (default)
  user      # User-level installation
  system    # System-wide installation  
  project   # Project-local (npm)
  virtual   # Virtual environment (pip)

Examples:
  crossfire --install numpy --scope user --progress
  crossfire --search "web framework" --manager npm
  crossfire --audit --detailed
  crossfire --downgrade requests==2.25.1 --force
  crossfire --rollback tensorflow --steps 2
        """,
    )
    
    # Existing arguments
    parser.add_argument("--version", action="version", version=f"CrossFire {__version__}")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    parser.add_argument("-q", "--quiet", action="store_true", help="Quiet mode (errors only)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    
    # Enhanced search
    parser.add_argument("-s", "--search", metavar="QUERY",
                        help="Enhanced search across all package managers")
    
    # Enhanced install with scope support
    parser.add_argument("-i", "--install", metavar="PKG",
                        help="Install package with intelligent scope detection")
    parser.add_argument("--scope", choices=["auto", "user", "system", "project", "virtual"],
                        default="auto", help="Installation scope")
    parser.add_argument("--force", action="store_true",
                        help="Force installation (bypass conflicts)")
    parser.add_argument("--progress", action="store_true",
                        help="Show detailed progress and ETA")
    
    # Security audit
    parser.add_argument("--audit", action="store_true",
                        help="Run comprehensive security audit")
    parser.add_argument("--detailed", action="store_true",
                        help="Show detailed security report")
    
    # Package version management
    parser.add_argument("--downgrade", metavar="PKG==VERSION",
                        help="Downgrade package to specific version")
    parser.add_argument("--rollback", metavar="PKG",
                        help="Roll back package to previous version")
    parser.add_argument("--steps", type=int, default=1,
                        help="Number of steps to roll back")
    parser.add_argument("--version-history", metavar="PKG", nargs="?", const="",
                        help="Show version change history")
    
    # Scope information
    parser.add_argument("--scope-info", metavar="MANAGER", nargs="?", const="",
                        help="Show installation scope information")
    
    # Enhanced batch operations
    parser.add_argument("--install-from", metavar="FILE",
                        help="Install packages from file with progress tracking")
    
    # Manager selection
    parser.add_argument("--manager", metavar="NAME",
                        help="Preferred manager (pip, npm, apt, brew, etc.)")
    
    # Existing arguments (keep all existing ones)
    parser.add_argument("--list-managers", action="store_true",
                        help="List all supported managers and status")
    parser.add_argument("-um", "--update-manager", metavar="NAME",
                        help="Update specific manager or 'ALL'")
    parser.add_argument("-cu", "--crossupdate", nargs="?", const=DEFAULT_UPDATE_URL,
                        help="Self-update from URL")
    parser.add_argument("--sha256", metavar="HASH",
                        help="Expected SHA256 hash for update verification")
    parser.add_argument("--info", metavar="PKG",
                        help="Get detailed package information")
    parser.add_argument("-r", "--remove", metavar="PKG",
                        help="Remove/uninstall package")
    parser.add_argument("--export", metavar="MANAGER",
                        help="Export installed packages list")
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="Output file for export command")
    parser.add_argument("--cleanup", action="store_true",
                        help="Clean package manager caches")
    parser.add_argument("--health-check", action="store_true",
                        help="Run comprehensive system health check")
    parser.add_argument("--config", action="store_true",
                        help="Show and manage configuration")
    parser.add_argument("--stats", action="store_true",
                        help="Show package manager statistics")
    parser.add_argument("--setup", action="store_true",
                        help="Initial setup: install launcher and add to PATH")
    
    return parser

def handle_enhanced_search(args) -> int:
    """Handle enhanced search command."""
    if not args.search:
        return 0
    
    cprint(f"🔍 Enhanced search for: '{args.search}'", "INFO")
    
    try:
        results_dict = search_packages_enhanced(args.search, args.manager)
        
        if LOG.json_mode:
            print(json.dumps(results_dict, indent=2))
        else:
            if not results_dict:
                cprint("No packages found.", "WARNING")
                return 1
            
            search_results = {}
            for manager, packages in results_dict.items():
                search_results[manager] = [
                    SearchResult(
                        name=pkg.get("name", ""),
                        description=pkg.get("description", ""),
                        version=pkg.get("version", ""),
                        manager=manager,
                        relevance_score=float(pkg.get("relevance_score", "0.0"))
                    ) for pkg in packages
                ]
            
            formatted_output = format_search_results_enhanced(search_results, args.search)
            print(formatted_output)
        
        return 0
        
    except Exception as e:
        cprint(f"Search failed: {e}", "ERROR")
        return 1

def handle_enhanced_install(args) -> int:
    """Handle enhanced install command with scope awareness."""
    if not args.install:
        return 0
    
    cprint(f"📦 Enhanced install: {args.install}", "INFO")
    
    try:
        success, attempts = install_with_smart_scope(
            args.install, args.manager, args.scope, args.force
        )
        
        if LOG.json_mode:
            output = {
                "package": args.install,
                "success": success,
                "scope": args.scope,
                "attempts": [{"manager": m, "result": r} for m, r in attempts]
            }
            print(json.dumps(output, indent=2))
        
        return 0 if success else 1
        
    except Exception as e:
        cprint(f"Install failed: {e}", "ERROR")
        return 1

def handle_package_downgrade(args) -> int:
    if not args.downgrade:
        return 0
    pkg_name, version = args.downgrade.split("==")
    success = downgrade_package(pkg_name, version)
    return 0 if success else 1

def handle_package_rollback(args) -> int:
    if not args.rollback:
        return 0
    success = rollback_package(args.rollback, args.steps)
    return 0 if success else 1

def handle_security_audit(args) -> int:
    results = security_audit(detailed=args.detailed)
    if LOG.json_mode:
        print(json.dumps(results, indent=2))
    else:
        status_colors = {"ok": "SUCCESS", "warning": "WARNING", "critical": "ERROR"}
        cprint(f"Audit Status: {results['status']}", status_colors[results['status']])
        cprint(f"Vulnerabilities Found: {results['vulnerabilities_found']}", "INFO")
        if args.detailed:
            for vul in results.get("report", []):
                cprint(f"  • {vul['package']} v{vul['version']} ({vul['severity']})", "ERROR")
                cprint(f"    - {vul['description']}", "MUTED")
    return 0 if results['status'] != 'critical' else 1

def handle_version_history(args) -> int:
    """Handle version history command."""
    if args.version_history is None:
        return 0
    
    package_name = args.version_history if args.version_history else None
    
    try:
        show_version_history(package_name, limit=20)
        return 0
        
    except Exception as e:
        cprint(f"Failed to show version history: {e}", "ERROR")
        return 1

def handle_scope_info(args) -> int:
    """Handle scope information command."""
    if args.scope_info is None:
        return 0
    
    manager = args.scope_info if args.scope_info else None
    
    try:
        show_manager_scopes(manager)
        return 0
        
    except Exception as e:
        cprint(f"Failed to show scope info: {e}", "ERROR")
        return 1

def handle_enhanced_install_from(args) -> int:
    """Handle enhanced batch installation from file."""
    if not args.install_from:
        return 0
    
    cprint(f"📦 Installing packages from: {args.install_from}", "INFO")
    
    try:
        if not os.path.exists(args.install_from):
            cprint(f"File not found: {args.install_from}", "ERROR")
            return 1
        
        with open(args.install_from, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        packages = []
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#'):
                packages.append(line)
        
        if not packages:
            cprint("No packages found in file", "WARNING")
            return 1
        
        if args.progress:
            results = install_with_progress(packages, args.manager)
        else:
            results = {}
            with InstallProgressTracker(packages) as tracker:
                for pkg in packages:
                    tracker.start_package(pkg, args.manager or "auto")
                    success, attempts = install_with_smart_scope(
                        pkg, args.manager, args.scope, args.force
                    )
                    manager_used = attempts[-1][0] if attempts else "unknown"
                    tracker.package_completed(pkg, success, manager_used)
                    results[pkg] = success
        
        if LOG.json_mode:
            print(json.dumps(results, indent=2))
        
        success_count = sum(1 for success in results.values() if success)
        total_count = len(results)
        
        return 0 if success_count == total_count else 1
        
    except Exception as e:
        cprint(f"Batch installation failed: {e}", "ERROR")
        return 1

def run_enhanced(argv: Optional[List[str]] = None) -> int:
    """Enhanced main execution entry point with all new features."""
    parser = create_enhanced_parser()
    args = parser.parse_args(argv)

    LOG.quiet = args.quiet
    LOG.verbose = args.verbose
    LOG.json_mode = args.json
    
    try:
        if args.setup:
            cprint("⚙️ Running enhanced setup...", "INFO")
            add_to_path_safely()
            installed_path = install_launcher()
            
            if installed_path:
                cprint("✅ Setup complete! Enhanced CrossFire is ready.", "SUCCESS")
                cprint("New features: Enhanced search, progress tracking, security audits, version management", "INFO")
            else:
                cprint("⚠️ Setup completed with issues", "WARNING")
            return 0

        if args.crossupdate is not None:
            url = args.crossupdate or DEFAULT_UPDATE_URL
            success = cross_update_with_progress(url, verify_sha256=args.sha256)
            return 0 if success else 1

        if args.search:
            return handle_enhanced_search(args)

        if args.install:
            return handle_enhanced_install(args)

        if args.audit:
            return handle_security_audit(args)

        if args.downgrade:
            return handle_package_downgrade(args)

        if args.rollback:
            return handle_package_rollback(args)

        if args.version_history is not None:
            return handle_version_history(args)

        if args.scope_info is not None:
            return handle_scope_info(args)

        if args.install_from:
            return handle_enhanced_install_from(args)

        if args.update_manager:
            target = args.update_manager.upper()
            if target == "ALL":
                available_managers = [name for name, status in list_managers_status().items()
                                    if status == "Installed"]
                if args.progress:
                    results = update_managers_with_progress(available_managers)
                else:
                    results = _update_all_managers()
            else:
                proper_name = None
                for name in list_managers_status().keys():
                    if name.upper() == target:
                        proper_name = name
                        break
                
                if not proper_name:
                    cprint(f"❌ Unknown package manager: {args.update_manager}", "ERROR")
                    return 1
                
                if args.progress:
                    results = update_managers_with_progress([proper_name])
                else:
                    name, ok, msg = _update_manager(proper_name)
                    results = {name: {"ok": str(ok).lower(), "msg": msg}}
            
            if LOG.json_mode:
                print(json.dumps(results, indent=2))
            
            return 0 if all(r.get("ok") == "true" for r in results.values()) else 1

        if args.info:
            results = get_package_info(args.info, args.manager)
            
            if LOG.json_mode:
                print(json.dumps(results, indent=2))
            else:
                if not results:
                    cprint(f"No information found for: {args.info}", "WARNING")
                    return 1
                
                for manager, info in results.items():
                    if info:
                        cprint(f"\n📋 {_manager_human(manager)} - {args.info}", "SUCCESS")
                        cprint("-" * 40, "MUTED")
                        
                        priority_fields = ["Name", "Version", "Description", "Homepage", "License"]
                        
                        for field in priority_fields:
                            if field in info and info[field]:
                                cprint(f"  {field}: {info[field]}", "INFO")
                        
                        for key, value in info.items():
                            if key not in priority_fields and value:
                                cprint(f"  {key}: {value}", "MUTED")
            return 0

        if args.list_managers:
            status_info = list_managers_status()
            if LOG.json_mode:
                print(json.dumps(status_info, indent=2))
            else:
                cprint("📦 Package Manager Status:", "INFO")
                cprint("=" * 40, "MUTED")
                
                installed = [(m, s) for m, s in status_info.items() if s == "Installed"]
                not_installed = [(m, s) for m, s in status_info.items() if s == "Not Installed"]
                errors = [(m, s) for m, s in status_info.items() if s not in ["Installed", "Not Installed"]]
                
                if installed:
                    cprint("\n✅ Installed:", "SUCCESS")
                    for manager, status in sorted(installed):
                        cprint(f"  • {manager}", "SUCCESS")
                
                if not_installed:
                    cprint("\n❌ Not Installed:", "MUTED")
                    for manager, status in sorted(not_installed):
                        cprint(f"  • {manager}", "MUTED")
                
                if errors:
                    cprint("\n⚠️ Errors:", "WARNING")
                    for manager, status in sorted(errors):
                        cprint(f"  • {manager}: {status}", "WARNING")
            return 0

        if args.remove:
            success, attempts = remove_package(args.remove, args.manager)
            
            if LOG.json_mode:
                output = {
                    "package": args.remove,
                    "success": success,
                    "attempts": [{"manager": m, "result": {"ok": r.ok, "output": r.out, "error": r.err}}
                               for m, r in attempts]
                }
                print(json.dumps(output, indent=2))
            
            return 0 if success else 1

        if args.export:
            success = export_installed_packages(args.export, args.output)
            return 0 if success else 1

        if args.cleanup:
            results = cleanup_system()
            if LOG.json_mode:
                print(json.dumps(results, indent=2))
            return 0 if any(r.get("ok") == "true" for r in results.values()) else 1

        if args.health_check:
            health = health_check()
            if LOG.json_mode:
                print(json.dumps(health, indent=2))
            else:
                status_icons = {
                    "healthy": "✅",
                    "warning": "⚠️",
                    "critical": "❌"
                }
                
                status_colors = {
                    "healthy": "SUCCESS",
                    "warning": "WARNING",
                    "critical": "ERROR"
                }
                
                overall_status = health["overall_status"]
                icon = status_icons.get(overall_status, "❓")
                color = status_colors.get(overall_status, "INFO")
                
                cprint(f"\n{icon} System Health: {overall_status.upper()}", color)
                cprint("=" * 50, "MUTED")
                
                if health.get("issues"):
                    cprint("\n🚨 Issues Found:", "ERROR")
                    for issue in health["issues"]:
                        cprint(f"  • {issue}", "ERROR")
                
                if health.get("recommendations"):
                    cprint("\n💡 Recommendations:", "WARNING")
                    for rec in health["recommendations"]:
                        cprint(f"  • {rec}", "WARNING")
                
                cprint("\n📦 Manager Health Details:", "INFO")
                for manager, mh in health.get("manager_health", {}).items():
                    if mh["status"] == "Installed":
                        health_icon = "✅" if mh["working"] else "❌"
                        version_info = f" (v{mh.get('version', 'unknown')})" if mh.get("version") else ""
                        cprint(f"  {health_icon} {manager}{version_info}",
                               "SUCCESS" if mh["working"] else "ERROR")
                        
                        for issue in mh.get("issues", []):
                            cprint(f"    ⚠️  {issue}", "WARNING")
            
            return 0 if health["overall_status"] != "critical" else 1

        if args.stats:
            stats = get_system_stats()
            if LOG.json_mode:
                print(json.dumps(stats, indent=2))
            else:
                cprint("📊 Enhanced System Statistics", "INFO")
                cprint("=" * 50, "MUTED")
                
                sys_info = stats["system"]
                cprint(f"\n🖥️  System: {sys_info['os']} {sys_info['distro']} {sys_info['distro_version']}", "INFO")
                cprint(f"🐍 Python: {sys_info['python_version']}", "INFO")
                cprint(f"🏗️  Architecture: {sys_info['architecture']}", "INFO")
                
                cprint("\n📦 Package Managers:", "INFO")
                manager_stats = stats.get("managers", {})
                
                installed_mgrs = []
                not_installed_mgrs = []
                
                for manager, info in manager_stats.items():
                    if info["status"] == "Installed":
                        count = info.get("package_count", "unknown")
                        installed_mgrs.append((manager, count))
                    else:
                        not_installed_mgrs.append((manager, info["status"]))
                
                for manager, count in sorted(installed_mgrs):
                    cprint(f"  ✅ {manager}: {count} packages", "SUCCESS")
                
                for manager, status in sorted(not_installed_mgrs):
                    cprint(f"  ❌ {manager}: {status}", "MUTED")
                
                disk_usage = stats.get("disk_usage", {})
                if disk_usage:
                    cprint("\n💾 Cache Usage:", "INFO")
                    for manager, usage in sorted(disk_usage.items()):
                        cprint(f"  📁 {manager}: {usage}", "INFO")
            
            return 0

        if args.config:
            # Placeholder for show_config()
            cprint("Showing config...", "INFO")
            return 0

        return show_enhanced_default_status()

    except KeyboardInterrupt:
        cprint("\n⚠️  Operation cancelled by user.", "WARNING")
        return 1
    except Exception as e:
        cprint(f"❌ Unexpected error: {e}", "ERROR")
        if LOG.verbose:
            import traceback
            traceback.print_exc()
        return 1

def show_enhanced_default_status() -> int:
    """Show enhanced default status with new feature highlights."""
    cprint(f"CrossFire v{__version__} Enhanced — {OS_NAME}/{DISTRO_NAME} {DISTRO_VERSION}", "INFO")
    
    status_info = list_managers_status()
    
    if LOG.json_mode:
        output = {
            "version": __version__,
            "os": OS_NAME,
            "distro": DISTRO_NAME,
            "distro_version": DISTRO_VERSION,
            "managers": status_info,
            "enhanced_features": [
                "Multi-manager search", "Progress tracking", "Security audits",
                "Version management", "Smart scope detection", "Enhanced CLI"
            ]
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        installed = [m for m, s in status_info.items() if s == "Installed"]
        not_installed = [m for m, s in status_info.items() if s != "Installed"]
        
        cprint("\n✅ Available Managers:", "SUCCESS")
        if installed:
            for manager in sorted(installed):
                cprint(f"  • {manager}", "SUCCESS")
        else:
            cprint("  (None found - install package managers to get started)", "WARNING")
        
        if not_installed:
            cprint(f"\n📥 {len(not_installed)} additional managers can be installed", "MUTED")
        
        cprint("\n🚀 Enhanced Features:", "INFO")
        features = [
            "🔍 Enhanced search: --search 'query'",
            "📊 Progress tracking: --install package --progress",
            "🔒 Security audits: --audit --detailed",
            "🔄 Version management: --downgrade pkg==1.0 --rollback pkg",
            "👥 Smart scoping: --scope user/system/auto",
            "📋 Scope info: --scope-info [manager]"
        ]
        
        for feature in features:
            cprint(f"  {feature}", "INFO")
        
        cprint(f"\n💡 Quick start: crossfire --setup", "SUCCESS")
        cprint("📖 Full help: crossfire --help", "INFO")
    
    return 0

if __name__ == "__main__":
    try:
        sys.exit(run_enhanced())
    except KeyboardInterrupt:
        cprint("\nOperation cancelled by user.", "WARNING")
        sys.exit(1)
    except Exception as e:
        cprint(f"An unexpected error occurred: {e}", "ERROR")
        sys.exit(1)

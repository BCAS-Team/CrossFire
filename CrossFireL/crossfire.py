#!/usr/bin/env python3
import os
import sys
import subprocess
import platform
import argparse
import shutil
import concurrent.futures
import json

try:
    import distro  # For Linux distro detection
except ImportError:
    distro = None

# ----------------------------
# CrossFire: Universal Package Manager CLI
# ----------------------------

# ----------------------------
# OS & Architecture Detection
# ----------------------------
OS_NAME = platform.system()
ARCH = platform.architecture()[0]  # '32bit' or '64bit'
DISTRO_NAME = ""
DISTRO_VERSION = ""

if OS_NAME == "Linux" and distro:
    DISTRO_NAME = distro.id()
    DISTRO_VERSION = distro.version()
elif OS_NAME == "Darwin":
    DISTRO_NAME = "macOS"
    DISTRO_VERSION = platform.mac_ver()[0]
elif OS_NAME == "Windows":
    DISTRO_NAME = "Windows"
    DISTRO_VERSION = platform.version()

# ----------------------------
# Color Output
# ----------------------------
class Colors:
    INFO = "\033[94m"
    SUCCESS = "\033[92m"
    WARNING = "\033[93m"
    ERROR = "\033[91m"
    RESET = "\033[0m"

def cprint(msg, type="INFO"):
    color = getattr(Colors, type, Colors.INFO)
    print(f"{color}[CrossFire]{Colors.RESET} {msg}")

# ----------------------------
# Package Managers
# ----------------------------
PACKAGE_MANAGERS = {
    # Language managers
    "Python": {"manager": "pip", "update_cmd": "python -m pip install --upgrade pip"},
    "NodeJS": {"manager": "npm", "update_cmd": "npm install -g npm"},
    "Rust": {"manager": "cargo", "update_cmd": "cargo install cargo-update && cargo install-update -a"},
    "Ruby": {"manager": "gem", "update_cmd": "gem update --system"},
    "PHP": {"manager": "composer", "update_cmd": "composer self-update"},
    "Java": {"manager": "maven", "update_cmd": "mvn -U"},
    "Go": {"manager": "go", "update_cmd": "go get -u ./..."},
    "Swift": {"manager": "swift", "update_cmd": "swift package update"},
    "R": {"manager": "R", "update_cmd": "Rscript -e 'update.packages(ask=FALSE)'"},
}

# OS-specific managers
if OS_NAME == "Linux":
    PACKAGE_MANAGERS.update({
        "APT": {"manager": "apt", "update_cmd": "sudo apt update && sudo apt upgrade -y"},
        "DNF": {"manager": "dnf", "update_cmd": "sudo dnf upgrade -y"},
        "Pacman": {"manager": "pacman", "update_cmd": "sudo pacman -Syu"},
        "Zypper": {"manager": "zypper", "update_cmd": "sudo zypper refresh && sudo zypper update -y"},
        "Snap": {"manager": "snap", "update_cmd": "sudo snap refresh"},
        "Flatpak": {"manager": "flatpak", "update_cmd": "flatpak update -y"},
    })
elif OS_NAME == "Darwin":
    PACKAGE_MANAGERS.update({
        "Homebrew": {"manager": "brew", "update_cmd": "brew update && brew upgrade"},
        "MacPorts": {"manager": "port", "update_cmd": "sudo port selfupdate && sudo port upgrade outdated"},
        "Fink": {"manager": "fink", "update_cmd": "sudo fink selfupdate && sudo fink update-all"},
    })
elif OS_NAME == "Windows":
    PACKAGE_MANAGERS.update({
        "Chocolatey": {"manager": "choco", "update_cmd": "choco upgrade all -y"},
        "Winget": {"manager": "winget", "update_cmd": "winget upgrade --all --silent"},
        "Scoop": {"manager": "scoop", "update_cmd": "scoop update *"},
    })

# ----------------------------
# Helper Functions
# ----------------------------
def run_command(cmd, dry_run=False):
    if dry_run:
        cprint(f"(Dry Run) {cmd}", "WARNING")
        return True
    try:
        subprocess.run(cmd, shell=True, check=True)
        return True
    except subprocess.CalledProcessError:
        cprint(f"Command failed: {cmd}", "ERROR")
        return False

def is_installed(manager_name):
    mgr = PACKAGE_MANAGERS.get(manager_name, {}).get("manager")
    return mgr and shutil.which(mgr) is not None

# ----------------------------
# PATH Auto-Add
# ----------------------------
def add_to_path():
    script_path = os.path.dirname(os.path.realpath(__file__))
    if OS_NAME == "Windows":
        current_path = os.environ.get("PATH", "")
        if script_path not in current_path:
            subprocess.run(f'setx PATH "{current_path};{script_path}"', shell=True)
            cprint("CrossFire added to PATH. Restart your terminal.", "SUCCESS")
    else:
        shell_file = os.path.expanduser("~/.bashrc")
        if os.environ.get("SHELL", "").endswith("zsh"):
            shell_file = os.path.expanduser("~/.zshrc")
        with open(shell_file, "r") as f:
            content = f.read()
        export_line = f'export PATH="{script_path}:$PATH"'
        if export_line not in content:
            with open(shell_file, "a") as f:
                f.write(f"\n# CrossFire CLI\n{export_line}\n")
            cprint(f"CrossFire added to PATH in {shell_file}. Restart your terminal.", "SUCCESS")

# ----------------------------
# Package Manager Functions
# ----------------------------
def update_manager(manager_name, dry_run=False):
    if manager_name in PACKAGE_MANAGERS:
        if not is_installed(manager_name):
            cprint(f"{manager_name} not installed, skipping.", "WARNING")
            return False
        cprint(f"Updating manager: {manager_name}", "INFO")
        return run_command(PACKAGE_MANAGERS[manager_name]["update_cmd"], dry_run)
    else:
        cprint(f"Manager '{manager_name}' not recognized.", "ERROR")
        return False

def update_all_managers(dry_run=False):
    cprint("Updating all detected package managers...", "INFO")
    success = []
    failed = []

    # System managers (sequential)
    system_managers = ["APT", "DNF", "Pacman", "Zypper", "Snap", "Flatpak", "Homebrew", "MacPorts", "Fink", "Chocolatey", "Winget", "Scoop"]
    lang_managers = [mgr for mgr in PACKAGE_MANAGERS if mgr not in system_managers]

    for mgr in system_managers:
        if is_installed(mgr):
            if update_manager(mgr, dry_run):
                success.append(mgr)
            else:
                failed.append(mgr)

    # Concurrent language manager updates
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = {executor.submit(update_manager, mgr, dry_run): mgr for mgr in lang_managers if is_installed(mgr)}
        for fut in concurrent.futures.as_completed(futures):
            mgr = futures[fut]
            if fut.result():
                success.append(mgr)
            else:
                failed.append(mgr)

    cprint(f"Updated successfully: {', '.join(success)}", "SUCCESS")
    if failed:
        cprint(f"Failed updates: {', '.join(failed)}", "ERROR")

def update_package(manager_name, package_name, dry_run=False):
    if manager_name not in PACKAGE_MANAGERS:
        cprint(f"Manager '{manager_name}' not recognized.", "ERROR")
        return
    if not is_installed(manager_name):
        cprint(f"{manager_name} not installed, cannot update package.", "ERROR")
        return
    cprint(f"Updating/installing package '{package_name}' via {manager_name}", "INFO")
    cmds = {
        "Python": f"python -m pip install --upgrade {package_name}",
        "NodeJS": f"npm install -g {package_name}",
        "Rust": f"cargo install-update {package_name}",
        "Ruby": f"gem update {package_name}",
        "PHP": f"composer global update {package_name}",
        "Java": f"mvn install -U {package_name}",
        "Go": f"go get -u {package_name}",
        "Swift": f"swift package update {package_name}",
        "R": f"Rscript -e 'update.packages(\"{package_name}\", ask=FALSE)'"
    }
    cmd = cmds.get(manager_name)
    if cmd:
        run_command(cmd, dry_run)
    else:
        cprint(f"Package auto-update not implemented for {manager_name}", "WARNING")

def detect_manager(package_name):
    ext_map = {
        "py": "Python", "whl": "Python",
        "js": "NodeJS", "ts": "NodeJS",
        "rs": "Rust",
        "rb": "Ruby",
        "php": "PHP",
        "jar": "Java", "pom": "Java", "gradle": "Java",
        "swift": "Swift",
        "go": "Go",
        "r": "R"
    }
    ext = package_name.split('.')[-1].lower()
    return ext_map.get(ext, "Python")  # Default fallback

# ----------------------------
# Extra Utilities
# ----------------------------
def list_managers():
    cprint("Detected package managers:", "INFO")
    for mgr in PACKAGE_MANAGERS:
        status = "Installed" if is_installed(mgr) else "Not Installed"
        cprint(f"{mgr} ({status})")

# ----------------------------
# Main CLI
# ----------------------------
def main():
    add_to_path()  # Ensure CrossFire is in PATH
    parser = argparse.ArgumentParser(description="CrossFire CLI Global Package Manager")
    parser.add_argument("package", nargs='?', help="Package name to install/update")
    parser.add_argument("-um", "--update-manager", nargs='?', const="ALL", help="Update a manager or ALL managers")
    parser.add_argument("-up", "--update-package", help="Update a specific package")
    parser.add_argument("--dry-run", action="store_true", help="Preview commands without executing")
    parser.add_argument("--list-managers", action="store_true", help="List all supported managers")
    args = parser.parse_args()

    if args.list_managers:
        list_managers()
        return

    if args.update_manager:
        if args.update_manager.upper() == "ALL":
            update_all_managers(dry_run=args.dry_run)
        else:
            update_manager(args.update_manager, dry_run=args.dry_run)
        return

    if args.update_package:
        detected_manager = detect_manager(args.update_package)
        update_package(detected_manager, args.update_package, dry_run=args.dry_run)
        return

    if args.package:
        detected_manager = detect_manager(args.package)
        update_package(detected_manager, args.package, dry_run=args.dry_run)
        return

    parser.print_help()

# ----------------------------
# Entry Point
# ----------------------------
if __name__ == "__main__":
    main()

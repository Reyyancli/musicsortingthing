#!/usr/bin/env python3
"""
installer.py — installs musicsortingthing.py onto PATH.

Windows : GUI installer (tkinter).  Copies the script to %LOCALAPPDATA%\\musicsortingthing\\
          and adds that directory to the current user's PATH via the registry.
Linux   : Copies to ~/.local/bin (if in PATH) or /usr/bin (if running as root).
          Prints instructions otherwise — never prompts for sudo itself.
macOS   : Same logic; uses /usr/local/bin instead of /usr/bin when root
          (macOS SIP protects /usr/bin on modern systems).
"""

import os
import sys
import shutil
import platform
from pathlib import Path

SCRIPT_NAME  = "musicsortingthing.py"
COMMAND_NAME = "musicsortingthing"          # name users will type in a terminal


# ── helpers ───────────────────────────────────────────────────────────────────

def _find_source() -> Path:
    here = Path(__file__).resolve().parent
    src  = here / SCRIPT_NAME
    if not src.exists():
        raise FileNotFoundError(
            f"'{SCRIPT_NAME}' not found next to the installer.\n"
            f"Expected: {src}"
        )
    return src


# ── Windows ───────────────────────────────────────────────────────────────────

def _windows_install(src: Path) -> None:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog

    local_app = os.environ.get("LOCALAPPDATA", "")
    default_dir = (
        Path(local_app) / COMMAND_NAME if local_app
        else Path.home() / "AppData" / "Local" / COMMAND_NAME
    )

    root = tk.Tk()
    root.title(f"{COMMAND_NAME} — installer")
    root.resizable(False, False)

    path_var = tk.StringVar(value=str(default_dir))

    def on_browse() -> None:
        chosen = filedialog.askdirectory(
            title="Choose install folder",
            initialdir=str(Path(path_var.get()).parent),
        )
        if chosen:
            path_var.set(chosen)

    def on_install() -> None:
        dest_dir = Path(path_var.get().strip())
        if not dest_dir.parts:
            messagebox.showwarning("Invalid path", "Please enter a valid install path.")
            return
        dest_script = dest_dir / SCRIPT_NAME
        dest_cmd    = dest_dir / f"{COMMAND_NAME}.cmd"
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dest_script))
            _write_cmd_wrapper(dest_cmd)
            _win_add_to_path(str(dest_dir))
            messagebox.showinfo(
                "Installation complete",
                f"Installed to:\n  {dest_dir}\n\n"
                "Open a new terminal and run:\n"
                f"  {COMMAND_NAME}",
            )
            root.destroy()
        except Exception as exc:
            messagebox.showerror("Installation failed", str(exc))

    frame = ttk.Frame(root, padding=28)
    frame.pack(fill="both", expand=True)

    ttk.Label(frame, text=COMMAND_NAME, font=("Segoe UI", 16, "bold")).pack(pady=(0, 14))

    # Install path row
    ttk.Label(frame, text="Install location:", font=("Segoe UI", 9)).pack(anchor="w")
    path_row = ttk.Frame(frame)
    path_row.pack(fill="x", pady=(4, 14))
    path_entry = ttk.Entry(path_row, textvariable=path_var, width=44)
    path_entry.pack(side="left", fill="x", expand=True)
    ttk.Button(path_row, text="Browse…", command=on_browse).pack(side="left", padx=(6, 0))

    ttk.Label(
        frame,
        text="The install folder will be added to your user PATH.",
        justify="center",
        font=("Segoe UI", 9),
    ).pack(pady=(0, 18))

    buttons = ttk.Frame(frame)
    buttons.pack()
    ttk.Button(buttons, text="Install", width=14, command=on_install).pack(side="left", padx=6)
    ttk.Button(buttons, text="Cancel",  width=14, command=root.destroy).pack(side="left", padx=6)

    # center on screen
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    x = (root.winfo_screenwidth()  - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.mainloop()


def _write_cmd_wrapper(dest: Path) -> None:
    """Write a .cmd shim that calls the script via py or python."""
    dest.write_text(
        "@echo off\n"
        f"where py >nul 2>&1\n"
        f"if %errorlevel% == 0 (\n"
        f'    py "%~dp0{SCRIPT_NAME}" %*\n'
        f") else (\n"
        f'    python "%~dp0{SCRIPT_NAME}" %*\n'
        f")\n",
        encoding="utf-8",
    )


def _win_add_to_path(new_dir: str) -> None:
    """Add new_dir to the current user's PATH in the Windows registry."""
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0,
        winreg.KEY_READ | winreg.KEY_WRITE,
    ) as key:
        try:
            current, _ = winreg.QueryValueEx(key, "PATH")
        except FileNotFoundError:
            current = ""

        dirs     = [d for d in current.split(os.pathsep) if d]
        lower_set = {d.lower() for d in dirs}
        if new_dir.lower() not in lower_set:
            dirs.append(new_dir)
            winreg.SetValueEx(key, "PATH", 0, winreg.REG_EXPAND_SZ, os.pathsep.join(dirs))

    # Notify Explorer / open terminals about the PATH change
    try:
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF,   # HWND_BROADCAST
            0x001A,   # WM_SETTINGCHANGE
            0, "Environment",
            0x0002,   # SMTO_ABORTIFHUNG
            5000, None,
        )
    except Exception:
        pass


# ── Unix (Linux + macOS) ──────────────────────────────────────────────────────

def _unix_install(src: Path) -> None:
    is_root  = (os.geteuid() == 0)
    is_mac   = (platform.system() == "Darwin")
    path_set = set(os.environ.get("PATH", "").split(os.pathsep))

    local_bin  = Path.home() / ".local" / "bin"
    # /usr/bin is SIP-protected on macOS; /usr/local/bin is the conventional alternative
    system_bin = Path("/usr/local/bin") if is_mac else Path("/usr/bin")

    if is_root:
        dest_dir = system_bin
    elif str(local_bin) in path_set:
        dest_dir = local_bin
    elif is_mac and "/usr/local/bin" in path_set:
        dest_dir = Path("/usr/local/bin")
    else:
        _unix_print_help(local_bin, is_mac)
        return

    print(f"Install '{COMMAND_NAME}' to {dest_dir / COMMAND_NAME}?  [y/N] ", end="", flush=True)
    if input().strip().lower() not in ("y", "yes"):
        print("Cancelled.")
        return

    dest_dir.mkdir(parents=True, exist_ok=True)

    # Read source, ensure it has a shebang, write without the .py extension
    content = src.read_bytes()
    if not content.startswith(b"#!"):
        content = b"#!/usr/bin/env python3\n" + content

    dest = dest_dir / COMMAND_NAME
    dest.write_bytes(content)
    dest.chmod(0o755)

    print(f"Installed : {dest}")
    print(f"Run with  : {COMMAND_NAME}")


def _unix_print_help(local_bin: Path, is_mac: bool) -> None:
    shell = Path(os.environ.get("SHELL", "sh")).name

    if shell == "fish":
        rc        = "~/.config/fish/config.fish"
        add_line  = "fish_add_path ~/.local/bin"
        src_cmd   = f"source {rc}"
    elif shell == "zsh" or is_mac:
        rc        = "~/.zshrc"
        add_line  = 'export PATH="$HOME/.local/bin:$PATH"'
        src_cmd   = f"source {rc}"
    else:
        rc        = "~/.bashrc"
        add_line  = 'export PATH="$HOME/.local/bin:$PATH"'
        src_cmd   = f"source {rc}"

    print()
    print("~/.local/bin is not in your PATH.")
    print()
    print("Option 1 — add it (no root needed), then re-run this installer:")
    print(f"    echo '{add_line}' >> {rc}")
    print(f"    {src_cmd}")
    print( "    python3 installer.py")
    print()
    print("Option 2 — install system-wide (requires root):")
    print("    sudo python3 installer.py")
    print()


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    try:
        src = _find_source()
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if platform.system() == "Windows":
        _windows_install(src)
    else:
        _unix_install(src)


if __name__ == "__main__":
    main()

# anker_client/core/installer.py
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from anker_client.config import UTILITY_EXE_PATTERNS
from anker_client.core.paths import sanitize_windows_name
import anker_client.settings as settings


def _is_utility(name: str) -> bool:
    lower = name.lower()
    return any(pat in lower for pat in UTILITY_EXE_PATTERNS)


def find_game_exe(game_dir: str, game_title: str) -> str | None:
    """
    Find the main game .exe in game_dir (depth=1 only).
    Returns absolute path string, or None if ambiguous (show picker).
    """
    root = Path(game_dir)
    # Collect only .exe files directly in the root (not subdirs)
    exes = [f for f in root.iterdir() if f.is_file() and f.suffix.lower() == ".exe"]

    # Step 1: blocklist
    exes = [e for e in exes if not _is_utility(e.name)]
    if not exes:
        return None

    if len(exes) == 1:
        return str(exes[0])

    # Step 2: name match
    norm_title = game_title.lower().replace(" ", "").replace("-", "").replace("_", "")
    for exe in exes:
        norm_exe = exe.stem.lower().replace(" ", "").replace("-", "").replace("_", "")
        if norm_title in norm_exe or norm_exe in norm_title:
            return str(exe)

    # Step 3a: Unity companion — {name}_Data/ folder
    for exe in exes:
        data_dir = root / (exe.stem + "_Data")
        if data_dir.is_dir():
            return str(exe)

    # Step 3b: Godot companion — {name}.pck
    for exe in exes:
        pck = root / (exe.stem + ".pck")
        if pck.is_file():
            return str(exe)

    # Step 3c: Unreal shipping pattern
    for exe in exes:
        if "-win64-shipping" in exe.name.lower():
            return str(exe)

    # Step 4: ambiguous — caller must show picker
    return None


def extract_archive(archive_path: str, dest_dir: str) -> None:
    """Extract archive_path into dest_dir using 7-Zip."""
    os.makedirs(dest_dir, exist_ok=True)
    result = subprocess.run(
        [settings.get_seven_zip(), "x", archive_path, f"-o{dest_dir}", "-y"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"7-Zip failed:\n{result.stdout}\n{result.stderr}")


def find_game_root(extracted_dir: str, junk_names: set, junk_exts: set) -> str:
    """
    Find the actual game subdirectory inside the extracted archive root.
    Skips junk files (*.url, *.bat, *.txt at root).
    Returns the path to the game folder.
    """
    root = Path(extracted_dir)
    subdirs = []
    for item in root.iterdir():
        if item.is_dir():
            subdirs.append(item)
    if len(subdirs) == 1:
        return str(subdirs[0])
    if len(subdirs) > 1:
        # Multiple dirs — return the one that looks most like a game (has an .exe)
        for d in subdirs:
            if any(f.suffix.lower() == ".exe" for f in d.iterdir() if f.is_file()):
                return str(d)
        return str(subdirs[0])
    # No subdirs — archive is flat, game is in the root
    return extracted_dir


def create_shortcut(target_exe: str, shortcut_path: str, working_dir: str) -> None:
    """Create a Windows .lnk shortcut."""
    import win32com.client
    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(shortcut_path)
    shortcut.TargetPath = target_exe
    shortcut.WorkingDirectory = working_dir
    shortcut.Save()


def install_game(archive_path: str, game_title: str, games_dir: str) -> str:
    """
    Full install pipeline:
      1. Extract archive to temp dir
      2. Find game root
      3. Move game root to games_dir/{title}
      4. Delete archive + temp
      5. Return installed game path
    """
    import shutil
    from anker_client.config import JUNK_FILENAMES, JUNK_EXTENSIONS

    safe_title = sanitize_windows_name(game_title)
    temp_root = os.path.join(games_dir, "_temp")
    os.makedirs(temp_root, exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix=f"{safe_title}-", dir=temp_root)
    game_dest = os.path.join(games_dir, safe_title)
    backup_dest = f"{game_dest}.anker-backup-{uuid.uuid4().hex}"
    moved_existing = False

    try:
        extract_archive(archive_path, temp_dir)
        game_root = find_game_root(temp_dir, JUNK_FILENAMES, JUNK_EXTENSIONS)

        # Preserve an existing installation until extraction has succeeded.
        # If the final move fails, restoring the backup leaves the user's game
        # usable instead of half-deleted.
        if os.path.exists(game_dest):
            os.replace(game_dest, backup_dest)
            moved_existing = True

        try:
            shutil.move(game_root, game_dest)
        except Exception:
            if moved_existing:
                if os.path.exists(game_dest):
                    shutil.rmtree(game_dest, ignore_errors=True)
                if not os.path.exists(game_dest):
                    os.replace(backup_dest, game_dest)
            raise

        if moved_existing:
            shutil.rmtree(backup_dest, ignore_errors=True)
        return game_dest
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        try:
            os.remove(archive_path)
        except OSError:
            pass
        downloads_dir = os.path.dirname(archive_path)
        if (
            os.path.basename(downloads_dir) == "downloads"
            and os.path.dirname(downloads_dir) == temp_root
        ):
            try:
                os.rmdir(downloads_dir)
            except OSError:
                pass
        try:
            os.rmdir(temp_root)  # Remove it only when no other job uses it.
        except OSError:
            pass

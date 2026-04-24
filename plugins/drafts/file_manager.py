"""File finder and mover plugin.

Say "Samsara, find my resume" to search for files.
Say "Samsara, move resume.pdf to Job Applications" to relocate files.

Searches common locations: Desktop, Documents, Downloads, Projects.
Creates destination folders automatically if they don't exist.

Trigger phrases:
  "find" / "locate" / "where is my" / "search for file"
  "move" / "transfer" / "put" / "move file"
"""

import os
import subprocess
import threading
from pathlib import Path

from samsara.plugin_commands import command


# Common search locations
_SEARCH_ROOTS = [
    Path.home() / "Desktop",
    Path.home() / "Documents",
    Path.home() / "Downloads",
    Path.home() / "Projects",
    Path.home() / "Pictures",
    Path.home() / "Videos",
    Path.home() / "Music",
]

# Max results to show
_MAX_RESULTS = 10
# Max depth to search
_MAX_DEPTH = 5


def _find_files(query, max_results=_MAX_RESULTS):
    """Search for files matching query across common locations.
    
    Returns list of Path objects sorted by modification time (newest first).
    Matches against filename (case-insensitive, partial match).
    """
    query_lower = query.lower().strip()
    results = []
    
    for root_dir in _SEARCH_ROOTS:
        if not root_dir.exists():
            continue
        try:
            for dirpath, dirnames, filenames in os.walk(root_dir):
                # Limit depth
                depth = dirpath.replace(str(root_dir), '').count(os.sep)
                if depth >= _MAX_DEPTH:
                    dirnames.clear()
                    continue
                
                # Skip hidden and system dirs
                dirnames[:] = [d for d in dirnames
                               if not d.startswith('.') and d not in
                               ('node_modules', '__pycache__', '.git', 'venv')]
                
                for filename in filenames:
                    if query_lower in filename.lower():
                        full_path = Path(dirpath) / filename
                        try:
                            results.append((full_path, full_path.stat().st_mtime))
                        except OSError:
                            continue
                
                if len(results) >= max_results * 3:
                    break  # enough candidates
        except PermissionError:
            continue
    
    # Sort by modification time (newest first), limit results
    results.sort(key=lambda x: x[1], reverse=True)
    return [r[0] for r in results[:max_results]]


def _move_file(source, dest_folder_name):
    """Move a file to a destination folder.
    
    dest_folder_name can be:
    - A simple name like "Job Applications" → ~/Documents/Job Applications/
    - A relative path like "Projects/archive" → ~/Projects/archive/
    - An absolute path
    
    Creates the destination folder if it doesn't exist.
    Returns (success, message).
    """
    import shutil
    
    source = Path(source)
    if not source.exists():
        return False, f"Source file not found: {source}"
    
    # Resolve destination
    dest_folder = Path(dest_folder_name)
    if not dest_folder.is_absolute():
        # Try to find it in common locations first
        for root in _SEARCH_ROOTS:
            candidate = root / dest_folder_name
            if candidate.exists() and candidate.is_dir():
                dest_folder = candidate
                break
        else:
            # Default: create under Documents
            dest_folder = Path.home() / "Documents" / dest_folder_name
    
    # Create destination if needed
    try:
        dest_folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"Cannot create folder: {e}"
    
    dest_path = dest_folder / source.name
    
    # Don't overwrite existing files
    if dest_path.exists():
        stem = source.stem
        suffix = source.suffix
        counter = 1
        while dest_path.exists():
            dest_path = dest_folder / f"{stem} ({counter}){suffix}"
            counter += 1
    
    try:
        shutil.move(str(source), str(dest_path))
        return True, f"Moved to {dest_path}"
    except Exception as e:
        return False, f"Move failed: {e}"


# Store last search results for "move the first one to..."
_last_results = []


@command("find file", aliases=[
    "locate file", "where is my", "find my",
    "search for file", "look for file",
    "where did i put", "find the file"
])
def handle_find_file(app, remainder):
    """Find files by name. Usage: 'Samsara, find my resume'"""
    global _last_results
    
    if not remainder or not remainder.strip():
        print("[FILE] No search term provided")
        return True
    
    query = remainder.strip()
    print(f"[FILE] Searching for '{query}'...")
    
    def _search():
        global _last_results
        results = _find_files(query)
        _last_results = results
        
        if not results:
            print(f"[FILE] No files found matching '{query}'")
            return
        
        print(f"[FILE] Found {len(results)} file(s):")
        for i, path in enumerate(results, 1):
            size = path.stat().st_size
            if size >= 1024 * 1024:
                size_str = f"{size / (1024*1024):.1f}MB"
            elif size >= 1024:
                size_str = f"{size / 1024:.0f}KB"
            else:
                size_str = f"{size}B"
            print(f"  {i}. {path.name} ({size_str}) — {path.parent}")
        
        # Open the folder of the first result in Explorer
        if results:
            first = results[0]
            try:
                subprocess.Popen(
                    ['explorer', '/select,', str(first)],
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                print(f"[FILE] Opened Explorer at: {first.parent}")
            except Exception:
                pass
    
    threading.Thread(target=_search, daemon=True, name="file-search").start()
    return True


@command("move file", aliases=[
    "transfer file", "move the file",
    "put file", "relocate file",
    "move it to", "transfer it to",
    "put it in"
])
def handle_move_file(app, remainder):
    """Move a file. Usage: 'Samsara, move file resume.pdf to Job Applications'"""
    global _last_results
    
    if not remainder or not remainder.strip():
        print("[FILE] Usage: 'move file resume.pdf to Job Applications'")
        return True
    
    text = remainder.strip()
    
    # Parse "filename to destination" or "to destination" (uses last search)
    source_path = None
    dest_name = None
    
    # Check for "to" separator
    if " to " in text.lower():
        parts = text.lower().split(" to ", 1)
        source_query = parts[0].strip()
        dest_name = parts[1].strip()
        
        # If source is empty or "it"/"this", use last search result
        if not source_query or source_query in ("it", "this", "that", "the file"):
            if _last_results:
                source_path = _last_results[0]
            else:
                print("[FILE] No previous search results — search first")
                return True
        else:
            # Find the file by name
            results = _find_files(source_query, max_results=1)
            if results:
                source_path = results[0]
            else:
                print(f"[FILE] Cannot find file: '{source_query}'")
                return True
    else:
        print("[FILE] Usage: 'move file resume.pdf to Job Applications'")
        print("[FILE] Say 'to' between the filename and destination")
        return True
    
    if not source_path or not dest_name:
        print("[FILE] Could not parse source and destination")
        return True
    
    print(f"[FILE] Moving {source_path.name} to {dest_name}...")
    
    def _do_move():
        success, message = _move_file(source_path, dest_name)
        if success:
            print(f"[FILE] {message}")
            # Open destination in Explorer
            dest_path = Path(message.replace("Moved to ", ""))
            try:
                subprocess.Popen(
                    ['explorer', '/select,', str(dest_path)],
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
            except Exception:
                pass
        else:
            print(f"[FILE] {message}")
    
    threading.Thread(target=_do_move, daemon=True, name="file-move").start()
    return True


@command("open folder", aliases=[
    "open directory", "show folder", "show directory",
    "open in explorer", "show in explorer"
])
def handle_open_folder(app, remainder):
    """Open a folder in Explorer. Usage: 'Samsara, open folder Downloads'"""
    if not remainder or not remainder.strip():
        # Open home directory
        subprocess.Popen(['explorer', str(Path.home())])
        return True
    
    folder_name = remainder.strip().lower()
    
    # Check common folder aliases
    aliases = {
        'desktop': Path.home() / 'Desktop',
        'documents': Path.home() / 'Documents',
        'downloads': Path.home() / 'Downloads',
        'pictures': Path.home() / 'Pictures',
        'videos': Path.home() / 'Videos',
        'music': Path.home() / 'Music',
        'projects': Path.home() / 'Projects',
        'home': Path.home(),
    }
    
    target = aliases.get(folder_name)
    if not target:
        # Search for the folder
        for root in _SEARCH_ROOTS:
            if not root.exists():
                continue
            for dirpath, dirnames, _ in os.walk(root):
                depth = dirpath.replace(str(root), '').count(os.sep)
                if depth >= 3:
                    dirnames.clear()
                    continue
                for d in dirnames:
                    if folder_name in d.lower():
                        target = Path(dirpath) / d
                        break
                if target:
                    break
            if target:
                break
    
    if target and target.exists():
        subprocess.Popen(['explorer', str(target)])
        print(f"[FILE] Opened: {target}")
    else:
        print(f"[FILE] Folder not found: '{remainder.strip()}'")
    
    return True

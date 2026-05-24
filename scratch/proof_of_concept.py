import os
import re
from pathlib import Path

def find_desktop_files():
    search_dirs = [
        Path(os.path.expanduser("~/.local/share/applications")),
        Path("/usr/share/applications"),
        Path("/usr/local/share/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
        Path(os.path.expanduser("~/.local/share/flatpak/exports/share/applications"))
    ]
    
    desktop_files = []
    for d in search_dirs:
        if d.exists() and d.is_dir():
            for filepath in d.glob("*.desktop"):
                desktop_files.append(filepath)
    return desktop_files

def parse_desktop_file(filepath):
    try:
        content = filepath.read_text(errors="ignore")
        entry = {}
        in_desktop_entry = False
        
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line == "[Desktop Entry]":
                in_desktop_entry = True
                continue
            elif line.startswith("[") and line.endswith("]"):
                in_desktop_entry = False
                continue
                
            if in_desktop_entry and "=" in line:
                key, val = line.split("=", 1)
                entry[key.strip()] = val.strip()
                
        return entry
    except Exception as e:
        print(f"Error parsing {filepath}: {e}")
        return None

def sanitize_exec(exec_cmd):
    if not exec_cmd:
        return ""
    # Strip field codes like %U, %f, %F, %u, %k, %i, %c, %d
    cleaned = re.sub(r'%[fFuUkicd]', '', exec_cmd)
    return cleaned.strip()

def search_app(name_query):
    query = name_query.lower()
    desktop_files = find_desktop_files()
    results = []
    
    for filepath in desktop_files:
        entry = parse_desktop_file(filepath)
        if not entry:
            continue
        
        name = entry.get("Name", "")
        exec_cmd = entry.get("Exec", "")
        no_display = entry.get("NoDisplay", "false").lower() == "true"
        hidden = entry.get("Hidden", "false").lower() == "true"
        
        if no_display or hidden:
            continue
            
        if not name or not exec_cmd:
            continue
            
        # Match query in Name, Exec, or filename
        if query in name.lower() or query in filepath.name.lower():
            results.append({
                "name": name,
                "exec": sanitize_exec(exec_cmd),
                "icon": entry.get("Icon", ""),
                "path": str(filepath)
            })
            
    return results

if __name__ == "__main__":
    print("Searching for 'heroic'...")
    heroic_matches = search_app("heroic")
    for match in heroic_matches:
        print(f"Found: {match['name']}")
        print(f"  Exec: {match['exec']}")
        print(f"  Icon: {match['icon']}")
        print(f"  Path: {match['path']}\n")
        
    print("Searching for 'steam'...")
    steam_matches = search_app("steam")
    for match in steam_matches:
        print(f"Found: {match['name']}")
        print(f"  Exec: {match['exec']}")
        print(f"  Icon: {match['icon']}")
        print(f"  Path: {match['path']}\n")

#!/usr/bin/env python3
"""
SWhereUsed for SOLIDWORKS
========================
A small local web app that answers one question fast: "where is this part used?"

It keeps an index of every assembly and drawing it can find (via Everything, or by scanning
folders you choose) and reads their references with the SOLIDWORKS Document Manager. SOLIDWORKS
itself does not need to be running. Files are only written when you rename or move one: then the
references in every assembly and drawing that uses it are updated, with a backup, and everything is
put back automatically if anything goes wrong.

Start:   double-click "Start SWhereUsed.bat"      (or: python SWhereUsed.py)
Then:    http://127.0.0.1:8790  opens in your browser.

Everything that belongs to you (settings, license key, index) lives in %USERPROFILE%\\.SWhereUsed,
so this app folder can be replaced by a newer version, or shared, without losing anything.

Command line:
  python SWhereUsed.py                 start the app and open the browser
  python SWhereUsed.py --no-browser    start without opening the browser
  python SWhereUsed.py --test FILE     read one assembly/drawing and print what the index would store
  python SWhereUsed.py --rename-job J  (started by the app itself: carries out one rename)
  python SWhereUsed.py --selftest      run the automated tests (no SOLIDWORKS needed)
"""
from __future__ import annotations

import base64
import functools
import json
import struct
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath

APP_NAME = "SWhereUsed"
APP_VERSION = "2.2.0"
# Set this to your GitHub repository ("owner/name") before publishing: the app then says when a newer release
# is available. Empty: no update check.
UPDATE_REPO = ""
# Where people can support the development (shown in the footer and on the Setup page). Empty: not shown.
DONATE_URL = "https://ko-fi.com/stefansterk"
APP_HOST = "127.0.0.1"                  # deliberately fixed: the app is only reachable from this pc
HERE = Path(__file__).resolve().parent

try:                                    # pywin32: COM initialisation for the Document Manager
    import pythoncom
    HAVE_PYWIN32 = True
except ImportError:
    pythoncom = None
    HAVE_PYWIN32 = False
try:                                    # comtypes: the Document Manager has no IDispatch interface
    import comtypes  # noqa: F401
    HAVE_COMTYPES = True
except ImportError:
    HAVE_COMTYPES = False


def _expand(p):
    """Expand %VARIABLES% and ~ (also on systems where os.path.expandvars skips %VAR%)."""
    p = re.sub(r"%([^%]+)%", lambda m: os.environ.get(m.group(1), m.group(0)), p or "")
    return os.path.expanduser(os.path.expandvars(p))


DATA_DIR = Path(_expand(os.environ.get("SWHEREUSED_HOME") or "%USERPROFILE%\\.SWhereUsed"
                        if os.name == "nt" else os.environ.get("SWHEREUSED_HOME") or "~/.SWhereUsed"))
SETTINGS_FILE = "settings.ini"

# ---------------------------------------------------------------------------
# Settings: these are the DEFAULTS. Your own values live in settings.ini in the data folder.
# ---------------------------------------------------------------------------
EVERYTHING_URL = "http://127.0.0.1:8080"
EVERYTHING_USER = ""
EVERYTHING_PASS = ""
EVERYTHING_START = "yes"
INDEX_SOURCE = "auto"
INDEX_FOLDERS: list = []
EVERYTHING_QUERY = "ext:sldasm;slddrw;sldprt"
INCLUDE_PARTS = "yes"
EXCLUDE = ["\\$Recycle.Bin\\", "\\AppData\\", "\\IC~~\\",
           "\\SOLIDWORKS Corp\\", "\\Public\\Documents\\SOLIDWORKS\\",   # the sample and tutorial files of SOLIDWORKS
           "\\ProgramData\\SOLIDWORKS\\"]                                    # its design library
DATABASE = ""                           # empty = index.sqlite in the data folder
WORKERS = 4
UPDATE_ON_START = "yes"
INTERVAL_MIN = 60
MAX_CONFIGS = 50
RETRY_HOURS = 24
FILE_TIMEOUT = 180
APP_PORT = 8790
OPEN_BROWSER = "yes"
SWDM_DLL = ""
DESCRIPTION_PROPS = ["Description"]
EXTRA_PROPS: list = []
THUMBNAILS = "yes"
THUMB_CACHE_MB = 500
DETECT_RENAMES = "yes"
CHECK_UPDATES = "yes"
RENAME_ALLOWED = "yes"
BACKUP_FOLDER = ""                      # empty = backups in the data folder
BACKUP_DAYS = 90

# (section, key, variable, type, help). Types: str, int, choice:a,b, "list," or "list;" (separator)
_SPEC = [
    ("index", "source", "INDEX_SOURCE", "choice:auto,everything,folders",
     "Where to find assemblies and drawings:\n"
     "  auto       = Everything if it is running with its HTTP server on, otherwise the folders below\n"
     "  everything = always Everything (voidtools)\n"
     "  folders    = scan the folders below (no Everything needed; slower on big network drives)"),
    ("index", "folders", "INDEX_FOLDERS", "list;",
     "Folders to scan, separated by semicolons. Example: D:\\Vault; \\\\server\\cad\\Projects"),
    ("index", "include_parts", "INCLUDE_PARTS", "choice:yes,no",
     "Also index parts: finds parts that refer to other parts (derived, mirrored, inserted parts) and shows\n"
     "the description of parts. The first index run takes longer, as there are usually many more parts."),
    ("index", "file_timeout", "FILE_TIMEOUT", "int",
     "Seconds one file may take to be read before it counts as stuck. Raise it for very large assemblies on a\n"
     "slow network."),
    ("index", "detect_renames", "DETECT_RENAMES", "choice:yes,no",
     "Recognise files renamed by other tools (SOLIDWORKS Explorer, PDM, ...) through the external references of an\n"
     "assembly that lists a file that no longer exists. Done in a separate helper process."),
    ("index", "everything_query", "EVERYTHING_QUERY", "str",
     "Everything search that lists the files to index. Limit it to a folder like this:\n"
     "  ext:sldasm;slddrw \"D:\\Vault\\\""),
    ("index", "exclude", "EXCLUDE", "list;",
     "Skip paths that contain any of these texts, separated by semicolons.\n"
     "Lock files (~$...) and SOLIDWORKS temp copies are always skipped."),
    ("index", "database", "DATABASE", "str", "Location of the index. Empty = index.sqlite in the data folder."),
    ("index", "workers", "WORKERS", "int", "Files read at the same time (1-8)."),
    ("index", "update_on_start", "UPDATE_ON_START", "choice:yes,no",
     "Update the index when the app starts (only new and changed files are read)."),
    ("index", "interval_minutes", "INTERVAL_MIN", "int", "Update automatically every N minutes. 0 = off."),
    ("index", "max_configurations", "MAX_CONFIGS", "int", "Configurations read per assembly (at most)."),
    ("index", "retry_after_hours", "RETRY_HOURS", "int", "Retry unreadable files after this many hours."),
    ("index", "max_seconds_per_file", "FILE_TIMEOUT", "int",
     "A file that takes longer than this is skipped until the next update."),

    ("everything", "url", "EVERYTHING_URL", "str",
     "Address of the HTTP server in Everything (Tools > Options > HTTP Server)."),
    ("everything", "user", "EVERYTHING_USER", "str", "Only if you set a user name in Everything's HTTP server."),
    ("everything", "password", "EVERYTHING_PASS", "str", ""),
    ("everything", "start_if_needed", "EVERYTHING_START", "choice:yes,no",
     "Start Everything (in the background) when SWhereUsed needs it and it is not running."),

    ("solidworks", "dll", "SWDM_DLL", "str", "Path to SwDocumentMgr.dll. Empty = find it automatically."),
    ("solidworks", "extra_properties", "EXTRA_PROPS", "list,",
     "Custom properties shown as extra columns in the structure and its CSV, separated by commas\n"
     "(for example: PartNo, Material). After a change, assemblies and parts are read once more."),
    ("solidworks", "description_properties", "DESCRIPTION_PROPS", "list,",
     "Custom properties shown as Description, in order of preference, separated by commas."),

    ("rename", "allowed", "RENAME_ALLOWED", "choice:yes,no",
     "Allow renaming and moving files from the app (with references updated). no = SWhereUsed only reads."),
    ("rename", "backup_folder", "BACKUP_FOLDER", "str",
     "Where a copy of every changed file is kept before renaming. Empty = backups in the data folder."),
    ("rename", "keep_backups_days", "BACKUP_DAYS", "int", "Delete backups older than this many days (0 = keep all)."),

    ("app", "port", "APP_PORT", "int", "Port of the app: http://127.0.0.1:<port>"),
    ("app", "open_browser", "OPEN_BROWSER", "choice:yes,no", "Open the browser when the app starts."),
    ("app", "thumbnails", "THUMBNAILS", "choice:yes,no", "Show the preview picture of files in lists (read when shown)."),
    ("app", "thumbnail_cache_mb", "THUMB_CACHE_MB", "int",
     "Most room the kept preview pictures may take, in MB. Above it, the pictures not looked at for the longest go first."),
    ("app", "check_updates", "CHECK_UPDATES", "choice:yes,no", "Say so when a newer version of SWhereUsed is available."),
]
SETTINGS_PROBLEMS: list = []
SW_KEY, SW_KEY_SOURCE = "", None


def _settings_path():
    return DATA_DIR / SETTINGS_FILE


def _setting_text(value, typ):
    if typ.startswith("list"):
        return (typ[4:] + " ").join(value)
    return str(value)


def _write_settings_template(path):
    lines = [f"# {APP_NAME} settings. Restart the app after changing this file.",
             "# Most of this can also be set on the Setup page of the app.", ""]
    section = None
    for sec, key, var, typ, help_ in _SPEC:
        if sec != section:
            lines += ["", f"[{sec}]"]
            section = sec
        lines += [f"# {h}" for h in help_.split("\n") if help_]
        lines.append(f"{key} = {_setting_text(globals()[var], typ)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse(raw, typ):
    if typ == "int":
        if not raw.lstrip("-").isdigit():
            raise ValueError("must be a whole number")
        return int(raw)
    if typ.startswith("list"):
        return [x.strip() for x in raw.split(typ[4:]) if x.strip()]
    if typ.startswith("choice:"):
        options = typ.split(":", 1)[1].split(",")
        if raw.lower() not in options:
            raise ValueError(f"choose from {', '.join(options)}")
        return raw.lower()
    return raw


def load_settings(write_missing=True):
    """Read settings.ini (create it on first start) and override the defaults above."""
    import configparser
    SETTINGS_PROBLEMS.clear()
    path = _settings_path()
    if not path.exists():
        try:
            _write_settings_template(path)
        except OSError as e:
            SETTINGS_PROBLEMS.append(f"{path} could not be created: {e}")
        _load_key()
        return
    cp = configparser.ConfigParser(interpolation=None, comment_prefixes=("#", ";"), inline_comment_prefixes=None)
    try:
        cp.read(path, encoding="utf-8-sig")
    except configparser.Error as e:
        SETTINGS_PROBLEMS.append(f"{SETTINGS_FILE} is not readable ({e}); defaults used")
        _load_key()
        return
    real = {s.lower(): s for s in cp.sections()}
    missing = []
    for sec, key, var, typ, _h in _SPEC:
        rsec = real.get(sec)
        if not rsec or not cp.has_option(rsec, key):
            missing.append((sec, key, var, typ))
            continue
        raw = cp.get(rsec, key).strip()
        try:
            globals()[var] = _parse(raw, typ)
        except ValueError as e:
            SETTINGS_PROBLEMS.append(f"[{sec}] {key} = {raw!r} is invalid ({e}); default used")
    if missing and write_missing:                 # new settings in a newer version: add them with their default
        for sec, key, var, typ in missing:
            try:
                set_setting(sec, key, _setting_text(globals()[var], typ))
            except OSError:
                pass
    _load_key()


def set_setting(section, key, value):
    """Change one line in settings.ini, keeping everything else (comments, order) as it is."""
    path = _settings_path()
    if not path.exists():
        _write_settings_template(path)
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    header = re.compile(r"^\s*\[([^\]]+)\]\s*$")
    start = next((i for i, l in enumerate(lines) if header.match(l) and header.match(l).group(1).strip().lower() == section),
                 None)
    if start is None:
        lines += ["", f"[{section}]", f"{key} = {value}"]
    else:
        end = next((i for i in range(start + 1, len(lines)) if header.match(lines[i])), len(lines))
        pat = re.compile(rf"^\s*{re.escape(key)}\s*=", re.I)
        hit = next((i for i in range(start + 1, end) if pat.match(lines[i])), None)
        if hit is not None:
            lines[hit] = f"{key} = {value}"
        else:
            while end > start + 1 and not lines[end - 1].strip():
                end -= 1
            lines.insert(end, f"{key} = {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _key_file():
    return DATA_DIR / "swdm_key.txt"


def _load_key():
    """The Document Manager key: data folder, then next to the app, then environment variable."""
    global SW_KEY, SW_KEY_SOURCE
    for f in (_key_file(), HERE / "swdm_key.txt"):
        if f.is_file():
            key = f.read_text(encoding="utf-8-sig").strip().strip('"')
            if key:
                SW_KEY, SW_KEY_SOURCE = key, str(f)
                return
    if os.environ.get("SWDM_LICENSE_KEY", "").strip():
        SW_KEY, SW_KEY_SOURCE = os.environ["SWDM_LICENSE_KEY"].strip(), "environment variable SWDM_LICENSE_KEY"
        return
    SW_KEY, SW_KEY_SOURCE = "", None


def save_key(key):
    key = (key or "").strip().strip('"')
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _key_file().write_text(key + "\n", encoding="utf-8")
    _load_key()


def db_path():
    return Path(_expand(DATABASE)) if DATABASE.strip() else DATA_DIR / "index.sqlite"


# ---------------------------------------------------------------------------
# Everything (voidtools) over its HTTP server
# ---------------------------------------------------------------------------
def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _filetime_to_unix(v):
    ft = _to_int(v)
    return (ft - 116444736000000000) / 10_000_000 if ft else None


# Never through a proxy: Everything and this app run on this pc (or the local network). A company proxy
# that Python picks up from Windows would otherwise refuse or delay these requests.
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def everything_search(query, count, offset=0, timeout=10, sort=None, ascending=True):
    params = {"search": query, "json": 1, "path_column": 1, "size_column": 1,
              "date_modified_column": 1, "count": count, "offset": offset}
    if sort:                                        # name, path, size or date_modified
        params.update(sort=sort, ascending=1 if ascending else 0)
    params = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{EVERYTHING_URL.rstrip('/')}/?{params}")
    if EVERYTHING_USER:
        token = base64.b64encode(f"{EVERYTHING_USER}:{EVERYTHING_PASS}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with _direct.open(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    results = [{"type": r.get("type", "file"), "path": os.path.join(r.get("path", ""), r.get("name", "")),
                "size": _to_int(r.get("size")), "modified": _filetime_to_unix(r.get("date_modified"))}
               for r in data.get("results", [])]
    return {"total": data.get("totalResults", len(results)), "results": results}


def everything_list(query, page=2000):
    out, offset = [], 0
    while True:
        chunk = everything_search(query, page, offset, timeout=60)
        out.extend(chunk["results"])
        offset += len(chunk["results"])
        if not chunk["results"] or offset >= (chunk["total"] or 0):
            return out


_ev_cache = {"t": 0.0, "value": None}


def everything_status(max_age=10):
    """(reachable, message), remembered for a few seconds."""
    if time.time() - _ev_cache["t"] < max_age and _ev_cache["value"]:
        return _ev_cache["value"]
    try:
        everything_search("ext:sldasm", 1, timeout=2)
        value = (True, f"Everything answers on {EVERYTHING_URL}")
    except urllib.error.HTTPError as e:
        value = (False, f"Everything answered with error {e.code}" + (" (check user/password)" if e.code == 401 else ""))
    except Exception as e:  # noqa: BLE001
        reason = getattr(e, "reason", e)
        value = (False, f"Everything is not reachable on {EVERYTHING_URL} ({reason})")
    _ev_cache.update(t=time.time(), value=value)
    return value


def _everything_running():
    """Is Everything running on this pc? (True / False / None = cannot tell)"""
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["tasklist", "/NH", "/FO", "CSV"], capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return None
    return '"everything.exe"' in out or '"everything64.exe"' in out


def find_everything_exe():
    """Where Everything is installed (None = not found)."""
    if os.name != "nt":
        return None
    cands = []
    try:
        import winreg
        for root, key in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Everything"),
                          (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Everything")):
            try:
                with winreg.OpenKey(root, key) as k:
                    for name in ("DisplayIcon", "UninstallString"):
                        try:
                            value = str(winreg.QueryValueEx(k, name)[0]).strip('"').split(",")[0]
                            folder = os.path.dirname(value.strip('"'))
                            cands += [os.path.join(folder, "Everything.exe"), os.path.join(folder, "Everything64.exe")]
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            cands += [os.path.join(base, "Everything", "Everything.exe"),
                      os.path.join(base, "Everything 1.5a", "Everything64.exe")]
    return next((c for c in cands if c and os.path.isfile(c)), None)


def start_everything_if_needed():
    """Everything installed but not running, and SWhereUsed needs it: start it in the background.
    Returns a short message for the console (None = nothing to do)."""
    if EVERYTHING_START != "yes" or INDEX_SOURCE == "folders" or os.name != "nt":
        return None
    if everything_status(max_age=0)[0]:
        return None
    running = _everything_running()
    if running:
        return ("Everything is running, but its HTTP server does not answer. Turn it on in Everything: "
                "Tools, Options, HTTP Server.")
    exe = find_everything_exe()
    if not exe:
        return None
    try:
        subprocess.Popen([exe, "-startup"], close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return f"Started Everything in the background ({exe})."
    except OSError as e:
        return f"Could not start Everything ({e})."


def index_source():
    """Which source the index uses right now: ('everything'|'folders'|None, explanation)."""
    if INDEX_SOURCE == "everything":
        ok, msg = everything_status()
        return ("everything", msg) if ok else (None, msg)
    if INDEX_SOURCE == "folders":
        return ("folders", f"{len(INDEX_FOLDERS)} folder(s)") if INDEX_FOLDERS else (None, "no folders set")
    ok, msg = everything_status()
    if ok:
        return "everything", msg
    if INDEX_FOLDERS:
        return "folders", f"{msg}; scanning {len(INDEX_FOLDERS)} folder(s) instead"
    return None, f"{msg}, and no folders are set"


# ---------------------------------------------------------------------------
# Folder scan (when Everything is not used)
# ---------------------------------------------------------------------------
_TEMP_PATH_RE = re.compile(r"[\\/]AppData[\\/]Local[\\/]Temp[\\/]|[\\/]swx\d+[\\/]|[\\/]IC~~[\\/]", re.I)
_VIRTUAL_PATH_RE = re.compile(r"[\\/]swx\d+[\\/]|[\\/]IC~~[\\/]", re.I)
INDEX_EXTS = (".sldasm", ".slddrw", ".sldprt")


def _index_exts():
    return INDEX_EXTS if INCLUDE_PARTS == "yes" else (".sldasm", ".slddrw")


def _everything_query():
    """The Everything search for the index. A query from before parts were indexed gets parts added."""
    q = EVERYTHING_QUERY
    if INCLUDE_PARTS == "yes" and "sldprt" not in q.lower():
        q = re.sub(r"ext:sldasm;slddrw\b", "ext:sldasm;slddrw;sldprt", q, flags=re.I)
    return q + "".join(f' !"{f}"' for f in _own_folders())


def _own_folders():
    """Folders of SWhereUsed itself: the backups in them are copies, not files of the vault."""
    out = [str(DATA_DIR)]
    if BACKUP_FOLDER.strip():
        out.append(_expand(BACKUP_FOLDER))
    return [f.replace("/", "\\").rstrip("\\").lower() + "\\" for f in out if f]


def _excluded(path):
    name = _fname(path)
    if name.startswith("~$") or _TEMP_PATH_RE.search(path):
        return True
    low_path = path.replace("/", "\\").lower()
    if any(low_path.startswith(f) for f in _own_folders()):
        return True
    low = path.lower()
    return any(x.lower() in low for x in EXCLUDE)


def _root_of(path):
    return PureWindowsPath(path).anchor or path[:3]


def _reachable(root, timeout=10):
    """Is a drive or share reachable within `timeout` seconds? (network paths can hang for a long time)"""
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("ok", os.path.isdir(root)), daemon=True)
    t.start()
    t.join(timeout)
    return bool(result.get("ok"))


def scan_folders(folders, progress=None):
    """All assemblies and drawings below the folders, in the same shape as Everything results.
    Returns (files, unreachable folders)."""
    out, unreachable = [], []
    for folder in folders:
        root = _expand(folder)
        if not _reachable(root):
            unreachable.append(root)
            continue
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if not _excluded(e.path + "\\"):
                                    stack.append(e.path)
                            elif e.name.lower().endswith(_index_exts()) and not _excluded(e.path):
                                st = e.stat()
                                out.append({"type": "file", "path": e.path, "size": st.st_size, "modified": st.st_mtime})
                        except OSError:
                            continue
            except OSError:
                continue
            if progress and len(out) % 500 == 0:
                progress(len(out))
    return out, unreachable


# ---------------------------------------------------------------------------
# SOLIDWORKS Document Manager (read-only)
# ---------------------------------------------------------------------------
SW_TYPES = {".sldprt": 1, ".sldasm": 2, ".slddrw": 3}
_sw_local = threading.local()
_sw_module = None
_sw_module_lock = threading.Lock()


def _com_init():
    pythoncom.CoInitialize()          # every worker thread needs its own COM initialisation


def find_swdm_dll():
    if SWDM_DLL:
        if Path(SWDM_DLL).is_file():
            return SWDM_DLL
        raise RuntimeError(f"SwDocumentMgr.dll not found at {SWDM_DLL} (setting [solidworks] dll)")
    if os.name != "nt":
        raise RuntimeError("the Document Manager only runs on Windows")
    import winreg
    try:
        clsid = winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, r"SwDocumentMgr.SwDMClassFactory\CLSID")
        dll = winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\InprocServer32").strip('"')
        if dll and Path(dll).is_file():
            return dll
    except OSError:
        pass
    default = r"C:\Program Files\Common Files\SOLIDWORKS Shared\SwDocumentMgr.dll"
    if Path(default).is_file():
        return default
    raise RuntimeError("SwDocumentMgr.dll not found; it is installed with SOLIDWORKS "
                       "(or set [solidworks] dll in settings.ini)")


def _patch_comtypes():
    """Some comtypes versions cannot convert arrays of IUnknown (VT_UNKNOWN = 13): KeyError 13.
    The Document Manager returns drawing sheets that way; add the missing mapping."""
    try:
        from ctypes import POINTER
        from comtypes import IUnknown, automation
        vt = getattr(automation, "VT_UNKNOWN", 13)
        automation._vartype_to_ctype.setdefault(vt, POINTER(IUnknown))
        automation._ctype_to_vartype.setdefault(POINTER(IUnknown), vt)
    except Exception:  # noqa: BLE001 — other comtypes version: leave it alone
        pass


def _swdm():
    global _sw_module
    with _sw_module_lock:
        if _sw_module is None:
            import comtypes.client
            _patch_comtypes()
            _sw_module = comtypes.client.GetModule(find_swdm_dll())
    return _sw_module


def _sw_app():
    """One Document Manager session per worker thread (COM objects are bound to their thread)."""
    if not hasattr(_sw_local, "app"):
        import comtypes.client
        mod = _swdm()
        factory = comtypes.client.CreateObject(mod.SwDMClassFactory, interface=mod.ISwDMClassFactory)
        app = factory.GetApplication(SW_KEY)
        if not app:
            raise RuntimeError("the Document Manager rejected the license key")
        _sw_local.app = app
    return _sw_local.app


def _best(obj, base):
    """Cast a COM object to the newest interface version it supports (e.g. ISwDMDocument30)."""
    mod = _swdm()
    cands = sorted(((int(n[len(base):] or 1), n) for n in dir(mod) if re.fullmatch(base + r"\d*", n)), reverse=True)
    for _, name in cands:
        try:
            return obj.QueryInterface(getattr(mod, name))
        except Exception:  # noqa: BLE001
            continue
    return obj


def _split(res):
    """comtypes returns [out] parameters as a tuple: split into (objects/text, numbers)."""
    items = res if isinstance(res, tuple) else (res,)
    return [x for x in items if not isinstance(x, int)], [x for x in items if isinstance(x, int)]


def _attr(obj, name, default=None):
    try:
        v = getattr(obj, name)
        if callable(v):
            v = v()
        rest, nums = _split(v)
        return rest[0] if rest else (nums[0] if nums else default)
    except Exception:  # noqa: BLE001 — not every interface version has every property
        return default


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    try:
        return list(v)
    except TypeError:
        return [v]


_EXPR_RE = re.compile(r'\$PRP(SHEET)?:|"SW-[^"]*"|"[^"@]+@[^"]+"', re.I)


def _prop_value(obj, name):
    """Evaluated value of one custom property (skips unevaluated expressions like $PRP:"...")."""
    found = []
    for method in ("GetCustomProperty2", "GetCustomPropertyValues", "GetCustomProperty"):
        try:
            res = getattr(obj, method)(name)
        except Exception:  # noqa: BLE001
            continue
        found += [x for x in (res if isinstance(res, tuple) else (res,)) if isinstance(x, str) and x]
    value = next((v for v in found if not _EXPR_RE.search(v)), "")
    return value[len("fromparent+"):] if value.lower().startswith("fromparent+") else value


def _props_of(names, *objs):
    """Values of the given custom properties, from the first object (configuration, document) that has them."""
    out = {}
    for obj in objs:
        if obj is None or len(out) == len(names):
            continue
        try:
            have = {str(n).lower(): str(n) for n in (obj.GetCustomPropertyNames() or [])}
        except Exception:  # noqa: BLE001
            continue
        for wanted in names:
            if wanted not in out and wanted.lower() in have:
                v = _prop_value(obj, have[wanted.lower()])
                if v:
                    out[wanted] = v
    return out


def _description(*objs):
    for obj in objs:
        if obj is None:
            continue
        try:
            names = {str(n).lower(): str(n) for n in (obj.GetCustomPropertyNames() or [])}
        except Exception:  # noqa: BLE001
            continue
        for wanted in DESCRIPTION_PROPS:
            real = names.get(wanted.lower())
            if real:
                v = _prop_value(obj, real)
                if v:
                    return v
    return None


_HRESULTS = {-2147467259: "unspecified error (E_FAIL)", -2147024891: "access denied",
             -2147024894: "file not found", -2147024893: "path not found",
             -2147024864: "file is in use by another process", -2147024786: "file is locked",
             -2147024882: "out of memory", -2147418113: "unexpected error (E_UNEXPECTED)"}


def human_error(e):
    code = getattr(e, "hresult", None)
    if code is None and getattr(e, "args", None) and isinstance(e.args[0], int):
        code = e.args[0]
    if isinstance(code, int) and code in _HRESULTS:
        return _HRESULTS[code]
    text = str(e)
    if len(text) < 12 or not isinstance(e, (OSError, RuntimeError)):
        return f"{type(e).__name__}: {text}" if text else type(e).__name__
    return text


def _file_hints(path):
    """Common reasons why a file cannot be read, in plain words."""
    hints = []
    if len(path) >= 260:
        hints.append(f"the path is {len(path)} characters long; above 260 the Document Manager often fails")
    try:
        attrs = getattr(os.stat(path), "st_file_attributes", 0)
        if attrs & 0x00400000 or attrs & 0x00040000 or attrs & 0x1000:
            hints.append("the file is online-only (OneDrive) and not downloaded to this pc")
    except OSError:
        pass
    try:
        with open(path, "rb") as f:
            if len(f.read(8)) < 8:
                hints.append("the file is empty or truncated")
    except PermissionError:
        hints.append("the file is locked or you have no read permission")
    except OSError as e:
        hints.append(f"the file cannot be read ({e.strerror or e})")
    return hints


_OPEN_ERRORS = {1: "could not open (permissions, in use, or missing)", 2: "not a SOLIDWORKS file",
                3: "file not found", 4: "file is read-only",
                5: "no valid Document Manager license for this file version",
                6: "saved with a newer SOLIDWORKS than this Document Manager can read"}


def _open_sw(path, readonly=True):
    rest, nums = _split(_sw_app().GetDocument(path, SW_TYPES[Path(path).suffix.lower()], readonly))
    doc = next((x for x in rest if x), None)
    code = next((n for n in nums if n), 0)
    # For writing every error code is fatal: the Document Manager would otherwise silently open read-only
    if doc is None or (code and not readonly):
        if doc is not None:
            try:
                doc.CloseDoc()
            except Exception:  # noqa: BLE001
                pass
        msg = (f"could not open the file{' for writing' if not readonly else ''} "
               f"(code {code}{': ' + _OPEN_ERRORS[code] if code in _OPEN_ERRORS else ''})")
        hints = _file_hints(path)
        raise RuntimeError(msg + (". Possible cause: " + "; ".join(hints) if hints else ""))
    return _best(doc, "ISwDMDocument")


def _open_sw_retry(path):
    try:
        return _open_sw(path)
    except Exception:  # noqa: BLE001 — E_FAIL is sometimes temporary under load
        time.sleep(0.3)
        return _open_sw(path)


def _is_virtual_path(p):
    return "^" in _fname(p) or bool(_VIRTUAL_PATH_RE.search(p))


FLAG_SUPPRESSED, FLAG_ENVELOPE, FLAG_NOT_IN_BOM = 1, 2, 4
ASM_READ_VERSION = 3          # 2: envelopes are read; 3: renames by other tools are recognised


def _flags(v):
    v = int(v or 0)
    return {"suppressed": bool(v & FLAG_SUPPRESSED), "envelope": bool(v & FLAG_ENVELOPE),
            "not_in_bom": bool(v & FLAG_NOT_IN_BOM)}


def _read_components(cfg):
    """Direct components of one configuration: (list, method used, errors)."""
    errors = []
    try:
        items = _as_list(cfg.GetComponents())
        out = []
        for comp in items:
            comp = _best(comp, "ISwDMComponent")
            p = str(_attr(comp, "PathName", "") or "")
            if p:
                # Flags: 1 suppressed, 2 envelope, 4 excluded from the BOM. The file is still used (so where-used
                # and renaming include it), but it does not count in quantities. IsEnvelope is a method (ISwDMComponent5).
                flags = ((FLAG_SUPPRESSED if _attr(comp, "IsSuppressed", False) else 0)
                         | (FLAG_ENVELOPE if _attr(comp, "IsEnvelope", False) else 0)
                         | (FLAG_NOT_IN_BOM if _attr(comp, "ExcludeFromBOM", False) else 0))
                out.append({"path": p, "config": str(_attr(comp, "ConfigurationName", "") or ""),
                            "suppressed": flags,
                            "virtual": bool(_attr(comp, "IsVirtual", False)) or _is_virtual_path(p)})
        if out:
            return out, "GetComponents", errors
        errors.append(f"GetComponents returned {len(items)} objects without a readable path" if items
                      else "GetComponents returned an empty list")
    except Exception as e:  # noqa: BLE001
        errors.append(f"GetComponents: {human_error(e)}")
    try:                                          # fallback: paths, configurations, suppressed, virtual
        res = cfg.GetReferencesInformation2()
        parts = list(res) if isinstance(res, tuple) else [res]
        refs, cfgs, supp, virt = ([_as_list(parts[i]) if len(parts) > i else [] for i in range(4)])
        out = [{"path": str(p), "config": str(cfgs[i]) if i < len(cfgs) and cfgs[i] else "",
                "suppressed": (FLAG_SUPPRESSED if supp[i] else 0) if i < len(supp) else 0,
                "virtual": (bool(virt[i]) if i < len(virt) else False) or _is_virtual_path(str(p))}
               for i, p in enumerate(refs) if p]
        if out:
            return out, "GetReferencesInformation2", errors
        errors.append("GetReferencesInformation2 returned no references")
    except Exception as e:  # noqa: BLE001
        errors.append(f"GetReferencesInformation2: {human_error(e)}")
    return [], None, errors


def read_assembly(path):
    """All configurations of one assembly -> rows for the refs table, number of configurations, description."""
    doc = _open_sw_retry(path)
    try:
        cm = _best(doc.ConfigurationManager, "ISwDMConfigurationMgr")
        names = [str(n) for n in (cm.GetConfigurationNames() or [])][:max(1, MAX_CONFIGS)]
        active = str(cm.GetActiveConfigurationName() or "")
        rows, description, props = [], None, {}
        for cname in names:
            cfg = cm.GetConfigurationByName(cname)
            if not cfg:
                continue
            cfg = _best(cfg, "ISwDMConfiguration")
            if cname == active:
                description = _description(cfg, doc)
                props = _props_of(EXTRA_PROPS, cfg, doc) if EXTRA_PROPS else {}
            comps, _via, errors = _read_components(cfg)
            grouped = {}
            for c in comps:
                key = (_fname(c["path"]).lower(), c["path"], c["config"], c["suppressed"], c["virtual"])
                grouped[key] = grouped.get(key, 0) + 1
            for (name, stored, ccfg, supp, virt), qty in grouped.items():
                rows.append((path.lower(), cname, name, stored, ccfg, qty, int(supp), int(virt)))
        if EXTRA_PROPS and not props:
            props = _props_of(EXTRA_PROPS, doc)
        detected = _detect_renames(path, rows)
        return rows, len(names), description or _description(doc), _attr(doc, "GetVersion", None), props, detected
    finally:
        try:
            doc.CloseDoc()
        except Exception:  # noqa: BLE001
            pass


EXT_REFS = {"enabled": True, "why": None}      # switched off when reading external references crashes (see probe)


def _pair_renames(missing, new):
    """Pair component paths that no longer exist with new paths in the external references. Only certain pairs:
    one gone and one new of the same type; with more at once, per folder, again only one-to-one."""
    def by(paths, key):
        out = {}
        for p in paths:
            out.setdefault(key(p), []).append(p)
        return out
    ext = lambda p: Path(p).suffix.lower()                               # noqa: E731
    pairs = []
    gone_by_ext, new_by_ext = by(missing, ext), by(new, ext)
    for e, gone in gone_by_ext.items():
        cands = new_by_ext.get(e, [])
        if len(gone) == 1 and len(cands) == 1:
            pairs.append((gone[0], cands[0]))
            continue
        folder = lambda p: (_folder_of(p) or "").lower()                 # noqa: E731
        gf, nf = by(gone, folder), by(cands, folder)
        for f, g in gf.items():
            if len(g) == 1 and len(nf.get(f, [])) == 1:
                pairs.append((g[0], nf[f][0]))
        # Still open: the old name inside exactly one new name, or the new inside the old (a prefix or suffix
        # was added or removed: "Mounting_plate_A" -> "4411001M_Mounting_plate_A")
        done_g, done_n = {a for a, _ in pairs}, {b for _, b in pairs}
        rest_g = [x for x in gone if x not in done_g]
        rest_n = [x for x in cands if x not in done_n]
        stem = lambda p: Path(PureWindowsPath(p).name).stem.lower()    # noqa: E731
        match = {g: [c for c in rest_n if stem(g) in stem(c) or stem(c) in stem(g)] for g in rest_g}
        claimed = {}
        for g, cs in match.items():
            for c in cs:
                claimed[c] = claimed.get(c, 0) + 1
        for g, cs in match.items():
            if len(cs) == 1 and claimed[cs[0]] == 1 and min(len(stem(g)), len(stem(cs[0]))) >= 4:
                pairs.append((g, cs[0]))
    return pairs


def _console_python():
    """Helpers always run with python.exe (also when SWhereUsed itself runs windowless with pythonw.exe): their
    input and output through pipes is most reliable that way. They never get a window of their own."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        return str(exe.parent / "python.exe")
    return sys.executable


PY_CHILD = _console_python()
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BACKGROUND = "--background" in sys.argv          # windowless, with an icon by the clock
_children = set()                                 # index run and rename/Pack & Go jobs: stopped with the app


def _quiet_flags():
    """In background mode: no console window for a helper that would otherwise print into ours."""
    return NO_WINDOW if BACKGROUND else 0


_ext = {"proc": None, "lock": threading.Lock(), "out": None}


# swDmSearchFilters: 1 external reference path, 2 root assembly folder, 4 subfolders, 8 in-context, 16/32/64 parts,
# assemblies, drawings. Without 2 and 4 the Document Manager does not look elsewhere: it gives the paths AS SAVED.
_DM_SEARCH_SAVED_ONLY = 1 | 16 | 32 | 64


def _search_option(saved_only=False):
    """The search option for GetAllExternalReferences4. saved_only: do not search (no folders, not the assembly's own
    folder), so a reference that is not where it was saved shows its SAVED path, not the file found by name."""
    opt = _keep(_sw_app().GetSearchOptionObject())
    if saved_only:
        try:
            opt.ClearAllSearchPaths()
        except Exception:  # noqa: BLE001
            pass
        try:
            opt.SearchFilters = _DM_SEARCH_SAVED_ONLY
        except Exception:  # noqa: BLE001 — older Document Manager: it searches anyway
            pass
    return opt


def _ext_refs_isolated(path, timeout=60, saved_only=False):
    """External references of an assembly, read in a separate helper process. With comtypes this function can
    corrupt memory when its results are cleaned up (heap corruption, 0xC0000374). The helper never cleans them up
    and ends without cleaning up; if it crashes anyway, only it stops: None is returned and it is started again."""
    import queue
    with _ext["lock"]:
        proc = _ext["proc"]
        if proc is None or proc.poll() is not None:
            proc = subprocess.Popen([PY_CHILD, str(Path(__file__).resolve()), "--ext-worker"], cwd=str(HERE),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, encoding="utf-8", bufsize=1,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = queue.Queue()
            threading.Thread(target=lambda: [out.put(line) for line in proc.stdout], daemon=True).start()
            _ext.update(proc=proc, out=out)
        try:
            proc.stdin.write(json.dumps({"path": path, "saved_only": saved_only}) + "\n")
            proc.stdin.flush()
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    answer = json.loads(_ext["out"].get(timeout=0.5))
                    return answer.get("refs") if answer.get("ok") else None
                except queue.Empty:
                    if proc.poll() is not None:        # the helper crashed on this file
                        break
        except (OSError, ValueError):
            pass
        _kill_tree(proc)
        _ext["proc"] = None
        return None


def _ext_worker_main():
    """The helper: reads {"path"} lines, answers {"ok", "refs"}. Keeps every COM result alive and ends with
    os._exit, so comtypes never clears them (that is where the memory corruption happens)."""
    _com_init()
    keep = []
    for line in sys.stdin:
        try:
            req = json.loads(line)
            path = req["path"]
            doc = _open_sw_retry(path)
            keep.append(doc)
            res = doc.GetAllExternalReferences4(_search_option(bool(req.get("saved_only"))))
            keep.append(res)
            refs = [str(x) for x in (_string_list(res) or [])]
            try:
                doc.CloseDoc()
            except Exception:  # noqa: BLE001
                pass
            print(json.dumps({"ok": True, "refs": refs}), flush=True)
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": human_error(e)}), flush=True)
    sys.stdout.flush()
    os._exit(0)


def _detect_renames(path, rows, say=None):
    """Renamed by another tool (SOLIDWORKS Explorer, PDM, a Document Manager tool): the component list still names
    the old file, the external references (what SOLIDWORKS loads) the new one. Read those only when a component
    path no longer exists. Returns [(old path, new path)]."""
    say = say or (lambda line: None)
    if DETECT_RENAMES != "yes":
        say("Recognising renames by other tools is switched off ([index] detect_renames = no).")
        return []
    comp = {r[3] for r in rows if r[3] and not r[7]}
    if not comp:
        return []
    existing = _existing_files(list(comp))
    missing = [p for p in comp if p.lower() not in existing and not _TEMP_PATH_RE.search(p)]
    say(f"Component paths that no longer exist: {len(missing)}" + "".join(f"\n    {p}" for p in missing))
    if not missing:
        return []
    ext = _ext_refs_isolated(path)
    if ext is None:
        say("External references could not be read (the helper process failed on this file).")
        return []
    known = {p.lower() for p in comp}
    new = [e for e in ext if e.lower() not in known and e.lower().endswith((".sldprt", ".sldasm"))]
    say(f"External references that are not in the component list: {len(new)}" + "".join(f"\n    {p}" for p in new))
    pairs = _pair_renames(missing, new)
    say(f"Recognised renames: {len(pairs)}" + "".join(f"\n    {a}\n      -> {b}" for a, b in pairs))
    unpaired = [p for p in missing if p not in {a for a, _ in pairs}]
    if unpaired:
        say("Not paired (not certain which new file it is): " + ", ".join(_fname(p) for p in unpaired))
    return pairs


def read_drawing(path):
    """Models shown in a drawing, through its sheets and views (ISwDMSheet / ISwDMView).
    Deliberately NOT via GetAllExternalReferences: with comtypes that corrupts the heap (0xC0000374)."""
    doc = _open_sw_retry(path)
    try:
        try:
            sheets = _as_list(doc.GetSheets())
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"drawing sheets not readable ({human_error(e)})") from e
        found = {}
        for sheet in sheets:
            sheet = _best(sheet, "ISwDMSheet")
            try:
                views = _as_list(sheet.GetViews())
            except Exception:  # noqa: BLE001 — sheet without views
                continue
            for view in views:
                view = _best(view, "ISwDMView")
                ref = str(_attr(view, "ReferencedDocument", "") or "")
                if ref:
                    cfg = str(_attr(view, "ReferencedConfiguration", "") or "")
                    entry = found.setdefault((ref.lower(), cfg), [ref, 0])
                    entry[1] += 1
        rows = [(path.lower(), "", _fname(ref).lower(), ref, cfg, n, 0, int(_is_virtual_path(ref)))
                for (_low, cfg), (ref, n) in found.items()]
        return rows, len(sheets), _description(doc), _attr(doc, "GetVersion", None)
    finally:
        try:
            doc.CloseDoc()
        except Exception:  # noqa: BLE001
            pass


PART_REFS = {"enabled": True, "why": None}      # switched off when reading part references crashes (see probe)


def _part_references(doc):
    """Files a part refers to: base part of a derived/mirrored/inserted part, split/save bodies, and so on.
    Returns ([(path, configuration)], method used). Tries the newest interface first; which one works is
    printed by --test."""
    app = _best(_sw_app(), "ISwDMApplication")
    opt, last = None, None
    for getter in ("GetExternalReferenceOptionObject2", "GetExternalReferenceOptionObject"):
        try:
            opt = getattr(app, getter)()
            if opt:
                break
        except Exception as e:  # noqa: BLE001
            last = e
    if not opt:
        raise RuntimeError(f"this Document Manager cannot read part references ({human_error(last) if last else 'no option object'})")
    opt = _best(opt, "ISwDMExternalReferenceOption")
    try:
        opt.SearchOption = app.GetSearchOptionObject()
    except Exception:  # noqa: BLE001 — older interfaces: the defaults are used
        pass
    for method in ("GetExternalFeatureReferences3", "GetExternalFeatureReferences2", "GetExternalFeatureReferences"):
        fn = getattr(doc, method, None)
        if fn is None:
            continue
        try:
            res = fn(opt)
        except Exception as e:  # noqa: BLE001
            last = e
            continue
        # comtypes returns an [in, out] parameter together with the count; read the lists from the object
        holder = next((x for x in (res if isinstance(res, tuple) else (res,)) if hasattr(x, "ExternalReferences")), opt)
        try:
            refs = _as_list(holder.ExternalReferences)
        except Exception:  # noqa: BLE001
            refs = []
        try:
            cfgs = _as_list(holder.ReferencedConfigurations)
        except Exception:  # noqa: BLE001
            cfgs = []
        out = [(str(r), str(cfgs[i]) if i < len(cfgs) and cfgs[i] else "") for i, r in enumerate(refs) if r]
        return out, method
    raise RuntimeError(f"part references not readable ({human_error(last) if last else 'no suitable method'})")


def read_part(path, with_refs=None):
    """A part: the files it refers to (derived, mirrored, inserted parts) and its description."""
    doc = _open_sw_retry(path)
    try:
        rows, error = [], None
        if PART_REFS["enabled"] if with_refs is None else with_refs:
            try:
                refs, _method = _part_references(doc)
                seen = {}
                for ref, cfg in refs:
                    if ref.lower() == path.lower():
                        continue
                    key = (ref.lower(), cfg)
                    if key not in seen:
                        seen[key] = (path.lower(), "", _fname(ref).lower(), ref, cfg, 1, 0, int(_is_virtual_path(ref)))
                rows = list(seen.values())
            except Exception as e:  # noqa: BLE001 — keep the description; say what went wrong
                error = human_error(e)
        desc = None
        try:
            cm = _best(doc.ConfigurationManager, "ISwDMConfigurationMgr")
            active = str(cm.GetActiveConfigurationName() or "")
            cfg = _best(cm.GetConfigurationByName(active), "ISwDMConfiguration") if active else None
            desc = _description(cfg, doc)
            props = _props_of(EXTRA_PROPS, cfg, doc) if EXTRA_PROPS else {}
        except Exception:  # noqa: BLE001
            desc = _description(doc)
            props = _props_of(EXTRA_PROPS, doc) if EXTRA_PROPS else {}
        if error and not rows:
            raise RuntimeError(f"part references: {error}")
        return rows, 1, desc, _attr(doc, "GetVersion", None), props
    finally:
        try:
            doc.CloseDoc()
        except Exception:  # noqa: BLE001
            pass


def read_document(path):
    low = path.lower()
    if low.endswith(".slddrw"):
        return read_drawing(path)
    if low.endswith(".sldprt"):
        return read_part(path)
    return read_assembly(path)


def _probe_ext_refs(assemblies):
    """Try reading the external references of a few assemblies in a separate process before the index uses it.
    A crash switches it off (remembered per SwDocumentMgr.dll); exit code 1 (an ordinary error) is not remembered."""
    try:
        dll = find_swdm_dll()
        stamp = f"{dll}|{os.path.getmtime(dll)}"
    except Exception:  # noqa: BLE001
        stamp = "unknown"
    try:
        known = json.loads(_meta_get("ext_refs_probe") or "{}")
    except ValueError:
        known = {}
    if known.get("stamp") == stamp:
        EXT_REFS.update(enabled=known["ok"], why=known.get("why"))
        return
    ok, why = True, None
    for p in assemblies[:3]:
        try:
            proc = subprocess.run([PY_CHILD, str(Path(__file__).resolve()), "--probe-ext", p], cwd=str(HERE),
                                  capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            ok, why = False, f"reading external references did not answer ({_fname(p)})"
            break
        if proc.returncode == 1:
            last = ((proc.stderr or "").strip().splitlines() or ["unknown error"])[-1]
            EXT_REFS.update(enabled=False, why=f"external references could not be tried: {last}")
            return
        if proc.returncode != 0:
            ok, why = False, f"reading external references crashed the Document Manager (exit code {proc.returncode}, {_fname(p)})"
            break
    EXT_REFS.update(enabled=ok, why=why)
    _meta_set("ext_refs_probe", json.dumps({"stamp": stamp, "ok": ok, "why": why}))


def _probe_part_references(parts):
    """Before the index reads thousands of parts: try reading part references on a few of them in a separate
    process. If that crashes (as one Document Manager function did with drawings), parts are read for their
    description only, and the reason is shown. The outcome is remembered for this SwDocumentMgr.dll."""
    try:
        dll = find_swdm_dll()
        stamp = f"{dll}|{os.path.getmtime(dll)}"
    except Exception:  # noqa: BLE001
        stamp = "unknown"
    known = _meta_get("part_refs_probe")
    if known:
        try:
            k = json.loads(known)
            if k.get("stamp") == stamp:
                PART_REFS.update(enabled=k["ok"], why=k.get("why"))
                return
        except ValueError:
            pass
    ok, why = True, None
    for p in parts[:3]:
        try:
            proc = subprocess.run([PY_CHILD, str(Path(__file__).resolve()), "--probe-part", p], cwd=str(HERE),
                                  capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            ok, why = False, f"reading part references did not answer ({_fname(p)})"
            break
        if proc.returncode == 1:
            # An ordinary Python error (no key, no Document Manager, ...): skip part references this round, say why,
            # and do NOT remember it: once the problem is solved, the next run tries again.
            last = ((proc.stderr or "").strip().splitlines() or ["unknown error"])[-1]
            PART_REFS.update(enabled=False, why=f"part references could not be tried: {last}")
            return
        if proc.returncode != 0:
            ok, why = False, f"reading part references crashed the Document Manager (exit code {proc.returncode}, {_fname(p)})"
            break
    PART_REFS.update(enabled=ok, why=why)
    _meta_set("part_refs_probe", json.dumps({"stamp": stamp, "ok": ok, "why": why}))


def sw_problem():
    """Why the Document Manager cannot be used (None = all fine as far as we can tell without opening it)."""
    if os.name != "nt":
        return "the SOLIDWORKS Document Manager only runs on Windows"
    if not HAVE_PYWIN32:
        return "the Python package pywin32 is missing"
    if not HAVE_COMTYPES:
        return "the Python package comtypes is missing"
    if not SW_KEY:
        return "no Document Manager license key"
    try:
        find_swdm_dll()
    except Exception as e:  # noqa: BLE001
        return str(e)
    return None


# ---------------------------------------------------------------------------
# The index (SQLite)
# ---------------------------------------------------------------------------
_db = None
_db_lock = threading.Lock()
_state = {"running": False, "phase": "", "total": 0, "done": 0, "errors": 0, "started": None,
          "finished": None, "message": None}
_state_lock = threading.Lock()
WORKER_MODE = "--index-worker" in sys.argv
WATCHDOG_SECONDS = 600


def conn():
    global _db
    if _db is None:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _db = sqlite3.connect(str(path), check_same_thread=False, timeout=60)
        _db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, display_path TEXT, mtime REAL, size INTEGER, indexed_at REAL,
                status TEXT, error TEXT, configs INTEGER, description TEXT);
            CREATE TABLE IF NOT EXISTS refs (
                parent TEXT, parent_config TEXT, child_name TEXT, child_stored_path TEXT,
                child_config TEXT, qty INTEGER, suppressed INTEGER, virtual INTEGER);
            CREATE INDEX IF NOT EXISTS refs_child ON refs(child_name);
            CREATE INDEX IF NOT EXISTS refs_parent ON refs(parent);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS versions (path TEXT PRIMARY KEY, version INTEGER);
            CREATE TABLE IF NOT EXISTS renamed (old TEXT PRIMARY KEY, new TEXT, at REAL, shown TEXT);
            CREATE TABLE IF NOT EXISTS readver (path TEXT PRIMARY KEY, v INTEGER);
            CREATE TABLE IF NOT EXISTS copied_refs (parent TEXT, old_name TEXT, new TEXT, at REAL,
                PRIMARY KEY (parent, old_name));
            CREATE TABLE IF NOT EXISTS detected_refs (parent TEXT, old TEXT, new TEXT, at REAL, PRIMARY KEY (parent, old));
            CREATE TABLE IF NOT EXISTS props (path TEXT, name TEXT, value TEXT, PRIMARY KEY (path, name));
            CREATE TABLE IF NOT EXISTS propsver (path TEXT PRIMARY KEY, key TEXT);
            CREATE TABLE IF NOT EXISTS runs (started REAL, finished REAL, files INTEGER, errors INTEGER,
                by_type TEXT, full INTEGER, message TEXT);
            CREATE TABLE IF NOT EXISTS inflight (path TEXT PRIMARY KEY, display_path TEXT,
                mtime REAL, size INTEGER, started REAL);
        """)
        if "shown" not in [r[1] for r in _db.execute("PRAGMA table_info(renamed)")]:
            _db.execute("ALTER TABLE renamed ADD COLUMN shown TEXT")     # the old name as it was written
            _db.commit()
    return _db


def _meta_get(key, default=None):
    with _db_lock:
        row = conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _meta_set(key, value):
    with _db_lock:
        db = conn()
        if value is None:
            db.execute("DELETE FROM meta WHERE key=?", (key,))
        else:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
        db.commit()


_progress_t = [0.0]


def _save_progress(force=False):
    """Worker: publish progress in the database so the server can show it."""
    now = time.time()
    if not force and now - _progress_t[0] < 1.0:
        return
    _progress_t[0] = now
    with _state_lock:
        snap = dict(_state, heartbeat=now)
    _meta_set("progress", json.dumps(snap))


def _set(**kw):
    with _state_lock:
        _state.update(kw)
    if WORKER_MODE:
        _save_progress(force=True)


def _read_progress():
    try:
        return json.loads(_meta_get("progress") or "{}")
    except (ValueError, sqlite3.Error):
        return {}


_counts_cache = {}


def status():
    with _state_lock:
        st = dict(_state)
    if st.get("running") and not WORKER_MODE:
        prog = _read_progress()
        for k in ("phase", "total", "done", "errors", "by_type"):
            if k in prog:
                st[k] = prog[k]
    try:
        counts = _counts_cache.get("v")
        if not counts or time.time() - counts[0] > 30 or st.get("running"):
            with _db_lock:
                db = conn()
                c = dict(db.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall())
                drw = db.execute("SELECT COUNT(*) FROM files WHERE status='ok' AND path LIKE '%.slddrw'").fetchone()[0]
                prt = db.execute("SELECT COUNT(*) FROM files WHERE status='ok' AND path LIKE '%.sldprt'").fetchone()[0]
            counts = (time.time(), {"assemblies": c.get("ok", 0) - drw - prt, "drawings": drw, "parts": prt,
                                    "failed": sum(c.get(k, 0) for k in ("error", "crash", "suspect", "timeout"))})
            _counts_cache["v"] = counts
        st.update(counts[1])
        with _db_lock:
            st["inflight"] = [{"path": dp, "seconds": round(time.time() - started)} for dp, started in
                              conn().execute("SELECT display_path, started FROM inflight ORDER BY started").fetchall()]
        st["unreachable"] = json.loads(_meta_get("unreachable") or "null")
        last = _meta_get("last_update")
        st["last_update"] = float(last) if last else None
        st["source"] = _meta_get("source")
        size = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                size += os.path.getsize(str(db_path()) + suffix)
            except OSError:
                pass
        st["db_size"] = size
        st["part_refs_off"] = _meta_get("part_refs_off")
        st["ext_refs_off"] = None
        hist = run_history(20)
        st["last_run"] = hist[0] if hist else None
        st["last_full"] = next((h for h in hist if h["full"]), None)
        src, why = index_source()
        st["waiting"] = None if src else why
        if not src and INDEX_SOURCE != "folders":
            st["waiting_for"] = "everything"
    except (sqlite3.Error, ValueError) as e:
        st["db_error"] = str(e)
    st["database"] = str(db_path())
    return st


def run_index():
    """Update the index (runs in the worker process): only new, changed or previously failed files."""
    _set(running=True, phase="Listing assemblies and drawings", total=0, done=0, errors=0,
         started=time.time(), finished=None, message=None)
    try:
        problem = sw_problem()
        if problem:
            raise RuntimeError(problem)
        source, why = index_source()
        if not source:
            raise RuntimeError(f"no file source: {why}")
        unreachable = []
        if source == "everything":
            _set(phase=f"Listing files via Everything ({_everything_query()})")
            listed = everything_list(_everything_query())
        else:
            _set(phase="Scanning folders")
            listed, unreachable = scan_folders(INDEX_FOLDERS, lambda n: _set(phase=f"Scanning folders: {n:,} files found"))
        found = {r["path"].lower(): r for r in listed
                 if r["type"] != "folder" and r["path"].lower().endswith(_index_exts()) and not _excluded(r["path"])}
        _meta_set("source", source)

        n_again = _cleanup_global_detections()
        if n_again:
            _set(phase=f"Reading {n_again} assemblies again that an earlier version linked to the wrong copy")
        if _meta_get("crash_reset") != "1.17.2":
            # 1.17.0 and 1.17.1 read external references inside the index process; that crashed on some
            # assemblies and marked them (and the files open at the same time) as crashed. Try those again once.
            with _db_lock:
                db = conn()
                db.execute("UPDATE files SET status='error', indexed_at=0 WHERE status IN ('crash','suspect')")
                db.commit()
            _meta_set("crash_reset", "1.17.2")
        with _db_lock:
            db = conn()
            db.execute("DELETE FROM inflight")
            known = {p: (m, s, st, ia) for p, m, s, st, ia in
                     db.execute("SELECT path, mtime, size, status, indexed_at FROM files")}
            db.commit()
        with _db_lock:
            have_version = {p for (p,) in conn().execute("SELECT path FROM versions")}
            read_ver = dict(conn().execute("SELECT path, v FROM readver"))
            props_ver = dict(conn().execute("SELECT path, key FROM propsver"))
        props_key = ",".join(p.lower() for p in EXTRA_PROPS)
        # Assemblies not yet read by a version that recognises renames by other tools, and that point to a file
        # that no longer exists: read once more (an unchanged file is otherwise never read again)
        recheck = set()
        older = [p for p, v in read_ver.items() if v < ASM_READ_VERSION]
        if older:
            _set(phase="Looking for assemblies that point to files that no longer exist")
            with _db_lock:
                pairs = []
                for i in range(0, len(older), 500):
                    chunk = older[i:i + 500]
                    pairs += conn().execute(
                        f"SELECT DISTINCT parent, child_stored_path FROM refs WHERE virtual=0 AND child_stored_path != '' "
                        f"AND parent IN ({','.join('?' * len(chunk))})", chunk).fetchall()
            stored = [sp for _p, sp in pairs if not _TEMP_PATH_RE.search(sp)]
            there = _existing_files(stored)
            recheck = {par for par, sp in pairs if not _TEMP_PATH_RE.search(sp) and sp.lower() not in there}
            with _db_lock:                              # the others need nothing: mark them as checked
                db = conn()
                db.executemany("INSERT OR REPLACE INTO readver VALUES (?,?)",
                               [(p, ASM_READ_VERSION) for p in older if p not in recheck])
                db.commit()
        retry_now = _meta_get("retry_errors") == "1"          # the Update button was pressed
        _meta_set("retry_errors", None)
        run_started = float(_meta_get("run_started") or 0)

        # Unreachable drives/shares are skipped in one go (otherwise every file waits for a network time-out)
        roots = sorted({_root_of(r["path"]) for r in found.values()})
        for n, root in enumerate(roots, 1):
            _set(phase=f"Checking {root} ({n} of {len(roots)})")
            if not _reachable(root):
                unreachable.append(root)
        dead = {u.lower() for u in unreachable}

        def lost(p):                        # gone from the listing, and its location is reachable
            return p not in found and _root_of(p).lower() not in dead and not any(p.startswith(u) for u in dead)
        gone = [p for p in known if lost(p)]
        with _db_lock:
            db = conn()
            for p in gone:
                db.execute("DELETE FROM refs WHERE parent=?", (p,))
                db.execute("DELETE FROM files WHERE path=?", (p,))
                db.execute("DELETE FROM versions WHERE path=?", (p,))
                db.execute("DELETE FROM props WHERE path=?", (p,))
            db.commit()

        retry_after = time.time() - max(0, RETRY_HOURS) * 3600

        def why_read(key, r):
            k = known.get(key)
            if not k:
                return "new"
            if abs((k[0] or 0) - (r["modified"] or 0)) > 1 or (k[1] or 0) != (r["size"] or 0):
                return "changed"
            if k[2] == "suspect":
                return "retried"
            if k[2] == "error" and (retry_now or (k[3] or 0) < retry_after):
                return "retried"
            if k[2] == "timeout" and (k[3] or 0) < run_started:
                return "retried"
            if k[2] == "ok" and key not in have_version:
                return "no version yet"     # indexed before SWhereUsed stored the SOLIDWORKS version: once
            if k[2] == "ok" and key.endswith(".sldasm") and read_ver.get(key, 1) < 2:
                return "read again for envelopes"
            if key in recheck:
                return "checked for renames by other tools"
            if k[2] == "ok" and props_key and not key.endswith(".slddrw") and props_ver.get(key) != props_key:
                return "read again for properties"
            return None                     # ok, or crashed and unchanged: nothing to do

        reasons, todo = {}, []
        for key, r in found.items():
            if _root_of(r["path"]).lower() in dead:
                continue
            why = why_read(key, r)
            if why:
                reasons[why] = reasons.get(why, 0) + 1
                todo.append(r)
        order = {".sldasm": 0, ".slddrw": 1, ".sldprt": 2}
        todo.sort(key=lambda r: (order.get(Path(r["path"]).suffix.lower(), 3), r["path"].lower()))
        _meta_set("unreachable", json.dumps({"roots": unreachable}) if unreachable else None)
        parts_todo = [r["path"] for r in todo if r["path"].lower().endswith(".sldprt")]
        if parts_todo:
            _set(phase="Checking that part references can be read safely")
            _probe_part_references(parts_todo)
        # Files that were open during an earlier crash go one by one: a new crash then points at one file
        suspects = [r for r in todo if (known.get(r["path"].lower()) or (0, 0, ""))[2] == "suspect"]
        todo = [r for r in todo if r not in suspects]
        by_type = {"asm": [0, 0, 0], "drw": [0, 0, 0], "prt": [0, 0, 0]}     # done, total, unreadable
        for r in todo + suspects:
            by_type[_type_key(r["path"])][1] += 1
        _set(phase="Reading files", total=len(todo) + len(suspects), by_type=by_type)

        def work(r):
            key = r["path"].lower()
            with _db_lock:
                db = conn()
                db.execute("INSERT OR REPLACE INTO inflight VALUES (?,?,?,?,?)",
                           (key, r["path"], r["modified"], r["size"], time.time()))
                db.commit()
            try:
                res = read_document(r["path"])
                rows, nconf, desc = res[:3]
                return (r, rows, nconf, desc, None, (res[3] if len(res) > 3 else None),
                        (res[4] if len(res) > 4 else None), (res[5] if len(res) > 5 else None))
            except Exception as e:  # noqa: BLE001
                return r, [], 0, None, human_error(e), None, None, None
            finally:
                # Read: no longer open. (Only a crash WHILE reading points at a file; the watchdog must not
                # blame a file that was read long ago and is only waiting to be stored.)
                with _db_lock:
                    db = conn()
                    db.execute("DELETE FROM inflight WHERE path=?", (key,))
                    db.commit()

        def store(result):
            r, rows, nconf, desc, err, version, props, detected = result
            key = r["path"].lower()
            if detected:
                _remember_detected(key, detected)        # before translating: this assembly's rows use them
            rows = _apply_hints_to_rows(_apply_copies_to_rows(_apply_renames_to_rows(rows)))   # before the lock: they read the database
            with _db_lock:
                db = conn()
                db.execute("DELETE FROM refs WHERE parent=?", (key,))
                if rows:
                    db.executemany("INSERT INTO refs VALUES (?,?,?,?,?,?,?,?)", rows)
                db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                           (key, r["path"], r["modified"], r["size"], time.time(),
                            "error" if err else "ok", err, nconf, desc))
                if not err and key.endswith(".sldasm"):
                    db.execute("INSERT OR REPLACE INTO readver VALUES (?,?)", (key, ASM_READ_VERSION))
                if not err and props is not None:
                    db.execute("DELETE FROM props WHERE path=?", (key,))
                    db.executemany("INSERT INTO props VALUES (?,?,?)", [(key, n, v) for n, v in props.items()])
                    db.execute("INSERT OR REPLACE INTO propsver VALUES (?,?)", (key, props_key))
                if not err:                 # also when unknown (0): tried once, not read again for it
                    db.execute("INSERT OR REPLACE INTO versions VALUES (?,?)",
                               (key, version if isinstance(version, int) and version > 0 else 0))
                db.execute("DELETE FROM inflight WHERE path=?", (key,))
                db.commit()
            with _state_lock:
                _state["done"] += 1
                _state["errors"] += bool(err)
                t = _state.get("by_type", {}).get(_type_key(r["path"]))
                if t:
                    t[0] += 1
                    t[2] += bool(err)
            _save_progress()

        if suspects:
            _set(phase=f"Re-checking {len(suspects)} suspect file(s) one by one")
            with ThreadPoolExecutor(max_workers=1, initializer=_com_init) as single:
                for r in suspects:
                    store(single.submit(work, r).result())
            _set(phase="Reading assemblies and drawings")
        with ThreadPoolExecutor(max_workers=max(1, min(WORKERS, 8)), initializer=_com_init) as pool:
            for fut in as_completed([pool.submit(work, r) for r in todo]):
                store(fut.result())

        _apply_hints_to_index()                       # files read before the assembly that found it out
        _meta_set("last_update", str(time.time()))
        _meta_set("part_refs_off", PART_REFS["why"] if not PART_REFS["enabled"] else None)

        why = ", ".join(f"{n} {r}" for r, n in sorted(reasons.items(), key=lambda x: -x[1]))
        extra = f" Skipped (not reachable): {', '.join(unreachable)}." if unreachable else ""
        _set(message=f"{len(todo) + len(suspects)} file(s) read{f' ({why})' if why else ''}, "
                     f"{len(gone)} removed from the index.{extra}")
    except Exception as e:  # noqa: BLE001
        _set(message=f"Update failed: {human_error(e)}")
    finally:
        _set(running=False, phase="", finished=time.time())


def _mark_inflight_crashed(reason, timeout=False):
    """After a crash or time-out of the worker: skip the file(s) that were open at that moment."""
    with _db_lock:
        db = conn()
        rows = db.execute("SELECT path, display_path, mtime, size FROM inflight").fetchall()
        single = len(rows) == 1
        for key, display, mtime, size in rows:
            db.execute("DELETE FROM refs WHERE parent=?", (key,))
            if single and timeout:
                st, msg = "timeout", f"no answer within {FILE_TIMEOUT} seconds; retried at the next update"
            elif single:
                st, msg = "crash", (f"the Document Manager crashed on this file ({reason}); "
                                    "skipped until the file changes")
            else:
                st, msg = "suspect", "was open during a crash; will be retried on its own"
            db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                       (key, display, mtime, size, time.time(), st, msg, 0, None))
        db.execute("DELETE FROM inflight")
        db.commit()
    return [r[1] for r in rows], single


def _record_run(run_started, finished, message, ok_before):
    """Keep how long an index run took and what it read. A run can span several index processes (after a crash
    the process is restarted), so the numbers come from the index itself: everything read since the start."""
    with _db_lock:
        db = conn()
        rows = db.execute("SELECT path, status FROM files WHERE indexed_at >= ?", (run_started,)).fetchall()
        in_index = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        by_type = {"asm": 0, "drw": 0, "prt": 0}
        errors = 0
        for path, st in rows:
            by_type[_type_key(path)] += 1
            errors += st != "ok"
        # Full: the first run, or most of the index read again (for example once for the SOLIDWORKS version)
        full = int(bool(rows) and (ok_before == 0 or len(rows) >= 0.8 * max(1, in_index)))
        db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                   (run_started, finished, len(rows), errors, json.dumps(by_type), full, message))
        db.execute("DELETE FROM runs WHERE rowid NOT IN (SELECT rowid FROM runs ORDER BY started DESC LIMIT 50)")
        db.commit()


def run_history(limit=10):
    with _db_lock:
        rows = conn().execute("SELECT started, finished, files, errors, by_type, full, message FROM runs "
                              "ORDER BY started DESC LIMIT ?", (limit,)).fetchall()
    return [{"started": a, "finished": b, "seconds": round((b or a) - a), "files": f, "errors": e,
             "by_type": json.loads(t or "{}"), "full": bool(fu), "message": m} for a, b, f, e, t, fu, m in rows]


_index_lock_file = None


def _single_index_process():
    """Hold an exclusive lock on index.lock for as long as this index process lives. A second one (left over,
    or started twice) cannot get it and stops. Windows releases the lock when the process ends, also on a crash."""
    global _index_lock_file
    f = None
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        f = open(DATA_DIR / "index.lock", "a+")
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _index_lock_file = f
        return True
    except OSError:
        if f:
            f.close()
        return False


def _supervise():
    """Server side: start the worker process and watch it; after a crash, skip the culprit and go on."""
    restarts, crashed_all, final = 0, [], None
    run_started = time.time()
    with _db_lock:
        ok_before = conn().execute("SELECT COUNT(*) FROM files WHERE status='ok'").fetchone()[0]
    _set(running=True, phase="Starting the index process", total=0, done=0, errors=0,
         started=run_started, finished=None, message=None)
    try:
        while True:
            _meta_set("progress", None)
            _meta_set("run_started", str(run_started))
            args = [PY_CHILD, str(Path(__file__).resolve()), "--index-worker"]
            proc = subprocess.Popen(args, cwd=str(HERE), creationflags=_quiet_flags())
            _children.add(proc)
            code, killed, timed_out = None, False, False
            while code is None:
                try:
                    code = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    with _db_lock:
                        oldest = conn().execute("SELECT MIN(started) FROM inflight").fetchone()[0]
                    if oldest and time.time() - oldest > FILE_TIMEOUT:
                        with _db_lock:          # only the hanging file stays 'in flight'
                            db = conn()
                            db.execute("DELETE FROM inflight WHERE started > ?", (time.time() - FILE_TIMEOUT,))
                            db.commit()
                        _kill_tree(proc)
                        killed = timed_out = True
                        continue
                    beat = _read_progress().get("heartbeat")
                    if beat and time.time() - beat > WATCHDOG_SECONDS and not oldest:
                        _kill_tree(proc)        # no progress and nothing open: stuck
                        killed = True
            prog = _read_progress()
            if code == 0 and not killed:
                final = prog.get("message") or "Index updated."
                break
            reason = (f"no answer within {FILE_TIMEOUT} s" if timed_out
                      else "stuck, stopped" if killed else f"exit code {code}")
            crashed, single = _mark_inflight_crashed(reason, timeout=timed_out)
            if single and not timed_out:
                crashed_all += crashed
            restarts += 1
            names = ", ".join(_fname(c) for c in crashed) or "unknown"
            print(f"Index process: {reason}; " + (f"skipped: {names}" if single else
                  f"suspect (retried one by one): {names}") + f". Restarting ({restarts}).")
            if not crashed or restarts >= 200:
                final = f"The index process stopped unexpectedly ({reason}). See crash_index.log in {DATA_DIR}."
                break
    except Exception as e:  # noqa: BLE001
        final = f"Update failed: {human_error(e)}"
    finally:
        if crashed_all and final:
            final += f" {len(crashed_all)} file(s) skipped because the Document Manager crashed on them."
        finished = time.time()
        try:
            _record_run(run_started, finished, final, ok_before)
        except sqlite3.Error:
            pass
        _set(running=False, phase="", finished=finished, message=final)
        threading.Thread(target=_warm_statistics, daemon=True, name="stats").start()


def update_index(retry_errors=False):
    with _state_lock:
        if _state["running"]:
            return False
        _state["running"] = True
    if retry_errors:
        _meta_set("retry_errors", "1")
    threading.Thread(target=_supervise, daemon=True, name="index").start()
    return True


_last_request = {"t": time.time()}


def maintain_index(force=False):
    """Tidy the index up (SQLite: fold the write-ahead log in, ANALYZE, VACUUM): smaller and a little faster after
    many index runs. Once a week, only when no index run or rename is busy and nobody used the app for 10 minutes."""
    try:
        last = json.loads(_meta_get("maintained") or "{}")
    except ValueError:
        last = {}
    if not force:
        if time.time() - last.get("time", 0) < 7 * 86400 or time.time() - _last_request["t"] < 600:
            return None
        if _state.get("running") or _rename_busy.locked():
            return None
    size = lambda: sum(os.path.getsize(str(db_path()) + x) for x in ("", "-wal") if os.path.exists(str(db_path()) + x))  # noqa: E731
    before, t0 = size(), time.time()
    with _db_lock:
        db = conn()
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("ANALYZE")
        db.execute("VACUUM")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    done = {"time": time.time(), "before_mb": round(before / 1048576, 1), "after_mb": round(size() / 1048576, 1),
            "seconds": round(time.time() - t0, 1)}
    _meta_set("maintained", json.dumps(done))
    return done


def _scheduler(tick=15):
    """Update at start and every interval. When the setup is not complete yet or Everything is not running
    (yet), keep checking every few seconds and start as soon as it can: no waiting for the next interval."""
    first = UPDATE_ON_START == "yes"
    last = time.time()
    while True:
        due = first or (INTERVAL_MIN > 0 and time.time() - last >= INTERVAL_MIN * 60)
        if due and not sw_problem() and index_source()[0]:
            if update_index() or first:
                first = False
                last = time.time()
        try:
            maintain_index()                       # once a week, only when nothing happens
        except Exception:  # noqa: BLE001 — never mind: next time
            pass
        time.sleep(tick)


_PATHLIKE_RE = re.compile(r"(?:[A-Za-z]:\\|\\\\)[^;,()]*")


def problems(limit=500):
    with _db_lock:
        db = conn()
        rows = db.execute("SELECT display_path, status, error, indexed_at FROM files "
                          "WHERE status IN ('error','crash','suspect','timeout') ORDER BY indexed_at DESC LIMIT ?",
                          (limit,)).fetchall()
        allrows = db.execute("SELECT path, status, error FROM files "
                             "WHERE status IN ('error','crash','suspect','timeout')").fetchall()
    groups = {}
    for path, st, error in allrows:
        kind = "drawing" if path.endswith(".slddrw") else "assembly"
        reason = _PATHLIKE_RE.sub("…", error or "unknown").strip()
        g = groups.setdefault((kind, st, reason), {"kind": kind, "status": st, "reason": reason,
                                                   "count": 0, "example": path})
        g["count"] += 1
    return {"problems": [{"path": p, "status": s, "error": e, "time": t} for p, s, e, t in rows],
            "summary": sorted(groups.values(), key=lambda g: -g["count"])}


# ---------------------------------------------------------------------------
# Where used
# ---------------------------------------------------------------------------
# The number GetVersion returns, per SOLIDWORKS release (the table from the Document Manager API help, 2000 to 2026).
# Up to 2012 the steps are irregular, so they are listed; from 2013 on it goes up by exactly 1000 a year, so newer
# releases work out without changing this table.
_SW_VERSIONS = [(44, "95"), (243, "96"), (483, "97"), (629, "97 Plus"), (822, "98"), (1008, "98 Plus"), (1137, "99"),
                (1500, "2000"), (1750, "2001"), (1950, "2001 Plus"), (2200, "2003"), (2500, "2004"), (2800, "2005"),
                (3100, "2006"), (3400, "2007"), (3800, "2008"), (4100, "2009"), (4400, "2010"), (4700, "2011"),
                (5000, "2012")]


def sw_release(version):
    """'2024' for 17000 (the release a file was last saved with); None when unknown."""
    if not isinstance(version, int) or version <= 0:
        return None
    if version >= 6000:
        return str(2013 + (version - 6000) // 1000)
    name = None
    for v, n in _SW_VERSIONS:
        if version >= v:
            name = n
    return name


def _type_key(p):
    low = p.lower()
    return "drw" if low.endswith(".slddrw") else "prt" if low.endswith(".sldprt") else "asm"


def _kind_of(p):
    low = p.lower()
    return "drawing" if low.endswith(".slddrw") else "part" if low.endswith(".sldprt") else "assembly"


def _fname(p):
    """File name from a Windows or network path (backslashes, on any system)."""
    return (p or "").replace("/", "\\").split("\\")[-1]


def _path_tail(p):
    return "\\".join(PureWindowsPath(p).parts[1:]).lower()


_reach_cache = {}
_reach_lock = threading.Lock()


def _root_reachable_cached(root, ttl=300, timeout=3):
    """Remembered for five minutes: an old server in a stored path would otherwise cost a time-out every time."""
    with _reach_lock:
        hit = _reach_cache.get(root.lower())
        if hit and time.time() - hit[1] < ttl:
            return hit[0]
    ok = _reachable(root, timeout=timeout)
    with _reach_lock:
        _reach_cache[root.lower()] = (ok, time.time())
    return ok


NOT_THIS_FILE = ("other_copy", "own_copy")


def match_kind(stored, target, isfile_cache=None, parent=None):
    """How does the path stored in the parent relate to the file we are looking for?
    own_copy: the saved path is gone, and the parent's own folder holds a file with that name (and type) that is not
    this one. SOLIDWORKS looks there first, so the parent uses ITS file: a copy of the project in another folder."""
    if not stored:
        return "path_missing"
    if stored.lower() == target.lower():
        return "exact"
    if _path_tail(stored) == _path_tail(target):
        return "remapped"                   # same place, other drive letter or network path
    if _TEMP_PATH_RE.search(stored):
        return "path_missing"
    key = stored.lower()
    if isfile_cache is not None and key in isfile_cache:
        exists = isfile_cache[key]
    else:
        exists = _root_reachable_cached(_root_of(stored)) and os.path.isfile(stored)
        if isfile_cache is not None:
            isfile_cache[key] = exists
    if not exists:
        if parent:
            own = _join(_folder_of(parent) or "", _fname(stored))
            if own.lower() != target.lower():
                okey = ("own", own.lower())
                if isfile_cache is not None and okey in isfile_cache:
                    there = isfile_cache[okey]
                else:
                    there = _root_reachable_cached(_root_of(own)) and os.path.isfile(own)
                    if isfile_cache is not None:
                        isfile_cache[okey] = there
                if there:
                    return "own_copy"
        return "path_missing"                  # stored path gone: most likely this file
    # It exists. Is it really another file, or the same file through another route (a drive letter mapped
    # deeper into a share, subst, a junction)? Windows can tell by the file's identity.
    same_key = ("same", key, target.lower())
    if isfile_cache is not None and same_key in isfile_cache:
        same = isfile_cache[same_key]
    else:
        try:
            same = os.path.samefile(stored, target)
        except OSError:
            same = False
        if isfile_cache is not None:
            isfile_cache[same_key] = same
    return "remapped" if same else "other_copy"


def whereused(path, config=None, include_other=False, with_top=True):
    """Direct parents (assemblies and drawings) and the top-level assemblies above them."""
    name = _fname(path).lower()
    isfile_cache, info = {}, {}

    def parents_of(child_name):
        with _db_lock:
            return conn().execute("SELECT parent, parent_config, child_stored_path, child_config, qty, suppressed "
                                  "FROM refs WHERE child_name=? AND virtual=0", (child_name,)).fetchall()

    def load_info(paths):
        need = [p for p in paths if p not in info]
        for i in range(0, len(need), 500):
            chunk = need[i:i + 500]
            with _db_lock:
                rows = conn().execute(f"SELECT path, display_path, description FROM files "
                                      f"WHERE path IN ({','.join('?' * len(chunk))})", chunk).fetchall()
            info.update({p: (d, desc) for p, d, desc in rows})

    disp = lambda p: (info.get(p) or (p, None))[0]           # noqa: E731
    direct_rows = parents_of(name)
    load_info({r[0] for r in direct_rows})
    child_configs = sorted({r[3] for r in direct_rows if r[3]}, key=str.lower)
    direct, hidden, rank = {}, {}, {"exact": 0, "remapped": 1, "path_missing": 2, "own_copy": 3, "other_copy": 4}
    for par, pcfg, stored, ccfg, qty, supp in direct_rows:
        if config and ccfg and ccfg != config:
            continue
        match = match_kind(stored, path, isfile_cache, disp(par))
        if match in NOT_THIS_FILE and not include_other:
            hidden.setdefault(par, stored)
            continue
        d = direct.setdefault(par, {"path": disp(par), "description": (info.get(par) or (None, None))[1],
                                    "match": match, "stored_path": stored,
                                    "kind": _kind_of(par), "configs": {}})
        if rank[match] < rank[d["match"]]:          # several instances: show the best way it is found
            d.update(match=match, stored_path=stored)
        # Same parent configuration + same child configuration (e.g. saved under two paths): add up
        c = d["configs"].setdefault((pcfg, ccfg, int(supp or 0)), {"config": pcfg, "qty": 0, "child_config": ccfg,
                                                                   "counted": not supp, **_flags(supp)})
        c["qty"] += qty
    for d in direct.values():
        d["configs"] = sorted(d["configs"].values(), key=lambda c: (c["config"].lower(), not c["counted"],
                                                                    c["child_config"].lower()))

    top, budget, cache = {}, [20000], {}

    def climb(asm, acfg, factor, chain):
        if budget[0] <= 0 or len(chain) > 40:
            return
        budget[0] -= 1
        a_name = _fname(disp(asm)).lower()
        if a_name not in cache:
            cache[a_name] = parents_of(a_name)
            load_info({r[0] for r in cache[a_name]})
        ups = []
        for p_asm, p_cfg, stored, ccfg, qty, supp in cache[a_name]:
            if supp or (ccfg and ccfg != acfg) or p_asm in chain or not p_asm.endswith(".sldasm"):
                continue                    # only assemblies contain it; drawings and derived parts are no level above
            if match_kind(stored, disp(asm), isfile_cache, disp(p_asm)) in NOT_THIS_FILE and not include_other:
                continue
            ups.append((p_asm, p_cfg, qty))
        if not ups:
            t = top.setdefault((asm, acfg), {"path": disp(asm), "description": (info.get(asm) or (None, None))[1],
                                             "config": acfg, "qty": 0, "chains": 0, "example": chain})
            t["qty"] += factor
            t["chains"] += 1
            return
        for p_asm, p_cfg, qty in ups:
            climb(p_asm, p_cfg, factor * qty, chain + [p_asm])

    for asm, d in direct.items():
        if d["kind"] != "assembly" or not with_top:
            continue
        for c in d["configs"]:
            if c["counted"]:
                climb(asm, c["config"], c["qty"], [asm])

    tops = sorted(top.values(), key=lambda t: (_fname(t["path"]).lower(), t["config"]))
    for t in tops:
        t["example"] = [disp(a) for a in t["example"]]
    hidden = {p: st for p, st in hidden.items() if p not in direct}     # also used through this file: not hidden
    other_files = sorted({st for st in hidden.values()}, key=str.lower)
    return {"target": path, "name": _fname(path), "config": config, "child_configs": child_configs,
            "hidden_other": len(hidden), "other_files": other_files[:5],
            "direct": sorted(direct.values(), key=lambda d: ({"assembly": 0, "part": 1}.get(d["kind"], 2),
                                                             _fname(d["path"]).lower())),
            "top": tops, "truncated": budget[0] <= 0, "index": status()}


def usage_for_paths(paths):
    """For each file: how many indexed assemblies/drawings use THIS file, and how many use a different
    file with the same name. Decided exactly like the where-used page, so the numbers match."""
    return {k: (len(used), len(other)) for k, (used, other) in parents_for_paths(paths).items()}


def parents_for_paths(paths):
    """For each file: the indexed assemblies/drawings (lower-case paths) that use THIS file, and those that use a
    different file with the same name."""
    names = sorted({_fname(p).lower() for p in paths})
    by_name = {}
    for i in range(0, len(names), 500):
        chunk = names[i:i + 500]
        with _db_lock:
            rows = conn().execute(f"SELECT child_name, parent, child_stored_path FROM refs WHERE virtual=0 AND "
                                  f"child_name IN ({','.join('?' * len(chunk))})", chunk).fetchall()
        for name, parent, stored in rows:
            by_name.setdefault(name, []).append((parent, stored))
    cache, out = {}, {}
    shown = _file_maps()["indexed"]                   # display paths of the parents (their folder matters)
    for p in paths:
        used, other = set(), set()
        for parent, stored in by_name.get(_fname(p).lower(), []):
            (other if match_kind(stored, p, cache, shown.get(parent, parent)) in NOT_THIS_FILE else used).add(parent)
        out[p.lower()] = (used, other - used)
    return out


# ---------------------------------------------------------------------------
# Structure of an assembly: the tree (as in the FeatureManager) and the flat list (every file once)
# ---------------------------------------------------------------------------
STRUCTURE_MAX_NODES = 20000


_listing_cache = {}
_listing_lock = threading.Lock()
LISTING_TTL = 60


def _folder_names(folder):
    """Lower-case names in a folder (None = cannot be read), remembered for a minute."""
    low = folder.lower()
    with _listing_lock:
        hit = _listing_cache.get(low)
        if hit and time.time() - hit[1] < LISTING_TTL:
            return hit[0]
    names = None
    if not _TEMP_PATH_RE.search(folder) and _root_reachable_cached(_root_of(folder)):
        try:
            with os.scandir(folder) as it:
                names = {e.name.lower() for e in it}
        except OSError:
            names = None
    with _listing_lock:
        _listing_cache[low] = (names, time.time())
    return names


def _existing_files(paths):
    """Which of these files exist? One folder listing per folder, all folders at the same time (on a network
    share every listing waits for the server), and remembered for a minute. Returns lower-case paths."""
    by_folder = {}
    for p in paths:
        folder = _folder_of(p)
        if folder:
            by_folder.setdefault(folder.lower(), (folder, set()))[1].add(_fname(p).lower())
    found = set()
    if not by_folder:
        return found
    items = list(by_folder.items())
    with ThreadPoolExecutor(max_workers=min(16, len(items))) as pool:
        listings = list(pool.map(lambda it: _folder_names(it[1][0]), items))
    for (low, (_folder, names)), present in zip(items, listings):
        if present:
            sep = "/" if "/" in low and "\\" not in low else "\\"
            found |= {low.rstrip(sep) + sep + n for n in names & present}
    return found


def _pick_config(configs, wanted=None):
    if wanted and wanted in configs:
        return wanted
    for c in configs:
        if c.lower() in ("default", "standaard", "standard", "vorgabe", "défaut"):
            return c
    return sorted(configs, key=str.lower)[0] if configs else ""


_index_maps = {"key": None}
_structure_cache = {}
STRUCTURE_CACHE_SECONDS = 60


def _file_maps():
    """Indexed files (path -> display), by name, and descriptions; kept until the index changes."""
    key = _tree_key()
    if _index_maps.get("key") == key:
        return _index_maps
    with _db_lock:
        rows = conn().execute("SELECT path, display_path, description, status, mtime FROM files").fetchall()
    indexed = {p: d for p, d, _desc, st, _m in rows if st == "ok"}
    by_name = {}
    for low, disp in indexed.items():
        by_name.setdefault(_fname(low), []).append(disp)
    _index_maps.update(key=key, indexed=indexed, by_name=by_name,
                       desc={p: desc for p, _d, desc, _st, _m in rows if desc}, mtime={p: m for p, _d, _de, _st, m in rows},
                       drawings={p.rsplit(".", 1)[0] for p in indexed if p.endswith(".slddrw")})
    return _index_maps


def structure(path, config=None, with_usage=True):
    """The build-up of an assembly from the index. Nothing is read from the CAD files themselves.
    Remembered for a minute (and only as long as the index does not change)."""
    t0 = time.perf_counter()
    root_low = path.lower()
    maps = _file_maps()
    ck = (root_low, config or "", with_usage, maps["key"])
    hit = _structure_cache.get(ck)
    if hit and time.time() - hit[1] < STRUCTURE_CACHE_SECONDS:
        return {**hit[0], "ms": round((time.perf_counter() - t0) * 1000), "cached": True}
    indexed, by_name, desc = maps["indexed"], maps["by_name"], maps["desc"]

    renamed = _renamed_map()
    with _db_lock:                                  # renames another tool did, found per assembly (names changed)
        detected_here = {(par, new.lower()): (old, at) for par, old, new, at in
                         conn().execute("SELECT parent, old, new, at FROM detected_refs")
                         if _fname(old).lower() != _fname(new).lower()}

    def resolve(stored, name, parent=None):
        """Which file is meant? ('path', 'how'): how = indexed, stored, by_name (missing is decided later)."""
        if stored and stored.lower() in renamed:
            stored = apply_rename(stored, renamed)
            name = _fname(stored).lower()
        low = (stored or "").lower()
        if low in indexed:
            return indexed[low], "indexed"
        cands = by_name.get(name, [])
        tail = _path_tail(stored) if stored else None
        for c in cands:
            if tail and _path_tail(c) == tail:
                return c, "indexed"               # same place, other drive letter or share
        if parent:                                # SOLIDWORKS looks in the assembly's own folder first
            own = _join(_folder_of(parent) or "", _fname(stored or name)).lower()
            if own in indexed and own != low:
                return indexed[own], "by_name"
        if len(cands) == 1 and not name.endswith(".sldprt"):
            return cands[0], "by_name"            # moved: SOLIDWORKS would look for it by name
        return stored or name, "stored"

    # All references of every assembly in the structure, fetched level by level: a few queries in total
    rows_of = {}
    frontier = {root_low}
    while frontier:
        need = sorted(frontier - rows_of.keys())
        frontier = set()
        for i in range(0, len(need), 500):
            chunk = need[i:i + 500]
            with _db_lock:
                got = conn().execute(
                    f"SELECT parent, parent_config, child_name, child_stored_path, child_config, qty, suppressed, virtual "
                    f"FROM refs WHERE parent IN ({','.join('?' * len(chunk))}) ORDER BY rowid", chunk).fetchall()
            for parent in chunk:
                rows_of.setdefault(parent, {})
            for parent, pcfg, name, stored, ccfg, qty, supp, virt in got:
                rows_of[parent].setdefault(pcfg, []).append((name, stored, ccfg, qty, supp, virt))
                if name.endswith(".sldasm") and not virt:
                    real, how = resolve(stored, name, indexed.get(parent, parent))
                    if how != "stored" and real.lower() not in rows_of:
                        frontier.add(real.lower())
    configs = sorted(rows_of.get(root_low, {}), key=str.lower)
    if root_low not in indexed and not configs:
        return {"path": path, "known": False, "configs": [], "config": None, "tree": [], "flat": [],
                "message": "This assembly is not in the index (yet)."}
    config = _pick_config(configs, config)

    count = [0]
    nodes_all = []

    def build(asm_low, cfg, chain):
        out = []
        for name, stored, ccfg, qty, supp, virt in rows_of.get(asm_low, {}).get(cfg, []):
            if count[0] >= STRUCTURE_MAX_NODES:
                break
            count[0] += 1
            parent_path = indexed.get(asm_low, asm_low)
            real, how = resolve(stored, name, parent_path)
            low_real = real.lower()                         # name and kind from the file that is meant
            kind = "asm" if low_real.endswith(".sldasm") else "part" if low_real.endswith(".sldprt") else "other"
            node = {"name": _fname(real), "path": real, "stored_path": stored, "parent_path": parent_path,
                    "config": ccfg, "qty": qty,
                    **_flags(supp), "virtual": bool(virt), "kind": kind, "how": how,
                    "description": desc.get(real.lower())}
            # Renamed by this app (or found renamed by another tool, for this assembly), and the assembly not saved
            # by SOLIDWORKS since: inside, it still lists the old name
            was = _renamed_from(real) or detected_here.get((asm_low, low_real))
            if was and (maps.get("mtime", {}).get(asm_low) or 0) <= was[1]:
                node["renamed_from"] = was[0]
            nodes_all.append(node)
            if kind == "asm" and not virt:
                child_low = real.lower()
                if child_low in chain:
                    node["cycle"] = True
                elif how in ("indexed", "by_name"):
                    use = _pick_config(list(rows_of.get(child_low, {})), ccfg)
                    node["config_used"] = use
                    node["children"] = build(child_low, use, chain | {child_low})
                else:
                    node["not_indexed"] = True
            out.append(node)
        return out

    tree = build(root_low, config, {root_low})

    # Which files exist (for parts: the index does not know them): one listing per folder
    maybe = [n["path"] for n in nodes_all if n["how"] == "stored" and not n["virtual"]]
    existing = _existing_files(maybe)
    for n in nodes_all:
        if n["how"] == "stored" and not n["virtual"]:
            n["how"] = "stored" if n["path"].lower() in existing else "missing"
    # Not at its saved path, but a file with that name is used elsewhere in this assembly: SOLIDWORKS takes the
    # file with that name that is already loaded, so this is the same file.
    loaded = {}
    for n in nodes_all:
        if n["how"] in ("stored", "indexed") and not n["virtual"]:
            loaded.setdefault(n["name"].lower(), n["path"])
    for n in nodes_all:
        if n["how"] == "missing" and n["name"].lower() in loaded:
            n["path"], n["how"] = loaded[n["name"].lower()], "by_name"
            n["name"] = _fname(n["path"])
    # Still not found: a file with that name in the assembly's own folder (the next place SOLIDWORKS looks)
    still = [n for n in nodes_all if n["how"] == "missing" and n.get("parent_path")]
    own_of = {id(n): _join(_folder_of(n["parent_path"]) or "", _fname(n["stored_path"] or n["name"])) for n in still}
    there = _existing_files(list(own_of.values())) if still else set()
    for n in still:
        own = own_of[id(n)]
        if own.lower() in there:
            n["path"], n["how"], n["name"] = own, "by_name", _fname(own)

    # Flat: every file once; quantities multiplied down the levels (suppressed branches do not count)
    flat = {}

    def walk(nodes, mult, parent):
        for n in nodes:
            if n["virtual"]:
                continue
            key = n["path"].lower()
            f = flat.get(key)
            if f is None:
                f = flat[key] = {"name": n["name"], "path": n["path"], "kind": n["kind"], "qty": 0, "configs": set(),
                                 "parents": set(), "how": n["how"], "suppressed_only": True,
                                 "description": n["description"]}
            f["parents"].add(parent)
            if n["config"]:
                f["configs"].add(n["config"])
            if not (n["suppressed"] or n["envelope"] or n["not_in_bom"]):
                f["qty"] += mult * n["qty"]
                f["suppressed_only"] = False
                walk(n.get("children") or [], mult * n["qty"], key)

    walk(tree, 1, root_low)
    # 6. A drawing with the same name in the same folder, and 5. the extra properties, for every file
    drawings = maps.get("drawings", set())
    props_of = {}
    keys = list(flat)
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        with _db_lock:
            for pth, name, value in conn().execute(
                    f"SELECT path, name, value FROM props WHERE path IN ({','.join('?' * len(chunk))})", chunk):
                props_of.setdefault(pth, {})[name] = value
    for n in nodes_all:
        low = n["path"].lower()
        n["has_drawing"] = low.rsplit(".", 1)[0] in drawings
        n["props"] = props_of.get(low, {})
    for key_, f in flat.items():
        f["has_drawing"] = key_.rsplit(".", 1)[0] in drawings
        f["props"] = props_of.get(key_, {})
    usage = usage_for_paths([f["path"] for f in flat.values()]) if with_usage else {}
    flat_list = []
    for key, f in flat.items():
        used, other = usage.get(key, (None, 0))
        flat_list.append({**f, "configs": sorted(f["configs"], key=str.lower), "parents": len(f["parents"]),
                          "used_in": used, "other_copies": other})
    flat_list.sort(key=lambda f: (f["kind"] != "asm", f["name"].lower()))
    result = {"path": indexed.get(root_low, path), "known": True, "configs": configs, "prop_names": list(EXTRA_PROPS),
              "config": config, "tree": tree, "flat": flat_list, "nodes": count[0],
              "truncated": count[0] >= STRUCTURE_MAX_NODES, "ms": round((time.perf_counter() - t0) * 1000),
              "files": sorted({f["path"] for f in flat_list} | {indexed.get(root_low, path)}, key=str.lower),
              "root_description": desc.get(root_low)}
    if len(_structure_cache) > 20:
        _structure_cache.clear()
    _structure_cache[ck] = (result, time.time())
    _structure_cache[(root_low, config, with_usage, maps["key"])] = (result, time.time())
    return result


def compact_structure(s):
    """The structure for the browser: every file once, the components refer to it by number.
    (A big assembly repeats the same paths thousands of times; this makes the answer several times smaller.)"""
    if not s.get("known"):
        return s
    files, index = [], {}

    def ref(n):
        key = (n["path"].lower(), n["how"])
        i = index.get(key)
        if i is None:
            i = index[key] = len(files)
            info = {"path": n["path"], "kind": n["kind"], "how": n["how"], "drw": bool(n.get("has_drawing"))}
            if n.get("description"):
                info["description"] = n["description"]
            if n.get("props"):
                info["props"] = n["props"]
            files.append(info)
        return i

    def node(n):
        out = {"f": ref(n), "q": n["qty"]}
        if n.get("stored_path") and n["stored_path"].lower() != n["path"].lower():
            out["sp"] = n["stored_path"]              # found by name: show where it was saved, on hover
        if n["config"]:
            out["c"] = n["config"]
        if n.get("renamed_from"):
            out["rf"] = n["renamed_from"]
        for k, short in (("suppressed", "s"), ("envelope", "e"), ("not_in_bom", "b"), ("virtual", "v"),
                         ("cycle", "x"), ("not_indexed", "n")):
            if n.get(k):
                out[short] = 1
        if n.get("children"):
            out["k"] = [node(c) for c in n["children"]]
        return out

    tree = [node(n) for n in s["tree"]]
    flat = [{"f": ref(f), "qty": f["qty"], "configs": f["configs"], "parents": f["parents"],
             "suppressed_only": f["suppressed_only"], "used_in": f["used_in"], "other_copies": f["other_copies"]}
            for f in s["flat"]]
    return {**{k: v for k, v in s.items() if k not in ("tree", "flat")}, "compact": True, "fileinfo": files,
            "tree": tree, "flat": flat}


# ---------------------------------------------------------------------------
# Statistics about the vault, from the index
# ---------------------------------------------------------------------------
_stats_cache = {"key": None}


_STOP = set("""a an and the of for with to in on at by or from met en de het een van voor op aan in
    bij of uit naar tot per te als zonder onder boven""".split())


def _version_counts(ok, version_of):
    """How many files per SOLIDWORKS release they were last saved with, newest first."""
    per, pending, unknown = {}, 0, 0
    for p, _d, _m, _sz, _desc in ok:
        v = version_of.get(p)
        if v is None:
            pending += 1                         # indexed before versions were stored; read at the next update
            continue
        rel = sw_release(v)
        if not rel:
            unknown += 1
            continue
        e = per.setdefault(rel, {"release": rel, "number": v, "asm": 0, "drw": 0, "prt": 0})
        e["number"] = min(e["number"], v)
        e[_type_key(p)] += 1
    return {"versions": sorted(per.values(), key=lambda e: -e["number"]),
            "versions_pending": pending, "versions_unknown": unknown}


def _fun_facts(ok, parents_of, configs, deepest, version_of=None):
    """Small, surprising facts about the files. ok: (path, display, mtime, size, description) of readable files."""
    fun = {}
    # Real dates only: after 1980, and not in the future (a pc with a wrong clock, odd copies)
    now = time.time() + 86400
    dated = [(m, d, p) for p, d, m, _sz, _desc in ok if m and 315532800 < m < now]
    used = [(m, d, p) for m, d, p in dated if _fname(p) in parents_of]
    if used:
        m, d, p = min(used)
        fun["oldest_used"] = {"path": d, "time": m, "used_in": len(parents_of[_fname(p)])}
    if dated:
        m, d, _p = max(dated)
        fun["newest"] = {"path": d, "time": m}
        days, weekdays, hours, night = {}, [0] * 7, [0] * 24, 0
        for m, _d, _p in dated:
            t = time.localtime(m)
            days[time.strftime("%Y-%m-%d", t)] = days.get(time.strftime("%Y-%m-%d", t), 0) + 1
            weekdays[t.tm_wday] += 1
            hours[t.tm_hour] += 1
            night += t.tm_hour >= 22 or t.tm_hour < 6
        day, count = max(days.items(), key=lambda x: (x[1], x[0]))
        if count > 1:
            fun["busiest_day"] = {"date": day, "count": count}
        fun["weekday"] = {"day": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][weekdays.index(max(weekdays))],
                          "count": max(weekdays), "weekend": weekdays[5] + weekdays[6]}
        fun["hour"] = {"hour": hours.index(max(hours)), "count": max(hours), "night": night}
    if ok:
        p, d, _m, sz, _desc = max(ok, key=lambda x: x[3] or 0)
        fun["biggest"] = {"path": d, "size": sz or 0}
        longest = max(ok, key=lambda x: len(x[1]))
        fun["longest_path"] = {"path": longest[1], "length": len(longest[1]),
                               "near_limit": sum(1 for x in ok if len(x[1]) >= 230)}
        fun["total_size"] = sum(x[3] or 0 for x in ok)
    asm_cfg = next(((d, c) for p, d, c in configs if p.endswith(".sldasm")), None)
    drw_sheets = next(((d, c) for p, d, c in configs if p.endswith(".slddrw")), None)
    if asm_cfg:
        fun["most_configs"] = {"path": asm_cfg[0], "count": asm_cfg[1]}
    if drw_sheets:
        fun["most_sheets"] = {"path": drw_sheets[0], "count": drw_sheets[1]}
    if deepest and deepest["levels"] > 1:
        fun["deepest"] = deepest
    # The oldest SOLIDWORKS release among files that are still used somewhere
    old = [((version_of or {}).get(p), d, p) for p, d, _m, _sz, _desc in ok
           if (version_of or {}).get(p) and _fname(p) in parents_of]
    if old:
        v, d, p = min(old)
        if sw_release(v):
            fun["oldest_release_used"] = {"path": d, "release": sw_release(v), "used_in": len(parents_of[_fname(p)])}
    words = {}
    for _p, _d, _m, _sz, desc in ok:
        for w in re.findall(r"[^\W\d_]{3,}", (desc or "").lower()):
            if w not in _STOP:
                words[w] = words.get(w, 0) + 1
    fun["words"] = [{"word": w, "count": c} for w, c in sorted(words.items(), key=lambda x: (-x[1], x[0]))[:5]]
    return fun


def _largest_assemblies(asm_refs, indexed, top=8):
    """The largest assemblies counted over ALL levels: different components (parts and subassemblies, each file
    once) and pieces (quantities multiplied down), in each assembly's largest configuration. Every subassembly
    configuration is worked out once and reused, so this stays quick for a whole vault."""
    rows_of = {}
    for parent, pcfg, name, stored, ccfg, qty, supp in asm_refs:
        rows_of.setdefault(parent, {}).setdefault(pcfg, []).append((name, stored, ccfg, qty, supp))
    by_name = {}
    for low in indexed:
        if low.endswith(".sldasm"):
            by_name.setdefault(_fname(low), []).append(low)

    def resolve(stored, name):
        low = (stored or "").lower()
        if low in rows_of:
            return low
        cands = by_name.get(name, [])
        tail = _path_tail(stored) if stored else None
        for c in cands:
            if tail and _path_tail(c) == tail:
                return c
        return cands[0] if len(cands) == 1 else None      # moved: SOLIDWORKS finds it by name

    memo, busy = {}, set()

    def calc(asm, cfg):
        key = (asm, cfg)
        if key in memo:
            return memo[key]
        if key in busy:                                   # an assembly inside itself (damaged files): stop
            return frozenset(), 0, 0
        busy.add(key)
        names, pieces, depth = set(), 0, 1
        for name, stored, ccfg, qty, supp in rows_of.get(asm, {}).get(cfg, []):
            if supp:
                continue
            names.add(name)
            below = 0
            if name.endswith(".sldasm"):
                child = resolve(stored, name)
                if child:
                    sub_names, below, sub_depth = calc(child, _pick_config(list(rows_of.get(child, {})), ccfg))
                    names |= sub_names
                    depth = max(depth, sub_depth + 1)
            pieces += qty * (1 + below)
        busy.discard(key)
        memo[key] = (frozenset(names), pieces, depth)
        return memo[key]

    sys.setrecursionlimit(max(sys.getrecursionlimit(), 5000))
    best, deepest = [], [0, None]
    for asm, cfgs in rows_of.items():
        own = _fname(asm)                                 # an assembly never counts itself (only in loops)
        options = [(len(calc(asm, c)[0] - {own}), calc(asm, c)[1], c) for c in cfgs]
        deep = max(calc(asm, c)[2] for c in cfgs)
        if deep > deepest[0] or (deep == deepest[0] and deepest[1] and asm < deepest[1]):
            deepest[:] = [deep, asm]
        unique, pieces, cfg = max(options, key=lambda o: (o[0], o[1]))
        direct = max(len({r[0] for r in rows if not r[4]}) for rows in cfgs.values())
        best.append((unique, pieces, asm, cfg, direct))
    best.sort(key=lambda b: (-b[0], -b[1], b[2]))
    largest = [{"path": indexed.get(a, a), "unique": u, "qty": p, "config": c, "direct": d} for u, p, a, c, d in best[:top]]
    return largest, ({"path": indexed.get(deepest[1], deepest[1]), "levels": deepest[0]} if deepest[1] else None)


_broken_cache = {"key": None}


def broken_references():
    """References to a file that does not exist and that SOLIDWORKS will not find by name either (no file with
    that name in the index). Those assemblies and drawings open with "file not found". Kept until the index changes."""
    key = _tree_key()
    if _broken_cache.get("key") == key:
        return _broken_cache["value"]
    maps = _file_maps()
    names = {_fname(p) for p in maps["indexed"]}
    with _db_lock:
        rows = conn().execute("SELECT DISTINCT parent, child_name, child_stored_path FROM refs WHERE virtual=0 "
                              "AND child_stored_path != ''").fetchall()
    renamed = _renamed_map()
    stored = {}
    for parent, name, path in rows:
        if _TEMP_PATH_RE.search(path) or _is_virtual_path(path):
            continue
        current = apply_rename(path, renamed) if path.lower() in renamed else path
        if _fname(current).lower() in names:
            continue                                  # SOLIDWORKS finds it by name, or it is in the index anyway
        stored.setdefault(current, set()).add(parent)
    existing = _existing_files(list(stored))
    missing = {p: ps for p, ps in stored.items() if p.lower() not in existing}
    parents = set().union(*missing.values()) if missing else set()
    items = sorted(({"path": p, "name": _fname(p), "parents": sorted(maps["indexed"].get(x, x) for x in ps)}
                    for p, ps in missing.items()), key=lambda x: (-len(x["parents"]), x["name"].lower()))
    value = {"files": len(missing), "parents": len(parents), "items": items[:500],
             "parts_indexed": INCLUDE_PARTS == "yes"}
    _broken_cache.update(key=key, value=value)
    return value


def statistics():
    """Numbers a CAD administrator can act on. Kept until the index changes."""
    t0 = time.perf_counter()
    key = _tree_key()
    if _stats_cache.get("key") == key:
        return {**_stats_cache["value"], "ms": round((time.perf_counter() - t0) * 1000), "cached": True}
    with _db_lock:
        db = conn()
        files = db.execute("SELECT path, display_path, mtime, size, status, description FROM files").fetchall()
        version_of = dict(db.execute("SELECT path, version FROM versions"))
        configs = db.execute("SELECT path, display_path, configs FROM files WHERE status='ok' AND configs > 1 "
                             "AND (path LIKE '%.sldasm' OR path LIKE '%.slddrw') ORDER BY configs DESC").fetchall()
        # The references once; every count below comes from this one pass
        all_refs = db.execute("SELECT parent, parent_config, child_name, child_stored_path, child_config, qty, suppressed "
                              "FROM refs WHERE virtual=0").fetchall()
    refs_total = len(all_refs)
    parents_of, stored_of = {}, {}
    for parent, _pc, name, stored, _cc, _q, _s in all_refs:
        parents_of.setdefault(name, set()).add(parent)
        if stored and name not in stored_of:
            stored_of[name] = stored
    usage = sorted(((n, len(ps), stored_of.get(n)) for n, ps in parents_of.items()), key=lambda x: (-x[1], x[0]))[:60]
    asm_refs = [r for r in all_refs if r[0].endswith(".sldasm")]
    ok = [(p, d, m, sz, desc) for p, d, m, sz, st, desc in files if st == "ok"]
    types = {"asm": {"count": 0, "size": 0}, "drw": {"count": 0, "size": 0}, "prt": {"count": 0, "size": 0}}
    by_name, drawings, per_year = {}, set(), {}
    no_desc = {"asm": 0, "prt": 0}
    for p, d, m, sz, desc in ok:
        t = _type_key(p)
        types[t]["count"] += 1
        types[t]["size"] += sz or 0
        by_name.setdefault(_fname(p), []).append(d)
        if t == "drw":
            drawings.add(p.rsplit(".", 1)[0])
        elif not desc:
            no_desc[t] += 1
        if m:
            y = time.localtime(m).tm_year
            per_year.setdefault(y, {"asm": 0, "drw": 0, "prt": 0})[t] += 1
    # Not used: nothing in the index refers to it (by name, as SOLIDWORKS finds files by name)
    # The same test as "Only not used" in the results list, so the number and the list behind it agree
    unused_parts = [r["path"] for r in _unused_from_index("", "part")]
    top_level = [r["path"] for r in _unused_from_index("", "asm")]
    # Without a drawing: no drawing with the same name in the same folder
    no_drw = {"asm": 0, "prt": 0}
    for p, d, m, sz, desc in ok:
        t = _type_key(p)
        if t in no_drw and p.rsplit(".", 1)[0] not in drawings:
            no_drw[t] += 1
    dupes = sorted(((n, ps) for n, ps in by_name.items() if len(ps) > 1), key=lambda x: (-len(x[1]), x[0]))
    indexed = {p: d for p, d, m, sz, desc in ok}
    largest, deepest = _largest_assemblies(asm_refs, indexed)
    # Most used: by name; show where the file is (first indexed file with that name, or the name)
    most_used = [{"name": n, "path": (by_name.get(n) or [stored])[0], "used_in": c} for n, c, stored in usage
                 if not n.endswith(".sldasm")][:10]
    this_year = time.localtime().tm_year
    years = [{"year": y, **per_year.get(y, {"asm": 0, "drw": 0, "prt": 0})} for y in range(this_year - 9, this_year + 1)]
    older = {"asm": 0, "drw": 0, "prt": 0}
    for y, c in per_year.items():
        if y < this_year - 9:
            for k in older:
                older[k] += c[k]
    value = {
        "types": types, "total": len(ok), "unreadable": len(files) - len(ok), "references": refs_total,
        "unused_parts": len(unused_parts), "unused_parts_sample": sorted(unused_parts, key=str.lower)[:10],
        "top_level": len(top_level), "top_level_sample": sorted(top_level, key=str.lower)[:10],
        "no_drawing": no_drw, "no_description": no_desc,
        "duplicate_names": len(dupes), "duplicate_files": sum(len(ps) for _n, ps in dupes),
        "duplicates": [{"name": _fname(sorted(ps, key=str.lower)[0]), "paths": sorted(ps, key=str.lower)[:6], "count": len(ps)}
                       for n, ps in dupes[:10]],
        "most_used": most_used,
        "largest": largest,
        "years": years, "older": older, "last_update": _meta_get("last_update"),
        "fun": _fun_facts(ok, parents_of, configs, deepest, version_of),
        "broken": {k: v for k, v in broken_references().items() if k != "items"}
                  | {"items": broken_references()["items"][:200]},
        "last_full_scan": next((h for h in run_history(20) if h["full"]), None),
        **_version_counts(ok, version_of),
    }
    value["computed_at"] = time.time()
    _stats_cache.update(key=key, value=value)
    try:
        _meta_set("stats_snapshot", json.dumps(value))     # shown right away after a restart too
    except (TypeError, ValueError, sqlite3.Error):
        pass
    return {**value, "ms": round((time.perf_counter() - t0) * 1000)}


_stats_bg = {"busy": False}
STATS_REFRESH_WHILE_INDEXING = 300


def _warm_statistics():
    """Work the statistics out in the background, so the page is instant when someone opens it."""
    if _stats_bg["busy"]:
        return
    _stats_bg["busy"] = True
    try:
        statistics()
    except Exception:  # noqa: BLE001 — the page will try again when opened
        pass
    finally:
        _stats_bg["busy"] = False


def statistics_page():
    """For the page: never make it wait for a full calculation (minutes on a big vault while the index changes
    every second). The last numbers come right away, marked when they are not current; new ones are worked out
    in the background: right after an index run, and while indexing at most every few minutes."""
    key = _tree_key()
    value = _stats_cache.get("value") if _stats_cache.get("key") is not None else None
    if value is None:
        try:
            value = json.loads(_meta_get("stats_snapshot") or "null")
        except (ValueError, sqlite3.Error):
            value = None
    if value is not None and _stats_cache.get("key") == key:
        return {**value, "fresh": True}
    indexing = bool(_state.get("running"))
    age = time.time() - ((value or {}).get("computed_at") or 0)
    if not _stats_bg["busy"] and (not indexing or age > STATS_REFRESH_WHILE_INDEXING or value is None):
        threading.Thread(target=_warm_statistics, daemon=True, name="stats").start()
    if value is None:
        return {"pending": True, "indexing": indexing}
    return {**value, "fresh": False, "refreshing": True, "indexing": indexing}


# ---------------------------------------------------------------------------
# Browse: the folder structure as the index knows it
# ---------------------------------------------------------------------------
BROWSE_EXTS = (".sldprt", ".sldasm", ".slddrw")
_tree = {"key": None, "folders": {}, "roots": [], "built": 0.0, "building": False}
TREE_REFRESH_SECONDS = 15
_tree_lock = threading.Lock()
_tree_version = [0]                    # bumped when paths change without a new index run (rename)


def _folder_of_slow(p):
    if os.name != "nt" and p.startswith("/"):             # tests on Linux use real Linux paths
        return os.path.dirname(p.rstrip("/")) if p.rstrip("/") else ""
    pp = PureWindowsPath(p)
    parent = str(pp.parent)
    return parent if parent != str(pp) else ""


def _folder_of(p):
    """Parent folder of a Windows path, keeping drive roots and share roots as they are ('D:\\', '\\\\srv\\share\\').
    Plain string work for the usual shapes (PureWindowsPath is slow when called hundreds of thousands of times);
    anything unusual goes through PureWindowsPath. Both give exactly the same answer (see the tests)."""
    if not p or "/" in p or "\\\\" in p[2:]:
        return _folder_of_slow(p)
    if len(p) >= 3 and p[1] == ":" and p[2] == "\\" and p[0].isalpha():
        anchor = 3                                          # D:\
    elif p.startswith("\\\\"):
        parts = p[2:].split("\\", 2)
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return _folder_of_slow(p)
        anchor = 2 + len(parts[0]) + 1 + len(parts[1]) + 1  # \\server\share\
    else:
        return _folder_of_slow(p)
    body = p.rstrip("\\")
    if len(body) < anchor:
        return ""                                           # p is a drive or share root itself
    i = body.rfind("\\")
    if i < anchor:
        return p[:anchor] if len(p) >= anchor else p + "\\"
    return body[:i]


def _tree_key():
    with _db_lock:
        return (conn().execute("SELECT COUNT(*), MAX(indexed_at) FROM files").fetchone(), _tree_version[0])


def _build_tree(allow_stale=False):
    """All folders that contain indexed assemblies/drawings or referenced files, with counts per folder
    (everything below it included). Rebuilt only when the index has changed.
    allow_stale: while the index is being updated it changes all the time; then keep using the tree we have
    and refresh it in the background at most every TREE_REFRESH_SECONDS, instead of making every click wait."""
    key = _tree_key()
    with _tree_lock:
        if _tree["key"] == key:
            return _tree
        have = _tree["key"] is not None and _tree["key"][1] == key[1]    # a rename always rebuilds right away
        if allow_stale and have:
            if not _tree["building"] and time.time() - _tree["built"] >= TREE_REFRESH_SECONDS:
                _tree["building"] = True
                threading.Thread(target=_rebuild_tree_bg, daemon=True, name="tree").start()
            return _tree
    return _compute_tree(key)


def _rebuild_tree_bg():
    try:
        _compute_tree(_tree_key())
    except Exception:  # noqa: BLE001 — the next request tries again
        pass
    finally:
        with _tree_lock:
            _tree["building"] = False


def _compute_tree(key):
    with _db_lock:
        db = conn()
        indexed = [r[0] for r in db.execute("SELECT display_path FROM files")]
        referenced = [r[0] for r in db.execute("SELECT DISTINCT child_stored_path FROM refs WHERE virtual=0 "
                                               "AND child_stored_path != ''")]
    direct = {}                                  # folder (lower case) -> [display, asm, drw, refs, parts]
    memo = {}

    def count(path, idx):
        i = path.rfind("\\")
        raw = path[:i] if i >= 0 else path
        folder = memo.get(raw)
        if folder is None:
            folder = memo[raw] = _folder_of(path)
        if folder:
            c = direct.get(folder.lower())
            if c is None:
                c = direct[folder.lower()] = [folder, 0, 0, 0, 0]
            c[idx] += 1

    for p in indexed:
        low = p.lower()
        count(p, 2 if low.endswith(".slddrw") else 4 if low.endswith(".sldprt") else 1)
    for p in referenced:
        if p and not _TEMP_PATH_RE.search(p) and not _is_virtual_path(p):
            count(p, 3)

    folders = {}

    def node(folder):
        low = folder.lower()
        f = folders.get(low)
        if f is None:
            f = folders[low] = {"path": folder, "children": set(), "asm": 0, "drw": 0, "refs": 0, "prt": 0, "own": 0}
        return f

    for low, (disp, a, d, r, pr) in direct.items():
        f = node(disp)
        f["own"] += a + d + r + pr
        child = None
        while f is not None:
            f["asm"] += a
            f["drw"] += d
            f["refs"] += r
            f["prt"] += pr
            if child is not None:
                f["children"].add(child)
            up = _folder_of(f["path"])
            child, f = f["path"].lower(), (node(up) if up else None)
    roots = sorted((low for low, f in folders.items() if not _folder_of(f["path"])), key=str)
    with _tree_lock:
        _tree.update(key=key, folders=folders, roots=roots, built=time.time())
    return _tree


_isdir_cache = {}


def _drive_letters():
    """The drive letters Windows knows on this PC (local, and mapped network drives, also when offline)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        return {chr(65 + i) for i in range(26) if mask & (1 << i)}
    except Exception:  # noqa: BLE001
        return None


def _folder_state(path, ttl=300):
    """'ok', 'missing' or 'unreachable', remembered for a while. A drive letter this PC does not have counts as
    missing (only in old saved references); a known drive or a share that does not answer is 'unreachable'."""
    hit = _isdir_cache.get(path.lower())
    if hit and time.time() - hit[1] < ttl:
        return hit[0]
    root = _root_of(path)
    letters = _drive_letters()
    if letters is not None and len(root) >= 2 and root[1] == ":" and root[0].upper() not in letters:
        state = "missing"
    elif not _root_reachable_cached(root):
        state = "unreachable"
    else:
        state = "ok" if os.path.isdir(path) else "missing"
    _isdir_cache[path.lower()] = (state, time.time())
    return state


def _folder_entry(f):
    return {"path": f["path"], "name": PureWindowsPath(f["path"]).name or f["path"],
            "assemblies": f["asm"], "drawings": f["drw"], "parts": f.get("prt", 0), "referenced": f["refs"],
            "subfolders": len(f["children"])}


_SKIP_DIRS = {"$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws", "config.msi"}


def folders_on_disk(path=""):
    """For the folder picker: the subfolders of a folder ON DISK (also empty ones), or the drives at the top.
    A folder that does not exist yet opens at the nearest existing folder above it."""
    asked = _expand((path or "").strip().strip('"'))
    if not asked:
        drives = []
        if os.name == "nt":
            import string
            for letter in string.ascii_uppercase:
                root = f"{letter}:\\"
                if _root_reachable_cached(root) and os.path.isdir(root):
                    drives.append(root)
        else:
            drives = ["/"]
        return {"path": "", "asked": "", "missing": False, "parent": None, "folders": drives, "drives": True}
    cur = asked.rstrip("\\/") or asked
    if len(cur) == 2 and cur[1] == ":":
        cur += "\\"
    missing = False
    while cur and not (_root_reachable_cached(_root_of(cur)) and os.path.isdir(cur)):
        missing = True
        up = _folder_of(cur)
        if not up or up.lower() == cur.lower():
            cur = ""
            break
        cur = up
    if not cur:
        return {**folders_on_disk(""), "asked": asked, "missing": True}
    subs = []
    try:
        with os.scandir(cur) as it:
            for e in it:
                try:
                    if e.is_dir() and not e.name.startswith(("$", "~$")) and e.name.lower() not in _SKIP_DIRS:
                        subs.append(e.path)
                except OSError:
                    continue
    except OSError as e:
        return {"path": cur, "asked": asked, "missing": missing, "parent": _folder_of(cur), "folders": [], "error": human_error(e)}
    up = _folder_of(cur.rstrip("\\/"))
    parent = None if (not up or up.lower() == cur.lower()) else up
    if parent is None and os.name == "nt":
        parent = ""                                       # from a drive's root: up to the list of drives
    return {"path": cur, "asked": asked, "missing": missing, "parent": parent,
            "folders": sorted(subs, key=lambda x: _fname(x).lower()), "drives": False}


def make_folder(parent, name):
    """Create one new folder in an existing folder (the folder picker's New folder)."""
    name = (name or "").strip()
    if not name or _BAD_NAME.search(name) or name.rstrip(". ") != name or name in (".", ".."):
        raise ValueError(f"'{name}' is not a valid folder name")
    parent = _expand((parent or "").strip().strip('"'))
    if not parent or not os.path.isdir(parent):
        raise ValueError(f"{parent or 'the folder'} does not exist")
    new = _join(parent, name)
    if os.path.exists(new):
        raise ValueError(f"{name} already exists in {parent}")
    os.mkdir(new)
    _isdir_cache.clear()
    return new


def browse(folder="", with_files=True, show_all=False):
    """Subfolders (from the index) and the SOLIDWORKS files in one folder (read from disk, plus what the index knows)."""
    t0 = time.perf_counter()
    tree = _build_tree(allow_stale=True)
    folders = tree["folders"]
    folder = (folder or "").strip().strip('"')
    if not folder:
        subs = [folders[k] for k in tree["roots"]]
        entries = [_folder_entry(f) for f in subs]
        with ThreadPoolExecutor(max_workers=8) as pool:
            for e, state in zip(entries, pool.map(_folder_state, [e["path"] for e in entries])):
                e["state"] = state
        # Not on this PC: drive letters it does not have, and network paths that do not answer (old locations in
        # saved references). Hidden unless asked for; a known drive that is offline stays (it comes back).
        def gone(e):
            return e["state"] == "missing" or (e["state"] == "unreachable" and e["path"].startswith("\\\\"))
        hidden = [e for e in entries if gone(e)]
        if not show_all:
            entries = [e for e in entries if not gone(e)]
        return {"folder": "", "parent": None, "subfolders": entries, "files": [], "known": True, "hidden": len(hidden)}
    if not folder.endswith("\\") and re.fullmatch(r"[A-Za-z]:", folder):
        folder += "\\"
    node = (folders.get(folder.lower()) or folders.get(folder.rstrip("\\").lower())
            or folders.get(folder.rstrip("\\").lower() + "\\"))       # share root: \\\\srv\\share\\
    display = node["path"] if node else folder
    state = _folder_state(display)

    # Read the folder ONCE: its subfolders and files. On a network share that is one request instead of one per
    # subfolder ("does this folder still exist?"), which is what made opening big folders slow.
    dirs, files, error = None, {}, None
    if state == "ok":
        try:
            dirs = set()
            with os.scandir(display) as it:
                for e in it:
                    try:
                        if e.is_dir():
                            dirs.add(e.name.lower())
                        elif e.name.lower().endswith(BROWSE_EXTS) and not e.name.startswith("~$"):
                            try:
                                st = e.stat()
                                files[e.path.lower()] = {"path": e.path, "modified": st.st_mtime, "size": st.st_size}
                            except OSError:
                                files[e.path.lower()] = {"path": e.path, "modified": None, "size": None}
                    except OSError:
                        continue
        except OSError as e:
            dirs, error = None, f"the folder cannot be read ({e.strerror or e})"

    entries = []
    if node:
        subs = sorted((folders[c] for c in node["children"] if c in folders), key=lambda f: f["path"].lower())
        entries = [_folder_entry(f) for f in subs]
        if dirs is not None:
            for e in entries:
                e["state"] = "ok" if e["name"].lower() in dirs else "missing"
                _isdir_cache[e["path"].lower()] = (e["state"], time.time())
        elif state == "unreachable":
            for e in entries:
                e["state"] = "unreachable"
        else:                                       # listing failed: ask per folder (rare)
            with ThreadPoolExecutor(max_workers=8) as pool:
                for e, st in zip(entries, pool.map(_folder_state, [e["path"] for e in entries])):
                    e["state"] = st
        entries = [e for e in entries if e["state"] != "missing"]    # only in old saved references: leave out
    chain, up = [display], _folder_of(display)
    while up:
        chain.insert(0, (folders.get(up.lower()) or {"path": up})["path"])
        up = _folder_of(up)
    result = {"folder": display, "parent": _folder_of(display) or "", "ancestors": chain, "subfolders": entries,
              "files": [], "known": bool(node), "state": state}
    if error:
        result["error"] = error
    if not with_files:
        return result

    prefix = display.rstrip("\\").lower() + "\\"
    like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with _db_lock:
        rows = conn().execute("SELECT path, display_path, status, error, mtime FROM files WHERE path LIKE ? ESCAPE '\\'",
                              (like,)).fetchall()
    known = {p: (d, st, err, m) for p, d, st, err, m in rows if "\\" not in p[len(prefix):]}
    for low, (disp, st, err, m) in known.items():
        f = files.setdefault(low, {"path": disp, "modified": m, "size": None, "gone": result["state"] == "ok"})
        f.update(index_status=st, index_error=err)
    counts = usage_for_paths([f["path"] for f in files.values()])
    for f in files.values():
        f["used_in"], f["other_copies"] = counts.get(f["path"].lower(), (0, 0))
        if f["path"].lower().endswith(_index_exts()) and "index_status" not in f:
            f["index_status"] = "new"               # not read by the index yet
    result["files"] = sorted(files.values(), key=lambda f: _fname(f["path"]).lower())
    result["ms"] = round((time.perf_counter() - t0) * 1000)
    return result


SEARCH_EXTS = {"part": ".sldprt", "asm": ".sldasm", "drw": ".slddrw"}
_count_cache = {}
SEARCH_ALL_MAX = 5000            # "Used in" sorting and "Only not used" work out every result: up to this many
_EV_SORT = {"name": "name", "folder": "path", "modified": "date_modified"}


def _ev_search_query(q, kind="", filt="", exact=False):
    """The Everything search for the search box, with the file type and the skipped paths built in, so that
    Everything counts and pages exactly the files that are shown."""
    exts = SEARCH_EXTS[kind][1:] if kind in SEARCH_EXTS else "sldprt;sldasm;slddrw"
    skip = " ".join([f'!"{x}"' for x in EXCLUDE if x.strip() and '"' not in x] + [f'!"{f}"' for f in _own_folders()])
    if exact and q and '"' not in q:
        q = f'wfn:"{q}"'                              # Everything: the whole file name, nothing more
    return " ".join(x for x in (f"ext:{exts}", q, filt, "!~$", skip) if x)


def _index_matches(q, filt="", exact=False):
    """Without Everything: the files the index knows (indexed files and the files they refer to)."""
    words = [w for w in ((q if not exact else "") + " " + filt).lower().split() if w]
    if exact:
        words = [q.lower()] + words
    like = "%" + (words[0] if words else "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with _db_lock:
        db = conn()
        files = db.execute("SELECT display_path, mtime FROM files WHERE status='ok' AND path LIKE ? ESCAPE '\\' "
                           "LIMIT 50000", (like,)).fetchall()
        kids = db.execute("SELECT MAX(child_stored_path) FROM refs WHERE virtual=0 AND child_stored_path LIKE ? "
                          "ESCAPE '\\' GROUP BY lower(child_stored_path) LIMIT 50000", (like,)).fetchall()
    seen, out = set(), []
    for p, m in files:
        seen.add(p.lower())
        out.append({"path": p, "modified": m})
    for (stored,) in kids:                           # every stored path separately: same name, other file
        if stored and stored.lower() not in seen and not _TEMP_PATH_RE.search(stored):
            seen.add(stored.lower())
            out.append({"path": stored, "modified": None})
    return [r for r in out if r["path"].lower().endswith(tuple(SEARCH_EXTS.values())) and not _excluded(r["path"])
            and all(w in r["path"].lower() for w in words) and (not exact or _fname(r["path"]).lower() == q.lower())]


def _sort_key(sort):
    return {"name": lambda r: _fname(r["path"]).lower(), "folder": lambda r: r["path"].lower(),
            "modified": lambda r: r.get("modified") or 0,
            "used": lambda r: (-1 if r["path"].lower().endswith(".slddrw") else (r.get("used_in") or 0),
                               )}.get(sort, lambda r: _fname(r["path"]).lower())


_unused_cache = {}


def _unused_from_index(q, kind="", filt="", exact=False):
    """Parts and assemblies in the index that nothing uses (this very file; the same test as "used in"),
    matching the search. Sorted by name; kept until the index changes."""
    key = (q.lower(), kind, (filt or "").lower(), bool(exact), _tree_key())
    hit = _unused_cache.get(key)
    if hit:
        return hit
    if kind == "drw":
        return []                                   # drawings are never used by anything
    exts = {"part": ("%.sldprt",), "asm": ("%.sldasm",)}.get(kind, ("%.sldprt", "%.sldasm"))
    with _db_lock:
        cands = conn().execute(
            "SELECT display_path, mtime FROM files WHERE status='ok' AND (" + " OR ".join("path LIKE ?" for _ in exts) + ")",
            exts).fetchall()
    words = [w for w in ((q if not exact else "") + " " + (filt or "")).lower().split() if w]
    cands = [{"path": d, "modified": m} for d, m in cands
             if all(w in d.lower() for w in words) and (not exact or _fname(d).lower() == q.lower())
             and not is_library(d)]                     # SOLIDWORKS' own library: not left-overs of yours
    usage = parents_for_paths([c["path"] for c in cands])
    out = []
    for c in cands:
        used, other = usage.get(c["path"].lower(), (set(), set()))
        if not used:
            out.append({**c, "used_in": 0, "other_copies": len(other)})
    out.sort(key=_sort_key("name"))
    if len(_unused_cache) > 20:
        _unused_cache.clear()
    _unused_cache[key] = out
    return out


def search(q, limit=60, offset=0, sort="name", desc=False, kind="", unused=False, filt="", exact=False):
    """Files for the search box and the results page: one page, sorted and counted over ALL matches.
    Via Everything when available (it sorts, counts and pages itself), otherwise from the index.
    Sorting by "used in" and "only not used" need the usage of every match: up to SEARCH_ALL_MAX matches."""
    q, filt = q.strip(), (filt or "").strip()
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    sort = sort if sort in ("name", "folder", "modified", "used") else "name"
    kind = kind if kind in SEARCH_EXTS else ""
    note = None
    via = "index"
    counts = {}
    page, total, everything_ok = [], 0, False
    if INDEX_SOURCE != "folders" and everything_status()[0]:
        try:
            keys = ["", "part", "asm", "drw"]
            ck = (q.lower(), filt.lower(), exact, tuple(EXCLUDE))
            hit = _count_cache.get(ck)
            if hit and time.time() - hit[1] < 30:
                counts = hit[0]                                      # sorting or paging: same counts
            else:
                with ThreadPoolExecutor(max_workers=4) as pool:      # the count per type: four quick questions
                    totals = list(pool.map(lambda k: everything_search(_ev_search_query(q, k, filt, exact), 1, timeout=10)["total"], keys))
                counts = dict(zip(keys, totals))
                if len(_count_cache) > 200:
                    _count_cache.clear()
                _count_cache[ck] = (counts, time.time())
            total = counts[kind]
            everything_ok, via = True, "everything"
        except Exception:  # noqa: BLE001 — fall back to the index
            everything_ok = False
    # "Only not used": the index knows exactly what is used, for any number of files (no 5,000 limit)
    if unused and (kind == "asm" or kind == "drw" or (INCLUDE_PARTS == "yes" and kind in ("", "part"))):
        rows = _unused_from_index(q, kind, filt, exact)
        if sort in ("folder", "modified"):
            rows = sorted(rows, key=_sort_key(sort), reverse=desc)
        elif desc and sort == "name":
            rows = rows[::-1]
        if not everything_ok:
            allr = _index_matches(q, filt, exact)
            counts = {"": len(allr), **{k: sum(1 for r in allr if r["path"].lower().endswith(e)) for k, e in SEARCH_EXTS.items()}}
        return {"results": [dict(r) for r in rows[offset:offset + limit]], "via": via, "total": len(rows),
                "offset": offset, "limit": limit, "sort": sort if sort != "used" else "name", "desc": bool(desc),
                "kind": kind, "unused": True, "exact": bool(exact), "counts": counts,
                "note": "Only files in the index are listed: a file added after the last index update is not in it yet."}
    need_all = sort == "used" or unused
    if everything_ok:
        if need_all and total > SEARCH_ALL_MAX:
            note = (f"Sorting by Used in and showing only unused files work for up to {SEARCH_ALL_MAX:,} results; "
                    f"these are {total:,}. Make the search more specific.")
            need_all, unused = False, False
            sort = "name" if sort == "used" else sort
        if need_all:
            rows, got = [], 0
            while got < total:
                chunk = everything_search(_ev_search_query(q, kind, filt, exact), 2000, got, timeout=30,
                                          sort="name", ascending=True)["results"]
                if not chunk:
                    break
                rows += [{"path": r["path"], "modified": r["modified"]} for r in chunk if r["type"] != "folder"]
                got += len(chunk)
            allrows = rows
        else:
            res = everything_search(_ev_search_query(q, kind, filt, exact), limit, offset, timeout=10,
                                    sort=_EV_SORT[sort], ascending=not desc)
            page = [{"path": r["path"], "modified": r["modified"]} for r in res["results"] if r["type"] != "folder"]
            total = res["total"]
    else:
        allrows = _index_matches(q, filt, exact)
        counts = {"": len(allrows)}
        for k, ext in SEARCH_EXTS.items():
            counts[k] = sum(1 for r in allrows if r["path"].lower().endswith(ext))
        if kind:
            allrows = [r for r in allrows if r["path"].lower().endswith(SEARCH_EXTS[kind])]
        total = len(allrows)
        if need_all and total > SEARCH_ALL_MAX:
            note = (f"Sorting by Used in and showing only unused files work for up to {SEARCH_ALL_MAX:,} results; "
                    f"these are {total:,}. Make the search more specific.")
            need_all, unused = False, False
            sort = "name" if sort == "used" else sort
        if not need_all:
            allrows.sort(key=_sort_key(sort), reverse=desc)
            page = allrows[offset:offset + limit]
    if need_all:
        usage = usage_for_paths([r["path"] for r in allrows])
        for r in allrows:
            r["used_in"], r["other_copies"] = usage.get(r["path"].lower(), (0, 0))
        if unused:
            allrows = [r for r in allrows if not r["path"].lower().endswith(".slddrw") and not r["used_in"]]
        allrows.sort(key=_sort_key("name"))
        if sort != "name" or desc:
            allrows.sort(key=_sort_key(sort), reverse=desc)       # stable: equal values stay in name order
        total = len(allrows)
        page = allrows[offset:offset + limit]
    else:
        usage = usage_for_paths([r["path"] for r in page])
        for r in page:
            r["used_in"], r["other_copies"] = usage.get(r["path"].lower(), (0, 0))
    return {"results": page, "via": via, "total": total, "offset": offset, "limit": limit, "sort": sort,
            "desc": bool(desc), "kind": kind, "unused": bool(unused), "exact": bool(exact), "counts": counts, "note": note}


# ---------------------------------------------------------------------------
# Open a file in SOLIDWORKS, or show it in Explorer
# ---------------------------------------------------------------------------
def _bring_to_front(title_parts, timeout=15.0):
    """Windows keeps a program opened by a background process behind the browser; pull its window forward."""
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def find():
        hits = []

        def cb(hwnd, _l):
            if user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, buf, 512)
                title = buf.value.lower()
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls, 256)
                if cls.value.lower() not in ("chrome_widgetwin_1", "mozillawindowclass", "consolewindowclass"):
                    if any(t and t in title for t in title_parts):
                        hits.append(hwnd)
            return True
        user32.EnumWindows(proc(cb), 0)
        return hits[0] if hits else None

    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = find()
        if hwnd:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)
            fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            me = kernel32.GetCurrentThreadId()
            attached = fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True)
            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(me, fg_thread, False)
            return True
        time.sleep(0.3)
    return False


def open_path(path, action="open"):
    if os.name != "nt":
        raise RuntimeError("only available on Windows")
    if action == "datadir":
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(DATA_DIR))
        return {"ok": True}
    if action == "explore":
        if not path or not os.path.isdir(path):
            raise RuntimeError("folder not found (moved, renamed, or the drive is not connected)")
        os.startfile(path)
        return {"ok": True}
    if not path or not os.path.isfile(path):
        raise RuntimeError("file not found (moved, renamed, or the drive is not connected)")
    if action == "folder":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        return {"ok": True}
    if Path(path).suffix.lower() not in SW_TYPES:
        raise RuntimeError("only SOLIDWORKS files can be opened from here")
    os.startfile(path)
    stem = Path(path).stem.lower()
    threading.Thread(target=_bring_to_front, args=([Path(path).name.lower(), f"[{stem}", f"{stem} - "],),
                     daemon=True).start()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Rename and move, with references updated in every assembly and drawing
#
# All or nothing. The work runs in a separate process (the Document Manager can crash); every step is
# written to a journal first, so that after an error or a crash everything is put back: files back in
# their place, changed files restored from the backup.
# ---------------------------------------------------------------------------
APP_LOCK_MARKER = "SWhereUsed"
_BAD_NAME = re.compile(r'[\\/:*?"<>|]')
_rename_busy = threading.Lock()                 # one rename at a time


def _force_unlink(p):
    import stat
    try:
        Path(p).unlink()
    except PermissionError:
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        Path(p).unlink()


def _read_lock_user(lock):
    """The readable text (user / computer) in a SOLIDWORKS lock file ~$name. The format is undocumented."""
    try:
        raw = Path(lock).read_bytes()[:2048]
    except OSError:
        return None
    if not raw:
        return None
    encodings = ("utf-16-le", "utf-8", "cp1252") if raw.count(0) / len(raw) > 0.3 else ("utf-8", "cp1252")
    for enc in encodings:
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin-1")
    parts = [x.strip() for x in re.split(r"[\x00-\x1f\x7f-\x9f\ufffd]+", text)]
    parts = [x for x in parts if len(x) >= 2 and sum(ch.isalnum() for ch in x) >= 2]
    return " / ".join(dict.fromkeys(parts))[:120] or None


def _file_in_use(path):
    """Does a program (SOLIDWORKS) hold the file open for writing? True / False / None = unknown.
    Opens it for READING while denying others write access: Windows refuses that when someone has it
    open with write access. Nothing about the file changes."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.CreateFileW(str(path), 0x80000000, 0x1, None, 3, 0x80, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            return True if ctypes.get_last_error() in (32, 33) else None
        k32.CloseHandle(handle)
        return False
    try:
        with open(path, "rb"):
            return False
    except PermissionError:
        return True
    except OSError:
        return None


def access_info(path):
    """Read-only flag, lock file (~$name) and whether the file is open somewhere."""
    info = {"exists": False, "readonly": False, "locked_by": None, "in_use": None, "lock_stale": False,
            "app_lock": False}
    p = Path(path)
    try:
        st = os.stat(p)
    except OSError:
        return info
    info["exists"] = True
    attrs = getattr(st, "st_file_attributes", None)
    info["readonly"] = bool(attrs & 0x1) if attrs is not None else not os.access(p, os.W_OK)
    info["in_use"] = _file_in_use(p)
    lock = p.with_name("~$" + p.name)
    if lock.is_file():
        who = _read_lock_user(lock) or "an unknown user"
        info["app_lock"] = APP_LOCK_MARKER.lower() in who.lower()
        info["locked_by"] = " / ".join(x for x in who.split(" / ") if x.lower() != APP_LOCK_MARKER.lower()) or who
        info["lock_stale"] = info["in_use"] is False
        try:
            info["lock_age"] = time.time() - lock.stat().st_mtime
        except OSError:
            info["lock_age"] = None
        # Our own lock file left behind by an interrupted rename: remove it (nothing is running now)
        if info["app_lock"] and info["lock_stale"] and not _rename_busy.locked():
            try:
                _force_unlink(lock)
                info.update(locked_by=None, lock_stale=False, app_lock=False)
            except OSError:
                pass
    return info


def blocking_reason(acc):
    """Why this file cannot be changed or moved right now (None = it can)."""
    if not acc.get("exists"):
        return "not found"
    if acc.get("locked_by") and acc.get("in_use"):
        return f"open in SOLIDWORKS by {acc['locked_by']}"
    if acc.get("in_use"):
        return "open in another program"
    if acc.get("locked_by") and acc.get("lock_stale"):
        age = _age_text(acc.get("lock_age"))
        return f"leftover lock file from {acc['locked_by']}" + (f" ({age})" if age else "")
    if acc.get("locked_by"):
        return f"in use by {acc['locked_by']}"
    if acc.get("readonly"):
        return "read-only"
    return None


def _status_key(acc):
    if not acc.get("exists"):
        return "missing"
    if acc.get("in_use") or (acc.get("locked_by") and not acc.get("lock_stale")):
        return "in_use"
    if acc.get("locked_by"):
        return "stale_lock"
    return "readonly" if acc.get("readonly") else "ok"


def _age_text(seconds):
    if seconds is None:
        return ""
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes} min old"
    if minutes < 48 * 60:
        return f"{minutes // 60} h old"
    return f"{minutes // 1440} days old"


def remove_stale_lock(path):
    """Delete the ~$ lock file of a file that nobody has open (SOLIDWORKS sometimes leaves one behind).
    Checked right before deleting, on the file AND on the lock file: when either is still held open by a
    program on any pc (Windows refuses our test open then), or when this cannot be determined, nothing is deleted."""
    lock = Path(path).with_name("~$" + Path(path).name)
    if not lock.is_file():
        return {"ok": True}
    if _file_in_use(path) is not False:
        raise RuntimeError("the file is still open somewhere, or this cannot be checked; the lock file stays")
    if _file_in_use(lock) is True:
        raise RuntimeError("SOLIDWORKS still holds the lock file open; the lock file stays")
    who = _read_lock_user(lock)
    try:
        age = _age_text(time.time() - lock.stat().st_mtime)
    except OSError:
        age = ""
    _force_unlink(lock)
    _log("lock_removed", str(path), "", f"lock file of {who or 'unknown'} removed" + (f" ({age})" if age else ""))
    return {"ok": True}


def same_name_drawing(path):
    """The drawing with the same name in the same folder (renamed along with the model)."""
    ext = Path(path).suffix
    if not ext or ext.lower() == ".slddrw":
        return None
    base = path[: -len(ext)]
    for cand in (base + ".SLDDRW", base + ".slddrw", base + ".SldDrw"):
        if os.path.isfile(cand):
            return cand
    return None


def impact(path, acc_cache=None, include_own=False):
    """What does renaming or moving this file touch? Only reads, changes nothing.
    acc_cache: shared between the files of one batch, so each referencing file is checked once."""
    def acc_of(p):
        if acc_cache is None:
            return access_info(p)
        key = p.lower()
        if key not in acc_cache:
            acc_cache[key] = access_info(p)
        return acc_cache[key]
    t0 = time.perf_counter()
    wu = whereused(path, include_other=True, with_top=False)
    other = [d for d in wu["direct"] if d["match"] == "other_copy"]
    own = [d for d in wu["direct"] if d["match"] == "own_copy"]
    # A copy of the project in another folder with its own file of this name: left alone unless chosen
    mine = [d for d in wu["direct"] if d["match"] != "other_copy" and (include_own or d["match"] != "own_copy")]
    with ThreadPoolExecutor(max_workers=8) as pool:
        accs = list(pool.map(acc_of, [d["path"] for d in mine]))
    files = []
    for d, acc in zip(mine, accs):
        files.append({"path": d["path"], "kind": d["kind"], "role": "reference", "match": d["match"],
                      "stored_path": d["stored_path"], "access": acc, "status": _status_key(acc),
                      "reason": blocking_reason(acc),
                      "suppressed_only": all(c["suppressed"] for c in d["configs"])})
    drawing = same_name_drawing(path)
    if drawing:
        acc = acc_of(drawing)
        existing = next((f for f in files if f["path"].lower() == drawing.lower()), None)
        if existing:
            existing["role"] = "rename_with"
        else:
            files.append({"path": drawing, "kind": "drawing", "role": "rename_with_unlinked", "match": None,
                          "stored_path": None, "access": acc, "status": _status_key(acc),
                          "reason": blocking_reason(acc), "suppressed_only": False})
    target_acc = acc_of(path)
    st = wu["index"]
    warnings = []
    if st.get("running"):
        warnings.append("The index is being updated right now; this overview may be incomplete.")
    if not (st.get("assemblies") or st.get("drawings")):
        warnings.append("The index is empty: references will be missed. Build the index first.")
    if st.get("failed"):
        warnings.append(f"{st['failed']} assemblies/drawings could not be read. If they reference this file, "
                        "they are not updated and will show it as missing.")
    if other:
        warnings.append(f"{len(other)} file(s) reference a different file with the same name; they are left alone.")
    if own and not include_own:
        warnings.append(f"{len(own)} assembly/drawing file(s) in other folders use their own file with this name "
                        "(a copy of the project); they are left alone unless you choose to include them.")
    if drawing and not any(f["role"] == "rename_with" for f in files):
        warnings.append("There is a drawing with the same name in this folder, but the index says it does not "
                        "reference this file. It is renamed along with it only if you tick the box.")
    if INCLUDE_PARTS != "yes":
        warnings.append("Parts are not indexed ([index] include_parts = no): derived and mirrored parts that are "
                        "based on this file are not updated.")
    elif _meta_get("part_refs_off"):
        warnings.append(f"Part references could not be read ({_meta_get('part_refs_off')}): derived and mirrored "
                        "parts that are based on this file are not updated.")
    files.sort(key=lambda f: (f["role"] == "reference", f["kind"] != "assembly", _fname(f["path"]).lower()))
    return {"target": path, "name": _fname(path), "target_access": target_acc, "target_status": _status_key(target_acc),
            "own_copies": [d["path"] for d in own], "include_own": bool(include_own),
            "target_reason": blocking_reason(target_acc), "same_name_drawing": drawing, "files": files,
            "counts": {"assemblies": sum(f["kind"] == "assembly" and f["role"] != "rename_with_unlinked" for f in files),
                       "drawings": sum(f["kind"] == "drawing" and f["role"] != "rename_with_unlinked" for f in files),
                       "blocked": sum(bool(f["reason"]) for f in files) + bool(blocking_reason(target_acc))},
            "warnings": warnings, "allowed": RENAME_ALLOWED == "yes",
            "ms": round((time.perf_counter() - t0) * 1000)}


_fstatus_cache = {}
FSTATUS_TTL = 20


def _folder_status(folder):
    """One listing of a folder: file names, names with a lock file (~$name) next to them, and read-only names.
    (On Windows the read-only flag comes with the listing itself, no extra request per file.) Kept 20 seconds."""
    low = folder.lower()
    hit = _fstatus_cache.get(low)
    if hit and time.time() - hit[1] < FSTATUS_TTL:
        return hit[0]
    result = None
    if not _TEMP_PATH_RE.search(folder) and _root_reachable_cached(_root_of(folder)):
        try:
            names, locks, readonly = set(), set(), set()
            with os.scandir(folder) as it:
                for e in it:
                    n = e.name.lower()
                    if n.startswith("~$"):
                        locks.add(n[2:])
                        continue
                    names.add(n)
                    if n.endswith(BROWSE_EXTS):
                        try:
                            attrs = getattr(e.stat(), "st_file_attributes", None)
                            if attrs is not None and attrs & 0x1:
                                readonly.add(n)
                        except OSError:
                            pass
            result = (names, locks, readonly)
        except OSError:
            result = None
    _fstatus_cache[low] = (result, time.time())
    return result


def rename_readiness(paths, with_drawings=True):
    """Before any new name is typed: can each file be renamed right now, together with its drawing and every
    assembly/drawing that refers to it? A quick check from folder listings (lock files and read-only flags);
    files with a lock file get the full check (who, and is it really still open). The full check of every file
    still happens right before renaming."""
    t0 = time.perf_counter()
    paths = list(dict.fromkeys(p for p in paths if p))
    maps = _file_maps()
    parents = parents_for_paths(paths)
    drawings = {}
    for p in paths:
        ext = Path(p).suffix
        if with_drawings and ext and ext.lower() != ".slddrw":
            drawings[p.lower()] = p[: -len(ext)] + ".SLDDRW"
    every = {p.lower(): p for p in paths}
    every.update({d.lower(): d for d in drawings.values()})
    for used, _other in parents.values():
        for q in used:
            every.setdefault(q, maps["indexed"].get(q, q))
    folders = {}
    for low, p in every.items():
        f = _folder_of(p)
        if f:
            folders.setdefault(f.lower(), f)
    items = list(folders.items())
    status_of = {}
    if items:
        with ThreadPoolExecutor(max_workers=min(32, len(items))) as pool:
            for (low, _f), st in zip(items, pool.map(lambda it: _folder_status(it[1]), items)):
                status_of[low] = st

    def quick(p):
        st = status_of.get((_folder_of(p) or "").lower())
        if st is None:
            return "unknown"
        names, locks, readonly = st
        n = _fname(p).lower()
        if n not in names:
            return "missing"
        if n in locks:
            return "locked"
        return "readonly" if n in readonly else "ok"

    quick_of = {low: quick(p) for low, p in every.items()}
    locked = [every[low] for low, q in quick_of.items() if q == "locked"]
    reason_of = {}
    if locked:
        with ThreadPoolExecutor(max_workers=min(16, len(locked))) as pool:
            for p, acc in zip(locked, pool.map(access_info, locked)):
                reason_of[p.lower()] = blocking_reason(acc)       # None when it was our own leftover lock
    for low, q in quick_of.items():
        if q == "readonly":
            reason_of[low] = "read-only"
        elif q == "missing":
            reason_of[low] = "not found"
        elif q == "unknown":
            reason_of[low] = None                             # could not look: the full check decides later

    out = {}
    for p in paths:
        low = p.lower()
        used = parents.get(low, (set(), set()))[0]
        drw = drawings.get(low)
        has_drw = bool(drw) and quick_of.get(drw.lower()) != "missing"
        blocked = []
        for q in sorted(used):
            r = reason_of.get(q)
            if r and r != "not found":                        # a referencing file that is gone is skipped, not a problem
                blocked.append({"path": every.get(q, q), "reason": r})
        target = reason_of.get(low)
        if not target and pdm_vault(p):
            target = "in a SOLIDWORKS PDM vault: rename it in PDM"
        drawing_reason = reason_of.get(drw.lower()) if has_drw else None
        if has_drw and drw.lower() in used:
            blocked = [b for b in blocked if b["path"].lower() != drw.lower()]
        out[low] = {"ok": not (target or drawing_reason or blocked), "reason": target,
                    "drawing": drw if has_drw else None, "drawing_reason": drawing_reason,
                    "updates": len(used), "blocked": blocked[:20], "blocked_count": len(blocked),
                    "checked": quick_of.get(low) != "unknown"}
    problem = None
    if RENAME_ALLOWED != "yes":
        problem = "renaming is switched off ([rename] allowed = no in settings.ini)"
    elif sw_problem():
        problem = f"SOLIDWORKS files cannot be written: {sw_problem()}"
    return {"files": out, "problem": problem, "folders": len(items),
            "ms": round((time.perf_counter() - t0) * 1000)}


def rename_plan(path, new_name, new_folder=None, with_drawing=True, acc_cache=None, include_own=False):
    """What renaming/moving would do, and what stops it. Changes nothing."""
    src = Path(path)
    ext = src.suffix
    name = (new_name or "").strip()
    other_ext = next((e for e in SW_TYPES if name.lower().endswith(e) and e != ext.lower()), None)
    if other_ext:
        name = name[: -len(other_ext)]
    if name and not name.lower().endswith(ext.lower()):
        name += ext
    folder_text = (new_folder or "").strip().strip('"')
    folder = Path(folder_text) if folder_text else src.parent
    blockers, warnings = [], []
    if other_ext:
        blockers.append(f"the file keeps its type: the name must end with {ext}, not {other_ext.upper()}")
    if RENAME_ALLOWED != "yes":
        blockers.append("renaming is switched off ([rename] allowed = no in settings.ini)")
    problem = sw_problem()
    if problem:
        blockers.append(f"SOLIDWORKS files cannot be written: {problem}")
    if not name or name.lower() == ext.lower():
        blockers.append("enter a new name")
    elif _BAD_NAME.search(name):
        blockers.append('a file name cannot contain \\ / : * ? " < > |')
    elif name.rstrip(". ") != name:
        blockers.append("a file name cannot end with a dot or a space")
    if not folder.is_dir():
        blockers.append(f"the folder {folder} does not exist")
    new_path = folder / name if name else src
    same_file = str(new_path).lower() == str(src).lower()
    if same_file and str(new_path) == str(src):
        blockers.append("the new name and folder are the same as now")
    elif new_path.exists() and not same_file:
        blockers.append(f"there already is a file {new_path.name} in that folder")
    if len(str(new_path)) >= 260:
        warnings.append(f"The new path is {len(str(new_path))} characters long; SOLIDWORKS has trouble above 259.")

    imp = impact(path, acc_cache, include_own)
    if imp["target_reason"]:
        blockers.append(f"{src.name}: {imp['target_reason']}")
    drawing = None
    if with_drawing and imp["same_name_drawing"]:
        old_d = Path(imp["same_name_drawing"])
        new_d = folder / (Path(name).stem + old_d.suffix)
        drawing = {"old": str(old_d), "new": str(new_d)}
        if new_d.exists() and str(new_d).lower() != str(old_d).lower():
            blockers.append(f"there already is a drawing {new_d.name} in that folder")
    parents = []
    for f in imp["files"]:
        if f["role"] == "rename_with_unlinked" and not drawing:
            continue                      # not renamed along and does not reference it: nothing to do
        if f["reason"] == "not found":
            warnings.append(f"{_fname(f['path'])} is in the index but was not found; skipped.")
            continue
        if f["reason"]:
            blockers.append(f"{_fname(f['path'])}: {f['reason']}")
        parents.append({"path": f["path"], "kind": f["kind"], "stored_path": f.get("stored_path"), "role": f["role"]})
    warnings += imp["warnings"]
    # SOLIDWORKS PDM: files in a vault must be renamed, moved and changed through PDM
    involved = [str(src), str(new_path)] + ([drawing["old"]] if drawing else []) + [p["path"] for p in parents]
    for f in involved:
        vault = pdm_vault(f)
        if vault:
            blockers.append(f"{_fname(f)} {PDM_MESSAGE.format(vault)}")
            break
    return {"path": str(src), "new_path": str(new_path), "drawing": drawing, "parents": parents,
            "own_copies": imp.get("own_copies", []), "include_own": bool(include_own),
            "blockers": blockers, "warnings": warnings, "files": imp["files"],
            "counts": {"assemblies": sum(p["kind"] == "assembly" for p in parents),
                       "drawings": sum(p["kind"] == "drawing" for p in parents)}}


def _journal_write(journal_path, journal):
    tmp = str(journal_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(journal, f, indent=1)
    os.replace(tmp, journal_path)                 # never a half-written journal


def _rollback(journal):
    """Put everything back as it was, following the journal."""
    problems = []
    for frm, to in reversed(journal.get("moved", [])):
        try:
            if frm.lower() == to.lower():
                if os.path.exists(to):
                    os.replace(to, frm)           # only the letter case was changed
            elif os.path.exists(to) and not os.path.exists(frm):
                shutil.move(to, frm)
        except OSError as e:
            problems.append(f"moving {Path(to).name} back to {frm} failed: {e}")
    for orig in journal.get("saved", []):
        backup = journal.get("backups", {}).get(orig)
        try:
            if backup and os.path.exists(backup):
                if os.path.exists(orig):
                    _force_unlink(orig)
                shutil.copy2(backup, orig)
        except OSError as e:
            problems.append(f"restoring {Path(orig).name} from {backup} failed: {e}")
    for lock in journal.get("locks", []):
        try:
            if os.path.exists(lock):
                _force_unlink(lock)
        except OSError:
            pass
    return problems


def _create_app_lock(path):
    """A SOLIDWORKS-style lock file ~$name with user and computer, so colleagues see the file is in use."""
    p = Path(path)
    lock = p.with_name("~$" + p.name)
    if lock.exists():
        raise RuntimeError(f"{p.name}: a lock file of {_read_lock_user(lock) or 'someone'} just appeared; "
                           "the file is in use")
    text = f"{os.environ.get('USERNAME', '')}\x00{os.environ.get('COMPUTERNAME', '')}\x00{APP_LOCK_MARKER}\x00"
    lock.write_bytes(text.encode("utf-16-le"))
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(lock), 0x2)      # hidden, like SOLIDWORKS does
        except Exception:  # noqa: BLE001
            pass
    return lock


def _string_list(res):
    """A list of paths from a COM answer (on its own, or as part of a tuple)."""
    if isinstance(res, (tuple, list)) and res and all(isinstance(v, str) for v in res):
        return list(res)
    parts = list(res) if isinstance(res, tuple) else [res]
    return next((list(x) for x in parts if isinstance(x, (tuple, list)) and x
                 and all(isinstance(v, str) for v in x)), None)


def _refs_of(path):
    """References saved in a file, read the way the index does (for parts: part references)."""
    rows = read_document(path)[0]
    return {r[3] for r in rows if r[3]}


def _external_refs(path, saved_only=False):
    """The external references of an assembly or drawing. ReplaceReference changes THIS list; the component list
    keeps the old name until SOLIDWORKS itself saves the file (confirmed: after ReplaceReference and Save, reopened
    from disk, GetAllExternalReferences4 shows the new name, the components the old one, and SOLIDWORKS loads the new)."""
    doc = _keep(_open_sw_retry(path))
    try:
        return set(_string_list(_keep(doc.GetAllExternalReferences4(_search_option(saved_only)))) or [])
    finally:
        try:
            doc.CloseDoc()
        except Exception:  # noqa: BLE001
            pass


def backup_root():
    return Path(_expand(BACKUP_FOLDER)) if BACKUP_FOLDER.strip() else DATA_DIR / "backups"


def _job_from_plans(plans):
    """One job for one or more renames: every file that moves, and per referencing file the references to replace.
    A file that references several renamed files is opened and saved once."""
    renames, parents = [], {}
    for p in plans:
        renames.append({"old": p["path"], "new": p["new_path"], "drawing": p.get("drawing")})
        for par in p["parents"]:
            if par["role"] == "rename_with_unlinked":
                continue                            # moves along, but does not reference it: nothing to replace
            e = parents.setdefault(par["path"].lower(), {"path": par["path"], "role": par["role"], "refs": []})
            e["refs"].append({"old": p["path"], "new": p["new_path"], "stored_path": par.get("stored_path")})
            if par["role"] != "reference":
                e["role"] = par["role"]
    return {"renames": renames, "parents": list(parents.values())}


def _job_moves(job):
    moves = []
    for r in job["renames"]:
        moves.append((r["old"], r["new"]))
        if r.get("drawing"):
            moves.append((r["drawing"]["old"], r["drawing"]["new"]))
    return moves


_COM_KEEP = []        # job process: Document Manager results are never cleaned up by comtypes (see _keep)


def _keep(*objs):
    """Keep COM results alive until the process ends (it ends with os._exit, without cleaning up). Letting
    comtypes clear the arrays that GetAllExternalReferences4 returns corrupts memory (0xC0000374); with one
    file that goes unnoticed, with a whole assembly the job crashes and everything is put back."""
    _COM_KEEP.extend(o for o in objs if o is not None)
    return objs[0] if objs else None


def _rlog(line):
    """rename_debug.log in the data folder: what each rename did, step by step (kept below about 1 MB)."""
    try:
        log = DATA_DIR / "rename_debug.log"
        if log.exists() and log.stat().st_size > 1_000_000:
            log.write_text(log.read_text(encoding="utf-8", errors="replace")[-300_000:], encoding="utf-8")
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def _rename_job(job):
    """The real work (runs in a separate process). Writes the journal after every step."""
    import datetime
    _rlog("rename: " + "; ".join(f"{r['old']} -> {r['new']}" for r in job["renames"]))
    jpath = job["journal"]
    journal = {"backups": {}, "moved": [], "saved": [], "locks": [], "step": "start"}
    _journal_write(jpath, journal)
    moves = _job_moves(job)
    final_of = {a.lower(): b for a, b in moves}           # where a file is after moving
    touched = list(dict.fromkeys([a for a, _ in moves] + [p["path"] for p in job["parents"]]))

    # 1. Check again right before the work: something may have changed since the check in the browser
    for p in touched:
        why = blocking_reason(access_info(p))
        if why:
            raise RuntimeError(f"{_fname(p)}: {why}")
    vacated = {a.lower() for a, _ in moves}
    for a, b in moves:
        if os.path.exists(b) and a.lower() != b.lower() and b.lower() not in vacated:
            raise RuntimeError(f"there already is a file {Path(b).name}")

    # 2. Lock files, so nobody opens the files during the work
    for p in touched:
        journal["locks"].append(str(_create_app_lock(p)))
        _journal_write(jpath, journal)

    # 3. A backup of everything that changes
    now = datetime.datetime.now()
    bdir = backup_root() / now.strftime("%Y%m%d") / now.strftime("%H%M%S")
    bdir.mkdir(parents=True, exist_ok=True)
    for i, p in enumerate(touched):
        b = bdir / f"{i:03d}_{Path(p).name}"
        shutil.copy2(p, b)
        journal["backups"][p] = str(b)
    (bdir / "what_happened.txt").write_text(
        "Renamed/moved:\n" + "\n".join(f"{a}\n-> {b}" for a, b in moves) +
        "\n\nOriginal location of each backup:\n" +
        "\n".join(f"{Path(b).name}  =  {p}" for p, b in journal["backups"].items()), encoding="utf-8")
    journal["backup_dir"] = str(bdir)
    journal["step"] = "backup"
    _journal_write(jpath, journal)

    # 4. Move/rename the files (the models, and their drawings with the same name)
    for a, b in moves:
        if a.lower() == b.lower() and a != b:        # only the letter case changes: via a temporary name
            tmp = a + ".whereused.tmp"
            os.replace(a, tmp)
            os.replace(tmp, b)
        else:
            shutil.move(a, b)
        journal["moved"].append([a, b])
        _journal_write(jpath, journal)
    journal["step"] = "moved"

    # 5. Update the references: every referencing file is opened and saved once, for all its renamed files
    results = []
    for par in job["parents"]:
        orig = par["path"]
        where = final_of.get(orig.lower(), orig)          # a file that was renamed itself: its new place
        doc = _keep(_open_sw(where, readonly=False))
        replaced, changes = 0, []
        try:
            res = _keep(doc.GetAllExternalReferences4(_keep(_sw_app().GetSearchOptionObject())))   # required before ReplaceReference
            refs = _string_list(res) or []
            _rlog(f"  {where}: external references: " + "; ".join(refs))
            for r in par["refs"]:
                old_name = _fname(r["old"]).lower()
                candidates = [x for x in refs if _fname(x).lower() == old_name]
                # Exactly this file (as saved, or at the old place); only if that is not there: by name
                exact = [x for x in candidates if x.lower() in (r["old"].lower(), (r.get("stored_path") or "").lower())]
                hits = exact or candidates
                for x in hits:
                    ret = _keep(doc.ReplaceReference(x, r["new"]))
                    _rlog(f"    ReplaceReference({x} -> {r['new']}) returned {ret!r} (saved as: {r.get('stored_path')})")
                    replaced += 1
                if not hits:
                    _rlog(f"    no reference named {old_name} found (index: {r.get('stored_path')})")
                if hits:
                    changes.append([r["old"], r["new"]])
            if where.lower().endswith(".sldprt") and len(changes) < len(par["refs"]):
                # A derived/mirrored part whose reference cannot be replaced would be left broken: stop, put back
                missing = [_fname(r["old"]) for r in par["refs"] if [r["old"], r["new"]] not in changes]
                raise RuntimeError(f"{_fname(where)}: its reference to {', '.join(missing)} could not be updated "
                                   "(part references); nothing has been changed")
            if replaced:
                save = _keep(doc.Save())
                code = next((n for n in _split(save)[1] if n), 0) if save is not None else 0
                _rlog(f"    Save returned {save!r}")
                if code:
                    raise RuntimeError(f"{_fname(where)}: saving failed (code {code})")
        finally:
            try:
                doc.CloseDoc()
            except Exception:  # noqa: BLE001
                pass
        if replaced:
            journal["saved"].append(orig)          # rollback moves files back first, then restores them here
            _journal_write(jpath, journal)
        results.append({"path": where, "replaced": replaced, "role": par.get("role"), "changes": changes,
                        "expected": len(par["refs"])})
    journal["step"] = "updated"
    _journal_write(jpath, journal)

    # 6. Verify, reading the safe way: does every file now point to the new files, and no longer to the old ones?
    for r in results:
        if not r["replaced"]:
            continue
        raw = _refs_of(r["path"]) if r["path"].lower().endswith(".sldprt") else _external_refs(r["path"])
        _rlog(f"  check {r['path']}: " + "; ".join(sorted(raw)))
        refs = {x.lower() for x in raw}
        for old, new in r["changes"]:
            if new.lower() == old.lower():            # only the letter case changes: compare exactly
                ok_new, still_old = new in raw, old in raw
            elif _fname(new).lower() == _fname(old).lower():   # only moved: check the full path
                ok_new, still_old = new.lower() in refs, old.lower() in refs
            else:
                ok_new = new.lower() in refs or any(_fname(x) == _fname(new).lower() for x in refs)
                still_old = old.lower() in refs
            if not ok_new:
                raise RuntimeError(f"check failed: {_fname(r['path'])} does not point to {new} "
                                   f"(details in {DATA_DIR / 'rename_debug.log'})")
            if still_old:
                raise RuntimeError(f"check failed: {_fname(r['path'])} still points to {old} "
                                   f"(details in {DATA_DIR / 'rename_debug.log'})")
        r["verified"] = True

    # 7. Done: remove the lock files
    for lock in journal["locks"]:
        try:
            if os.path.exists(lock):
                _force_unlink(lock)
        except OSError:
            pass
    journal["step"] = "done"
    _journal_write(jpath, journal)
    missed = [r["path"] for r in results if len(r["changes"]) < r["expected"] and r["role"] != "rename_with_unlinked"]
    return {"ok": True, "results": results, "backup_dir": str(bdir), "not_found_in": missed}


def _log(action, old, new, result):
    import csv
    log = DATA_DIR / "rename_log.csv"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fresh = not log.exists()
        with open(log, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if fresh:
                w.writerow(["Time", "User", "Action", "Old", "New", "Result"])
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), os.environ.get("USERNAME", ""), action, old, new, result])
    except OSError:
        pass


def _remember_detected(parent, pairs):
    """What the external references of ONE assembly say about its components whose saved path is gone (renamed
    or moved by another tool). Only for that assembly: several copies of a project can share the same old path
    (an old location), and each copy uses its own files. (Kept like Pack & Go's copies: per parent, by name.)"""
    with _db_lock:
        db = conn()
        for old, new in pairs:
            db.execute("INSERT OR REPLACE INTO copied_refs VALUES (?,?,?,?)",
                       (parent.lower(), _fname(old).lower(), new, time.time()))
            db.execute("DELETE FROM detected_refs WHERE parent=? AND lower(old)=?", (parent.lower(), old.lower()))
            db.execute("INSERT INTO detected_refs VALUES (?,?,?,?)", (parent.lower(), old, new, time.time()))
        db.commit()
    _copied_cache["t"] = 0.0
    _hint_cache["t"] = 0.0


_hint_cache = {"t": 0.0, "map": {}}


def _apply_hints_to_index():
    """At the end of an index run: rows stored before an assembly found the new file (a drawing read earlier in the
    run). Only hints all assemblies agree on; through the index on file name."""
    _hint_cache["t"] = 0.0
    _apply_hints_to_rows([])                          # (fills the cache)
    hints = _hint_cache["map"]
    if not hints:
        return
    with _db_lock:
        db = conn()
        db.executemany("UPDATE refs SET child_name=?, child_stored_path=? WHERE child_name=? AND lower(child_stored_path)=?",
                       [(_fname(n).lower(), n, _fname(o).lower(), o) for o, n in hints.items()])
        db.commit()


def _apply_hints_to_rows(rows):
    """For a file that cannot find out itself (a drawing: its external references cannot be read safely): an old
    path that the assemblies found renamed or moved, but ONLY when they all agree on the new file. Copies of a
    project share old paths and each uses its own files; then they disagree, and nothing is taken over."""
    if time.time() - _hint_cache["t"] > 30:
        with _db_lock:
            found = {}
            for old, new in conn().execute("SELECT old, new FROM detected_refs"):
                found.setdefault(old.lower(), set()).add(new)
        _hint_cache.update(t=time.time(), map={o: next(iter(n)) for o, n in found.items()
                                               if len({x.lower() for x in n}) == 1})
    hints = _hint_cache["map"]
    if not hints or not rows:
        return rows or []
    out = []
    for row in rows:
        new = hints.get((row[3] or "").lower())
        if new:
            row = (row[0], row[1], _fname(new).lower(), new) + tuple(row[4:])
        out.append(row)
    return out


def _own_renames_logged():
    """Old paths (lower case) of the renames and moves this app did itself, from rename_log.csv."""
    import csv
    out = set()
    try:
        with open(DATA_DIR / "rename_log.csv", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if (row.get("Action") or "").startswith("rename") and row.get("Old"):
                    out.add(row["Old"].lower())
    except (OSError, ValueError):
        pass
    return out


def _cleanup_global_detections():
    """Once (1.24.0): earlier versions kept a rename found in ONE assembly for ALL of them, and rewrote every
    assembly that named the old path. With copies of a project sharing an old location, that pointed every copy at
    the files of the first one read. Remove those (the app's own renames, in the log, stay) and read the assemblies
    they touched again, so each gets its own files."""
    if _meta_get("detect_cleanup") == "1.24.0":
        return 0
    own = _own_renames_logged()
    with _db_lock:
        db = conn()
        rows = db.execute("SELECT old, new FROM renamed").fetchall()
        wipe = [(o, n) for o, n in rows if o.lower() not in own]
        news = sorted({n.lower() for _o, n in wipe})
        parents = set()
        for i in range(0, len(news), 500):
            chunk = news[i:i + 500]
            parents |= {p for (p,) in db.execute(
                f"SELECT DISTINCT parent FROM refs WHERE lower(child_stored_path) IN ({','.join('?' * len(chunk))})", chunk)}
        db.executemany("DELETE FROM renamed WHERE old=?", [(o,) for o, _n in wipe])
        db.executemany("UPDATE files SET mtime=0 WHERE path=?", [(p,) for p in parents])   # read again
        db.commit()
    _meta_set("detect_cleanup", "1.24.0")
    _renamed_cache["t"] = 0.0
    return len(parents)


def _remember_renames(job):
    """Old path -> new path for every file this app moved. The component list of an assembly keeps the old name
    until SOLIDWORKS itself saves it; reading it later, the index translates it (apply_rename)."""
    with _db_lock:
        db = conn()
        for a, b in _job_moves(job):
            if a.lower() != b.lower():
                db.execute("INSERT OR REPLACE INTO renamed (old, new, at, shown) VALUES (?,?,?,?)", (a.lower(), b, time.time(), a))
        db.commit()
    _renamed_cache["t"] = 0.0


_renamed_cache = {"t": 0.0, "map": {}, "rev": {}}


def _renamed_map():
    """old (lower case) -> new path; read again after at most 30 seconds. Takes the database lock itself, so it
    must be called OUTSIDE 'with _db_lock:' (the lock is not re-entrant: inside it would wait on itself)."""
    if time.time() - _renamed_cache["t"] > 30:
        with _db_lock:
            rows = conn().execute("SELECT old, new, at, shown FROM renamed ORDER BY at").fetchall()
        _renamed_cache["map"] = {o: n for o, n, _a, _s in rows}
        _renamed_cache["rev"] = {n.lower(): (sh or o, a) for o, n, a, sh in rows}     # newest rename wins
        _renamed_cache["t"] = time.time()
    return _renamed_cache["map"]


def _renamed_from(path):
    """(old path, time) when this app renamed a file to this path; else None."""
    _renamed_map()
    return _renamed_cache["rev"].get((path or "").lower())


def apply_rename(path, mapping=None):
    """The current path of a file this app renamed (also through several renames; at most 10 steps).
    Only when the old path no longer exists: a new file with the old name is left alone."""
    mapping = _renamed_map() if mapping is None else mapping
    cur, steps = path, 0
    while cur and cur.lower() in mapping and steps < 10:
        if steps == 0 and os.path.exists(cur):
            return path
        cur, steps = mapping[cur.lower()], steps + 1
    return cur


_copied_cache = {"t": 0.0, "map": {}}


def _copied_map():
    """parent (lower case) -> {old file name: new path}: what Pack & Go changed in each copy. Reads the database
    itself: call it OUTSIDE 'with _db_lock:'."""
    if time.time() - _copied_cache["t"] > 30:
        with _db_lock:
            rows = conn().execute("SELECT parent, old_name, new FROM copied_refs").fetchall()
        m = {}
        for parent, old, new in rows:
            m.setdefault(parent, {})[old] = new
        _copied_cache.update(t=time.time(), map=m)
    return _copied_cache["map"]


def _apply_copies_to_rows(rows):
    """A copy made by Pack & Go still lists the ORIGINAL files in its component list (until SOLIDWORKS saves it),
    while SOLIDWORKS loads the copies. Only in that copy: old name -> the copy. The originals exist and are used
    elsewhere, so this cannot be a general translation like renames."""
    m = _copied_map()
    if not m or not rows:
        return rows
    out = []
    for row in rows:
        mine = m.get(row[0])
        new = mine.get(row[2]) if mine else None
        if new:
            row = (row[0], row[1], _fname(new).lower(), new) + tuple(row[4:])
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Fix paths: references whose saved path no longer exists ("found by name") get the path where the file really is
# ---------------------------------------------------------------------------
def fix_paths_plan(parents):
    """What Fix paths would change. Read from the FILES themselves (their external references, in the safe helper
    process), not from the index, which already translates old paths. The real file, as SOLIDWORKS finds it: a file
    with that name in the assembly's own folder, otherwise the only file with that name in the index. More than one
    candidate: left alone (no guessing). Changes nothing."""
    maps = _file_maps()
    blockers, unsure, notfound, items = [], [], [], []
    for parent in dict.fromkeys(parents):
        if not parent.lower().endswith(".sldasm"):
            continue
        refs = _ext_refs_isolated(parent, saved_only=True)
        if refs is None:
            unsure.append(f"{_fname(parent)}: its references could not be read")
            continue
        # Safety net: the component list as the index read it (saved paths), for references the Document Manager
        # reports as found elsewhere anyway
        have = {_fname(r).lower() for r in refs if r and not os.path.exists(r)}
        with _db_lock:
            saved = [sp for (sp,) in conn().execute("SELECT DISTINCT child_stored_path FROM refs WHERE parent=? AND virtual=0 "
                                                     "AND child_stored_path != ''", (parent.lower(),))]
        refs = list(refs) + [sp for sp in saved if not os.path.exists(sp) and _fname(sp).lower() not in have]
        fixes = []
        for ref in refs:
            if not ref or os.path.exists(ref) or _TEMP_PATH_RE.search(ref) or _is_virtual_path(ref):
                continue
            own = _join(_folder_of(parent) or "", _fname(ref))
            if os.path.isfile(own):
                fixes.append({"old": ref, "new": own, "how": "in the assembly's own folder"})
                continue
            cands = [p for p in (maps["indexed"].get(x, x) for x in maps["by_name"].get(_fname(ref).lower(), [])) if os.path.isfile(p)]
            if len(cands) == 1:
                fixes.append({"old": ref, "new": cands[0], "how": "the only file with this name"})
            elif cands:
                unsure.append(f"{_fname(parent)}: {_fname(ref)} exists {len(cands)} times; left alone")
            else:
                notfound.append(f"{_fname(parent)}: {_fname(ref)} is nowhere to be found")
        if not fixes:
            continue
        why = blocking_reason(access_info(parent)) or (f"in a SOLIDWORKS PDM vault ({pdm_vault(parent)}): do it in PDM" if pdm_vault(parent) else None)
        if why:
            blockers.append(f"{_fname(parent)}: {why}")
        items.append({"parent": parent, "fixes": fixes})
    return {"items": items, "blockers": blockers, "unsure": unsure, "notfound": notfound,
            "counts": {"assemblies": len(items), "references": sum(len(i["fixes"]) for i in items)}}


def fix_paths(parents, dry_run=True):
    plan = fix_paths_plan(parents)
    if dry_run or plan["blockers"] or not plan["items"]:
        return {**plan, "executed": False}
    if not _rename_busy.acquire(blocking=False):
        return {**plan, "executed": False, "blockers": ["a rename or Pack & Go is still running"]}
    try:
        job = {"kind": "fixpaths", "items": plan["items"], "parents": [i["parent"] for i in plan["items"]], "renames": []}
        return {**plan, "executed": True, "result": _execute_job(job)}
    finally:
        _rename_busy.release()


def _fixpaths_job(job):
    """In the job process: back up every assembly that changes, replace the paths, save, and check."""
    import datetime
    jpath = job["journal"]
    journal = {"kind": "fixpaths", "backups": [], "step": "start"}
    _journal_write(jpath, journal)
    now = datetime.datetime.now()
    bdir = backup_root() / now.strftime("%Y%m%d") / (now.strftime("%H%M%S") + "_fixpaths")
    bdir.mkdir(parents=True, exist_ok=True)
    for i, item in enumerate(job["items"]):
        saved = bdir / f"{i:03d}_{_fname(item['parent'])}"
        shutil.copy2(item["parent"], str(saved))
        journal["backups"].append([item["parent"], str(saved)])
        _journal_write(jpath, journal)
    journal["step"] = "backed up"
    _rlog("fix paths: " + "; ".join(f"{i['parent']}: {len(i['fixes'])}" for i in job["items"]))
    for item in job["items"]:
        doc = _keep(_open_sw(item["parent"], readonly=False))
        try:
            refs = _string_list(_keep(doc.GetAllExternalReferences4(_search_option(saved_only=True)))) or []
            by_low = {r.lower(): r for r in refs}
            by_name = {}
            for r in refs:
                by_name.setdefault(_fname(r).lower(), []).append(r)
            _rlog(f"  {item['parent']}: references (as saved): " + "; ".join(refs))
            changed = 0
            for f in item["fixes"]:
                ref = by_low.get(f["old"].lower())
                if ref is None and len(by_name.get(_fname(f["old"]).lower(), [])) == 1:
                    ref = by_name[_fname(f["old"]).lower()][0]      # reported under another path: the only one by name
                if ref is None:
                    _rlog(f"    {f['old']}: not among the references; skipped")
                    continue
                ret = _keep(doc.ReplaceReference(ref, f["new"]))
                _rlog(f"  {item['parent']}: ReplaceReference({ref} -> {f['new']}) returned {ret!r}")
                changed += 1
            if changed:
                save = _keep(doc.Save())
                code = next((n for n in _split(save)[1] if n), 0) if save is not None else 0
                if code:
                    raise RuntimeError(f"{_fname(item['parent'])}: saving failed (code {code})")
        finally:
            try:
                doc.CloseDoc()
            except Exception:  # noqa: BLE001
                pass
    journal["step"] = "changed"
    _journal_write(jpath, journal)
    for item in (job["items"] if job.get("undo_check", True) else []):   # check: new paths in, old out (not for Undo)
        refs = {x.lower() for x in _external_refs(item["parent"], saved_only=True)}
        for f in item["fixes"]:
            if f["new"].lower() not in refs or f["old"].lower() in refs:
                raise RuntimeError(f"check failed: {_fname(item['parent'])} does not point to {f['new']} "
                                   f"(details in {DATA_DIR / 'rename_debug.log'})")
    journal["step"] = "done"
    _journal_write(jpath, journal)
    return {"ok": True, "items": job["items"], "backups": journal["backups"]}


def _fixpaths_rollback(journal):
    """Put the assemblies back as they were (from their backups)."""
    problems = []
    for original, saved in reversed(journal.get("backups", [])):
        try:
            shutil.copy2(saved, original)
        except OSError as e:
            problems.append(f"putting back {original} failed: {e} (the backup is {saved})")
    return problems


def _remember_copies(result):
    """After Pack & Go: per copy, the references it now has (by file name, unique within one assembly)."""
    with _db_lock:
        db = conn()
        for r in result.get("results", []):
            for old, new in r.get("changes", []):
                db.execute("INSERT OR REPLACE INTO copied_refs VALUES (?,?,?,?)",
                           (r["path"].lower(), _fname(old).lower(), new, time.time()))
        db.commit()
    _copied_cache["t"] = 0.0


def _apply_renames_to_rows(rows):
    """Index rows (parent, cfg, child_name, stored, ccfg, qty, flags, virtual) with renamed children translated."""
    mapping = _renamed_map()
    if not mapping:
        return rows
    out = []
    for row in rows:
        stored = row[3]
        if stored and stored.lower() in mapping:
            new = apply_rename(stored, mapping)
            if new != stored:
                row = (row[0], row[1], _fname(new).lower(), new) + tuple(row[4:])
        out.append(row)
    return out


def _index_after_rename(job, result):
    """Bring the index up to date straight away, without reading anything: new names and paths."""
    moves = _job_moves(job)
    with _db_lock:
        db = conn()
        for a, b in moves:                           # moved assemblies/drawings keep their own references
            db.execute("UPDATE refs SET parent=? WHERE parent=?", (b.lower(), a.lower()))
            db.execute("UPDATE files SET path=?, display_path=? WHERE path=?", (b.lower(), b, a.lower()))
            db.execute("UPDATE OR REPLACE versions SET path=? WHERE path=?", (b.lower(), a.lower()))
            for table in ("props", "readver", "propsver"):
                db.execute(f"UPDATE OR REPLACE {table} SET path=? WHERE path=?", (b.lower(), a.lower()))
        for r in result.get("results", []):
            if not r.get("replaced"):
                continue
            p = r["path"].lower()                    # already the new place of a moved file
            for old, new in r.get("changes", []):
                db.execute("UPDATE refs SET child_name=?, child_stored_path=? WHERE parent=? AND child_name=?",
                           (_fname(new).lower(), new, p, _fname(old).lower()))
            db.execute("UPDATE files SET mtime=0 WHERE path=?", (p,))   # changed: read again at the next update
        db.commit()
    _counts_cache.clear()
    _tree_version[0] += 1


_job_now = {"job": None, "started": 0.0}


def job_progress():
    """How far the running rename / Pack & Go / Fix paths is (read from its journal), for the progress bar."""
    job = _job_now["job"]
    if not job:
        return {"running": False}
    try:
        j = json.loads(Path(job["journal"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        j = {}
    copies, update = len(job.get("copies") or []), len(job.get("update") or job.get("items") or job.get("parents") or [])
    props = copies if job.get("props_rules") else 0
    copied, updated, props_n = len(j.get("created") or []), j.get("updated_n", 0), j.get("props_n", 0)
    total = max(1, copies + update + props)
    phase = ("Copying files" if copied < copies else "Pointing the copies to each other" if updated < update
             else "Setting the properties" if props_n < props else "Checking")
    return {"running": True, "kind": job.get("kind"), "phase": phase, "done": min(total, copied + updated + props_n), "total": total,
            "copied": copied, "copies": copies, "updated": updated, "update": update, "props": props_n, "props_total": props,
            "seconds": round(time.time() - _job_now["started"])}


def _execute_job(job):
    """Run a job in a separate process; after a crash or hang put everything back here. Returns the result."""
    jobs = DATA_DIR / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    stamp = f"{int(time.time() * 1000)}"
    job = {**job, "journal": str(jobs / f"{stamp}.journal.json"), "result": str(jobs / f"{stamp}.result.json")}
    _job_now.update(job=job, started=time.time())
    try:
        return _execute_job_run(job, jobs, stamp)
    finally:
        _job_now["job"] = None


def _execute_job_run(job, jobs, stamp):
    job_file = jobs / f"{stamp}.job.json"
    job_file.write_text(json.dumps(job), encoding="utf-8")
    proc = subprocess.Popen([PY_CHILD, str(Path(__file__).resolve()), "--rename-job", str(job_file)], cwd=str(HERE),
                            creationflags=_quiet_flags())
    _children.add(proc)
    try:
        code = proc.wait(timeout=max(900, 60 * len(job["parents"]) + 300))
    except subprocess.TimeoutExpired:
        _kill_tree(proc)               # the whole tree: the job must really have stopped before anything is put back
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        code = "stuck"
    result = None
    try:
        result = json.loads(Path(job["result"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if not result or (not result.get("ok") and not result.get("rolled_back") and not result.get("rollback_problems")):
        # Crash or stuck: the server puts everything back following the journal
        try:
            journal = json.loads(Path(job["journal"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            journal = {}
        problems = (_packgo_rollback(journal) if job.get("kind") == "packgo" else
                    _fixpaths_rollback(journal) if job.get("kind") == "fixpaths" else _rollback(journal))
        result = {"ok": False, "rolled_back": not problems, "rollback_problems": problems,
                  "error": (result or {}).get("error") or f"the rename process stopped unexpectedly ({code})",
                  "backup_dir": journal.get("backup_dir")}
    updated = sum(1 for r in result.get("results", []) if r.get("replaced"))
    outcome = (f"ok, {updated} file(s) updated" if result.get("ok")
               else f"FAILED: {result.get('error')}" + ("; everything put back" if result.get("rolled_back") else ""))
    if job.get("kind") == "fixpaths":
        n = sum(len(i["fixes"]) for i in job["items"])
        _log("fix paths" if not job.get("undo_of") else "undo fix paths", job["items"][0]["parent"] if job["items"] else "",
             f"{n} reference(s) in {len(job['items'])} assembly/assemblies", outcome)
        if result.get("ok"):
            # the component lists still name the old paths until SOLIDWORKS saves: the index reads them as the new
            with _db_lock:
                db = conn()
                for it in job["items"]:
                    for f in it["fixes"]:
                        db.execute("INSERT OR REPLACE INTO copied_refs VALUES (?,?,?,?)",
                                   (it["parent"].lower(), _fname(f["old"]).lower(), f["new"], time.time()))
                        # right away in the index too (names are unique within one assembly): the page shows it at once
                        db.execute("UPDATE refs SET child_name=?, child_stored_path=? WHERE parent=? AND child_name=?",
                                   (_fname(f["new"]).lower(), f["new"], it["parent"].lower(), _fname(f["old"]).lower()))
                    db.execute("UPDATE files SET mtime=0 WHERE path=?", (it["parent"].lower(),))   # read again
                db.commit()
            _copied_cache["t"] = 0.0
            _structure_cache.clear()                          # structures kept from before: no longer right
            for cache in (_broken_cache, _index_maps):
                cache["key"] = None
            _history_add({"kind": "fixpaths", "items": [{"old": it["parent"], "new": it["parent"], "fixes": it["fixes"]} for it in job["items"]],
                          "undo_of": job.get("undo_of")})
            for f in (job_file, Path(job["journal"]), Path(job["result"])):
                try:
                    f.unlink()
                except OSError:
                    pass
            update_index()
        return result
    if job.get("kind") == "packgo":
        _log("pack and go", job["copies"][0]["old"] if job["copies"] else "", f"{len(job['copies'])} file(s)", outcome)
        if result.get("ok"):
            _remember_copies(result)
            _history_add({"kind": "packgo", "items": [{"old": c["old"], "new": c["new"]} for c in job["copies"]],
                          "created": [[f, _mtime(f)] for f in result.get("created", [])],
                          "replaced": result.get("replaced", []), "dirs": result.get("dirs", [])})
            for f in (job_file, Path(job["journal"]), Path(job["result"])):
                try:
                    f.unlink()
                except OSError:
                    pass
            _listing_cache.clear()
            _fstatus_cache.clear()
            _isdir_cache.clear()
            update_index()
        return result
    for r in job["renames"]:
        _log("rename" if len(job["renames"]) == 1 else f"rename (batch of {len(job['renames'])})",
             r["old"] + (f" (+ {r['drawing']['old']})" if r.get("drawing") else ""), r["new"], outcome)
    if result.get("ok"):
        _remember_renames(job)
        _index_after_rename(job, result)
        _history_add({"kind": "rename", "include_own": bool(job.get("include_own")),
                      "with_drawings": any(r.get("drawing") for r in job["renames"]),
                      "items": [{"old": r["old"], "new": r["new"], "drawing": r.get("drawing")} for r in job["renames"]],
                      "undo_of": job.get("undo_of")})
        _listing_cache.clear()                   # the folders changed: forget what was read from them
        _fstatus_cache.clear()
        _isdir_cache.clear()
        for f in (job_file, Path(job["journal"]), Path(job["result"])):
            try:
                f.unlink()
            except OSError:
                pass
        update_index()
    return result


# ---------------------------------------------------------------------------
# History and Undo
# ---------------------------------------------------------------------------
_history_lock = threading.Lock()


def _history_file():
    return DATA_DIR / "history.json"


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _history_load():
    try:
        return json.loads(_history_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _history_save(entries):
    tmp = _history_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(entries[-300:], indent=1), encoding="utf-8")
    os.replace(tmp, _history_file())


def _history_add(entry):
    import getpass
    import uuid
    with _history_lock:
        entries = _history_load()
        entry = {"id": uuid.uuid4().hex[:12], "time": time.time(), "user": getpass.getuser(), "undone": None, **entry}
        if entry.get("undo_of"):                                   # this one undid an earlier one
            for e in entries:
                if e["id"] == entry["undo_of"]:
                    e["undone"] = entry["time"]
        entries.append(entry)
        _history_save(entries)
    return entry


def history(limit=200):
    """What this app did, newest first."""
    return list(reversed(_history_load()))[:limit]


def _undo_rename_items(entry):
    """Undoing a rename: the same rename back (new -> old), with the same engine (references, backups, all or
    nothing). Not restoring the old backups: work done in the files since then would be lost."""
    items = []
    for it in entry["items"]:
        old, new = it["old"], it["new"]
        folder = _folder_of(old) if (_folder_of(old) or "").lower() != (_folder_of(new) or "").lower() else None
        items.append({"path": new, "name": Path(PureWindowsPath(old).name).stem, **({"folder": folder} if folder else {})})
    return items


def undo(entry_id, dry_run=True, force=False):
    entry = next((e for e in _history_load() if e["id"] == entry_id), None)
    if not entry:
        raise ValueError("this action is not in the history")
    if entry.get("undone"):
        raise ValueError("this action has been undone already")
    if entry["kind"] == "fixpaths":
        items = [{"parent": it["old"], "fixes": [{"old": f["new"], "new": f["old"], "how": "back"} for f in it["fixes"]]} for it in entry["items"]]
        if dry_run:
            return {"kind": "fixpaths", "plan": {"items": items, "blockers": [], "warnings": [],
                                                 "counts": {"references": sum(len(i["fixes"]) for i in items)}}}
        if not _rename_busy.acquire(blocking=False):
            raise ValueError("a rename or Pack & Go is still running")
        try:
            job = {"kind": "fixpaths", "items": items, "parents": [i["parent"] for i in items], "renames": [], "undo_of": entry["id"],
                   "undo_check": False}
            return {"kind": "fixpaths", "executed": True, "result": _execute_job(job)}
        finally:
            _rename_busy.release()
    if entry["kind"] == "rename":
        items = _undo_rename_items(entry)
        if dry_run:
            return {"kind": "rename", "plan": rename_batch(items, entry.get("with_drawings", True), True, entry.get("include_own", False))}
        if not _rename_busy.acquire(blocking=False):
            raise ValueError("a rename or Pack & Go is still running")
        _rename_busy.release()
        plans = rename_batch(items, entry.get("with_drawings", True), True, entry.get("include_own", False))
        if plans.get("blockers"):
            return {"kind": "rename", "plan": plans, "executed": False}
        # carried out like any batch rename, recorded as the undo of this entry
        res = _undo_execute_batch(items, entry)
        return {"kind": "rename", "executed": True, "result": res}
    # Pack & Go: remove the copies again, put back what they replaced
    blockers, warnings = [], []
    created = [(f, m) for f, m in entry.get("created", [])]
    gone = [f for f, _m in created if not os.path.exists(f)]
    changed = [f for f, m in created if os.path.exists(f) and m is not None and abs((_mtime(f) or 0) - m) > 2]
    if gone:
        warnings.append(f"{len(gone)} copy/copies no longer exist (moved or removed since); they are skipped.")
    if changed:
        msg = f"{len(changed)} copy/copies were changed after Pack & Go ({', '.join(_fname(f) for f in changed[:3])}{'…' if len(changed) > 3 else ''})"
        (warnings if force else blockers).append(msg + ("; they are removed anyway, as you chose." if force else
                                                        "; removing them loses that work. Tick 'remove anyway' to go on."))
    created_set = {f.lower() for f, _m in created}
    users = parents_for_paths([f for f, _m in created if os.path.exists(f)])
    outside = sorted({_file_maps()["indexed"].get(p, p) for f, (used, _o) in users.items() for p in used if p not in created_set})
    if outside:
        warnings.append(f"{len(outside)} file(s) outside this Pack & Go use the copies ({', '.join(_fname(x) for x in outside[:3])}"
                        f"{'…' if len(outside) > 3 else ''}); they will no longer find them.")
    for f, _m in created:
        acc = access_info(f) if os.path.exists(f) else None
        why = blocking_reason(acc) if acc else None
        if why:
            blockers.append(f"{_fname(f)}: {why}")
    plan = {"kind": "packgo", "remove": [f for f, _m in created if f not in gone], "put_back": [o for o, _b in entry.get("replaced", [])],
            "blockers": blockers, "warnings": warnings, "outside": outside}
    if dry_run or blockers:
        return {**plan, "executed": False}
    problems = []
    for f in reversed(plan["remove"]):
        try:
            _force_unlink(f)
        except OSError as e:
            problems.append(f"removing {f} failed: {e}")
    for d in reversed(entry.get("dirs", [])):
        try:
            os.rmdir(d)
        except OSError:
            pass
    for original, saved in reversed(entry.get("replaced", [])):
        try:
            if not os.path.exists(original):
                shutil.move(saved, original)
        except OSError as e:
            problems.append(f"putting back {original} failed: {e} (the backup is {saved})")
    with _history_lock:
        entries = _history_load()
        for e in entries:
            if e["id"] == entry_id:
                e["undone"] = time.time()
        _history_save(entries)
    _log("undo pack and go", plan["remove"][0] if plan["remove"] else "", f"{len(plan['remove'])} file(s) removed",
         "done" if not problems else "; ".join(problems))
    _listing_cache.clear()
    _fstatus_cache.clear()
    _isdir_cache.clear()
    update_index()
    return {**plan, "executed": True, "result": {"ok": not problems, "problems": problems}}


def _undo_execute_batch(items, entry):
    plans, acc_cache = [], {}
    for it in items:
        plans.append(rename_plan(it["path"], it["name"], it.get("folder"), entry.get("with_drawings", True),
                                 acc_cache=acc_cache, include_own=entry.get("include_own", False)))
    if not _rename_busy.acquire(blocking=False):
        return {"ok": False, "error": "a rename or Pack & Go is still running"}
    try:
        return _execute_job({**_job_from_plans(plans), "include_own": entry.get("include_own", False), "undo_of": entry["id"]})
    finally:
        _rename_busy.release()


def rename_execute(path, new_name, new_folder=None, with_drawing=True, include_own=False):
    """Make the plan and, when nothing stops it, carry it out in a separate process."""
    plan = rename_plan(path, new_name, new_folder, with_drawing, include_own=include_own)
    if plan["blockers"]:
        return {**plan, "executed": False}
    if not _rename_busy.acquire(blocking=False):
        return {**plan, "executed": False, "blockers": ["another rename is still running"]}
    try:
        return {**plan, "executed": True, "result": _execute_job({**_job_from_plans([plan]), "include_own": bool(include_own)})}
    finally:
        _rename_busy.release()


BATCH_LIMIT = 500


def rename_batch(items, with_drawings=True, dry_run=True, include_own=False):
    """Rename and/or move several files at once. items: [{"path", "name", "folder"?}]; without a folder a file
    stays in its own folder.
    Returns per file what happens and what stops it, the files that change in total, and (when carried out)
    the result. All or nothing: one job, one backup, everything put back if anything fails."""
    items = [it for it in items if str(it.get("path") or "").strip() and str(it.get("name") or "").strip()]
    blockers = []
    if not items:
        blockers.append("no new names given")
    if len(items) > BATCH_LIMIT:
        blockers.append(f"at most {BATCH_LIMIT} files at once")
        items = items[:BATCH_LIMIT]
    acc_cache = {}
    plans = []
    for it in items:
        plan = rename_plan(str(it["path"]), str(it["name"]), (str(it.get("folder") or "").strip() or None),
                           with_drawings, acc_cache=acc_cache, include_own=include_own)
        if str(plan["new_path"]) == str(plan["path"]):
            continue                                 # unchanged: nothing to do
        plans.append(plan)

    # Checks across the whole batch
    olds = {p["path"].lower() for p in plans}
    olds |= {p["drawing"]["old"].lower() for p in plans if p.get("drawing")}
    seen = {}
    for p in plans:
        targets = [p["new_path"]] + ([p["drawing"]["new"]] if p.get("drawing") else [])
        for t in targets:
            low = t.lower()
            if low in seen and seen[low] != p["path"]:
                p["blockers"].append(f"{_fname(t)} would be the new name of two files ({_fname(seen[low])} too)")
            seen[low] = p["path"]
            if low in olds and low not in (p["path"].lower(), (p.get("drawing") or {}).get("old", "").lower()):
                p["blockers"].append(f"{_fname(t)} is the current name of another file in this list; "
                                     "swapping or chaining names is not possible in one go")
        # "there already is a file X" is fine when X itself is renamed away in this batch? No: keep it simple
    for p in plans:
        p["blockers"] = list(dict.fromkeys(p["blockers"]))
    item_blockers = [f"{_fname(p['path'])}: {b}" for p in plans for b in p["blockers"]]

    moved = {p["path"].lower() for p in plans} | {p["drawing"]["old"].lower() for p in plans if p.get("drawing")}
    updated = {}
    for p in plans:
        for par in p["parents"]:
            if par["role"] != "rename_with_unlinked":
                updated.setdefault(par["path"].lower(), par["path"])
    warnings = list(dict.fromkeys(w for p in plans for w in p["warnings"]))
    own_copies = sorted({o for p in plans for o in p.get("own_copies", [])}, key=str.lower)
    summary = {
        "items": [{"path": p["path"], "new_path": p["new_path"], "drawing": p.get("drawing"),
                   "blockers": p["blockers"], "updates": [par["path"] for par in p["parents"]
                                                          if par["role"] != "rename_with_unlinked"]} for p in plans],
        "blockers": blockers + item_blockers, "warnings": warnings,
        "counts": {"renamed": len(plans), "drawings": sum(1 for p in plans if p.get("drawing")),
                   "updated": len(updated), "updated_not_renamed": len([u for u in updated if u not in moved]),
                   "total_changed": len(moved | set(updated))},
        "updated": sorted(updated.values(), key=str.lower), "own_copies": own_copies, "include_own": bool(include_own)}
    if dry_run or summary["blockers"] or not plans:
        return {**summary, "executed": False}
    if not _rename_busy.acquire(blocking=False):
        return {**summary, "executed": False, "blockers": ["another rename is still running"]}
    try:
        return {**summary, "executed": True, "result": _execute_job({**_job_from_plans(plans), "include_own": bool(include_own)})}
    finally:
        _rename_busy.release()


# ---------------------------------------------------------------------------
# Pack & Go: copy an assembly (all or part of it) under new names; the copies refer to each other.
# The originals are never touched. On any failure the new copies (and folders made for them) are removed again.
# ---------------------------------------------------------------------------
_TOOLBOX_RE = re.compile(r"[\\/](toolbox|solidworks data|sldworks data)[\\/]", re.I)


_vault_cache = {}
_PDM_MARKERS = ("conisio", "epdm", "edmres", "solidworks pdm", "vaultname", "pdmworks")


_PDM_REG_KEYS = [r"SOFTWARE\SolidWorks\Applications\PDMWorks Enterprise\Databases",
                 r"SOFTWARE\WOW6432Node\SolidWorks\Applications\PDMWorks Enterprise\Databases",
                 r"SOFTWARE\Conisio\Databases", r"SOFTWARE\WOW6432Node\Conisio\Databases"]
_pdm_reg_cache = {"t": 0.0, "roots": {}}


def pdm_registry_vaults():
    """{vault view folder: vault name} as SOLIDWORKS PDM registers its local views on this PC: under
    ...\\PDMWorks Enterprise\\Databases\\<vault>, value ShellRoot (the view's folder) and DbName (the vault's name);
    for all users in HKEY_LOCAL_MACHINE, for one user in HKEY_CURRENT_USER. Read again after a few minutes."""
    if time.time() - _pdm_reg_cache["t"] < 300:
        return _pdm_reg_cache["roots"]
    roots = {}
    try:
        import winreg
    except ImportError:                                   # not Windows
        winreg = None
    if winreg:
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for key in _PDM_REG_KEYS:
                try:
                    with winreg.OpenKey(hive, key) as k:
                        for i in range(winreg.QueryInfoKey(k)[0]):
                            name = winreg.EnumKey(k, i)
                            loc, vault = "", name
                            try:
                                with winreg.OpenKey(k, name) as v:
                                    # ShellRoot: the folder of the local vault view (seen in SOLIDWORKS PDM);
                                    # Location: kept as a spare, in case a version uses that name
                                    for value in ("ShellRoot", "Location"):
                                        try:
                                            loc = str(winreg.QueryValueEx(v, value)[0] or "").strip()
                                        except OSError:
                                            continue
                                        if loc:
                                            break
                                    try:
                                        vault = str(winreg.QueryValueEx(v, "DbName")[0] or "").strip() or name
                                    except OSError:
                                        pass
                            except OSError:
                                continue
                            if loc:
                                roots[loc.rstrip("\\/")] = vault
                except OSError:
                    continue
    _pdm_reg_cache.update(t=time.time(), roots=roots)
    return roots


def _in_folder(path, folder):
    """Is path this folder or inside it (whole folder names: D:\\Vault is not D:\\Vault2)?"""
    p, f = path.replace("/", "\\").lower().rstrip("\\"), folder.replace("/", "\\").lower().rstrip("\\")
    return p == f or p.startswith(f + "\\")


def pdm_vault(path):
    """The root folder of the SOLIDWORKS PDM vault view this file is in, or None. First the vault views SOLIDWORKS
    PDM registered in the Windows registry; as a safety net, a desktop.ini that refers to PDM (Conisio, its old
    name, and its icons). Answer remembered per folder."""
    for root in pdm_registry_vaults():
        if _in_folder(path, root):
            return root
    folder = _folder_of(path) if Path(path).suffix else path
    seen = []
    while folder:
        low = folder.lower()
        if low in _vault_cache:
            found = _vault_cache[low]
            break
        seen.append(low)
        found = None
        try:
            ini = Path(folder) / "desktop.ini"
            if ini.is_file():
                raw = ini.read_bytes()[:8192]
                text = (raw.decode("utf-16", errors="ignore") if raw[:2] in (b"\xff\xfe", b"\xfe\xff")
                        else raw.decode("latin-1", errors="ignore")).lower()
                if any(m in text for m in _PDM_MARKERS):
                    found = folder
                    break
        except OSError:
            pass
        up = _folder_of(folder)
        folder = up if up and up.lower() != low else None
    for low in seen:
        _vault_cache[low] = found
    return found


PDM_MESSAGE = "is in a SOLIDWORKS PDM vault ({}): rename or move it in PDM; outside PDM the vault gets damaged"


def is_toolbox(path):
    return bool(_TOOLBOX_RE.search(path or ""))


_LIBRARY_RE = re.compile(r"[\\/](toolbox|solidworks data|sldworks data|design library)[\\/]|[\\/]programdata[\\/]solidworks[\\/]", re.I)


def is_library(path):
    """Library files of SOLIDWORKS (Toolbox, Design Library): not your own design work."""
    return bool(_LIBRARY_RE.search(path or ""))


def clean_dest(dest):
    """A usable, full folder path from what was typed (quotes from "Copy as path", %USERPROFILE%, ~)."""
    d = _expand((dest or "").strip().strip('"').strip("'").strip())
    if not d:
        raise ValueError("enter the folder to copy to")
    if not (os.path.isabs(d) or PureWindowsPath(d).is_absolute()):
        hint = f" Did you mean {d[:2]}\\{d[2:]}?" if len(d) >= 2 and d[1] == ":" else ""
        raise ValueError(f"'{dest}' is not a full path, for example D:\\Projects\\P26050.{hint}")
    return d.rstrip("\\/") or d


def _join(folder, *parts):
    sep = "/" if "/" in folder and "\\" not in folder else "\\"
    return sep.join([folder.rstrip("\\/")] + [p.strip("\\/") for p in parts if p])


def _pathmod(p):
    """Windows path rules for Windows paths, whatever system this runs on (the tests and the demo run on Linux)."""
    import ntpath
    return ntpath if ("\\" in p or re.match(r"^[A-Za-z]:", p)) else os.path


def _common_folder(folders):
    try:
        return _pathmod(folders[0]).commonpath(folders) if folders else None
    except ValueError:                      # different drives: no common folder
        return None


def _clean_prop_rules(rules):
    """Valid rules only; with what is wrong with the others."""
    ok, bad = [], []
    for r in rules or []:
        act, name = str(r.get("action") or ""), str(r.get("name") or "").strip()
        if act not in PROP_ACTIONS or not name:
            if name or r.get("value") or r.get("find"):
                bad.append(f"property rule '{name or '?'}': choose what to do and the name of the property")
            continue
        if act == "replace" and not str(r.get("find") or ""):
            bad.append(f"property rule '{name}': say which text to replace")
            continue
        # For which kinds of file: a list (["prt", "asm"]) or one kind ("drw", as saved before); empty or all = all
        raw = r.get("only") or []
        kinds = sorted({k for k in ([raw] if isinstance(raw, str) else raw) if k in ("prt", "asm", "drw")})
        ok.append({"action": act, "name": name, "value": str(r.get("value") or ""), "find": str(r.get("find") or ""),
                   "only": [] if len(kinds) == 3 else kinds})
    return ok, bad


def packgo_plan(root, items, target, keep_structure=True, with_drawings=True, overwrite=False,
                props_rules=None, props_scope="both"):
    """What Pack & Go would do. items: [{"path", "name", "copy"}]; the root assembly is always copied.
    Changes nothing."""
    blockers, warnings = [], []
    try:
        target = clean_dest(target)
    except ValueError as e:
        return {"blockers": [str(e)], "warnings": [], "copies": [], "update": [], "counts": {}, "target": target}
    if sw_problem():
        blockers.append(f"SOLIDWORKS files cannot be written: {sw_problem()}")
    by_path = {}
    for it in items:
        p = str(it.get("path") or "")
        if p:
            by_path[p.lower()] = {"path": p, "name": str(it.get("name") or "").strip(),
                                  "copy": bool(it.get("copy", True)), "folder": str(it.get("folder") or "").strip()}
    if root.lower() not in by_path:
        by_path[root.lower()] = {"path": root, "name": Path(root).stem, "copy": True, "folder": ""}
    chosen = [x for x in by_path.values() if x["copy"]]
    if not chosen:
        blockers.append("nothing is chosen to be copied")
    rules, bad_rules = _clean_prop_rules(props_rules)
    blockers += bad_rules
    kept = [x for x in by_path.values() if not x["copy"]]
    base = _common_folder([_folder_of(x["path"]) for x in chosen]) if keep_structure else None
    if keep_structure and base is None:
        warnings.append("The files are on different drives: they all go into the target folder itself.")
    copies, seen = [], {}
    for x in chosen:
        src, ext = x["path"], Path(x["path"]).suffix
        name = x["name"] or Path(src).stem
        if name.lower().endswith(ext.lower()):
            name = name[: -len(ext)]
        if not name or _BAD_NAME.search(name) or name.rstrip(". ") != name:
            blockers.append(f"{_fname(src)}: '{name}' is not a valid file name")
            continue
        if not os.path.isfile(src):
            blockers.append(f"{_fname(src)}: not found")
            continue
        if x["folder"]:                              # a folder of its own for this file
            try:
                new = _join(clean_dest(x["folder"]), name + ext)
            except ValueError as e:
                blockers.append(f"{_fname(src)}: {e}")
                continue
        else:
            rel = ""
            if base:
                folder = _folder_of(src)
                rel = _pathmod(folder).relpath(folder, base) if folder.lower() != base.lower() else ""
                rel = "" if rel == "." else rel
            new = _join(target, rel, name + ext)
        copies.append({"old": src, "new": new, "kind": _type_key(src)})
    # Drawings with the same name in the same folder go along, under the new name
    if with_drawings:
        cands = {c["old"].rsplit(".", 1)[0] + ".SLDDRW": c for c in copies if c["kind"] != "drw"}
        have = _existing_files(list(cands))
        for drw, c in cands.items():
            if drw.lower() in have:
                copies.append({"old": drw, "new": c["new"].rsplit(".", 1)[0] + ".SLDDRW", "kind": "drw", "with": c["old"]})
    conflicts, overwritten = {}, []
    for c in copies:
        low = c["new"].lower()
        if low in seen:
            blockers.append(f"{_fname(c['new'])} would be the copy of two files ({seen[low]} and {c['old']})")
            for o in (seen[low], c["old"]):
                conflicts[o.lower()] = {"level": "error", "reason": f"{_fname(c['new'])} would be the copy of two different "
                                        f"files in {_folder_of(c['new'])}: give one of them another name or folder"}
        seen[low] = c["old"]
        if os.path.exists(c["new"]):
            why = None
            if not overwrite:
                why = f"already exists in {_folder_of(c['new'])}: choose another name or folder, or tick Overwrite existing files"
            elif c["new"].lower() in {x["old"].lower() for x in copies}:
                why = "is itself one of the files being copied: it cannot be overwritten by a copy"
            elif pdm_vault(c["new"]):
                why = f"is in a SOLIDWORKS PDM vault ({pdm_vault(c['new'])}): replace it through PDM"
            else:
                why = blocking_reason(access_info(c["new"]))
            if why:
                blockers.append(f"{_fname(c['new'])} {why}" if why.startswith(("already", "is ")) else f"{_fname(c['new'])}: {why}")
                conflicts.setdefault(c["old"].lower(), {"level": "error", "reason": f"{_fname(c['new'])} {why}"})
            else:
                overwritten.append(c["new"])
                conflicts.setdefault(c["old"].lower(), {"level": "warn", "reason": f"{_fname(c['new'])} already exists in "
                                                        f"{_folder_of(c['new'])} and will be overwritten (backed up first)"})
        if len(c["new"]) >= 260:
            warnings.append(f"{_fname(c['new'])}: the new path is {len(c['new'])} characters; SOLIDWORKS has trouble above 259.")
    names = {}
    for c in copies:
        if c["kind"] != "drw":
            names.setdefault(_fname(c["new"]).lower(), []).append(c)
    for group in names.values():
        olds = {c["old"].lower() for c in group}
        if len(olds) > 1:
            for c in group:
                if c["old"].lower() not in conflicts:
                    others = ", ".join(_folder_of(o["new"]) for o in group if o is not c)
                    conflicts[c["old"].lower()] = {"level": "warn", "reason": f"{_fname(c['new'])} also exists as a copy in "
                                                   f"{others}: in one assembly SOLIDWORKS loads only one of them"}
    # Which copies must be changed: those that refer to another copied file (by name, as SOLIDWORKS does)
    copied_names = {_fname(c["old"]).lower() for c in copies if c["kind"] != "drw"}
    olds = [c["old"].lower() for c in copies]
    refs_of = {}
    for i in range(0, len(olds), 500):
        chunk = olds[i:i + 500]
        with _db_lock:
            for parent, name in conn().execute(
                    f"SELECT DISTINCT parent, child_name FROM refs WHERE virtual=0 AND parent IN ({','.join('?' * len(chunk))})", chunk):
                refs_of.setdefault(parent, set()).add(name)
    # Copies that refer to another copy, AND copies that refer to a kept file: in the copy (another folder) a kept
    # file must be named by its real path, or the copy cannot find it (the original may name it by an old path
    # that only works from its own folder)
    kept_names = {_fname(x["path"]).lower() for x in kept}
    update = [c["new"] for c in copies if refs_of.get(c["old"].lower(), set()) & (copied_names | kept_names)]
    children = {c["new"].lower(): sorted(refs_of.get(c["old"].lower(), set())) for c in copies if c["new"] in update}
    vault = pdm_vault(target) if target else None
    if vault:
        warnings.append(f"The target folder is in a SOLIDWORKS PDM vault ({vault}): the copies are new local files; "
                        "check them in with PDM afterwards.")
    if overwritten:
        users = parents_for_paths(overwritten)
        n_users = len(set().union(*(u for u, _o in users.values()))) if users else 0
        warnings.append(f"{len(overwritten)} existing file(s) are overwritten; they are backed up first and put back if "
                        "anything fails." + (f" {n_users} assemblies and drawings use them and get the copy from now on; "
                                             "SOLIDWORKS may say the internal ID differs, as a copy keeps the ID of its "
                                             "original." if n_users else ""))
    toolbox = [x["path"] for x in chosen if is_toolbox(x["path"])]
    if toolbox:
        warnings.append(f"{len(toolbox)} Toolbox part(s) are copied; usually they are kept and referred to.")
    return {"root": root, "target": target, "copies": copies, "update": update, "children": children,
            "conflicts": conflicts, "overwrite": overwritten, "props_rules": rules,
            "props_scope": props_scope if props_scope in ("both", "file", "configs") else "both",
            "blockers": list(dict.fromkeys(blockers)),
            "warnings": warnings, "kept": [x["path"] for x in kept],
            "counts": {"copied": sum(1 for c in copies if c["kind"] != "drw"), "drawings": sum(1 for c in copies if c["kind"] == "drw"),
                       "kept": len(kept), "updated": len(update), "overwritten": len(overwritten),
                       "prop_rules": len(rules)}}


def _pick_copy(ref, cands, parent_folder=None, exists=os.path.exists):
    """Which copy is meant by this reference? Decided the way SOLIDWORKS finds the file when it opens the assembly:
    1. the saved path, when that file still exists;
    2. otherwise a file with that name in the assembly's own folder (SOLIDWORKS looks there first);
    3. otherwise the original that matches the reference over the most folders, counted from the end (the reference
       can hold an older location, "D:\\Old\\Hettich\\9238051\\x.sldprt" for "D:\\New\\Hettich\\9238051\\x.sldprt").
    One file of that name: that one. Not certain: None."""
    if len(cands) <= 1:
        return cands[0] if cands else None
    low = ref.lower()
    for c in cands:
        if c["old"].lower() == low and exists(c["old"]):
            return c
    if parent_folder:
        here = [c for c in cands if (_folder_of(c["old"]) or "").lower() == parent_folder.lower()]
        if len(here) == 1:
            return here[0]
    parts = [x for x in re.split(r"[\\/]+", low) if x]

    def score(c):
        theirs = [x for x in re.split(r"[\\/]+", c["old"].lower()) if x]
        n = 0
        while n < min(len(parts), len(theirs)) and parts[-1 - n] == theirs[-1 - n]:
            n += 1
        return n
    scored = sorted(((score(c), c) for c in cands), key=lambda x: -x[0])
    if scored[0][0] > scored[1][0]:
        return scored[0][1]
    return None


PROP_ACTIONS = ("clear", "delete", "set", "replace")
DM_TEXT = 30                 # swDmCustomInfoText


def _raw_prop(obj, name):
    """The value of a custom property as stored (an expression stays an expression), not its evaluated value."""
    try:
        res = obj.GetCustomProperty(name)
    except Exception:  # noqa: BLE001
        return ""
    return next((x for x in (res if isinstance(res, tuple) else (res,)) if isinstance(x, str)), "")


def _prop_targets(doc, path, scope):
    """(where, object) pairs: the file itself (Custom tab) and/or each configuration (Configuration Specific)."""
    out = []
    if scope in ("both", "file"):
        out.append(("file", doc))
    if scope in ("both", "configs") and not path.lower().endswith(".slddrw"):
        try:
            cm = _keep(_best(doc.ConfigurationManager, "ISwDMConfigurationMgr"))
            for n in _string_list(_keep(cm.GetConfigurationNames())) or []:
                out.append((f"configuration {n}", _keep(_best(cm.GetConfigurationByName(n), "ISwDMConfiguration"))))
        except Exception:  # noqa: BLE001 — no configurations readable: only the file itself
            pass
    return out


def _props_of_file(path):
    """All custom properties of one file: {"file": {name: value}, "configs": {config: {name: value}}} (stored values,
    expressions stay expressions). Runs in a separate process (see read_props_isolated)."""
    doc = _keep(_open_sw(path, readonly=True))
    try:
        out = {"file": {}, "configs": {}}
        for where, obj in _prop_targets(doc, path, "both"):
            vals = {str(n): _raw_prop(obj, str(n)) for n in (_string_list(_keep(obj.GetCustomPropertyNames())) or [])}
            if where == "file":
                out["file"] = vals
            else:
                out["configs"][where[len("configuration "):]] = vals
        return out
    finally:
        try:
            doc.CloseDoc()
        except Exception:  # noqa: BLE001
            pass


def _read_props_main(job_file):
    """The separate process: read the properties of the files listed in job_file, print them as JSON."""
    _com_init()
    paths = json.loads(Path(job_file).read_text(encoding="utf-8"))
    out = {}
    for p in paths:
        try:
            out[p] = _props_of_file(p)
        except Exception as e:  # noqa: BLE001
            out[p] = {"error": human_error(e)}
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()
    os._exit(0)                                     # no comtypes clean-up (see _keep)


_props_read_cache = {}
_props_read_lock = threading.Lock()


def read_props_isolated(paths, timeout=300):
    """Properties of many files, read in ONE separate process; what was read before is kept per file version."""
    with _props_read_lock:                         # one reading process at a time; the second finds the first's work
        return _read_props_locked(paths, timeout)


def _read_props_locked(paths, timeout):
    todo, out = [], {}
    for p in paths:
        key = (p.lower(), _mtime(p))
        if key in _props_read_cache:
            out[p] = _props_read_cache[key]
        else:
            todo.append(p)
    if todo:
        jobs = DATA_DIR / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        job_file = jobs / f"props_{int(time.time() * 1000)}.json"
        job_file.write_text(json.dumps(todo), encoding="utf-8")
        proc = subprocess.Popen([PY_CHILD, str(Path(__file__).resolve()), "--read-props", str(job_file)], cwd=str(HERE),
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            raw, _err = proc.communicate(timeout=timeout)
            got = json.loads(raw or "{}")
        except (subprocess.TimeoutExpired, ValueError):
            _kill_tree(proc)
            got = {}
        finally:
            try:
                job_file.unlink()
            except OSError:
                pass
        for p in todo:
            res = got.get(p, {"error": "not read (the reading process stopped)"})
            out[p] = res
            if "error" not in res:
                _props_read_cache[(p.lower(), _mtime(p))] = res
    return out


def props_files(paths, with_drawings=True):
    """Properties of these files AND the drawings with the same name next to them, per file. The page reads all
    of them once and shows what belongs to the files that are copied at that moment (no new read on every click)."""
    paths = list(dict.fromkeys(paths))
    if with_drawings:
        cands = [p.rsplit(".", 1)[0] + ".SLDDRW" for p in paths if not p.lower().endswith(".slddrw")]
        there = _existing_files(cands)
        paths += [c for c in cands if c.lower() in there and c not in paths]
    return {"files": read_props_isolated(paths)}


def props_overview(paths):
    """Which custom properties occur in these files: per property, in how many files, where, and a few values."""
    from collections import Counter
    read = read_props_isolated(paths)
    by = {}
    failed = []
    for p, res in read.items():
        if "error" in res:
            failed.append({"path": p, "error": res["error"]})
            continue
        seen = {}
        for name, value in res.get("file", {}).items():
            seen.setdefault(name.lower(), [name, set(), []])[1].add("file")
            seen[name.lower()][2].append(value)
        for cfg, vals in res.get("configs", {}).items():
            for name, value in vals.items():
                seen.setdefault(name.lower(), [name, set(), []])[1].add("configs")
                seen[name.lower()][2].append(value)
        for low, (name, where, values) in seen.items():
            e = by.setdefault(low, {"name": name, "files": 0, "file": 0, "configs": 0, "values": Counter()})
            e["files"] += 1
            e["file"] += "file" in where
            e["configs"] += "configs" in where
            e["values"].update(v for v in values if v)
    props = sorted(({**{k: v for k, v in e.items() if k != "values"},
                     "values": [v for v, _n in e["values"].most_common(4)], "distinct": len(e["values"])}
                    for e in by.values()), key=lambda x: (-x["files"], x["name"].lower()))
    return {"files": len(paths), "properties": props, "failed": failed}


def _apply_prop_rules(path, old, rules, scope):
    """Clear, delete, set or replace text in custom properties of ONE copy; saved, then read back to check."""
    import datetime
    values = {"{name}": Path(path).stem, "{oldname}": Path(old).stem, "{date}": datetime.date.today().isoformat()}
    expand = lambda v: functools.reduce(lambda acc, kv: acc.replace(*kv), values.items(), v or "")   # noqa: E731
    doc = _keep(_open_sw(path, readonly=False))
    expect = []
    try:
        kind = _type_key(path)                         # a rule can be for one kind of file only
        for where, obj in _prop_targets(doc, path, scope):
            have = {str(x).lower(): str(x) for x in (_string_list(_keep(obj.GetCustomPropertyNames())) or [])}
            for r in rules:
                if r.get("only") and kind not in r["only"]:
                    continue
                real = have.get(r["name"].lower())
                act = r["action"]
                if act == "delete" and real:
                    _keep(obj.DeleteCustomProperty(real))
                    expect.append((where, real, None))
                elif act == "clear" and real:
                    _keep(obj.SetCustomProperty(real, ""))
                    expect.append((where, real, ""))
                elif act == "set":
                    value = expand(r.get("value"))
                    if real:
                        _keep(obj.SetCustomProperty(real, value))
                    else:
                        _keep(obj.AddCustomProperty(r["name"], DM_TEXT, value))
                    expect.append((where, real or r["name"], value))
                elif act == "replace" and real and r.get("find"):
                    cur = _raw_prop(obj, real)
                    new = cur.replace(r["find"], expand(r.get("value")))
                    if new != cur:
                        _keep(obj.SetCustomProperty(real, new))
                        expect.append((where, real, new))
        _rlog(f"  properties of {path}: " + ("; ".join(f"{w}: {n} = {v!r}" for w, n, v in expect) or "nothing to change"))
        if expect:
            save = _keep(doc.Save())
            code = next((n for n in _split(save)[1] if n), 0) if save is not None else 0
            if code:
                raise RuntimeError(f"{_fname(path)}: saving the properties failed (code {code})")
    finally:
        try:
            doc.CloseDoc()
        except Exception:  # noqa: BLE001
            pass
    if not expect:
        return 0
    check = _keep(_open_sw(path, readonly=True))          # read back: is it really so?
    try:
        objs = dict(_prop_targets(check, path, scope))
        for where, name, value in expect:
            obj = objs.get(where)
            names = {str(x).lower() for x in (_string_list(_keep(obj.GetCustomPropertyNames())) or [])} if obj is not None else set()
            if value is None and name.lower() in names:
                raise RuntimeError(f"check failed: {_fname(path)} ({where}) still has the property {name}")
            if value is not None and (name.lower() not in names or _raw_prop(obj, name) != value):
                raise RuntimeError(f"check failed: {_fname(path)} ({where}): {name} is not {value!r}")
    finally:
        try:
            check.CloseDoc()
        except Exception:  # noqa: BLE001
            pass
    return len(expect)


def _packgo_job(job):
    """The work (in the separate process): copy, point the copies to each other, check. Journal after every step."""
    jpath = job["journal"]
    journal = {"kind": "packgo", "created": [], "dirs": [], "step": "start"}
    _journal_write(jpath, journal)
    _rlog("pack and go: " + "; ".join(f"{c['old']} -> {c['new']}" for c in job["copies"]))
    replace = {p.lower() for p in job.get("overwrite", [])}
    journal["replaced"] = []
    bdir = None
    for c in job["copies"]:                               # 1. copy (over an existing file only when that was chosen)
        if os.path.exists(c["new"]):
            if c["new"].lower() not in replace:
                raise RuntimeError(f"{_fname(c['new'])} already exists")
            if bdir is None:
                import datetime
                now = datetime.datetime.now()
                bdir = backup_root() / now.strftime("%Y%m%d") / (now.strftime("%H%M%S") + "_packgo")
                bdir.mkdir(parents=True, exist_ok=True)
            saved = bdir / f"{len(journal['replaced']):03d}_{_fname(c['new'])}"
            shutil.move(c["new"], str(saved))              # out of the way, kept as the backup
            journal["replaced"].append([c["new"], str(saved)])
            _journal_write(jpath, journal)
            _rlog(f"  overwritten (backed up as {saved}): {c['new']}")
        folder = _folder_of(c["new"])
        missing = []
        while folder and not os.path.isdir(folder):
            missing.append(folder)
            folder = _folder_of(folder)
        for d in reversed(missing):
            os.mkdir(d)
            journal["dirs"].append(d)
        shutil.copy2(c["old"], c["new"])
        journal["created"].append(c["new"])
        _journal_write(jpath, journal)
    journal["step"] = "copied"
    # Copies by file name; several files can have the same name in different folders (a supplier's set per product)
    by_name = {}
    for c in job["copies"]:
        if c["kind"] != "drw":
            by_name.setdefault(_fname(c["old"]).lower(), []).append(c)
    old_of = {c["new"].lower(): c["old"] for c in job["copies"]}
    kept_by_name = {}                                     # kept files: referred to by their real path
    for k in job.get("kept", []):
        kept_by_name.setdefault(_fname(k).lower(), []).append({"old": k, "new": k})
    results = []
    for path in job["update"]:                            # 2. in each copy: references to copied files -> the copies
        doc = _keep(_open_sw(path, readonly=False))
        changes = []
        allowed = set(job.get("children", {}).get(path.lower(), []))   # the files directly in it, per the index
        try:
            refs = _string_list(_keep(doc.GetAllExternalReferences4(_keep(_sw_app().GetSearchOptionObject())))) or []
            _rlog(f"  {path}: external references: " + "; ".join(refs))
            for ref in refs:
                if allowed and _fname(ref).lower() not in allowed:
                    continue                        # not directly in this file: not ours to replace here
                # Copied AND kept files with this name compete together, the way SOLIDWORKS would find one (saved
                # path, own folder, closest folders). A kept file that wins keeps its real path: a reference to a kept
                # file is never turned into the copy of another file that happens to have the same name.
                cands = by_name.get(_fname(ref).lower(), []) + kept_by_name.get(_fname(ref).lower(), [])
                pick = _pick_copy(ref, cands, _folder_of(old_of.get(path.lower(), "")))
                if cands and pick is None:
                    raise RuntimeError(f"{_fname(path)}: refers to {ref}; {len(cands)} copied files have that name and "
                                       f"it is not certain which one is meant")
                new = pick["new"] if pick else None
                if new and ref.lower() != new.lower():
                    ret = _keep(doc.ReplaceReference(ref, new))
                    _rlog(f"    ReplaceReference({ref} -> {new}) returned {ret!r}")
                    changes.append([ref, new])
            if changes:
                save = _keep(doc.Save())
                code = next((n for n in _split(save)[1] if n), 0) if save is not None else 0
                _rlog(f"    Save returned {save!r}")
                if code:
                    raise RuntimeError(f"{_fname(path)}: saving failed (code {code})")
        finally:
            try:
                doc.CloseDoc()
            except Exception:  # noqa: BLE001
                pass
        results.append({"path": path, "changes": changes, "from": old_of.get(path.lower())})
        journal["updated_n"] = journal.get("updated_n", 0) + 1     # for the progress bar on the page
        _journal_write(jpath, journal)
    rules = job.get("props_rules") or []
    props_changed = 0
    if rules:                                             # properties of the copies: cleared, deleted, set, replaced
        for c in job["copies"]:
            props_changed += _apply_prop_rules(c["new"], c["old"], rules, job.get("props_scope", "both"))
            journal["props_n"] = journal.get("props_n", 0) + 1
            _journal_write(jpath, journal)
    journal["step"] = "updated"
    _journal_write(jpath, journal)
    for r in results:                                     # 3. check: each copy points to the copies, not the originals
        if not r["changes"]:
            continue
        raw = _refs_of(r["path"]) if r["path"].lower().endswith(".sldprt") else _external_refs(r["path"])
        refs = {x.lower() for x in raw}
        for old, new in r["changes"]:
            if new.lower() not in refs:
                raise RuntimeError(f"check failed: the copy {_fname(r['path'])} does not point to {new} "
                                   f"(details in {DATA_DIR / 'rename_debug.log'})")
            if old.lower() in refs:
                raise RuntimeError(f"check failed: the copy {_fname(r['path'])} still points to the original {old}")
    journal["step"] = "done"
    _journal_write(jpath, journal)
    return {"ok": True, "results": results, "created": journal["created"], "props_changed": props_changed,
            "replaced": journal.get("replaced", []), "dirs": journal.get("dirs", [])}


def _packgo_rollback(journal):
    """Remove what Pack & Go made: the new files, then the folders it created (only when empty)."""
    problems = []
    for f in reversed(journal.get("created", [])):
        try:
            if os.path.exists(f):
                _force_unlink(f)
        except OSError as e:
            problems.append(f"removing {f} failed: {e}")
    for d in reversed(journal.get("dirs", [])):
        try:
            os.rmdir(d)
        except OSError:
            pass
    for original, saved in reversed(journal.get("replaced", [])):   # files that were overwritten: back in place
        try:
            if os.path.exists(original):
                _force_unlink(original)
            shutil.move(saved, original)
        except OSError as e:
            problems.append(f"putting back {original} failed: {e} (the backup is {saved})")
    return problems


def packgo(root, items, target, keep_structure=True, with_drawings=True, dry_run=True, overwrite=False,
           props_rules=None, props_scope="both"):
    plan = packgo_plan(root, items, target, keep_structure, with_drawings, overwrite, props_rules, props_scope)
    if dry_run or plan["blockers"] or not plan["copies"]:
        return {**plan, "executed": False}
    if not _rename_busy.acquire(blocking=False):
        return {**plan, "executed": False, "blockers": ["a rename or Pack & Go is still running"]}
    try:
        job = {"kind": "packgo", "copies": plan["copies"], "update": plan["update"], "children": plan["children"],
               "overwrite": plan["overwrite"], "props_rules": plan["props_rules"], "props_scope": plan["props_scope"],
               "kept": plan["kept"],
               "parents": plan["update"], "renames": []}
        return {**plan, "executed": True, "result": _execute_job(job)}
    finally:
        _rename_busy.release()


def _run_rename_job(job_file):
    """Carry out one job file; on any error put everything back. Writes and returns the result."""
    job = json.loads(Path(job_file).read_text(encoding="utf-8"))
    try:
        kind = job.get("kind")
        result = _packgo_job(job) if kind == "packgo" else _fixpaths_job(job) if kind == "fixpaths" else _rename_job(job)
    except Exception as e:  # noqa: BLE001 — put everything back and report
        try:
            journal = json.loads(Path(job["journal"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            journal = {}
        problems = (_packgo_rollback(journal) if job.get("kind") == "packgo" else
                    _fixpaths_rollback(journal) if job.get("kind") == "fixpaths" else _rollback(journal))
        result = {"ok": False, "error": human_error(e), "rolled_back": not problems,
                  "rollback_problems": problems, "backup_dir": journal.get("backup_dir")}
    Path(job["result"]).write_text(json.dumps(result), encoding="utf-8")
    return result


def _rename_worker_main(job_file):
    """Entry of the rename process (python SWhereUsed.py --rename-job job.json)."""
    if HAVE_PYWIN32:
        pythoncom.CoInitialize()
    _run_rename_job(job_file)
    sys.stdout.flush()
    os._exit(0)           # stop right away: no comtypes clean-up (that is where the crash risk is)


def cleanup_backups():
    """Delete backup day folders (YYYYMMDD) older than keep_backups_days."""
    import datetime
    root = backup_root()
    if BACKUP_DAYS <= 0 or not root.is_dir():
        return 0
    limit = datetime.date.today() - datetime.timedelta(days=BACKUP_DAYS)
    removed = 0
    for d in root.iterdir():
        if d.is_dir() and re.fullmatch(r"\d{8}", d.name):
            try:
                if datetime.datetime.strptime(d.name, "%Y%m%d").date() < limit:
                    shutil.rmtree(d)
                    removed += 1
            except (ValueError, OSError):
                continue
    return removed


# ---------------------------------------------------------------------------
# Start with Windows: a small file in the user's Startup folder (no administrator rights needed)
# ---------------------------------------------------------------------------
def _startup_file():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "SWhereUsed.cmd"


def _migrate_autostart():
    """SWhereUsed used to be called WhereUsed: when Windows still starts it under the old name (a file pointing to
    files that no longer exist), set it up again under the new name, the same way (in a window or in the background)."""
    f = _startup_file()
    if f is None:
        return
    old_cmd, old_lnk = f.with_name("WhereUsed.cmd"), f.with_name("WhereUsed.lnk")
    if not (old_cmd.is_file() or old_lnk.is_file()):
        return
    mode = "background" if old_lnk.is_file() else "window"
    for old in (old_cmd, old_lnk):
        try:
            old.unlink()
        except OSError:
            pass
    try:
        set_autostart(True, mode)
        print(f"  Starting when you sign in: moved to the new name ({mode})")
    except Exception as e:  # noqa: BLE001
        print(f"  Starting when you sign in could not be set up under the new name: {e}. Switch it on again in Setup.")


def _startup_link():
    f = _startup_file()
    return f.with_suffix(".lnk") if f else None


def autostart_state():
    f, lnk = _startup_file(), _startup_link()
    bg = bool(lnk and lnk.is_file())
    return {"supported": os.name == "nt" and f is not None, "on": bool(f and f.is_file()) or bg,
            "mode": "background" if bg else "window", "file": str(lnk if bg else f) if f else None, "background_now": BACKGROUND}


def set_autostart(on, mode="window"):
    """Windows runs this when you sign in. window: Start SWhereUsed.bat, minimised. background: a shortcut straight to
    pythonw.exe (no window at all, not even for a moment), with the icon by the clock. No browser either way."""
    f, lnk = _startup_file(), _startup_link()
    if f is None:
        raise RuntimeError("the Startup folder was not found (no APPDATA)")
    for old in (f, lnk):
        if old.is_file():
            old.unlink()
    if not on:
        return autostart_state()
    if mode == "background":
        pyw = Path(PY_CHILD).with_name("pythonw.exe")
        if not pyw.is_file():
            raise RuntimeError(f"{pyw} was not found")
        import pythoncom
        from win32com.shell import shell
        link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
        link.SetPath(str(pyw))
        link.SetArguments(f'"{Path(__file__).resolve()}" --background --no-browser')
        link.SetWorkingDirectory(str(HERE))
        link.SetDescription("SWhereUsed for SOLIDWORKS, in the background (icon by the clock)")
        if (HERE / "SWhereUsed.ico").is_file():
            link.SetIconLocation(str(HERE / "SWhereUsed.ico"), 0)
        lnk.parent.mkdir(parents=True, exist_ok=True)
        link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(lnk), 0)
        return autostart_state()
    bat = HERE / "Start SWhereUsed.bat"
    if not bat.is_file():
        raise RuntimeError(f"{bat.name} is not next to SWhereUsed.py")
    f.parent.mkdir(parents=True, exist_ok=True)
    # chcp 65001: cmd reads the rest as UTF-8, so a folder name with é or ü still works
    f.write_bytes(("@echo off\r\n"
                   "chcp 65001 >nul\r\n"
                   "rem Starts SWhereUsed minimised when you sign in. Made on the Setup page of SWhereUsed;\r\n"
                   "rem switch it off there, or delete this file.\r\n"
                   f'start "SWhereUsed" /min cmd /c ""{bat}" --no-browser"\r\n').encode("utf-8"))
    return autostart_state()


# ---------------------------------------------------------------------------
# Thumbnails: the preview picture SOLIDWORKS stores in each file, read when it is shown
# ---------------------------------------------------------------------------
PNG_SIG = b"\x89PNG\r\n\x1a\n"
_thumb = {"proc": None, "lock": threading.Lock(), "out": None}


def _thumb_dir():
    return DATA_DIR / "thumbs"


def _thumb_files(path, mtime):
    import hashlib
    stem = hashlib.sha1(path.lower().encode("utf-8")).hexdigest()[:24]
    f = _thumb_dir() / f"{stem}-{int(mtime)}.png"
    return f, f.with_suffix(".none"), stem


def _to_bytes(v):
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v)
    try:
        return bytes((int(x) & 0xFF) for x in v)
    except (TypeError, ValueError):
        return None


def _extract_preview(doc, cfg):
    """The preview picture (PNG) of an open document: of the configuration, otherwise of the document."""
    for obj in (cfg, doc):
        fn = getattr(obj, "GetPreviewPNGBitmapBytes", None) if obj is not None else None
        if fn is None:
            continue
        try:
            raw = fn()
        except Exception:  # noqa: BLE001
            continue
        for r in [raw] + (list(raw) if isinstance(raw, tuple) else []):
            if isinstance(r, int):
                continue
            b = _to_bytes(r)
            if b and b.startswith(PNG_SIG):
                return b
    return None


def _kill_tree(proc):
    """Stop a process AND its children. On Windows python.exe in a venv is a launcher that starts the real Python
    as a child: killing only the launcher leaves that child running (a second index process, "database is locked")."""
    if proc is None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _ask_thumb_helper(path, target, none_marker, timeout=20):
    """Ask the helper process (started when first needed) to write the picture. Never waits longer than timeout:
    a file the Document Manager hangs on just gets no picture, and the helper is started again."""
    import queue
    with _thumb["lock"]:
        proc = _thumb["proc"]
        if proc is None or proc.poll() is not None:
            proc = subprocess.Popen([PY_CHILD, str(Path(__file__).resolve()), "--thumb-worker"], cwd=str(HERE),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, encoding="utf-8", bufsize=1,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = queue.Queue()
            threading.Thread(target=lambda: [out.put(line) for line in proc.stdout], daemon=True).start()
            _thumb.update(proc=proc, out=out)
        try:
            proc.stdin.write(json.dumps({"path": path, "target": str(target), "none": str(none_marker)}) + "\n")
            proc.stdin.flush()
            _thumb["out"].get(timeout=timeout)
        except Exception:  # noqa: BLE001 — stuck or gone: stop it; the next request starts a fresh one
            _kill_tree(proc)
            _thumb["proc"] = None
            try:
                none_marker.parent.mkdir(parents=True, exist_ok=True)
                none_marker.write_bytes(b"")
            except OSError:
                pass


def thumbnail(path):
    """PNG bytes of the preview picture of a SOLIDWORKS file, or None. Kept on disk per file version."""
    if THUMBNAILS != "yes" or Path(path).suffix.lower() not in SW_TYPES or sw_problem():
        return None
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    f, none, stem = _thumb_files(path, mtime)
    if f.exists():
        try:
            if time.time() - f.stat().st_mtime > 86400:   # looked at again: mark it (at most once a day)
                os.utime(f)
        except OSError:
            pass
        return f.read_bytes()
    if none.exists():
        return None
    _thumb_dir().mkdir(parents=True, exist_ok=True)
    for old in _thumb_dir().glob(stem + "-*"):          # an older version of this file: no longer needed
        try:
            old.unlink()
        except OSError:
            pass
    _ask_thumb_helper(path, f, none)
    _thumb_made["n"] += 1
    if _thumb_made["n"] % 50 == 0 or time.time() - _thumb_made["pruned"] > 600:
        threading.Thread(target=prune_thumbnails, daemon=True, name="thumbs").start()
    return f.read_bytes() if f.exists() else None


_thumb_made = {"n": 0, "pruned": 0.0}


def prune_thumbnails(limit_mb=None):
    """Keep the preview pictures within THUMB_CACHE_MB: when above it, remove the ones not looked at for the longest
    until 80% of it is left. Returns (MB before, MB after)."""
    _thumb_made["pruned"] = time.time()
    limit = (THUMB_CACHE_MB if limit_mb is None else limit_mb) * 1024 * 1024
    files = []
    try:
        for e in os.scandir(_thumb_dir()):
            try:
                st = e.stat()
                files.append((st.st_mtime, st.st_size, e.path))
            except OSError:
                continue
    except OSError:
        return 0, 0
    total = sum(f[1] for f in files)
    before = total
    if limit > 0 and total > limit:
        for _m, size, path in sorted(files):
            if total <= limit * 0.8:
                break
            try:
                os.remove(path)
                total -= size
            except OSError:
                continue
    return round(before / 1048576, 1), round(total / 1048576, 1)


def _thumb_worker_main():
    """The helper process: reads {"path", "target", "none"} lines, writes the picture (or the 'none' marker)."""
    _com_init()
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        png = None
        try:
            doc = _open_sw_retry(req["path"])
            try:
                cfg = None
                try:
                    cm = _best(doc.ConfigurationManager, "ISwDMConfigurationMgr")
                    active = str(cm.GetActiveConfigurationName() or "")
                    cfg = _best(cm.GetConfigurationByName(active), "ISwDMConfiguration") if active else None
                except Exception:  # noqa: BLE001 — drawings have no configurations
                    cfg = None
                png = _extract_preview(doc, cfg)
            finally:
                try:
                    doc.CloseDoc()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            png = None
        try:
            if png:
                tmp = req["target"] + ".tmp"
                Path(tmp).write_bytes(png)
                os.replace(tmp, req["target"])
            else:
                Path(req["none"]).write_bytes(b"")
        except OSError:
            pass
        print("done", flush=True)


# ---------------------------------------------------------------------------
# Diagnostics, settings in the app, and the update check
# ---------------------------------------------------------------------------
_SECRET_SETTINGS = {"EVERYTHING_PASS"}


def _tail(path, lines):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-lines:]
    except OSError:
        return []


_update_error = {"text": None}
RESTART_CODE = 3                  # Start SWhereUsed.bat starts SWhereUsed again when it ends with this code


def update_status(force=False):
    """For Setup: what the update check knows (and checks now when asked)."""
    if not UPDATE_REPO:
        return {"enabled": False, "current": APP_VERSION, "why": "no GitHub repository is set in SWhereUsed.py (UPDATE_REPO)"}
    if CHECK_UPDATES != "yes" and not force:
        return {"enabled": False, "current": APP_VERSION, "why": "switched off (check_updates = no)"}
    _update_error["text"] = None
    if force:
        check_for_update(force=True)
    try:
        known = json.loads(_meta_get("update_info") or "{}")
    except ValueError:
        known = {}
    newer = bool(known.get("latest")) and _version_tuple(known["latest"]) > _version_tuple(APP_VERSION)
    return {"enabled": True, "current": APP_VERSION, "repo": UPDATE_REPO, "checked": known.get("checked"),
            "latest": known.get("latest"), "available": newer, "url": known.get("url"),
            "notes": known.get("notes") if newer else None, "error": _update_error["text"]}


def install_update():
    """Download the newest release, check it, back up the program files, put the new ones in place, and restart.
    Never touches the data folder (settings, key, index, history). Not while an index run, a rename or a Pack & Go
    is busy. Start SWhereUsed.bat itself is written as 'Start SWhereUsed.bat.new': cmd reads a running .bat from disk,
    so it is swapped by the .bat itself when it restarts."""
    import io
    import zipfile
    st = update_status()
    if not st.get("available"):
        raise ValueError("there is no newer version")
    if _state.get("running"):
        raise ValueError("the index is being updated; try again when it is done")
    if _rename_busy.locked():
        raise ValueError("a rename or Pack & Go is busy; try again when it is done")
    known = json.loads(_meta_get("update_info") or "{}")
    req = urllib.request.Request(known["asset"], headers={"User-Agent": APP_NAME, "Accept": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read(100 * 1024 * 1024 + 1)
    if len(data) > 100 * 1024 * 1024:
        raise ValueError("the download is larger than expected; not installed")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("the download is not a zip file; not installed")
    main = [n for n in z.namelist() if n.replace("\\", "/").split("/")[-1] == "SWhereUsed.py"]
    if not main:
        raise ValueError("SWhereUsed.py is not in the download; not installed")
    prefix = min(main, key=len)[: -len("SWhereUsed.py")]
    if prefix + "SWhereUsed.html" not in z.namelist():
        raise ValueError("SWhereUsed.html is not in the download; not installed")
    m = re.search(r'APP_VERSION = "([^"]+)"', z.read(prefix + "SWhereUsed.py").decode("utf-8", "replace"))
    if not m or _version_tuple(m.group(1)) <= _version_tuple(APP_VERSION):
        raise ValueError("the download is not a newer SWhereUsed; not installed")
    new_version = m.group(1)
    here = HERE.resolve()
    # First check EVERY file in the download; only then write: never half an update
    todo = []
    for name in z.namelist():
        if not name.startswith(prefix) or name.endswith("/"):
            continue
        target = (here / name[len(prefix):]).resolve()
        if here not in target.parents:                # a path that would land outside the program folder
            raise ValueError(f"unsafe path in the download ({name}); not installed")
        todo.append((name, target))
    backup = DATA_DIR / "app_backups" / f"{APP_VERSION}_{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copytree(here, backup, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    written = []
    for name, target in todo:
        rel = str(target.relative_to(here))
        if target.name == "Start SWhereUsed.bat":
            if target.exists() and target.read_bytes() == z.read(name):
                continue
            target = target.with_name("Start SWhereUsed.bat.new")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(z.read(name))
        os.replace(tmp, target)
        written.append(rel)
    _meta_set("update_done", json.dumps({"from": APP_VERSION, "to": new_version, "time": time.time(), "backup": str(backup)}))
    _log("update", APP_VERSION, new_version, f"{len(written)} files; old version kept in {backup}")
    threading.Timer(1.5, _restart_app).start()        # after this answer has reached the page
    return {"ok": True, "version": new_version, "files": len(written), "backup": str(backup)}


def shutdown_app():
    """Stop SWhereUsed: its helpers, an index run or a job that is busy, the icon by the clock, and itself."""
    _log("stop", "", "", "stopped from the icon or the Setup page")
    for helper in (_thumb, _ext):
        _kill_tree(helper.get("proc"))
    for proc in list(_children):
        if proc.poll() is None:
            _kill_tree(proc)
    _tray_remove()
    os._exit(0)


_tray = {"hwnd": None}


def _tray_remove():
    try:
        if _tray["hwnd"]:
            import win32gui
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (_tray["hwnd"], 0))
            _tray["hwnd"] = None
    except Exception:  # noqa: BLE001
        pass


def _tray_icon(open_page):
    """The icon by the clock (background mode): double-click opens SWhereUsed; right-click: Open, Stop."""
    try:
        import win32api
        import win32con
        import win32gui
    except ImportError:
        return
    msg_id = win32con.WM_USER + 20

    def notify(hwnd, msg, wparam, lparam):
        if lparam == win32con.WM_LBUTTONDBLCLK:
            open_page()
        elif lparam == win32con.WM_RBUTTONUP:
            menu = win32gui.CreatePopupMenu()
            win32gui.AppendMenu(menu, win32con.MF_STRING, 1, "Open SWhereUsed")
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(menu, win32con.MF_STRING, 2, "Stop SWhereUsed")
            win32gui.SetMenuDefaultItem(menu, 1, False)
            x, y = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(hwnd)
            win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN | win32con.TPM_RIGHTBUTTON, x, y, 0, hwnd, None)
            win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
        return True

    def command(hwnd, msg, wparam, lparam):
        choice = win32api.LOWORD(wparam)
        if choice == 1:
            open_page()
        elif choice == 2:
            threading.Thread(target=shutdown_app, daemon=True).start()
        return True

    wc = win32gui.WNDCLASS()
    wc.hInstance = win32api.GetModuleHandle(None)
    wc.lpszClassName = "SWhereUsedTray"
    wc.lpfnWndProc = {msg_id: notify, win32con.WM_COMMAND: command}
    try:
        cls = win32gui.RegisterClass(wc)
    except Exception:  # noqa: BLE001 — registered before (restart in the same process): use it
        cls = "SWhereUsedTray"
    hwnd = win32gui.CreateWindow(cls, "SWhereUsed", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)
    ico = HERE / "SWhereUsed.ico"
    try:
        hicon = win32gui.LoadImage(wc.hInstance, str(ico), win32con.IMAGE_ICON, 0, 0,
                                   win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE)
    except Exception:  # noqa: BLE001
        hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
    flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
    win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, (hwnd, 0, flags, msg_id, hicon, f"SWhereUsed {APP_VERSION}: running"))
    _tray["hwnd"] = hwnd
    if _meta_get("tray_told") != "1":                # the first time: say where it is
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, (hwnd, 0, win32gui.NIF_INFO, msg_id, hicon, "", "SWhereUsed runs in the "
                                      "background. Double-click this icon to open it; right-click to stop it.", 10, "SWhereUsed"))
            _meta_set("tray_told", "1")
        except Exception:  # noqa: BLE001
            pass
    win32gui.PumpMessages()


def _restart_app():
    """Start the new version: through Start SWhereUsed.bat (it starts SWhereUsed again on RESTART_CODE), or, when
    started some other way, as a new process that waits until this one has let go of the port."""
    for helper in (_thumb, _ext):
        _kill_tree(helper.get("proc"))
    _tray_remove()
    if os.environ.get("SWHEREUSED_LAUNCHER") != "bat":
        args = [a for a in sys.argv[1:] if a not in ("--no-browser", "--after-update")]
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if BACKGROUND \
            else getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *args, "--no-browser", "--after-update"], cwd=str(HERE),
                         creationflags=flags)
    os._exit(RESTART_CODE)


def _meta_update():
    """The update found by the last check (without checking now)."""
    if not UPDATE_REPO or CHECK_UPDATES != "yes":
        return None
    try:
        known = json.loads(_meta_get("update_info") or "{}")
    except (ValueError, sqlite3.Error):
        return None
    if known.get("latest") and _version_tuple(known["latest"]) > _version_tuple(APP_VERSION):
        return {"latest": known["latest"], "url": known.get("url")}
    return None


def diagnostics():
    """Everything useful for a bug report, as plain text to paste on the forum. Never the license key or passwords."""
    h, st = health(), status()
    out = [f"SWhereUsed {APP_VERSION}", f"Python {h['python']} ({h['bits']}-bit) on {sys.platform}",
           f"pywin32: {h['pywin32']}, comtypes: {h['comtypes']}",
           f"Document Manager DLL: {h['dll'] or h['dll_error']}",
           f"License key: {'present' if h['key'] else 'MISSING'} ({'from ' + h['key_source'] if h['key_source'] else '-'})",
           f"File source: {h['source'] or 'none'} ({h['source_message']}); Everything: {h['everything']['message']}",
           f"Parts indexed: {INCLUDE_PARTS}; part references: {'off: ' + str(st.get('part_refs_off')) if st.get('part_refs_off') else 'on'}",
           f"Recognise renames by other tools: {DETECT_RENAMES} (external references read in a helper process)",
           "SOLIDWORKS PDM vault views (registry): " + (", ".join(f"{n} = {r}" for r, n in pdm_registry_vaults().items()) or "none"),
           f"Index: {st.get('assemblies', 0)} assemblies, {st.get('drawings', 0)} drawings, {st.get('parts', 0)} parts, "
           f"{st.get('failed', 0)} unreadable; running: {st.get('running')}", ""]
    out.append("Settings (defaults not listed):")
    for sec, key, var, typ, _h in _SPEC:
        if var in _SECRET_SETTINGS:
            continue
        value = _setting_text(globals()[var], typ)
        out.append(f"  [{sec}] {key} = {value}")
    for pr in SETTINGS_PROBLEMS:
        out.append(f"  Note: {pr}")
    out.append("")
    out.append("Last index runs:")
    for r in run_history(5):
        out.append(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['started']))}  {'full' if r['full'] else 'update'}  "
                   f"{r['seconds']} s  {r['files']} files  {r['errors']} unreadable  {r['message'] or ''}")
    slow = slow_summary()
    if slow:
        out += ["", "Actions that took longer than a second:"] + slow
    try:
        m = json.loads(_meta_get("maintained") or "{}")
    except ValueError:
        m = {}
    out.append("")
    out.append(f"Index tidied up: {time.strftime('%Y-%m-%d %H:%M', time.localtime(m['time']))}, {m['before_mb']} MB -> {m['after_mb']} MB "
               f"in {m['seconds']} s" if m.get("time") else "Index tidied up: not yet (weekly, when the app is idle)")
    tb = sum(e.stat().st_size for e in os.scandir(_thumb_dir())) / 1048576 if _thumb_dir().exists() else 0
    out.append(f"Preview pictures kept: {tb:.0f} MB of at most {THUMB_CACHE_MB} MB")
    pr = problems(limit=0)
    if pr["summary"]:
        out.append("")
        out.append("Why files could not be read (most common first):")
        for g in pr["summary"][:10]:
            out.append(f"  {g['count']}x {g['kind']} {g['status']}: {g['reason']}")
    for name, n in (("rename_debug.log", 40), ("crash_index.log", 15), ("crash_rename.log", 15), ("crash_server.log", 15),
                    ("console.log", 25)):
        lines = _tail(DATA_DIR / name, n)
        if lines:
            out += ["", f"Last lines of {name}:"] + ["  " + x for x in lines]
    return "\n".join(out)


def settings_list():
    """Every setting with its value and help, for the settings form in the app."""
    restart = {"APP_PORT", "DATABASE"}
    out = []
    for sec, key, var, typ, help_ in _SPEC:
        out.append({"section": sec, "key": key, "type": typ, "help": help_,
                    "value": "" if var in _SECRET_SETTINGS else _setting_text(globals()[var], typ),
                    "secret": var in _SECRET_SETTINGS, "restart": var in restart})
    return out


def change_setting(section, key, value):
    """Change one setting from the app: checked like settings.ini, then stored there and used right away."""
    spec = next(((s_, k, v, t) for s_, k, v, t, _h in _SPEC if s_ == section and k == key), None)
    if not spec:
        raise ValueError(f"unknown setting [{section}] {key}")
    _s, _k, var, typ = spec
    parsed = _parse(str(value).strip(), typ)
    if var == "WORKERS" and not 1 <= parsed <= 8:
        raise ValueError("choose 1 to 8")
    if var in ("APP_PORT",) and not 1024 <= parsed <= 65535:
        raise ValueError("choose a port from 1024 to 65535")
    if typ == "int" and parsed < 0:
        raise ValueError("must be 0 or more")
    globals()[var] = parsed
    set_setting(section, key, _setting_text(parsed, typ))
    if var in ("INDEX_SOURCE", "EVERYTHING_URL", "EVERYTHING_USER", "EVERYTHING_PASS"):
        _ev_cache.update(t=0.0, value=None)
    return settings_list()


def _version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def check_for_update(force=False):
    """The newest release on GitHub (UPDATE_REPO), checked at most twice a day. None when there is nothing newer."""
    if not UPDATE_REPO or CHECK_UPDATES != "yes":
        return None
    try:
        known = json.loads(_meta_get("update_info") or "{}")
    except ValueError:
        known = {}
    if force or time.time() - known.get("checked", 0) > 12 * 3600:
        try:
            req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                                         headers={"Accept": "application/vnd.github+json", "User-Agent": APP_NAME})
            with urllib.request.urlopen(req, timeout=8) as r:
                rel = json.loads(r.read().decode("utf-8"))
            tag = rel.get("tag_name", "")
            zips = [a for a in rel.get("assets") or [] if str(a.get("name", "")).lower().endswith(".zip")]
            asset = next((a for a in zips if "whereused" in a["name"].lower()), zips[0] if zips else None)
            known = {"checked": time.time(), "latest": tag.lstrip("vV"), "tag": tag, "notes": (rel.get("body") or "")[:6000],
                     "url": rel.get("html_url") or f"https://github.com/{UPDATE_REPO}/releases/latest",
                     "asset": (asset or {}).get("browser_download_url") or f"https://api.github.com/repos/{UPDATE_REPO}/zipball/{tag}"}
            _meta_set("update_info", json.dumps(known))
        except Exception as e:  # noqa: BLE001 — offline, proxy, rate limit: try again later
            _update_error["text"] = human_error(e)
            return None
    if known.get("latest") and _version_tuple(known["latest"]) > _version_tuple(APP_VERSION):
        return {"latest": known["latest"], "url": known.get("url")}
    return None


# ---------------------------------------------------------------------------
# Setup checks
# ---------------------------------------------------------------------------
def health():
    source, source_msg = index_source()
    try:
        dll, dll_error = find_swdm_dll(), None
    except Exception as e:  # noqa: BLE001
        dll, dll_error = None, str(e)
    ev_ok, ev_msg = everything_status(max_age=3)
    h = {"app": APP_NAME, "version": APP_VERSION, "windows": os.name == "nt", "python": sys.version.split()[0],
         "bits": 64 if sys.maxsize > 2 ** 32 else 32, "pywin32": HAVE_PYWIN32, "comtypes": HAVE_COMTYPES,
         "dll": dll, "dll_error": dll_error, "key": bool(SW_KEY), "key_source": SW_KEY_SOURCE,
         "everything": {"ok": ev_ok, "message": ev_msg, "url": EVERYTHING_URL},
         "source_setting": INDEX_SOURCE, "folders": INDEX_FOLDERS, "source": source, "source_message": source_msg,
         "data_dir": str(DATA_DIR), "settings_file": str(_settings_path()), "database": str(db_path()),
         "settings_problems": list(SETTINGS_PROBLEMS), "rename": RENAME_ALLOWED == "yes",
         "autostart": autostart_state(), "description_properties": DESCRIPTION_PROPS,
         "thumbnails": THUMBNAILS == "yes", "update": _meta_update(), "donate_url": DONATE_URL,
         "backups": str(backup_root())}
    h["ready"] = bool(h["windows"] and h["bits"] == 64 and HAVE_PYWIN32 and HAVE_COMTYPES and dll and SW_KEY and source)
    return h


def check_dm():
    """Open a Document Manager session with the key (in a separate process, see --check-dm)."""
    try:
        proc = subprocess.run([PY_CHILD, str(Path(__file__).resolve()), "--check-dm"], cwd=str(HERE),
                              capture_output=True, text=True, timeout=90)
        line = next((l for l in reversed(proc.stdout.splitlines()) if l.startswith("{")), None)
        if line:
            return json.loads(line)
        return {"ok": False, "message": (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()[-400:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "the Document Manager did not answer within 90 seconds"}


def _check_dm_main():
    problem = sw_problem()
    if problem:
        print(json.dumps({"ok": False, "message": problem}))
        return
    try:
        _com_init()
        app = _best(_sw_app(), "ISwDMApplication")
        latest = _attr(app, "GetLatestSupportedFileVersion", None)
        print(json.dumps({"ok": True, "message": "The Document Manager accepted the key.", "latest_version": latest}))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "message": human_error(e)}))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
APP_ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" '
                'fill="#002B36"/><path d="M16 6 8 10.5v9L16 24l8-4.5v-9z" fill="none" stroke="#2AA198" '
                'stroke-width="2.2" stroke-linejoin="round"/><path d="M8 10.5 16 15l8-4.5M16 15v9" fill="none" '
                'stroke="#2AA198" stroke-width="2.2" stroke-linejoin="round"/><circle cx="23.5" cy="23.5" r="4.6" '
                'fill="#002B36" stroke="#B58900" stroke-width="2.2"/><path d="m26.8 26.8 3 3" stroke="#B58900" '
                'stroke-width="2.6" stroke-linecap="round"/></svg>')


SLOW_SECONDS = 1.0
_slow_lock = threading.Lock()


def _log_slow(method, path, took):
    """One line per slow request in slow.log (kept below about 1,000 lines)."""
    url = urllib.parse.urlparse(path)
    what = url.path + ("?" + urllib.parse.unquote(url.query)[:160] if url.query else "")
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{took:.1f}\t{method} {what}\n"
    with _slow_lock:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            log = DATA_DIR / "slow.log"
            if log.exists() and log.stat().st_size > 200_000:
                log.write_text("".join(log.read_text(encoding="utf-8", errors="replace").splitlines(True)[-600:]), encoding="utf-8")
            with open(log, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


def slow_summary(last=15):
    """For the diagnostics: per kind of request how often slow, the longest, and the last few."""
    try:
        lines = (DATA_DIR / "slow.log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    by = {}
    for ln in lines:
        parts = ln.split("\t")
        if len(parts) != 3:
            continue
        kind = parts[2].split("?")[0]
        e = by.setdefault(kind, [0, 0.0])
        e[0] += 1
        e[1] = max(e[1], float(parts[1] or 0))
    out = [f"  {k}: {n}x slow, longest {mx:.1f} s" for k, (n, mx) in sorted(by.items(), key=lambda x: -x[1][0])]
    if lines:
        out += ["  Last ones:"] + ["    " + ln.replace("\t", "  ") for ln in lines[-last:]]
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{APP_VERSION}"

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", cache or "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True       # the browser stopped listening: not an error

    def _host_ok(self):
        # Only our own address: protects against web pages that try to reach this app via DNS tricks
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{APP_PORT}", f"localhost:{APP_PORT}")

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        return not origin or origin in (f"http://127.0.0.1:{APP_PORT}", f"http://localhost:{APP_PORT}")

    def do_GET(self):
        self._timed(self._do_get)

    def do_POST(self):
        self._timed(self._do_post)

    def _timed(self, fn):
        """Every request timed; longer than SLOW_SECONDS goes into slow.log (shown in Copy diagnostics)."""
        t0 = time.perf_counter()
        _last_request["t"] = time.time()
        try:
            fn()
        finally:
            took = time.perf_counter() - t0
            if took >= SLOW_SECONDS:
                _log_slow(self.command, self.path, took)

    def _do_get(self):
        if not self._host_ok():
            return self._send(403, {"error": "Unknown host."})
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        arg = lambda k, d="": qs.get(k, [d])[0]            # noqa: E731

        if url.path in ("/", "/index.html", "/whereused", "/whereused.html", "/swhereused", "/swhereused.html", "/SWhereUsed.html", "/setup"):
            return self._send(200, (HERE / "SWhereUsed.html").read_bytes(), "text/html; charset=utf-8")
        if url.path in ("/favicon.svg", "/favicon.ico"):
            return self._send(200, APP_ICON_SVG.encode("utf-8"), "image/svg+xml")
        if url.path == "/api/health":
            return self._send(200, health())
        if url.path == "/api/check-dm":
            return self._send(200, check_dm())
        if url.path == "/api/status":
            return self._send(200, status())
        if url.path == "/api/thumb":
            png = thumbnail(arg("path")) if arg("path") else None
            if not png:
                return self._send(404, b"", "text/plain", cache="max-age=600")
            return self._send(200, png, "image/png", cache="max-age=3600")
        if url.path == "/api/diagnostics":
            return self._send(200, {"text": diagnostics()})
        if url.path == "/api/settings":
            return self._send(200, {"settings": settings_list()})
        if url.path == "/api/broken":
            return self._send(200, broken_references())
        if url.path == "/api/runs":
            return self._send(200, {"runs": run_history(20)})
        if url.path == "/api/problems":
            return self._send(200, problems())
        if url.path == "/api/search":
            if not arg("q").strip():
                return self._send(400, {"error": "Type part of a file name."})
            try:
                return self._send(200, search(arg("q"), _to_int(arg("limit")) or 60, _to_int(arg("offset")) or 0,
                                              arg("sort", "name"), arg("dir") == "desc", arg("kind"),
                                              arg("unused") == "1", arg("filter"), arg("exact") == "1"))
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": human_error(e)})
        if url.path == "/api/impact":
            if not arg("path"):
                return self._send(400, {"error": "No file given."})
            try:
                return self._send(200, impact(arg("path"), include_own=arg("own") == "1"))
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": human_error(e)})
        if url.path == "/api/structure":
            if not arg("path"):
                return self._send(400, {"error": "No file given."})
            try:
                return self._send(200, compact_structure(structure(arg("path"), arg("config") or None, with_usage=False)))
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": human_error(e)})
        if url.path == "/api/stats":
            try:
                return self._send(200, statistics_page())
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": human_error(e)})
        if url.path == "/api/job":
            return self._send(200, job_progress())
        if url.path == "/api/update":
            return self._send(200, update_status(force=arg("check") == "1"))
        if url.path == "/api/history":
            return self._send(200, {"entries": history()})
        if url.path == "/api/folders":
            try:
                return self._send(200, folders_on_disk(arg("path")))
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": human_error(e)})
        if url.path == "/api/browse":
            try:
                return self._send(200, browse(arg("folder"), arg("files", "1") != "0", arg("all") == "1"))
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": human_error(e)})
        if url.path == "/api/whereused":
            if not arg("path"):
                return self._send(400, {"error": "No file given."})
            try:
                return self._send(200, whereused(arg("path"), arg("config") or None, arg("other") == "1"))
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": human_error(e)})
        self._send(404, {"error": "Not found"})

    def _do_post(self):
        if not self._host_ok() or not self._origin_ok():
            return self._send(403, {"error": "Request from an unknown origin refused."})
        try:
            length = _to_int(self.headers.get("Content-Length")) or 0
            body = json.loads(self.rfile.read(min(length, 1_000_000)) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "Invalid JSON."})

        if self.path == "/api/update":
            started = update_index(retry_errors=bool(body.get("retry", True)))
            return self._send(200, {"started": started, "status": status()})
        if self.path == "/api/open":
            try:
                return self._send(200, open_path(str(body.get("path") or ""), str(body.get("action") or "open")))
            except Exception as e:  # noqa: BLE001
                return self._send(400, {"error": str(e)})
        if self.path == "/api/setup":
            return self._send(*apply_setup(body))
        if self.path == "/api/rename":
            path = str(body.get("path") or "")
            if not path or not os.path.isfile(path):
                return self._send(404, {"error": "File not found."})
            args = (path, str(body.get("name") or ""), str(body.get("folder") or "") or None,
                    bool(body.get("with_drawing", True)))
            own = body.get("include_own") is True
            try:
                if body.get("dry_run", True) is not False:
                    return self._send(200, {**rename_plan(*args, include_own=own), "executed": False})
                return self._send(200, rename_execute(*args, include_own=own))
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": human_error(e)})
        if self.path == "/api/settings":
            try:
                return self._send(200, {"settings": change_setting(str(body.get("section")), str(body.get("key")),
                                                                     str(body.get("value", "")))})
            except (ValueError, OSError) as e:
                return self._send(400, {"error": str(e)})
        if self.path == "/api/usage":
            paths = [str(x) for x in (body.get("paths") or [])][:20000]
            try:
                return self._send(200, {"usage": {k: list(v) for k, v in usage_for_paths(paths).items()}})
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": human_error(e)})
        if self.path == "/api/rename-ready":
            paths = [str(x) for x in (body.get("paths") or [])][:10000]
            try:
                return self._send(200, rename_readiness(paths, bool(body.get("with_drawings", True))))
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": human_error(e)})
        if self.path == "/api/props":
            paths = [str(x) for x in (body.get("paths") or []) if str(x).lower().endswith(tuple(SW_TYPES))][:3000]
            try:
                if body.get("per_file"):
                    return self._send(200, props_files(paths, body.get("with_drawings", True) is not False))
                return self._send(200, props_overview(paths))
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": human_error(e)})
        if self.path == "/api/quit":
            self._send(200, {"ok": True})
            threading.Timer(0.6, shutdown_app).start()       # after this answer has reached the page
            return None
        if self.path == "/api/fixpaths":
            try:
                parents = [str(x) for x in (body.get("paths") or [])][:2000]
                return self._send(200, fix_paths(parents, body.get("dry_run", True) is not False))
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": human_error(e)})
        if self.path == "/api/update/install":
            try:
                return self._send(200, install_update())
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": human_error(e)})
        if self.path == "/api/undo":
            try:
                return self._send(200, undo(str(body.get("id") or ""), body.get("dry_run", True) is not False, body.get("force") is True))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": human_error(e)})
        if self.path == "/api/mkdir":
            try:
                return self._send(200, {"path": make_folder(str(body.get("parent") or ""), str(body.get("name") or ""))})
            except (ValueError, OSError) as e:
                return self._send(400, {"error": str(e) if isinstance(e, ValueError) else human_error(e)})
        if self.path == "/api/packgo":
            try:
                items = body.get("items") if isinstance(body.get("items"), list) else []
                return self._send(200, packgo(str(body.get("root") or ""), items, str(body.get("target") or ""),
                                              body.get("keep_structure", True) is not False,
                                              body.get("with_drawings", True) is not False,
                                              body.get("dry_run", True) is not False,
                                              body.get("overwrite") is True,
                                              body.get("props_rules") if isinstance(body.get("props_rules"), list) else [],
                                              str(body.get("props_scope") or "both")))
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": human_error(e)})
        if self.path == "/api/rename-batch":
            try:
                items = body.get("items") if isinstance(body.get("items"), list) else []
                return self._send(200, rename_batch(items, bool(body.get("with_drawings", True)),
                                                    body.get("dry_run", True) is not False,
                                                    body.get("include_own") is True))
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": human_error(e)})
        if self.path == "/api/unlock":
            try:
                return self._send(200, remove_stale_lock(str(body.get("path") or "")))
            except Exception as e:  # noqa: BLE001
                return self._send(400, {"error": human_error(e)})
        self._send(404, {"error": "Not found"})

    def log_message(self, fmt, *args):
        if "--verbose" in sys.argv:
            print(f"[{self.log_date_time_string()}] {fmt % args}")


def apply_setup(body):
    """Save what the Setup page sends: license key, file source, folders, Everything address."""
    global INDEX_SOURCE, INDEX_FOLDERS, EVERYTHING_URL
    try:
        if isinstance(body.get("key"), str) and body["key"].strip():
            save_key(body["key"])
        if body.get("source") in ("auto", "everything", "folders"):
            INDEX_SOURCE = body["source"]
            set_setting("index", "source", INDEX_SOURCE)
        if isinstance(body.get("folders"), list):
            INDEX_FOLDERS = [str(f).strip().strip('"') for f in body["folders"] if str(f).strip()]
            set_setting("index", "folders", "; ".join(INDEX_FOLDERS))
        if isinstance(body.get("everything_url"), str) and body["everything_url"].strip():
            url = body["everything_url"].strip().rstrip("/")
            if not re.match(r"^https?://", url):
                url = "http://" + url
            EVERYTHING_URL = url
            set_setting("everything", "url", url)
        _ev_cache.update(t=0.0, value=None)
        if isinstance(body.get("autostart"), bool):
            set_autostart(body["autostart"], "background" if body.get("autostart_mode") == "background" else "window")
    except (OSError, RuntimeError) as e:
        return 500, {"error": f"Could not save the settings: {e}"}
    return 200, health()


def _enable_crash_log(name):
    """Log hard crashes (outside Python) to a file in the data folder."""
    import faulthandler
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        f = open(DATA_DIR / name, "a", encoding="utf-8")
        f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} started (pid {os.getpid()}) ===\n")
        f.flush()
        faulthandler.enable(file=f, all_threads=True)
        return f
    except OSError:
        return None


def _port_state():
    """Who is on our port? 'free', 'whereused' (already running) or 'other' (another program)."""
    import socket
    try:
        with socket.create_connection((APP_HOST, APP_PORT), timeout=1):
            pass
    except OSError:
        return "free"
    try:
        with _direct.open(f"http://{APP_HOST}:{APP_PORT}/api/health", timeout=3) as r:
            return "whereused" if json.loads(r.read().decode("utf-8")).get("app") == APP_NAME else "other"
    except Exception:  # noqa: BLE001
        return "other"


class Server(ThreadingHTTPServer):
    """On Windows, the usual 'reuse address' lets a second program start on a port that is already taken,
    after which the browser still talks to the first one. Claim the port exclusively instead."""
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        import socket
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _diagnose_reference_ids(path):
    """--test only: what the Document Manager says about each reference (status, time stamp) and which
    ID-like functions this version offers. Meant to find out whether it can tell "same name, other file"
    (the internal ID SOLIDWORKS checks). Status names are read from SwDocumentMgr.dll itself."""
    print("\n--- Reference status and internal ID (diagnostics) ---")
    mod = _swdm()
    names = {}
    for n in dir(mod):
        if n.lower().startswith("swdmreferencestatus"):
            try:
                names[int(getattr(mod, n))] = n
            except (TypeError, ValueError):
                pass
    print("Status values in this DLL: " + (", ".join(f"{v}={n}" for v, n in sorted(names.items())) or "none found"))
    doc = _open_sw_retry(path)
    try:
        ids = sorted({n for iface in dir(mod) if iface.startswith("ISwDMDocument") for n in dir(getattr(mod, iface))
                      if re.search(r"(^|[a-z])(ID|Id)([A-Z]|$)|Time|Stamp|Guid", n) and not n.startswith("_")})
        print("ID/time-like functions on documents: " + (", ".join(ids) or "none"))
        if path.lower().endswith(".slddrw"):
            print("(Drawing: GetAllExternalReferences4 crashed on drawings before; if this window closes, that is why.)")
        res = doc.GetAllExternalReferences4(_sw_app().GetSearchOptionObject())
        parts = list(res) if isinstance(res, tuple) else [res]
        refs = _string_list(res) or []
        others = [_as_list(x) for x in parts if isinstance(x, (tuple, list)) and not (x and all(isinstance(v, str) for v in x))]
        print(f"{len(refs)} reference(s):")
        for i, ref in enumerate(refs):
            extra = []
            for j, arr in enumerate(others):
                if i < len(arr):
                    v = arr[i]
                    label = ["status", "virtual", "time stamp"][j] if j < 3 else f"value {j + 1}"
                    if label == "status" and isinstance(v, int) and v in names:
                        v = f"{v} ({names[v]})"
                    extra.append(f"{label}: {v}")
            print(f"    {ref}\n        " + "; ".join(extra))
    except Exception as e:  # noqa: BLE001
        print(f"GetAllExternalReferences4: {human_error(e)}")
    finally:
        try:
            doc.CloseDoc()
        except Exception:  # noqa: BLE001
            pass
    print("--- end of diagnostics ---\n")


def main():
    if "--selftest" in sys.argv:
        import unittest
        tests = HERE / "tests"
        suite = unittest.defaultTestLoader.discover(str(tests), pattern="test_*.py", top_level_dir=str(tests))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)

    load_settings(write_missing=not WORKER_MODE and not any(a in sys.argv for a in ("--check-dm", "--rename-job", "--probe-part", "--probe-ext", "--thumb-worker", "--ext-worker", "--read-props")))
    if "--after-update" in sys.argv:                # the old version may still be letting go of the port
        import socket
        for _ in range(40):
            with socket.socket() as probe:
                if probe.connect_ex((APP_HOST, APP_PORT)) != 0:
                    break
            time.sleep(0.5)

    if "--check-dm" in sys.argv:
        _check_dm_main()
        sys.exit(0)

    if "--read-props" in sys.argv:
        _read_props_main(sys.argv[sys.argv.index("--read-props") + 1])

    if "--ext-worker" in sys.argv:
        _ext_worker_main()

    if "--probe-ext" in sys.argv:                   # started by the index: can external references be read safely?
        if sw_problem():
            print(json.dumps({"ok": False, "message": sw_problem()}))
            os._exit(0)
        _com_init()
        doc = _open_sw_retry(sys.argv[sys.argv.index("--probe-ext") + 1])
        try:
            refs = _string_list(doc.GetAllExternalReferences4(_sw_app().GetSearchOptionObject())) or []
            print(json.dumps({"ok": True, "refs": len(refs)}))
        finally:
            doc.CloseDoc()
        sys.stdout.flush()
        os._exit(0)

    if "--probe-part" in sys.argv:                  # started by the index: can part references be read safely?
        problem = sw_problem()
        if problem:
            print(json.dumps({"ok": False, "message": problem}))
            os._exit(0)
        _com_init()
        try:
            rows = read_part(sys.argv[sys.argv.index("--probe-part") + 1], with_refs=True)[0]
            print(json.dumps({"ok": True, "refs": len(rows)}))
        except Exception as e:  # noqa: BLE001 — an error is fine; only a crash counts against it
            print(json.dumps({"ok": True, "error": human_error(e)}))
        sys.stdout.flush()
        os._exit(0)

    if "--thumb-worker" in sys.argv:
        _thumb_worker_main()
        os._exit(0)

    if "--rename-job" in sys.argv:
        _crash = _enable_crash_log("crash_rename.log")  # noqa: F841
        _rename_worker_main(sys.argv[sys.argv.index("--rename-job") + 1])

    if WORKER_MODE:
        _crash = _enable_crash_log("crash_index.log")  # noqa: F841 — keep the file open
        if os.name == "nt":
            try:                                        # lower priority: SOLIDWORKS stays responsive
                import ctypes
                ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
            except Exception:  # noqa: BLE001
                pass
        if not _single_index_process():
            print("Another index process is still running; this one stops.")
            sys.exit(0)
        if HAVE_PYWIN32:
            pythoncom.CoInitialize()
        run_index()
        sys.exit(0)

    if "--test" in sys.argv:
        target = sys.argv[sys.argv.index("--test") + 1]
        if struct.calcsize("P") * 8 == 32:              # the Document Manager is 64-bit: a 32-bit Python cannot use it
            venv_py = DATA_DIR / "venv" / "Scripts" / "python.exe"
            print(f"This is a 32-bit Python ({sys.executable}); the Document Manager needs 64-bit.")
            print("Use the Python of SWhereUsed instead, or drag the file onto 'Test a file.bat':")
            print(f'  "{venv_py}" "{Path(__file__).resolve()}" --test "{target}"')
            sys.exit(2)
        problem = sw_problem()
        if problem:
            sys.exit(f"Cannot read SOLIDWORKS files: {problem}")
        _com_init()
        print(f"Python {sys.version.split()[0]}, {'64' if sys.maxsize > 2 ** 32 else '32'}-bit; key from {SW_KEY_SOURCE}")
        print(f"DLL: {find_swdm_dll()}")
        hints = _file_hints(target)
        print(f"File checks: {'; '.join(hints) if hints else 'no problems found'}")
        if target.lower().endswith(".sldprt"):
            doc = _open_sw_retry(target)
            try:
                refs, method = _part_references(doc)
                print(f"Part references read with {method} (what the index uses): {len(refs)} found")
                for ref, cfg in refs:
                    print(f"    {ref}" + (f"  <{cfg}>" if cfg else ""))
            except Exception as e:  # noqa: BLE001
                print(f"Part references: {human_error(e)}")
            # For comparison: does the general function find more (for example in-context references)?
            # (It crashed on drawings with comtypes; here only this test process is at risk.)
            try:
                res = doc.GetAllExternalReferences4(_sw_app().GetSearchOptionObject())
                allrefs = _string_list(res) or []
                print(f"For comparison, GetAllExternalReferences4 finds {len(allrefs)}:")
                for ref in allrefs:
                    print(f"    {ref}")
            except Exception as e:  # noqa: BLE001
                print(f"For comparison, GetAllExternalReferences4: {human_error(e)}")
            finally:
                try:
                    doc.CloseDoc()
                except Exception:  # noqa: BLE001
                    pass
        _diagnose_reference_ids(target)
        res = read_document(target)
        rows, n, desc = res[:3]
        if target.lower().endswith(".sldasm"):
            print("\n--- References: as found (the Document Manager searches) and as saved (no searching) ---")
            found = _ext_refs_isolated(target) or []
            saved = _ext_refs_isolated(target, saved_only=True) or []
            for title, lst in (("As found", found), ("As saved", saved)):
                print(f"{title}: {len(lst)}")
                for r in lst:
                    print(f"    {'exists ' if os.path.exists(r) else 'MISSING'}  {r}")
            print("Same lists: the Document Manager searched anyway (then Fix paths uses the saved paths from the index)."
                  if sorted(found) == sorted(saved) else "Different lists: Fix paths can see the saved paths.")
            print("--- end ---")
            print("\n--- Renames by other tools ---")
            _detect_renames(target, rows, print)
            print("--- end ---\n")
        version = res[3] if len(res) > 3 else None
        print(f"{n} configuration(s)/sheet(s); description: {desc!r}")
        print(f"Last saved with SOLIDWORKS {sw_release(version) or 'unknown'} (file version number {version})")
        for r in rows:
            print(f"  [{r[1] or 'drawing'}] {r[5]}x {r[3]}  <{r[4]}>"
                  f"{''.join('  ' + k.replace('_', ' ') for k, v in _flags(r[6]).items() if v)}"
                  f"{'  virtual' if r[7] else ''}")
        sys.exit(0)

    if BACKGROUND or sys.stdout is None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            log = DATA_DIR / "console.log"
            if log.exists() and log.stat().st_size > 1_000_000:
                log.replace(log.with_suffix(".old.log"))
            sys.stdout = sys.stderr = open(log, "a", encoding="utf-8", buffering=1)
            print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}  {APP_NAME} {APP_VERSION} started in the background")
        except OSError:
            pass
    port = _port_state()
    if port == "whereused":
        print(f"{APP_NAME} is already running on http://{APP_HOST}:{APP_PORT} (in another window).")
        if "--no-browser" not in sys.argv and not os.environ.get("SWHEREUSED_NO_BROWSER"):   # (not after an update)
            import webbrowser
            webbrowser.open(f"http://{APP_HOST}:{APP_PORT}/")
            print("Opened it in your browser.")
        sys.exit(0)
    if port == "other":
        sys.exit(f"Port {APP_PORT} is already used by another program. Close that program, or give SWhereUsed "
                 f"another port: [app] port in {_settings_path()}")

    print(f"{APP_NAME} {APP_VERSION}")
    print(f"  Data folder: {DATA_DIR}")
    for p in SETTINGS_PROBLEMS:
        print(f"  Note: {p}")
    msg = start_everything_if_needed()
    if msg:
        print(f"  {msg}")
    problem = sw_problem()
    source, source_msg = index_source()
    if problem:
        print(f"  Setup needed: {problem}. Open the app to finish the setup.")
    elif not source:
        print(f"  Waiting for a file source: {source_msg}. The index starts by itself as soon as it is there.")
    else:
        print(f"  Files from: {source} ({source_msg})")
    _crash = _enable_crash_log("crash_server.log")  # noqa: F841
    try:
        removed = cleanup_backups()
        if removed:
            print(f"  Removed {removed} backup folder(s) older than {BACKUP_DAYS} days.")
    except OSError:
        pass
    try:
        server = Server((APP_HOST, APP_PORT), Handler)
    except OSError as e:
        sys.exit(f"Port {APP_PORT} is in use by another program ({e}). Change [app] port in {_settings_path()}.")
    url = f"http://{APP_HOST}:{APP_PORT}/"
    if BACKGROUND:
        print(f"  Running on {url} in the background (icon by the clock)")
        if os.name == "nt":
            import webbrowser
            threading.Thread(target=_tray_icon, args=(lambda: webbrowser.open(url),), daemon=True, name="tray").start()
    else:
        print(f"  Running on {url}  (keep this window open; close it or press Ctrl+C to stop)")
    if os.name == "nt":
        _migrate_autostart()
    threading.Thread(target=_scheduler, daemon=True, name="scheduler").start()
    threading.Thread(target=_rebuild_tree_bg, daemon=True, name="tree").start()   # Browse is quick from the first click
    threading.Thread(target=_warm_statistics, daemon=True, name="stats").start()
    threading.Thread(target=check_for_update, daemon=True, name="update").start()
    if OPEN_BROWSER == "yes" and "--no-browser" not in sys.argv:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()

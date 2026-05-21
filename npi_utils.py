#!/usr/bin/env python3
"""
npi_utils.py — Shared utilities for NPI-based tools.

Provides:
    bootstrap_pynpi()                — set LD_LIBRARY_PATH + sys.path for pynpi; re-exec if needed
    bootstrap_pynpi_for_daidir(d)    — same, but auto-detect Verdi from daidir markers
                                       and re-exec into the matching Verdi-bundled python
    detect_verdi_install_for_daidir(d) — daidir -> (verdi_install_path, version_str)
    auto_daidir()                    — locate vcs_sim_exe.daidir in common runout layouts
    sig_names(stmts)                 — extract full signal names from DrvLoadStmt list
    csv_escape(s)                    — CSV-safe quoting
"""

import glob as _glob
import os
import re as _re
import sys


def bootstrap_pynpi() -> None:
    """
    Ensure pynpi is importable:
      1. On first call, prepend NPI lib dirs to LD_LIBRARY_PATH and re-exec.
      2. On the re-exec'd call, add pynpi Python path to sys.path.

    Call this before any `import pynpi.*`.
    """
    verdi = os.environ.get('VERDI_HOME', '')
    if not verdi:
        sys.exit('ERROR: $VERDI_HOME is not set.')

    npi_lib  = os.path.join(verdi, 'share', 'NPI', 'lib', 'linux64')
    plat_bin = os.path.join(verdi, 'platform', 'linux64', 'bin')
    npi_py   = os.path.join(verdi, 'share', 'NPI', 'python')

    sentinel = '__NPI_LDPATH_SET__'
    if os.environ.get(sentinel) != '1':
        cur = os.environ.get('LD_LIBRARY_PATH', '')
        os.environ['LD_LIBRARY_PATH'] = (f'{npi_lib}:{plat_bin}:{cur}'
                                         if cur else f'{npi_lib}:{plat_bin}')
        os.environ[sentinel] = '1'
        os.execve(sys.executable, [sys.executable] + sys.argv, os.environ)

    if npi_py not in sys.path:
        sys.path.insert(0, npi_py)


def detect_verdi_install_for_daidir(daidir: str):
    """
    Recover the Verdi/VCS install that built this daidir, by reading markers
    inside the daidir itself (no $VERDI_HOME / module switch dependency).

    Marker priority:
      1. <daidir>/kdbintegratorLog/compiler.log    -> "btIdent Verdi_<ver>"
      2. <daidir>/debug_dump/.version              -> "<ver>_Full64"
      3. <daidir>/kdbintegratorLog/turbo.log       -> "/tool/cbar/apps/vcs/<ver>/" path
                                                      (swap vcs -> verdi)

    Returns (verdi_install_abs_path, version_str), or (None, None) if not detectable.
    """
    if not daidir or not os.path.isdir(daidir):
        return None, None

    ver = None
    log_path = os.path.join(daidir, 'kdbintegratorLog', 'compiler.log')
    if os.path.isfile(log_path):
        try:
            log = open(log_path, errors='replace').read()
            m = _re.search(r'btIdent\s+Verdi_([\w.\-]+)', log)
            if m: ver = m.group(1)
        except Exception:
            pass
    if ver is None:
        v2 = os.path.join(daidir, 'debug_dump', '.version')
        if os.path.isfile(v2):
            try:
                ver = open(v2).readline().strip().split('_')[0]
            except Exception:
                pass

    install = None
    if ver is not None:
        # Strip the single-letter release prefix (W-, X-, Y-, U-, V- ...).
        # str.lstrip('W-') is WRONG — it strips chars not a prefix; use regex.
        short = _re.sub(r'^[A-Z]-', '', ver)
        cand = f'/tool/cbar/apps/verdi/{short}'
        if os.path.isdir(cand):
            install = cand
    if install is None:
        # Fallback A: parse VCS path from turbo.log, swap vcs -> verdi
        turbo = os.path.join(daidir, 'kdbintegratorLog', 'turbo.log')
        if os.path.isfile(turbo):
            try:
                first = open(turbo, errors='replace').readline()
                m2 = _re.search(r'(/tool/cbar/apps/vcs/[^/]+)/', first)
                if m2:
                    cand = m2.group(1).replace('/vcs/', '/verdi/')
                    if os.path.isdir(cand):
                        install = cand
            except Exception:
                pass

    if install is None and ver is not None:
        # Fallback B: query the env-modules system for verdi/<ver>.
        # AMD's modulefile commonly uses `setenv VERDI <real_install>` while
        # `setenv VERDI_HOME` points to an *anchor* (different older version).
        # We want the actual install (the VERDI line), NOT VERDI_HOME.
        install = _verdi_install_via_module(_re.sub(r'^[A-Z]-', '', ver))

    return install, ver


def _verdi_install_via_module(ver_short: str):
    """Run `modulecmd ... bash show verdi/<ver_short>` and parse out the
    `setenv VERDI <path>` line. Returns abs path or None.

    Works without sourcing cbwa_init: discover MODULESHOME and tclsh from
    well-known AMD locations or env. Falls through silently on any failure.
    """
    msh   = os.environ.get('MODULESHOME', '/tool/pandora64/.package/modulecmd-tcl-amd-1.07')
    cmd   = os.path.join(msh, 'modulecmd.tcl')
    tclsh = os.environ.get('TCLSH') or '/tool/pandora64/.package/tcltk-8.6.6/bin/tclsh'
    if not (os.path.isfile(cmd) and os.path.isfile(tclsh)):
        return None
    # Ensure MODULEPATH covers AMD's verdi modulefiles even if caller didn't
    # source cbwa_init (typical when started from raw cron / IDE).
    mpath = os.environ.get('MODULEPATH', '')
    for extra in ('/tool/cbar/etc/ATL/modules', '/tool/pandora64/etc/modules',
                  '/proj/verif_release_ro/modules/current'):
        if extra not in mpath:
            mpath = f'{mpath}:{extra}' if mpath else extra
    import subprocess
    try:
        out = subprocess.run([tclsh, cmd, 'bash', 'show', f'verdi/{ver_short}'],
                             env={**os.environ, 'MODULEPATH': mpath, 'MODULESHOME': msh},
                             capture_output=True, text=True, timeout=15)
        text = (out.stdout or '') + '\n' + (out.stderr or '')
    except Exception:
        return None
    m = _re.search(r'^\s*setenv\s+VERDI\s+(\S+)', text, _re.MULTILINE)
    if m and os.path.isdir(m.group(1)):
        return m.group(1)
    return None


def _select_verdi_python(verdi_install: str):
    """Newest python-<X> shipped under the Verdi install, or None."""
    pys = sorted(_glob.glob(os.path.join(verdi_install,
                 'platform', 'linux64', 'python-*', 'bin', 'python3')))
    return pys[-1] if pys else None


def bootstrap_pynpi_for_daidir(daidir: str) -> None:
    """
    Daidir-aware variant of bootstrap_pynpi():
      1. Detect Verdi install from markers in the daidir (NOT from $VERDI_HOME).
      2. Set $VERDI_HOME to the detected install (so downstream tools agree).
      3. If running under a different python interpreter, re-exec into the
         Verdi-bundled python (avoids libstdc++ ABI mismatch with system/hdk python).
      4. Set LD_LIBRARY_PATH so the bundled python's libstdc++ + NPI .so resolve.
      5. Add pynpi Python path to sys.path.

    Call this before any `import pynpi.*` when you have a known daidir target.
    Falls back to bootstrap_pynpi() if detection fails AND $VERDI_HOME is set.
    """
    install, ver = detect_verdi_install_for_daidir(daidir)
    if not install:
        if os.environ.get('VERDI_HOME'):
            return bootstrap_pynpi()
        sys.exit(f'cannot detect Verdi from {daidir} and $VERDI_HOME not set')

    py = _select_verdi_python(install)
    if not py:
        # Fall back to plain bootstrap with the detected $VERDI_HOME
        os.environ['VERDI_HOME'] = install
        return bootstrap_pynpi()

    os.environ['VERDI_HOME'] = install
    py_lib   = os.path.dirname(os.path.dirname(py)) + '/lib'
    npi_lib  = os.path.join(install, 'share', 'NPI', 'lib', 'linux64')
    plat_bin = os.path.join(install, 'platform', 'linux64', 'bin')
    cur = os.environ.get('LD_LIBRARY_PATH', '')
    os.environ['LD_LIBRARY_PATH'] = ':'.join(p for p in (py_lib, npi_lib, plat_bin, cur) if p)

    sentinel = '__NPI_LDPATH_SET__'
    if os.path.realpath(sys.executable) != os.path.realpath(py) and os.environ.get(sentinel) != '1':
        os.environ[sentinel] = '1'
        os.execve(py, [py] + sys.argv, os.environ)
    elif os.environ.get(sentinel) != '1':
        # Already on the right interpreter; mark sentinel so a later
        # bootstrap_pynpi() call doesn't re-exec a second time.
        os.environ[sentinel] = '1'

    npi_py = os.path.join(install, 'share', 'NPI', 'python')
    if npi_py not in sys.path:
        sys.path.insert(0, npi_py)


def auto_daidir() -> str:
    """
    Locate a VCS design database (*.daidir) in common AMD DJ runout layouts.

    Search order:
      1. simws.vcs / simws — read build_size pointer file, or fall back to
         exec/vcs_sim_exe.daidir
      2. vcs_sim_exe.daidir / simv.daidir directly in cwd
      3. Any *.daidir in cwd (prefer name containing 'vcs' or 'sim')
      4. One level down — prefer exact name vcs_sim_exe.daidir

    Returns the path if found, '' otherwise.
    """
    cwd = os.getcwd()

    for simws_name in ('simws.vcs', 'simws'):
        simws = os.path.join(cwd, simws_name)
        if not os.path.exists(simws):
            continue
        ptr_file = os.path.join(simws, 'build_size.vcs_sim_exe.daidir')
        if os.path.isfile(ptr_file):
            try:
                content = open(ptr_file).read().split()
                candidate = content[-1] if content else ''
                if candidate and os.path.isdir(candidate):
                    return candidate
            except Exception:
                pass
        for sub in ('exec/vcs_sim_exe.daidir', 'vcs_sim_exe.daidir'):
            p = os.path.join(simws, sub)
            if os.path.isdir(p):
                return p

    for name in ('vcs_sim_exe.daidir', 'simv.daidir'):
        p = os.path.join(cwd, name)
        if os.path.isdir(p):
            return p

    hits = [h for h in sorted(_glob.glob(os.path.join(cwd, '*.daidir')))
            if os.path.isdir(h)]
    if hits:
        preferred = [h for h in hits
                     if 'vcs' in os.path.basename(h) or 'sim' in os.path.basename(h)]
        return preferred[0] if preferred else hits[0]

    hits = [h for h in sorted(_glob.glob(os.path.join(cwd, '*', '*.daidir')))
            if os.path.isdir(h)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        preferred = [h for h in hits
                     if os.path.basename(h) == 'vcs_sim_exe.daidir']
        return preferred[0] if preferred else hits[0]

    return ''


def sig_names(stmts) -> list:
    """Extract full signal names from a list of DrvLoadStmt objects."""
    names = []
    for stmt in stmts:
        try:
            for s in stmt.get_sig_hdl_list():
                try:
                    n = s.full_name() or ''
                    if n:
                        names.append(n)
                except Exception:
                    pass
        except Exception:
            pass
    return names


def csv_escape(s) -> str:
    """Return a CSV-safe string, quoting if the value contains commas, quotes, or newlines."""
    s = str(s) if s is not None else ''
    if ',' in s or '"' in s or '\n' in s:
        return '"' + s.replace('"', '""') + '"'
    return s

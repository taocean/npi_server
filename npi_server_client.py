#!/usr/bin/env python3
"""
npi_server_client.py — Client for npi_rtl_server.py.

Two modes:
  1. Daemon mode (DEFAULT) — connect to long-lived Unix-socket daemon. If no
     daemon is running for the given daidir, spawn one. Closing the client
     does NOT kill the daemon, so subsequent scripts reuse the loaded design
     (no 40-second reload).

  2. Subprocess mode — fork+pipe single-use server (legacy). Pass
     `use_daemon=False` to opt in.

Usage (library):
    from npi_server_client import NpiServerClient
    DAIDIR = "/path/to/vcs_sim_exe.daidir"
    with NpiServerClient(daidir=DAIDIR) as srv:        # spawn or connect daemon
        print(srv.query({"cmd": "ping"}))

    # Next script:
    with NpiServerClient(daidir=DAIDIR) as srv:        # reuse same daemon (instant)
        print(srv.query({"cmd": "handle_by_name", "signal": "tb..."}))

To explicitly stop the daemon:
    NpiServerClient(daidir=DAIDIR).shutdown_daemon()

CLI (one-shot query):
    python3 npi_server_client.py --daidir <path> --cmd ping
    python3 npi_server_client.py --daidir <path> --shutdown-daemon
"""

import argparse
import fcntl
import hashlib
import json
import os
import socket
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER_PY = os.path.join(_HERE, "npi_rtl_server.py")

if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _default_socket_path(daidir: str) -> str:
    """Derive a stable AF_UNIX socket path from the absolute daidir path.

    /tmp is per-host but that's fine for AF_UNIX (same-host transport by
    definition). md5(daidir)[:12] = one daemon per daidir per user.
    """
    abs_d = os.path.abspath(daidir) if daidir else "default"
    h = hashlib.md5(abs_d.encode()).hexdigest()[:12]
    user = os.environ.get("USER", "user")
    return f"/tmp/npi_server_{user}_{h}.sock"


# ── TCP transport helpers (used when transport='tcp' / NPI_TRANSPORT=tcp) ─────
#
# See the long header comment in npi_rtl_server.py near `_serve_tcp` for the
# WHY (cross-host bsub'd daemon + NFS rendezvous + token auth). This client side
# just consumes that contract:
#   1. Look up the rendezvous file on shared NFS.
#   2. Read <hostname>\n<port>\n<pid>\n<token>.
#   3. TCP connect with a short timeout (treat refused/timeout as stale → bail).
#   4. Send {"cmd":"auth","token":"..."} as the FIRST message.
#   5. Read {"status":"ready",...} → proceed with normal command loop.
#
# Auto-spawn policy in TCP mode: we DO NOT auto-spawn the daemon. The daidir is
# typically 15-40 GB and only fits on a beefy LSF exec host — a random login
# host can't host it. The client raises a clear error telling the user how to
# bsub the daemon instead. This mirrors fsdb_skill's behavior.
#
# Path convention: rendezvous file lives in the cwd at daemon start (typically
# the test runout). The client uses its OWN cwd to derive the default path,
# so both must be invoked from the same dir, OR the client must pass
# --rendezvous PATH explicitly (or set $NPI_RENDEZVOUS_PATH).

def _default_transport() -> str:
    """Auto-detect default transport based on LSF job context.

    Rule (mirrors server's _default_transport()):
      - Inside an LSF job ($LSB_JOBID set) → 'unix' (AF_UNIX zero-config)
      - Login host / devsrv / container → 'tcp' (multi-session / multi-host
        reuse)

    Override: client kwarg `transport=` > $NPI_TRANSPORT env > this default.
    """
    return "unix" if os.environ.get("LSB_JOBID") else "tcp"


def _detect_user_mem_limit_mb():
    """Return per-user memory quota in MB, or None if no quota enforced.

    Tries cgroups v2 first, then v1. Login hosts at AMD enforce per-user
    cgroups limits (e.g. atletx8-neu022 = 16 GiB / user). Return value is
    used to decide whether we can safely auto-spawn a 15-40 GB NPI daemon
    on the current host vs forcing the user to bsub it.
    """
    uid = os.getuid()
    # cgroups v2
    for p in [f"/sys/fs/cgroup/user.slice/user-{uid}.slice/memory.max",
              "/sys/fs/cgroup/memory.max"]:
        try:
            v = open(p).read().strip()
            if v == "max":
                return None
            return int(v) // (1024 * 1024)
        except (FileNotFoundError, PermissionError, ValueError):
            continue
    # cgroups v1
    for p in [f"/sys/fs/cgroup/memory/user.slice/user-{uid}.slice/memory.limit_in_bytes",
              "/sys/fs/cgroup/memory/memory.limit_in_bytes"]:
        try:
            v = int(open(p).read().strip())
            # v1 uses a huge sentinel for "no limit" (often 2^63-1, page-aligned)
            if v > (1 << 60):
                return None
            return v // (1024 * 1024)
        except (FileNotFoundError, PermissionError, ValueError):
            continue
    return None


def _estimate_daidir_rss_mb(daidir: str):
    """Estimate daemon RSS at full load = du(daidir) * 2.0.

    Empirical baseline: mango_nodf 11 GB on disk → 11.8 GB max RSS (LSF
    report = ratio 1.07). But that's just the bind-time RSS — RSS keeps
    growing as NPI lazily materializes symbol tables on demand (each
    handle_by_name / trace_driver call can pull more into memory). The 2.0x
    multiplier is a deliberately conservative cap that survives heavy query
    workloads without OOM, at the cost of refusing some daidirs that would
    actually fit. Returns None if du fails (caller treats unknown as fit).
    """
    try:
        import subprocess
        out = subprocess.check_output(["du", "-sb", daidir], stderr=subprocess.DEVNULL).decode()
        bytes_ = int(out.split()[0])
        return int(bytes_ * 2.0 // (1024 * 1024))
    except Exception:
        return None


def _default_rendezvous_path(daidir: str) -> str:
    """Endpoint file path. Override priority:

        --rendezvous flag  >  $NPI_RENDEZVOUS_PATH env  >  cwd default

    Default = <cwd>/.npi_server_<user>_<md5(daidir)>.endpoint.

    Matches fsdb_skill convention: launch the daemon (typically via bsub) from
    the test runout directory; the rendezvous file lives right there. The
    client must be invoked from the SAME cwd (or pass --rendezvous explicitly /
    set $NPI_RENDEZVOUS_PATH) to find it.

    Dot-prefix so it doesn't clutter `ls`. md5(daidir)[:12] in the name still
    guarantees one daemon per daidir per user when multiple daemons share a cwd.
    """
    abs_d = os.path.abspath(daidir) if daidir else "default"
    h = hashlib.md5(abs_d.encode()).hexdigest()[:12]
    user = os.environ.get("USER", "user")
    return os.path.join(os.getcwd(), f".npi_server_{user}_{h}.endpoint")


def _read_endpoint(path: str):
    """Parse rendezvous file. Returns (host, port, pid, token) or None if absent
    or malformed. Tolerant of trailing whitespace / extra blank lines.
    """
    try:
        with open(path) as f:
            lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
    except (FileNotFoundError, PermissionError):
        return None
    if len(lines) < 4:
        return None
    try:
        host, port_s, pid_s, token = lines[0], lines[1], lines[2], lines[3]
        return host, int(port_s), int(pid_s), token
    except ValueError:
        return None


class NpiServerClient:
    """
    Context-manager client for npi_rtl_server.py.

    Parameters
    ----------
    daidir : str
        Path to vcs_sim_exe.daidir or the build_size pointer file.
    top : str
        Optional top module override.
    startup_timeout : float
        Seconds to wait for daemon to print ready (only when spawning new one).
    server_py : str
        Path to npi_rtl_server.py.
    socket_path : str or None
        Unix socket path. If None and use_daemon=True, derived from daidir.
    use_daemon : bool
        True (default) = use Unix-socket daemon (fast reuse).
        False = fork single-use subprocess (legacy stdin/stdout).
    """

    def __init__(self, daidir: str = "", top: str = "",
                 startup_timeout: float = 240.0,
                 server_py: str = _SERVER_PY,
                 socket_path: str = None,
                 use_daemon: bool = True,
                 transport: str = None,
                 rendezvous_path: str = None):
        """
        transport : 'unix' | 'tcp' | None
            None → take from $NPI_TRANSPORT env, default 'unix'.
            'unix' = AF_UNIX same-host (original; auto-spawns daemon).
            'tcp'  = AF_INET cross-host (does NOT auto-spawn — user must bsub).
        rendezvous_path : str | None
            TCP mode only; default derived from md5(daidir).
        """
        self.daidir = daidir
        self.top    = top
        self.startup_timeout = startup_timeout
        self.server_py = server_py
        self.use_daemon = use_daemon

        # Resolve transport: arg > $NPI_TRANSPORT env > auto-detect.
        # Auto = 'unix' inside LSF job, 'tcp' on login host.
        self.transport = (transport
                          or os.environ.get("NPI_TRANSPORT")
                          or _default_transport())
        if self.transport not in ("unix", "tcp"):
            raise ValueError(f"Invalid transport: {self.transport!r}")

        # AF_UNIX path (only meaningful for transport='unix')
        if socket_path is None and use_daemon and self.transport == "unix":
            socket_path = _default_socket_path(daidir)
        self.socket_path = socket_path

        # TCP rendezvous path (only meaningful for transport='tcp').
        # Priority: explicit arg > $NPI_RENDEZVOUS_PATH env > cwd default.
        if rendezvous_path is None and use_daemon and self.transport == "tcp":
            rendezvous_path = (os.environ.get("NPI_RENDEZVOUS_PATH", "")
                               or _default_rendezvous_path(daidir))
        self.rendezvous_path = rendezvous_path
        # TCP endpoint state, populated lazily on connect
        self._tcp_host = None
        self._tcp_port = None
        self._tcp_token = None

        # State
        self._sock = None              # socket.socket if daemon mode (unix or tcp)
        self._proc = None              # subprocess.Popen if subprocess mode
        self._spawned_daemon = False   # whether THIS client spawned the daemon
        self._stderr_fh = None
        self._stderr_log = None

    # ── context manager ───────────────────────────────────────────────────────

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        if self.use_daemon:
            if self.transport == "tcp":
                self._start_daemon_tcp()
            else:
                self._start_daemon()
        else:
            self._start_subprocess()

    # --- daemon mode -------------------------------------------------------

    def _try_connect(self) -> bool:
        """Try connecting to an existing daemon socket. Read its 'ready' line."""
        if not os.path.exists(self.socket_path):
            return False
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(3.0)
            s.connect(self.socket_path)
        except Exception:
            return False
        # Read ready line
        try:
            buf = b""
            deadline = time.time() + 3.0
            while b"\n" not in buf and time.time() < deadline:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            line, _ = (buf.split(b"\n", 1) + [b""])[:2]
            obj = json.loads(line.decode().strip())
            if obj.get("status") == "ready":
                s.settimeout(None)
                self._sock = s
                return True
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass
        return False

    def _start_daemon(self):
        # Try existing daemon first (cheap path, no lock needed)
        if self._try_connect():
            print(f"[client] Reusing existing daemon at {self.socket_path}",
                  file=sys.stderr, flush=True)
            return

        # Acquire spawn lock so concurrent clients don't each spawn a daemon
        # (each daemon loads daidir = ~15-40 GB; 5 stale daemons = OOM kill).
        lock_path = self.socket_path + ".spawn.lock"
        lock_fh = open(lock_path, "w")
        spawn_t0 = time.time()
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        except Exception as e:
            lock_fh.close()
            raise RuntimeError(f"Failed to acquire spawn lock {lock_path}: {e}")

        waited = time.time() - spawn_t0
        if waited > 0.5:
            # Someone else just held the lock — likely they spawned the daemon.
            # Re-try connect before spawning our own.
            print(f"[client] Waited {waited:.1f}s for spawn lock; "
                  f"another client may have spawned daemon, retrying connect",
                  file=sys.stderr, flush=True)
            if self._try_connect():
                print(f"[client] Reusing daemon spawned by another client at {self.socket_path}",
                      file=sys.stderr, flush=True)
                try:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                    lock_fh.close()
                except Exception:
                    pass
                return

        # We hold the lock; spawn the daemon
        cmd = [sys.executable, self.server_py, "--socket", self.socket_path]
        if self.daidir:
            cmd += ["--daidir", self.daidir]
        if self.top:
            cmd += ["--top", self.top]

        print(f"[client] Spawning new daemon: {' '.join(cmd)}",
              file=sys.stderr, flush=True)
        try:
            self._stderr_log = os.path.join(
                os.environ.get("TMPDIR", "/tmp"),
                f"npi_daemon_{os.getpid()}.stderr.log",
            )
            self._stderr_fh = open(self._stderr_log, "w")

            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,    # daemon prints ready JSON on stdout
                stderr=self._stderr_fh,
                text=True,
                bufsize=1,
                start_new_session=True,    # detach from controlling terminal
            )
            self._spawned_daemon = True

            # Wait for daemon to print {"status":"ready"} on stdout
            deadline = time.time() + self.startup_timeout
            ready = False
            while time.time() < deadline:
                line = self._proc.stdout.readline()
                if not line:
                    if self._proc.poll() is not None:
                        raise RuntimeError(
                            f"Daemon exited before becoming ready. "
                            f"See stderr log: {self._stderr_log}"
                        )
                    continue
                try:
                    obj = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                if obj.get("status") == "ready":
                    ready = True
                    break
                if obj.get("status") == "error":
                    raise RuntimeError(f"Daemon startup error: {obj.get('msg','?')}")
            if not ready:
                raise TimeoutError(
                    f"Daemon did not become ready in {self.startup_timeout}s"
                )

            # Now connect to its socket (small retry loop, socket may be 1-2 ms behind)
            for _ in range(30):
                if self._try_connect():
                    print(f"[client] Daemon ready, connected at {self.socket_path}",
                          file=sys.stderr, flush=True)
                    return
                time.sleep(0.1)
            raise RuntimeError(
                f"Daemon reported ready but socket not connectable: {self.socket_path}"
            )
        finally:
            # Release spawn lock so any waiting peers can proceed (they'll find
            # our just-spawned daemon via _try_connect and reuse it).
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                lock_fh.close()
            except Exception:
                pass

    # --- daemon mode: TCP transport ---------------------------------------
    #
    # Cross-host variant: client just connects, does NOT auto-spawn (because
    # 15-40 GB RSS won't fit on most login hosts — daemon must be bsub'd
    # explicitly to a beefy LSF exec host).
    #
    # Spawn lock for tcp mode lives in the same NFS rendezvous dir, so
    # concurrent clients across hosts coordinate (NFSv3+local_lock=none gives
    # real cross-host fcntl.flock via NLM). The lock only matters as a future
    # extension; the current implementation does not auto-spawn so it's purely
    # advisory — left in place so a future "auto-bsub" feature can drop in.

    def _try_connect_tcp(self) -> bool:
        """Read rendezvous file, AF_INET connect, send auth, read ready line.

        Returns True if a live authenticated session is established.
        Returns False if the endpoint is missing/stale/refusing — caller can
        decide whether to bail (current policy) or spawn (future).

        Stale-endpoint cleanup: if connect refuses or times out we DON'T
        unlink the endpoint file ourselves — the daemon may just be restarting,
        and racing another client to unlink causes flapping. Instead, the
        daemon is responsible for cleanup on graceful exit; on hard crashes,
        the user re-bsub's, which overwrites the file.
        """
        ep = _read_endpoint(self.rendezvous_path)
        if ep is None:
            return False
        host, port, _pid, token = ep
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3.0)   # short — daemon is either alive or not
        try:
            s.connect((host, port))
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            try: s.close()
            except Exception: pass
            print(f"[client] TCP connect to {host}:{port} failed ({e}); "
                  f"treating endpoint as stale", file=sys.stderr, flush=True)
            return False
        # Send auth as first message BEFORE expecting ready.
        try:
            s.sendall((json.dumps({"cmd": "auth", "token": token}) + "\n").encode())
        except OSError:
            try: s.close()
            except Exception: pass
            return False
        # Read response: should be {"status":"ready",...} on success or
        # {"error":"Auth failed"} on token mismatch.
        s.settimeout(10.0)
        buf = b""
        try:
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    raise OSError("server closed connection during auth")
                buf += chunk
        except (socket.timeout, OSError) as e:
            print(f"[client] TCP auth handshake failed: {e}",
                  file=sys.stderr, flush=True)
            try: s.close()
            except Exception: pass
            return False
        first_line, _ = buf.split(b"\n", 1)
        try:
            obj = json.loads(first_line.decode().strip())
        except json.JSONDecodeError:
            try: s.close()
            except Exception: pass
            return False
        if obj.get("status") != "ready":
            print(f"[client] TCP auth rejected: {obj.get('error', obj)}",
                  file=sys.stderr, flush=True)
            try: s.close()
            except Exception: pass
            return False
        # Auth OK. Restore blocking mode for the long-running session.
        s.settimeout(None)
        self._sock = s
        self._tcp_host, self._tcp_port, self._tcp_token = host, port, token
        return True

    def _start_daemon_tcp(self):
        """TCP mode: connect existing, else auto-spawn locally if budget allows.

        Decision flow:
          1. _try_connect_tcp() → if a live daemon answers, reuse it (done).
          2. No live daemon → check whether the CURRENT host can hold a fresh
             one without blowing the user's mem quota:
                est_rss = du(daidir) * 2.0
                free    = user_cgroup_quota_mb - HEADROOM_MB   (default 2048)
                fits    = est_rss <= free
          3. fits → _spawn_tcp_locally() (Popen, wait for ready, connect).
             not  → raise with bsub-template instructions.

        Overrides:
          NPI_FORCE_LOCAL_SPAWN=1 → skip budget check, always spawn locally
          NPI_FORCE_LOCAL_SPAWN=0 → never spawn locally, always refuse → bsub
          NPI_LOCAL_SPAWN_HEADROOM_MB=<N> → adjust safety headroom (default 2048)
        """
        if self._try_connect_tcp():
            print(f"[client] Reusing TCP daemon at {self._tcp_host}:{self._tcp_port} "
                  f"(rendezvous={self.rendezvous_path})",
                  file=sys.stderr, flush=True)
            return

        # ── decide: local spawn vs refuse ──
        force = os.environ.get("NPI_FORCE_LOCAL_SPAWN", "")
        headroom = int(os.environ.get("NPI_LOCAL_SPAWN_HEADROOM_MB", "2048"))
        est_rss = _estimate_daidir_rss_mb(self.daidir)
        quota = _detect_user_mem_limit_mb()

        if force == "1":
            decision = "spawn (forced via NPI_FORCE_LOCAL_SPAWN=1)"
            fits = True
        elif force == "0":
            decision = "refuse (forced via NPI_FORCE_LOCAL_SPAWN=0)"
            fits = False
        elif quota is None:
            decision = "spawn (no cgroup quota detected — assuming host has room)"
            fits = True
        elif est_rss is None:
            decision = f"spawn (cannot du daidir to estimate RSS — quota={quota} MB)"
            fits = True
        else:
            available = quota - headroom
            fits = est_rss <= available
            decision = (f"{'spawn' if fits else 'refuse'} "
                        f"(est_rss={est_rss}MB, quota={quota}MB, "
                        f"headroom={headroom}MB, available={available}MB)")

        print(f"[client] No live TCP daemon; budget decision: {decision}",
              file=sys.stderr, flush=True)

        if fits:
            self._spawn_tcp_locally()
            return

        # Refuse with actionable error.
        raise RuntimeError(
            f"Cannot auto-spawn TCP daemon on this host — would exceed memory quota.\n"
            f"  daidir:           {self.daidir}\n"
            f"  rendezvous:       {self.rendezvous_path}\n"
            f"  estimated RSS:    {est_rss} MB  (= du(daidir) * 2.0)\n"
            f"  user mem quota:   {quota} MB    (from cgroups)\n"
            f"  required headroom:{headroom} MB (NPI_LOCAL_SPAWN_HEADROOM_MB)\n"
            f"\n"
            f"  Fix — bsub the daemon to a beefy LSF exec host:\n"
            f"    cd $(dirname '{self.rendezvous_path}')\n"
            f"    {os.path.dirname(self.server_py)}/scripts/bsub_npi_tcp.csh "
            f"{self.daidir}\n"
            f"\n"
            f"  Or override:\n"
            f"    NPI_FORCE_LOCAL_SPAWN=1 ...   # spawn anyway (risk OOM)\n"
            f"    NPI_LOCAL_SPAWN_HEADROOM_MB=512 ...  # shrink safety margin"
        )

    def _spawn_tcp_locally(self):
        """Popen npi_rtl_server with --transport tcp; wait for ready + rendezvous.

        Mirrors _start_daemon (AF_UNIX path) closely but passes TCP-specific
        args and after 'ready' polls for rendezvous file to appear before
        the auth-handshake connect.
        """
        cmd = [sys.executable, self.server_py,
               "--transport", "tcp",
               "--rendezvous", self.rendezvous_path]
        if self.daidir:
            cmd += ["--daidir", self.daidir]
        if self.top:
            cmd += ["--top", self.top]

        # Stderr log next to rendezvous so it travels with the daemon trace.
        self._stderr_log = self.rendezvous_path + ".spawnlog"
        self._stderr_fh = open(self._stderr_log, "w")
        print(f"[client] Spawning TCP daemon locally: {' '.join(cmd)}",
              file=sys.stderr, flush=True)
        print(f"[client]   stderr log: {self._stderr_log}",
              file=sys.stderr, flush=True)

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=self._stderr_fh,
            text=True,
            bufsize=1,
            start_new_session=True,    # detach from controlling terminal
        )
        self._spawned_daemon = True

        # Wait for {"status":"ready"} on stdout.
        deadline = time.time() + self.startup_timeout
        ready = False
        while time.time() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                if self._proc.poll() is not None:
                    raise RuntimeError(
                        f"Daemon exited before becoming ready. "
                        f"See stderr log: {self._stderr_log}"
                    )
                continue
            try:
                obj = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if obj.get("status") == "ready":
                ready = True
                break
            if obj.get("status") == "error":
                raise RuntimeError(f"Daemon startup error: {obj.get('msg','?')}")
        if not ready:
            raise TimeoutError(f"Daemon did not become ready in {self.startup_timeout}s")

        # Server wrote rendezvous AFTER emitting 'ready' (in _serve_tcp's bind+write).
        # Poll briefly until rendezvous file appears, then connect+auth.
        for _ in range(50):
            if os.path.exists(self.rendezvous_path) and self._try_connect_tcp():
                print(f"[client] Locally-spawned daemon ready at "
                      f"{self._tcp_host}:{self._tcp_port}",
                      file=sys.stderr, flush=True)
                return
            time.sleep(0.1)
        raise RuntimeError(
            f"Daemon reported ready but rendezvous file never appeared or "
            f"auth failed: {self.rendezvous_path}"
        )

    # --- subprocess mode (legacy) -----------------------------------------

    def _start_subprocess(self):
        cmd = [sys.executable, self.server_py]
        if self.daidir:
            cmd += ["--daidir", self.daidir]
        if self.top:
            cmd += ["--top", self.top]

        print(f"[client] Starting subprocess server: {' '.join(cmd)}",
              file=sys.stderr, flush=True)

        self._stderr_log = os.path.join(
            os.environ.get("TMPDIR", "/tmp"),
            f"npi_server_{os.getpid()}.stderr.log",
        )
        self._stderr_fh = open(self._stderr_log, "w")

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_fh,
            text=True,
            bufsize=1,
        )

        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                raise RuntimeError("Server exited before printing ready/error")
            try:
                obj = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            status = obj.get("status", "")
            if status == "ready":
                print(f"[client] Server ready. daidir={obj.get('daidir','?')!r}",
                      file=sys.stderr, flush=True)
                return
            if status == "error":
                raise RuntimeError(f"Server startup error: {obj.get('msg','?')}")
        raise TimeoutError(f"Server did not become ready within {self.startup_timeout}s")

    # ── stop ─────────────────────────────────────────────────────────────

    def stop(self):
        """Disconnect from server. In daemon mode, leaves daemon running."""
        # Daemon mode: just disconnect socket
        if self._sock is not None:
            try:
                self._sock.sendall((json.dumps({"cmd": "quit"}) + "\n").encode())
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

        # Subprocess mode (legacy): also send quit and reap process
        if self._proc is not None and not self._spawned_daemon:
            try:
                if self._proc.stdin and not self._proc.stdin.closed:
                    self._proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                    self._proc.stdin.flush()
                    self._proc.stdin.close()
                self._proc.wait(timeout=10)
            except Exception:
                self._proc.kill()
            self._proc = None

        # Daemon stays alive — only close OUR stderr handle
        if self._stderr_fh:
            self._stderr_fh.close()
            self._stderr_fh = None

        # Report log path only if non-empty (else delete)
        if self._stderr_log:
            try:
                if os.path.getsize(self._stderr_log) == 0:
                    os.remove(self._stderr_log)
                else:
                    print(f"[client] Server stderr log: {self._stderr_log}",
                          file=sys.stderr, flush=True)
            except OSError:
                pass

    def shutdown_daemon(self):
        """Send 'shutdown' command to kill the daemon (if connected).

        Works for both transports: unix uses _try_connect (AF_UNIX), tcp uses
        _try_connect_tcp (AF_INET + auth handshake).
        """
        if not self.use_daemon:
            return self.stop()
        # Connect if not already
        if self._sock is None:
            connected = (self._try_connect_tcp() if self.transport == "tcp"
                         else self._try_connect())
            if not connected:
                target = (self.rendezvous_path if self.transport == "tcp"
                          else self.socket_path)
                print(f"[client] No {self.transport} daemon running at {target}",
                      file=sys.stderr, flush=True)
                return
        try:
            self._sock.sendall((json.dumps({"cmd": "shutdown"}) + "\n").encode())
            # Read ack
            buf = b""
            self._sock.settimeout(5.0)
            while b"\n" not in buf:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            print(f"[client] Daemon shutdown requested", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[client] Shutdown error: {e}", file=sys.stderr, flush=True)
        finally:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    # ── eval helper ──────────────────────────────────────────────────────

    def eval(self, code: str, timeout: float = 60.0) -> dict:
        return self.query({"cmd": "eval", "code": code}, timeout=timeout)

    # ── query ─────────────────────────────────────────────────────────────

    def query(self, request: dict, timeout: float = 120.0) -> dict:
        """Send one JSON request, return parsed JSON response."""
        if self._sock is not None:
            return self._query_socket(request, timeout)
        if self._proc is not None:
            return self._query_subprocess(request, timeout)
        raise RuntimeError("Not connected. Call start() first.")

    def _query_socket(self, request: dict, timeout: float) -> dict:
        line = json.dumps(request) + "\n"
        t_send = time.time()
        self._sock.sendall(line.encode())
        self._sock.settimeout(timeout)
        buf = b""
        try:
            while b"\n" not in buf:
                chunk = self._sock.recv(65536)
                if not chunk:
                    raise RuntimeError("Daemon closed connection during query")
                if not buf:
                    # First byte received — measure how long we waited in queue
                    wait = time.time() - t_send
                    if wait > 5.0:
                        # Daemon was busy serving another concurrent client.
                        # Tell the AI explicitly so it knows to be patient instead
                        # of assuming hang and bailing out.
                        print(f"[client] NPI query waited {wait:.1f}s before first "
                              f"byte (daemon serializes concurrent clients; "
                              f"please wait — this is queue, not hang). "
                              f"cmd={request.get('cmd')!r}",
                              file=sys.stderr, flush=True)
                buf += chunk
        except socket.timeout:
            raise TimeoutError(f"No response after {timeout}s for cmd={request.get('cmd')!r}")
        resp_line, _ = buf.split(b"\n", 1)
        try:
            return json.loads(resp_line.decode().strip())
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Daemon returned invalid JSON: {e}\nRaw: {resp_line[:200]}")

    def _query_subprocess(self, request: dict, timeout: float) -> dict:
        line = json.dumps(request) + "\n"
        self._proc.stdin.write(line)
        self._proc.stdin.flush()

        import select
        deadline = time.time() + timeout
        buf = ""
        while time.time() < deadline:
            rlist, _, _ = select.select([self._proc.stdout], [], [], 1.0)
            if not rlist:
                if self._proc.poll() is not None:
                    raise RuntimeError("Server exited unexpectedly during query")
                continue
            chunk = self._proc.stdout.readline()
            if not chunk:
                raise RuntimeError("Server closed stdout during query")
            buf = chunk.strip()
            if buf:
                break
        else:
            raise TimeoutError(f"No response after {timeout}s for cmd={request.get('cmd')!r}")

        try:
            return json.loads(buf)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Server returned invalid JSON: {e}\nRaw: {buf[:200]}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="npi_server_client — CLI wrapper for npi_rtl_server.py",
    )
    ap.add_argument("--daidir", default="",
                    help="Path to daidir or build_size pointer file")
    ap.add_argument("--top", default="", help="Top module override")
    ap.add_argument("--cmd", help="Command to send (e.g. ping, handle_by_name)")
    ap.add_argument("--scope", default="", help="Scope (for find_signal/get_filelist)")
    ap.add_argument("--pattern", default="*", help="Signal glob pattern")
    ap.add_argument("--signal", default="", help="Full signal name")
    ap.add_argument("--depth", type=int, default=5, help="Max trace depth")
    ap.add_argument("--socket", default=None,
                    help="AF_UNIX socket path (default: auto from daidir; "
                         "unix transport only)")
    ap.add_argument("--transport", choices=["unix", "tcp"], default=None,
                    help="Transport selection: 'unix' (default, AF_UNIX same-host) "
                         "or 'tcp' (AF_INET cross-host, requires pre-bsub'd daemon). "
                         "Defaults to $NPI_TRANSPORT or 'unix'.")
    ap.add_argument("--rendezvous", default=None,
                    help="TCP mode only: full path to endpoint file. "
                         "Default: <cwd>/.npi_server_<user>_<md5(daidir)>.endpoint "
                         "(override via $NPI_RENDEZVOUS_PATH).")
    ap.add_argument("--no-daemon", action="store_true",
                    help="Use legacy subprocess mode (each call spawns new server)")
    ap.add_argument("--shutdown-daemon", action="store_true",
                    help="Stop the running daemon for this daidir")
    ap.add_argument("--json-out", action="store_true",
                    help="Pretty-print JSON output")
    args = ap.parse_args()

    client = NpiServerClient(
        daidir=args.daidir, top=args.top,
        socket_path=args.socket,
        use_daemon=not args.no_daemon,
        transport=args.transport,
        rendezvous_path=args.rendezvous,
    )

    if args.shutdown_daemon:
        client.shutdown_daemon()
        return

    if not args.cmd:
        ap.error("--cmd required (or use --shutdown-daemon)")

    req = {"cmd": args.cmd}
    if args.scope:   req["scope"]   = args.scope
    if args.pattern: req["pattern"] = args.pattern
    if args.signal:  req["signal"]  = args.signal
    if args.depth:   req["depth"]   = args.depth

    with client as srv:
        result = srv.query(req)

    if args.json_out:
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(result))


if __name__ == "__main__":
    main()

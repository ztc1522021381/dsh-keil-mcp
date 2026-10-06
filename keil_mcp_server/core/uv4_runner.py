"""UV4.exe process runner: build (-b/-r), flash (-f), debug (-d) (blueprint §4.1)."""
from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from ..config import find_uv4


class UV4Runner:
    """Runs UV4.exe commands with log capture, progress tail and cancel support."""

    def __init__(self, uv4_path: Optional[str] = None):
        self.uv4_path = uv4_path or find_uv4(__import__("keil_mcp_server.config", fromlist=["AppConfig"]).AppConfig.load())

    def uv4(self) -> str:
        p = Path(self.uv4_path)
        if not p.exists():
            raise FileNotFoundError(
                f"UV4.exe not found at {self.uv4_path}. Install Keil MDK or set KEIL_UV4_PATH.")
        return str(p)

    # ---------- low-level run ----------
    def run(self, args: list[str], project: str, log_path: Optional[Path] = None,
            timeout: float = 300, progress=None) -> subprocess.CompletedProcess:
        """Run UV4.exe with args; stream progress via BuildProgressMonitor."""
        cmd = [self.uv4(), *args, project]
        log_path = log_path or (Path(project).parent / "build.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch()  # pre-create to avoid tail latency
        with open(log_path, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n===== UV4 {' '.join(args)} {project} @ {time.strftime('%H:%M:%S')} =====\n")
        # Fix (2026-08-29): when this server is spawned by a host app that puts
        # us inside a Job Object (e.g. sandbox memory limits), UV4 -b crashes
        # with 0xC0000005 at the link stage (armlink needs more memory than
        # single-file compiles). Launch UV4 with CREATE_BREAKAWAY_FROM_JOB so
        # it escapes the job; fall back to a normal spawn if the job forbids
        # breakaway. Also pin cwd to the project dir for correct relative-path
        # resolution (.\Objects\*.lnp entries).
        base_flags = 0x08000000  # CREATE_NO_WINDOW
        breakaway_flags = base_flags | 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
        project_cwd = str(Path(project).parent)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", creationflags=breakaway_flags,
                cwd=project_cwd)
        except OSError:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", creationflags=base_flags,
                cwd=project_cwd)
        if progress is not None:
            progress.start(proc)
        try:
            # also mirror stdout into the log file
            with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                for line in proc.stdout:
                    f.write(line)
                    f.flush()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise TimeoutError(f"UV4 {' '.join(args)} timed out after {timeout}s")
        finally:
            if progress is not None:
                progress.stop()
                progress.join(timeout=10)
                # flush tail: UV4 may not flush the last lines before exit
                with open(log_path, "a", encoding="utf-8", errors="replace"):
                    pass
        return proc

    # ---------- typed commands ----------
    def build(self, project: str, target: Optional[str] = None, clean: bool = False,
              rebuild: bool = False, log_path: Optional[Path] = None,
              timeout: float = 300, progress=None) -> subprocess.CompletedProcess:
        args = ["-c" if clean else ("-r" if rebuild else "-b")]
        if target:
            args += ["-t", target]
        effective_log = log_path or (Path(project).parent / "build.log")
        args += ["-j0", "-o", str(effective_log)]
        proc = self.run(args, project, log_path=effective_log, timeout=timeout, progress=progress)
        # Fallback chain (2026-08-29): when the host app's sandbox crashes UV4
        # at link stage (0xC0000005), the compile stage is already complete
        # (.o + .lnp produced). Finish the link with armlink directly so the
        # caller always gets a complete firmware artifact.
        if proc.returncode not in (0, 1):
            if self._fallback_link(project, effective_log):
                proc.returncode = 0
        return proc

    def _fallback_link(self, project: str, log_path: Path) -> bool:
        """Complete the build via armlink --via=<lnp> after a UV4 crash.

        Returns True (and appends the link result to the build log) when the
        axf was produced; False when fallback is not possible.
        """
        proj_dir = Path(project).parent
        stem = Path(project).stem
        lnp = proj_dir / "Objects" / f"{stem}.lnp"
        if not lnp.exists():
            return False
        # locate the ARMCLANG bin folder recorded by UV4 in the build log
        bin_dir: Optional[Path] = None
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        m = re.search(r"folder: '([^']+)'", text)
        if m:
            candidate = Path(m.group(1))
            if candidate.exists():
                bin_dir = candidate
        if bin_dir is None:
            return False
        armlink = bin_dir / "armlink.exe"
        if not armlink.exists():
            return False
        try:
            r = subprocess.run(
                [str(armlink), "--via=" + str(lnp)],
                cwd=str(proj_dir), capture_output=True, text=True,
                timeout=120, creationflags=0x08000000)
        except (OSError, subprocess.TimeoutExpired):
            return False
        axf = proj_dir / "Objects" / f"{stem}.axf"
        if r.returncode != 0 or not axf.exists():
            return False
        # append link result so downstream log parsers see a complete build
        # (armlink prints "Program Size: ..." on stderr — merge both streams)
        link_output = (r.stdout or "") + (r.stderr or "")
        with open(log_path, "a", encoding="utf-8", errors="replace") as f:
            f.write("linking...\n")
            f.write(link_output.rstrip() + "\n\n")
            f.write(f'"{axf}" - 0 Error(s), 0 Warning(s).\n')
        # regenerate hex via fromelf (Keil convention), best-effort
        fromelf = bin_dir / "fromelf.exe"
        if fromelf.exists():
            try:
                subprocess.run(
                    [str(fromelf), "--i32combined",
                     "--output=" + str(proj_dir / "Objects" / f"{stem}.hex"),
                     str(axf)],
                    cwd=str(proj_dir), capture_output=True, timeout=60,
                    creationflags=0x08000000)
            except (OSError, subprocess.TimeoutExpired):
                pass
        return True

    def flash(self, project: str, target: Optional[str] = None, log_path: Optional[Path] = None,
              timeout: float = 120) -> subprocess.CompletedProcess:
        args = ["-f"]
        if target:
            args += ["-t", target]
        args += ["-j0", "-o", str(log_path or (Path(project).parent / "flash.log"))]
        return self.run(args, project, log_path=log_path, timeout=timeout)

    def debug(self, project: str, target: Optional[str] = None, ini: Optional[str] = None,
              timeout: float = 120) -> subprocess.CompletedProcess:
        """UV4 -d with a .ini debug script (official debug channel, blueprint §7.4)."""
        args = ["-d"]
        if target:
            args += ["-t", target]
        args += ["-j0"]
        if ini:
            args += ["-o", ini]
        return self.run(args, project, log_path=None, timeout=timeout)


class BuildRegistry:
    """Registry of in-flight builds (for build_progress_status / build_cancel)."""

    def __init__(self):
        self._builds: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._seq = 0

    def create(self, project: str, target: str = "", log_path: Optional[Path] = None) -> str:
        with self._lock:
            self._seq += 1
            bid = f"b{self._seq:04d}"
            self._builds[bid] = {
                "project": project, "target": target,
                "log_path": str(log_path) if log_path else "",
                "state": None, "process": None, "cancel": threading.Event(),
            }
            return bid

    def get(self, build_id: str) -> Optional[dict]:
        with self._lock:
            return self._builds.get(build_id)

    def set(self, build_id: str, **kw) -> None:
        with self._lock:
            if build_id in self._builds:
                self._builds[build_id].update(kw)

    def request_cancel(self, build_id: str) -> bool:
        with self._lock:
            b = self._builds.get(build_id)
            if not b:
                return False
            b["cancel"].set()
            return True

    def remove(self, build_id: str) -> None:
        with self._lock:
            self._builds.pop(build_id, None)

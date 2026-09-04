#!/usr/bin/env python3
"""The Novix Mac runner: the one lane that runs outside the E2B container.

WHY THIS EXISTS. Novix verifies a drafted patch by cloning the repo and running the
customer's own build and tests in a Linux container. Two things it can never do there:
compile an Xcode target, and photograph a page in real Safari. Both need real Apple
hardware — an E2B sandbox is a Firecracker microVM with no nested virtualization, so
there is no ``/dev/kvm`` to hand QEMU, and Apple's licence permits macOS on Apple
hardware only. See ``backend/app/apple_ci.py``; that decision is settled and this
script is the other half of it.

WHY IT POLLS OUT. This Mac sits on a home network. Polling needs no port forwarding,
no static IP, no tunnel and no SSH key stored in Render — the shape every CI runner
uses. Novix never calls this machine; this machine asks Novix for work.

IT IS STDLIB ONLY, ON PURPOSE. No pip install, no virtualenv to keep alive, nothing to
break when macOS moves Python. The system ``python3`` that ships with the Command Line
Tools runs it as it stands.

FOUR RULES THIS SCRIPT HOLDS, and each one is a way the feature could quietly lie:

  1. **NEVER SUBSTITUTE A BROWSER.** If Safari cannot be driven, no picture is taken.
     A Chrome picture filed under a Safari label is evidence of something that never
     happened, which is worse than no evidence at all.
  2. **A THING THAT DID NOT RUN REPORTS NOTHING, NEVER A FAILURE.** ``passed``,
     ``buildOk`` and ``testOk`` are tri-state all the way to the server, and a step
     that was skipped sends null. Novix re-drafts a patch on a False, so reporting
     "we did not run the tests" as a test failure would burn a deep-tier redraft on a
     working patch.
  3. **THE REVERT PROVES ITSELF.** The "before" picture is only taken once ``git``
     confirms the tree actually moved. A checkout of an unmodified file exits 0, so a
     command built only out of reverts succeeds whether or not anything changed — and
     the "before" would then be a photograph of the patched app under the wrong label.
  4. **THE CLONE TOKEN NEVER REACHES A LOG.** It is handed over so this machine can
     clone a private repository; every line posted back runs through :func:`scrub`
     first. The server scrubs again on arrival, because a guard on the far side of a
     network boundary is not a guard — but that is a backstop, not a licence to be
     careless here.

Run it: ``NOVIX_URL=https://app.getnovix.ai NOVIX_MAC_RUNNER_TOKEN=... python3
novix_mac_runner.py``. See README.md for the LaunchAgent that keeps it running.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

# BUMP THIS WHENEVER THIS FILE CHANGES, and it is not bookkeeping. The public
# distribution manifest, the self-updater's forward-only decision and /api/health's
# fleet visibility all rest on this string. Two different programs reporting the same
# version makes a stale copy indistinguishable from a current one and prevents the
# updater from retrieving it — the exact silent drift automatic updates exist to end.
VERSION = "1.4.0"

# A PUBLIC, RUNNER-ONLY REPOSITORY is the update boundary. The main Novix repository
# is private, and giving this Mac a credential that can read the whole product just
# to fetch one stdlib file would turn convenience into unnecessary access. The
# publishing workflow copies only runner/ into that repository after the main gates
# pass. A checksum from its manifest is verified before the candidate is even parsed.
UPDATE_MANIFEST_URL = (
    "https://raw.githubusercontent.com/jakemoshel/Novix-Mac-Runner/main/manifest.json"
)
UPDATE_SOURCE_URL = (
    "https://raw.githubusercontent.com/jakemoshel/Novix-Mac-Runner/main/novix_mac_runner.py"
)
UPDATE_INTERVAL_SECONDS = 300
UPDATE_TIMEOUT_SECONDS = 30
UPDATE_MAX_BYTES = 1024 * 1024

# How long each external command may take. A cold xcodebuild on a fresh clone is
# minutes; the server's own job ceiling (NOVIX_MAC_RUNNER_JOB_TTL_SECONDS, 30 minutes
# by default) is what actually bounds the whole job, and these sit under it so one
# wedged command cannot eat the whole budget.
CLONE_TIMEOUT = 600
BUILD_TIMEOUT = 1500
TEST_TIMEOUT = 1500
SHOT_TIMEOUT = 120
# THE RUNNER STOPS BEFORE IT FILLS THE DISK. This machine is somebody's own Mac, not
# disposable infrastructure, and a full startup disk is the one failure that outlives
# the job that caused it — it breaks the Mac for its owner, not just for Novix. A job
# nobody claims expires as UNPROVEN, which is already the honest answer, so refusing
# to claim is strictly better than starting a clone that runs the volume dry.
MIN_FREE_DISK_GB = 20
SERVE_BOOT_TIMEOUT = 120

# HOW LONG THIS JOB MAY TAKE IN TOTAL, and the reason it is checked rather than hoped
# for. The server treats a claimed job older than its own ceiling as abandoned: it
# requeues it once and retires it the second time, and a result posted after that
# point is refused. So a pass that overruns does not lose the comparison, it loses
# the VERDICT -- every minute of Xcode this machine just spent goes in the bin. The
# ceiling arrives on the job as `deadlineSeconds`; this default is what a server too
# old to send it means.
JOB_BUDGET_SECONDS = 1800
# Held back so the result still has time to be posted inside the ceiling. A verdict
# nobody receives is the same as no verdict, at twice the price.
POST_RESERVE_SECONDS = 45
# Under this there is not enough left for a second xcodebuild to say anything useful,
# so the comparison is skipped rather than started. That leaves the patch UNPROVEN,
# which is exactly what an inconclusive baseline already means.
MIN_BASELINE_SECONDS = 300

# Log kept per command. The server caps the whole blob at 20k. A plain tail is not
# enough: xcodebuild prints the real Swift diagnostic before pages of compile-command
# and failure-summary noise (ticket 3cddab08), so :func:`tail` preserves diagnostic
# windows from anywhere in the output and spends the rest of the budget on the tail.
LOG_TAIL_CHARS = 8000
_DIAGNOSTIC = re.compile(
    r"(?i)(?:fatal error:|\berror:|undefined symbols?|test case .* failed|"
    r"failed assertion|\bfailures?:\b)"
)
_DIAGNOSTIC_CONTEXT_LINES = 2

POLL_IDLE_SECONDS = 20


# -- talking to Novix ------------------------------------------------------------


class Api:
    """Every request to Novix, in one place, so the token is added exactly once."""

    def __init__(self, base: str, token: str, runner_id: str, label: str):
        self.base = base.rstrip("/")
        self.token = token
        self.runner_id = runner_id
        self.label = label

    def _request(
        self, path: str, payload: Optional[dict] = None, *,
        body: Optional[bytes] = None, headers: Optional[dict] = None, timeout: int = 60,
    ) -> Tuple[int, Any]:
        url = f"{self.base}{path}"
        head = {"authorization": f"Bearer {self.token}"}
        head.update(headers or {})
        if body is None:
            body = json.dumps(payload or {}).encode()
            head["content-type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=head, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                raw = res.read()
                try:
                    return res.status, json.loads(raw)
                except Exception:
                    return res.status, raw
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            except Exception:
                return exc.code, None
        except Exception as exc:
            log(f"request to {path} failed: {exc.__class__.__name__}")
            return 0, None

    def checkin(self, capabilities: dict) -> Optional[dict]:
        status, data = self._request("/api/mac-runner/checkin", {
            "runnerId": self.runner_id, "label": self.label,
            "version": VERSION, "capabilities": capabilities,
        })
        if status == 404:
            log("this deployment has no Mac lane configured (NOVIX_MAC_RUNNER_TOKEN unset)")
        elif status == 401:
            log("the token was refused")
        return data if status == 200 and isinstance(data, dict) else None

    def claim(self, capabilities: dict) -> Optional[dict]:
        status, data = self._request("/api/mac-runner/claim", {
            "runnerId": self.runner_id, "label": self.label,
            "version": VERSION, "capabilities": capabilities,
        })
        if status != 200 or not isinstance(data, dict):
            return None
        job = data.get("job")
        return job if isinstance(job, dict) else None

    def upload_shot(self, job: dict, png: bytes) -> Optional[str]:
        boundary = uuid.uuid4().hex
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="file"; filename="shot.png"\r\n',
            b"Content-Type: image/png\r\n\r\n", png, b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        status, data = self._request(
            f"/api/mac-runner/jobs/{job['jobId']}/shot?ticketId={job['ticketId']}",
            body=body,
            headers={
                "content-type": f"multipart/form-data; boundary={boundary}",
                "x-novix-claim": job["claimToken"],
            },
            timeout=120,
        )
        if status != 200 or not isinstance(data, dict):
            log(f"a screenshot was not accepted (HTTP {status})")
            return None
        return data.get("fileId")

    def post_result(self, job: dict, result: dict) -> bool:
        status, data = self._request(f"/api/mac-runner/jobs/{job['jobId']}/result", {
            "ticketId": job["ticketId"], "claimToken": job["claimToken"], "result": result,
        })
        if status == 409:
            # Not an error worth retrying into. The commonest cause is honest: this
            # run took longer than the job ceiling, the job was re-queued, and
            # somebody else holds it now.
            log(f"the result was refused: {(data or {}).get('detail')}")
            return False
        if status != 200:
            log(f"the result was not accepted (HTTP {status})")
            return False
        return True


def log(message: str) -> None:
    print(f"[novix-mac-runner] {message}", flush=True)


# -- updating the installed runner ------------------------------------------------


def _version_tuple(value: str) -> Optional[Tuple[int, int, int]]:
    """A release version, or None. Deliberately narrower than a package parser."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(value or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def _manifest_fields(manifest_bytes: bytes) -> Tuple[Optional[str], Optional[str], str]:
    try:
        manifest = json.loads(manifest_bytes)
    except Exception:
        return None, None, "the update manifest is not valid JSON"
    if not isinstance(manifest, dict):
        return None, None, "the update manifest is not an object"
    version = str(manifest.get("version") or "").strip()
    wanted = str(manifest.get("sha256") or "").strip().lower()
    if _version_tuple(version) is None:
        return None, None, "the update manifest has an invalid version"
    if not re.fullmatch(r"[0-9a-f]{64}", wanted):
        return None, None, "the update manifest has an invalid checksum"
    return version, wanted, ""


def verified_update(
    manifest_bytes: bytes,
    source: bytes,
    current_version: str = VERSION,
) -> Tuple[Optional[str], str]:
    """Return the newer candidate version only when every distribution fact agrees.

    The manifest and source share a public repository, so the hash is an integrity
    check against partial publication and CDN skew, not a substitute for HTTPS. The
    version has to move forward: an old manifest may never roll a working machine
    backward, and changed source under the same version is refused because the fleet
    would otherwise report two different programs as identical.
    """
    version, wanted, invalid = _manifest_fields(manifest_bytes)
    if invalid or version is None or wanted is None:
        return None, invalid
    remote_tuple = _version_tuple(version)
    current_tuple = _version_tuple(current_version)
    if current_tuple is None:
        return None, "the update manifest has an invalid version"
    got = hashlib.sha256(source).hexdigest()
    if got != wanted:
        return None, "the downloaded runner does not match the published checksum"
    match = re.search(rb'^VERSION\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
    source_version = match.group(1).decode("ascii", "replace") if match else ""
    if source_version != version:
        return None, "the downloaded runner and manifest name different versions"
    if remote_tuple <= current_tuple:
        return None, "up to date" if remote_tuple == current_tuple else "refusing a downgrade"
    try:
        compile(source, "novix_mac_runner.py", "exec")
    except (SyntaxError, ValueError):
        return None, "the downloaded runner does not compile"
    return version, ""


def _installed_path() -> str:
    return os.path.join(os.path.expanduser("~"), "novix", "novix_mac_runner.py")


def _auto_update_enabled() -> bool:
    setting = os.environ.get("NOVIX_MAC_RUNNER_AUTO_UPDATE", "1").strip().lower()
    if setting in {"0", "false", "no", "off"}:
        return False
    # A source checkout is somebody's working tree, never an installation target.
    # The installer always lands here, so this also prevents a developer's --once
    # probe from rewriting the repository underneath them.
    return os.path.abspath(__file__) == os.path.abspath(_installed_path())


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": f"novix-mac-runner/{VERSION}"})
    with urllib.request.urlopen(request, timeout=UPDATE_TIMEOUT_SECONDS) as response:
        data = response.read(UPDATE_MAX_BYTES + 1)
    if len(data) > UPDATE_MAX_BYTES:
        raise ValueError("update payload is too large")
    return data


def maybe_self_update(last_check: List[float], *, force: bool = False) -> bool:
    """Install a published runner between jobs and replace this process.

    Returns False when no restart happened. A successful update never returns:
    ``execv`` keeps the LaunchAgent and worker identity intact. The update lock makes
    several workers on one Mac converge on one atomic file replacement; a worker
    that finds its sibling already installed the file simply restarts into it.
    """
    if not _auto_update_enabled():
        return False
    now = time.monotonic()
    if not force and last_check and now - last_check[0] < UPDATE_INTERVAL_SECONDS:
        return False
    if last_check:
        last_check[0] = now
    try:
        manifest = _download(UPDATE_MANIFEST_URL)
    except Exception as exc:
        log(f"automatic update check failed: {exc.__class__.__name__}")
        return False

    remote_version, _wanted, invalid = _manifest_fields(manifest)
    remote_tuple = _version_tuple(remote_version or "")
    current_tuple = _version_tuple(VERSION)
    if invalid:
        log(f"automatic update refused: {invalid}")
        return False
    if remote_tuple is None or current_tuple is None or remote_tuple <= current_tuple:
        return False
    try:
        source = _download(UPDATE_SOURCE_URL)
    except Exception as exc:
        log(f"automatic update download failed: {exc.__class__.__name__}")
        return False

    version, why = verified_update(manifest, source)
    if not version:
        if why not in {"up to date", "refusing a downgrade"}:
            log(f"automatic update refused: {why}")
        return False

    target = _installed_path()
    lock_path = f"{target}.update.lock"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(lock_path, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                with open(target, "rb") as current_file:
                    current = current_file.read()
            except OSError:
                current = b""
            wanted = hashlib.sha256(source).hexdigest()
            if hashlib.sha256(current).hexdigest() != wanted:
                fd, staged = tempfile.mkstemp(prefix=".novix-runner-", dir=os.path.dirname(target))
                try:
                    with os.fdopen(fd, "wb") as candidate:
                        candidate.write(source)
                        candidate.flush()
                        os.fsync(candidate.fileno())
                    os.chmod(staged, 0o755)
                    # Compile happened in-memory above. This second check imports the
                    # staged file with THIS Mac's Python before it can replace the
                    # known-good copy, catching a platform/import failure CI cannot.
                    checked = subprocess.run(
                        [sys.executable, staged, "--version"],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        env={**os.environ, "NOVIX_MAC_RUNNER_AUTO_UPDATE": "0"},
                    )
                    if checked.returncode != 0 or checked.stdout.strip() != version:
                        log("automatic update refused: the staged runner failed its Mac self-test")
                        return False
                    if current:
                        backup = f"{target}.previous"
                        with open(backup, "wb") as old:
                            old.write(current)
                        os.chmod(backup, 0o755)
                    os.replace(staged, target)
                    staged = ""
                finally:
                    if staged:
                        try:
                            os.unlink(staged)
                        except OSError:
                            pass
                log(f"updated runner {VERSION} -> {version}; restarting between jobs")
            else:
                log(f"runner {version} was installed by another worker; restarting into it")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    os.execv(sys.executable, [sys.executable, *sys.argv])
    return True  # pragma: no cover - execv does not return


# -- keeping secrets out of what we send -----------------------------------------

_SECRET_SHAPES = (
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"), "[redacted]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[redacted]"),
    (re.compile(r"(https://)[^/@\s]*:[^@\s]*(@)"), r"\1[redacted]\2"),
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+"), r"\1[redacted]"),
)


def scrub(text: str, secrets: List[str]) -> str:
    """Rule 4. Every line before it leaves this machine."""
    out = text or ""
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "[redacted]")
    for pattern, replacement in _SECRET_SHAPES:
        out = pattern.sub(replacement, out)
    return out


def run(
    args: List[str], *, cwd: Optional[str] = None, timeout: int = 300,
    env: Optional[dict] = None,
) -> Tuple[Optional[int], str]:
    """One command. ``(exit_code, combined_output)``; a None code means it never
    finished, which is deliberately not the same as a non-zero one (rule 2)."""
    try:
        proc = subprocess.run(
            args, cwd=cwd, timeout=timeout, capture_output=True, text=True,
            env={**os.environ, **(env or {})},
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        return None, f"{partial}\n[timed out after {timeout}s]"
    except FileNotFoundError:
        return None, f"[{args[0]} is not installed]"
    except Exception as exc:
        return None, f"[{args[0]} could not run: {exc.__class__.__name__}]"


def tail(text: str) -> str:
    """A bounded command excerpt that keeps diagnostics even when they are not last.

    The name stays ``tail`` because every command path already calls this one
    chokepoint. For short output it remains byte-for-byte unchanged. For long output,
    half the allowance is reserved for windows around compiler/test diagnostics and
    the rest carries the ordinary tail and its exit summary."""
    if len(text) <= LOG_TAIL_CHARS:
        return text
    lines = text.splitlines()
    wanted = set()
    for index, line in enumerate(lines):
        if _DIAGNOSTIC.search(line):
            start = max(0, index - _DIAGNOSTIC_CONTEXT_LINES)
            stop = min(len(lines), index + _DIAGNOSTIC_CONTEXT_LINES + 1)
            wanted.update(range(start, stop))
    if not wanted:
        return text[-LOG_TAIL_CHARS:]

    diagnostics = "\n".join(lines[index] for index in sorted(wanted))
    heading = "[diagnostics retained from earlier output]\n"
    divider = "\n\n[tail of command output]\n"
    diagnostic_budget = LOG_TAIL_CHARS // 2
    if len(diagnostics) > diagnostic_budget:
        diagnostics = diagnostics[-diagnostic_budget:]
    tail_budget = LOG_TAIL_CHARS - len(heading) - len(diagnostics) - len(divider)
    return heading + diagnostics + divider + text[-max(0, tail_budget):]


def budget_left(started: float, job: dict) -> float:
    """Seconds this job may still spend before the server calls it abandoned.

    Reads the server's own ceiling off the job so the two can never drift, and keeps
    :data:`POST_RESERVE_SECONDS` back for the post itself."""
    try:
        ceiling = float(job.get("deadlineSeconds") or JOB_BUDGET_SECONDS)
    except (TypeError, ValueError):
        ceiling = float(JOB_BUDGET_SECONDS)
    return max(0.0, ceiling - (time.monotonic() - started) - POST_RESERVE_SECONDS)


# -- what this Mac can do --------------------------------------------------------


def xcode_version() -> Optional[str]:
    """The installed Xcode's version string, or None.

    ``xcodebuild -version`` fails outright when only the Command Line Tools are
    present, which is the honest signal: swiftc exists, Xcode does not, and an app
    target cannot be built. Reported as a capability so the server never queues a
    build job this machine can only refuse."""
    code, out = run(["xcodebuild", "-version"], timeout=60)
    if code != 0:
        return None
    first = (out or "").strip().splitlines()
    return first[0].strip() if first else None


def safari_ready() -> bool:
    """Whether ``safaridriver`` can actually be driven.

    NOT "does the binary exist" — it always does on macOS. Remote automation is off
    until somebody runs ``safaridriver --enable`` once with sudo AND ticks Develop >
    Allow Remote Automation in Safari, so the only honest test is starting a session
    and getting one. Reporting Safari as available on a machine where it is switched
    off would produce jobs that can only ever be refused."""
    driver = SafariDriver()
    try:
        return driver.start() and driver.new_session()
    except Exception:
        return False
    finally:
        driver.stop()


def capabilities() -> dict:
    xcode = xcode_version()
    return {
        "xcode": xcode or False,
        "safari": safari_ready(),
        "macos": platform.mac_ver()[0] or platform.platform(),
        "arch": platform.machine(),
    }


# -- driving Safari --------------------------------------------------------------


class SafariDriver:
    """The W3C WebDriver session ``safaridriver`` serves, over plain HTTP.

    Stdlib rather than Selenium for the reason at the top of this file, and it costs
    almost nothing: WebDriver is JSON over HTTP and this uses four of its endpoints.

    RULE 1 LIVES HERE. There is no fallback browser and there must not be one. Every
    failure path returns None and the caller reports no picture."""

    PORT = 4444

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.session: Optional[str] = None

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.PORT}{path}"

    def _call(self, method: str, path: str, payload: Optional[dict] = None) -> Optional[dict]:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self._url(path), data=data, method=method,
            headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=SHOT_TIMEOUT) as res:
                return json.loads(res.read())
        except Exception:
            return None

    def start(self) -> bool:
        if shutil.which("safaridriver") is None:
            return False
        try:
            self.proc = subprocess.Popen(
                ["safaridriver", "-p", str(self.PORT)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            return False
        for _ in range(20):
            time.sleep(0.25)
            try:
                with socket.create_connection(("127.0.0.1", self.PORT), timeout=1):
                    return True
            except OSError:
                continue
        return False

    def new_session(self) -> bool:
        data = self._call("POST", "/session", {
            "capabilities": {"alwaysMatch": {"browserName": "safari"}},
        })
        value = (data or {}).get("value") or {}
        self.session = value.get("sessionId")
        return bool(self.session)

    def shoot(self, url: str, width: int, height: int) -> Optional[Tuple[bytes, int, int]]:
        """One screenshot. ``(png, real_width, real_height)`` or None.

        THE REAL RECT IS READ BACK AND RETURNED, never the one we asked for. macOS
        clamps how narrow a Safari window may be, so a 390px phone request lands at
        whatever width Safari would actually give — and labelling that picture "390px"
        would be a small, confident, wrong claim about what a reviewer is looking at.
        """
        if not self.session:
            return None
        rect = self._call("POST", f"/session/{self.session}/window/rect", {
            "width": width, "height": height, "x": 0, "y": 0,
        })
        if self._call("POST", f"/session/{self.session}/url", {"url": url}) is None:
            return None
        # Safari's own load is synchronous here, but a client-rendered app paints
        # after it. A short settle beats a screenshot of an empty shell.
        time.sleep(3)
        data = self._call("GET", f"/session/{self.session}/screenshot")
        raw = (data or {}).get("value")
        if not isinstance(raw, str):
            return None
        try:
            png = base64.b64decode(raw)
        except Exception:
            return None
        if png[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        got = (rect or {}).get("value") or {}
        return png, int(got.get("width") or width), int(got.get("height") or height)

    def stop(self) -> None:
        try:
            if self.session:
                self._call("DELETE", f"/session/{self.session}")
        except Exception:
            pass
        try:
            if self.proc:
                self.proc.terminate()
                self.proc.wait(timeout=10)
        except Exception:
            try:
                if self.proc:
                    self.proc.kill()
            except Exception:
                pass


# -- the repository --------------------------------------------------------------


def clone(job: dict, directory: str) -> Tuple[bool, str]:
    """Shallow-clone the job's repo at its ref.

    The credential rides in an ``http.extraHeader`` rather than in the remote url, so
    it is never written into ``.git/config`` — which git prints back on half its
    errors, and which outlives this process in the clone directory."""
    token = job.get("cloneToken") or ""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    code, out = run([
        "git", "-c", f"http.extraHeader=Authorization: Basic {basic}",
        "clone", "--depth", "1", "--branch", str(job.get("ref") or "main"),
        f"https://github.com/{job['repo']}.git", directory,
    ], timeout=CLONE_TIMEOUT)
    return code == 0, out


def apply_patch(directory: str, diff: str) -> Tuple[bool, str]:
    """Apply the drafted patch to the clone, the same tolerant way the PR path does."""
    path = os.path.join(directory, ".novix-patch.diff")
    with open(path, "w") as handle:
        handle.write(diff if diff.endswith("\n") else diff + "\n")
    code, out = run(["git", "apply", "--3way", path], cwd=directory, timeout=120)
    if code != 0:
        code, out2 = run(["git", "apply", path], cwd=directory, timeout=120)
        out = out + out2
    try:
        os.unlink(path)
    except Exception:
        pass
    return code == 0, out


def revert_patch(directory: str) -> bool:
    """Put the repository back, and PROVE it moved (rule 3).

    ``git checkout .`` exits 0 whether or not anything was modified, so the exit code
    proves nothing. ``git status --porcelain`` going quiet is the only evidence that
    the tree is really the customer's own code again, and without it the "before"
    picture is the patched app under the wrong label."""
    run(["git", "checkout", "--", "."], cwd=directory, timeout=120)
    run(["git", "clean", "-fd"], cwd=directory, timeout=120)
    code, out = run(["git", "status", "--porcelain"], cwd=directory, timeout=60)
    return code == 0 and not (out or "").strip()


# -- building on Apple hardware ---------------------------------------------------


def find_xcode_target(directory: str) -> Optional[Tuple[str, str]]:
    """``(flag, path)`` for the workspace or project to build, or None.

    A workspace WINS over a project when both exist, because that is what CocoaPods
    and most multi-module apps expect to be built and building the bare project
    misses every pod."""
    best_project = None
    for root, dirs, _files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "Pods", "build")]
        if root.count(os.sep) - directory.count(os.sep) > 3:
            dirs[:] = []
            continue
        for name in sorted(dirs):
            if name.endswith(".xcworkspace") and "project.xcworkspace" not in name:
                return "-workspace", os.path.join(root, name)
            if name.endswith(".xcodeproj") and best_project is None:
                best_project = ("-project", os.path.join(root, name))
    return best_project


def xcode_schemes(flag: str, path: str) -> List[str]:
    code, out = run(["xcodebuild", flag, path, "-list", "-json"], timeout=180)
    if code != 0:
        return []
    try:
        data = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except Exception:
        return []
    holder = data.get("workspace") or data.get("project") or {}
    return [s for s in holder.get("schemes", []) if isinstance(s, str)]


def build_apple(
    job: dict, directory: str, deadline: Optional[float] = None,
) -> Dict[str, Any]:
    """Build and (when there is one) test this patch on real Apple hardware.

    Returns the tri-state result the server stores. EVERY 'we did not do this' is a
    null, never a False — rule 2, and it is the one the engine acts on: Novix
    re-drafts a patch on a False, so a missing toolchain reported as a failed build
    would spend a deep-tier redraft on code that is fine."""
    workdir = directory
    if job.get("workingDirectory"):
        candidate = os.path.join(directory, str(job["workingDirectory"]).lstrip("/"))
        if os.path.isdir(candidate):
            workdir = candidate

    result: Dict[str, Any] = {"buildOk": None, "testOk": None, "log": "", "toolchain": None}
    chunks: List[str] = []

    def window(cap: int) -> int:
        """This command's timeout, never past the job's own ceiling.

        Only the comparison build passes a deadline, and a command cut short by it
        returns a None code, which is UNPROVEN rather than a failure — the honest
        answer when the clock, not the code, is what stopped us."""
        if deadline is None:
            return cap
        return max(60, min(cap, int(deadline - time.monotonic())))

    custom_build = job.get("buildCommand")
    custom_test = job.get("testCommand")
    if custom_build:
        # A workspace on Pro replaced command detection for this repository. Its
        # answer wins here exactly as it does in the container.
        code, out = run(["/bin/sh", "-lc", str(custom_build)], cwd=workdir,
                        timeout=window(BUILD_TIMEOUT))
        result["buildOk"] = code == 0 if code is not None else None
        chunks.append(f"$ {custom_build}\n{tail(out)}")
        if custom_test and result["buildOk"]:
            code, out = run(["/bin/sh", "-lc", str(custom_test)], cwd=workdir,
                            timeout=window(TEST_TIMEOUT))
            result["testOk"] = code == 0 if code is not None else None
            chunks.append(f"$ {custom_test}\n{tail(out)}")
        result["log"] = "\n\n".join(chunks)
        result["toolchain"] = xcode_version()
        return result

    target = find_xcode_target(workdir)
    if target is None:
        if os.path.exists(os.path.join(workdir, "Package.swift")):
            # A SwiftPM package. The container already ran `swift build` on Linux, so
            # this adds the Apple SDKs — the half that catches a UIKit or AppKit
            # import Linux never sees.
            code, out = run(["swift", "build"], cwd=workdir, timeout=window(BUILD_TIMEOUT))
            result["buildOk"] = code == 0 if code is not None else None
            chunks.append(f"$ swift build\n{tail(out)}")
            if result["buildOk"]:
                code, out = run(["swift", "test"], cwd=workdir, timeout=window(TEST_TIMEOUT))
                # A package with no test targets is not a failing test suite. Only a
                # real run of real tests sets this.
                if code is not None and "no tests found" not in (out or "").lower():
                    result["testOk"] = code == 0
                chunks.append(f"$ swift test\n{tail(out)}")
            result["log"] = "\n\n".join(chunks)
            result["toolchain"] = xcode_version()
            return result
        result["log"] = "No Xcode workspace, project or Package.swift was found in this repository."
        return result

    flag, path = target
    schemes = xcode_schemes(flag, path)
    if not schemes:
        result["log"] = f"{os.path.basename(path)} declares no shared scheme, so there is nothing to build."
        return result
    # The scheme named like the project first, else the first shared one. An app's
    # own scheme is what a person means by "does it build"; a scheme list is
    # alphabetical and its head is often a dependency.
    stem = os.path.basename(path).rsplit(".", 1)[0]
    scheme = stem if stem in schemes else schemes[0]

    base = [
        "xcodebuild", "-quiet", flag, path, "-scheme", scheme,
        # Generic, so no simulator has to boot for a build. Signing off, because this
        # machine holds no certificate for somebody else's app and a signing refusal
        # is not information about the patch.
        "-destination", "generic/platform=iOS Simulator",
        "CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO",
        # EVERYTHING THIS BUILD WRITES GOES IN THE JOB'S OWN DIRECTORY, so `rmtree` in
        # handle()'s finally really is the whole cleanup. Left to itself xcodebuild
        # writes to ~/Library/Developer/Xcode/DerivedData and SwiftPM checks packages
        # out beside it — both OUTSIDE the temp tree, both hundreds of megabytes per
        # project, and neither ever deleted. On a machine that takes a job a day that
        # is a disk quietly filling with the build products of repositories whose
        # tasks closed months ago.
        "-derivedDataPath", os.path.join(directory, "DerivedData"),
        "-clonedSourcePackagesDirPath", os.path.join(directory, "SourcePackages"),
    ]
    code, out = run(base + ["build"], cwd=workdir, timeout=window(BUILD_TIMEOUT))
    if code is None:
        # Never finished. That is not a broken patch.
        result["log"] = tail(out)
        result["toolchain"] = xcode_version()
        return result
    if code != 0 and "platform=iOS Simulator" in " ".join(base) and _is_platform_mismatch(out):
        # A macOS-only or watchOS target. Retry once on the platform it really wants
        # rather than reporting a destination mismatch as a build failure.
        base = [a if a != "generic/platform=iOS Simulator" else "generic/platform=macOS" for a in base]
        code, out = run(base + ["build"], cwd=workdir, timeout=window(BUILD_TIMEOUT))
    result["buildOk"] = code == 0 if code is not None else None
    chunks.append(f"$ xcodebuild -scheme {scheme} build\n{tail(out)}")

    if result["buildOk"] and _has_tests(schemes, workdir):
        destination = simulator_destination() or "generic/platform=iOS Simulator"
        try:
            code, out = run(
                [a if not a.startswith("generic/platform") else destination for a in base] + ["test"],
                cwd=workdir, timeout=window(TEST_TIMEOUT),
            )
        finally:
            # PUT THE SIMULATOR BACK. `xcodebuild test` boots the device and leaves it
            # running, and this Mac is expected to sit powered on for months — so a
            # simulator per tested repository accumulates until the machine is doing
            # nothing but hosting idle simulators. Shutting one down that is already
            # off is an error we deliberately ignore; it is never worth a verdict.
            shutdown_simulator(destination)
        if code is not None:
            result["testOk"] = code == 0
        chunks.append(f"$ xcodebuild -scheme {scheme} test\n{tail(out)}")

    result["log"] = "\n\n".join(chunks)
    result["toolchain"] = xcode_version()
    return result


def failure_attribution(patched: dict, baseline: dict) -> Optional[bool]:
    """Whether a red patched run is attributable to the patch.

    ``True`` means the base ref passed the exact phase the patch failed. ``False``
    means the base ref is already red, and ``None`` means the comparison itself did
    not reach a verdict. A repository that is red before Novix touches it cannot be
    evidence that this patch broke it; both False and None therefore become UNPROVEN
    at the caller rather than a fabricated patch failure."""
    if patched.get("buildOk") is False:
        if baseline.get("buildOk") is True:
            return True
        if baseline.get("buildOk") is False:
            return False
        return None
    if patched.get("testOk") is False:
        if baseline.get("buildOk") is False or baseline.get("testOk") is False:
            return False
        if baseline.get("buildOk") is True and baseline.get("testOk") is True:
            return True
        return None
    return None


def compared_log(patched: dict, baseline: Optional[dict]) -> str:
    """The two real command excerpts, labelled so a reader knows what each proved."""
    parts = ["[patched tree]", str(patched.get("log") or "").strip()]
    if baseline is not None:
        parts.extend(["", "[base ref, without the patch]",
                      str(baseline.get("log") or "").strip()])
    return "\n".join(parts).strip()


def _is_platform_mismatch(out: str) -> bool:
    lowered = (out or "").lower()
    return "unable to find a destination" in lowered or "does not support the platform" in lowered


def _has_tests(schemes: List[str], directory: str) -> bool:
    """Whether this project plausibly has a test bundle to run.

    Deliberately conservative: running ``xcodebuild test`` on a scheme with no test
    action fails, and a failure there is not a failing test suite. Reporting no
    verdict is the honest answer when we cannot tell."""
    if any(re.search(r"tests?$", s, re.I) for s in schemes):
        return True
    for root, dirs, _files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "Pods", "build")]
        if any(d.endswith("Tests") or d.endswith("Tests.xctest") for d in dirs):
            return True
        if root.count(os.sep) - directory.count(os.sep) > 2:
            dirs[:] = []
    return False


def simulator_destination() -> Optional[str]:
    """A destination naming a simulator that actually exists on this Mac, or None."""
    code, out = run(["xcrun", "simctl", "list", "devices", "available", "-j"], timeout=120)
    if code != 0:
        return None
    try:
        data = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except Exception:
        return None
    for runtime, devices in sorted((data.get("devices") or {}).items(), reverse=True):
        if "iOS" not in runtime:
            continue
        for device in devices:
            if device.get("isAvailable") and str(device.get("name", "")).startswith("iPhone"):
                return f"platform=iOS Simulator,id={device['udid']}"
    return None


def shutdown_simulator(destination: str) -> None:
    """Shut down the simulator named by a ``platform=iOS Simulator,id=UDID`` string.

    Silent on every failure by design: the device may already be off, may have been
    booted by the person using this Mac, or may not be a udid destination at all. None
    of that is information about the patch, and none of it may reach a verdict."""
    match = re.search(r"id=([0-9A-Fa-f-]{8,})", destination or "")
    if not match:
        return
    run(["xcrun", "simctl", "shutdown", match.group(1)], timeout=60)


def free_disk_gb() -> float:
    """Free space on the volume the jobs are unpacked into."""
    try:
        return shutil.disk_usage(tempfile.gettempdir()).free / (1024 ** 3)
    except Exception:
        # Unmeasurable is not "full": failing the other way would idle a healthy Mac
        # forever on a bookkeeping error, and the timeouts already bound a bad job.
        return float("inf")


# -- photographing the page in real Safari ----------------------------------------


def start_web_app(job: dict, directory: str) -> Tuple[Optional[subprocess.Popen], Optional[str], str]:
    """Boot the repository's web app. ``(process, base_url, note)``.

    ``dev`` beats ``start`` for the same two reasons the container gives: ``next
    start`` refuses without a production build, and a dev server hot-reloads, which is
    what lets one boot serve both the before and the after picture instead of paying
    for two cold starts."""
    command = job.get("startCommand")
    package = os.path.join(directory, "package.json")
    if not command:
        if not os.path.exists(package):
            return None, None, "the repository declares no start command"
        try:
            with open(package) as handle:
                scripts = (json.load(handle) or {}).get("scripts") or {}
        except Exception:
            scripts = {}
        if "dev" in scripts:
            command = "npm run dev"
        elif "start" in scripts:
            command = "npm start"
        else:
            return None, None, "the repository declares no dev or start script"

    if os.path.exists(package):
        code, out = run(["npm", "install", "--no-audit", "--no-fund"],
                        cwd=directory, timeout=CLONE_TIMEOUT)
        if code != 0:
            return None, None, "the repository's dependencies would not install"

    port = 3000
    env = {"PORT": str(port), "BROWSER": "none", "CI": "1"}
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-lc", str(command)], cwd=directory,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, **env},
        )
    except Exception:
        return None, None, "the start command would not run"

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + SERVE_BOOT_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            return None, None, "the app exited before it served anything"
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return proc, base, ""
        except OSError:
            time.sleep(2)
    stop_process(proc)
    return None, None, "the app did not come up in time"


def stop_process(proc: Optional[subprocess.Popen]) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=15)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def capture_shots(job: dict, directory: str, api: Api) -> Tuple[List[dict], str, bool]:
    """The before/after pair in real Safari. ``(shot_rows, reason, tree_reverted)``.

    Order matters and mirrors the container's: photograph the PATCHED app first (the
    tree already holds the patch), then revert and photograph the original. The revert
    has to prove itself before the second picture is allowed to be called "before"
    (rule 3).

    ``tree_reverted`` IS RETURNED RATHER THAN ASSUMED, and the caller needs it: a build
    after this has to re-apply the patch, but only if the revert really happened.
    Re-applying to a tree that still holds the patch fails, and that failure would be
    reported as "the patch could not be re-applied" — losing the build verdict on a run
    where nothing was actually wrong."""
    plan = [s for s in (job.get("shots") or []) if isinstance(s, dict)]
    if not plan:
        return [], "", False

    driver = SafariDriver()
    if not driver.start() or not driver.new_session():
        driver.stop()
        # Rule 1. No substitute browser, so there is simply no picture.
        return [], "Safari could not be driven on this Mac, so no picture was taken", False

    proc = None
    rows: List[dict] = []
    reverted = False
    try:
        proc, base, note = start_web_app(job, directory)
        if base is None:
            return [], note or "the app could not be started to photograph", False
        route = str(job.get("route") or "/")
        if not route.startswith("/"):
            route = "/" + route

        for step in plan:
            got = driver.shoot(f"{base}{route}", int(step.get("width") or 1280),
                               int(step.get("height") or 800))
            if got is None:
                continue
            png, width, height = got
            file_id = api.upload_shot(job, png)
            if file_id:
                rows.append({"kind": "after", "fileId": file_id, "size": step.get("size") or "desktop",
                             "route": route, "width": width, "height": height})

        if not revert_patch(directory):
            # The "before" would be a picture of the patched app. Rather than label it
            # wrongly, ship only the "after".
            return rows, "the patch could not be taken back out, so there is no before picture", False
        reverted = True
        time.sleep(4)  # the dev server recompiles the reverted tree
        for step in plan:
            got = driver.shoot(f"{base}{route}", int(step.get("width") or 1280),
                               int(step.get("height") or 800))
            if got is None:
                continue
            png, width, height = got
            file_id = api.upload_shot(job, png)
            if file_id:
                rows.append({"kind": "before", "fileId": file_id, "size": step.get("size") or "desktop",
                             "route": route, "width": width, "height": height})
        return rows, "", reverted
    finally:
        stop_process(proc)
        driver.stop()


# -- one job ----------------------------------------------------------------------


def handle(job: dict, api: Api) -> None:
    secrets = [str(job.get("cloneToken") or ""), api.token]
    work = list(job.get("work") or [])
    log(f"job {job.get('jobId')} on {job.get('repo')}: {', '.join(work) or 'nothing'}")

    started = time.monotonic()
    directory = tempfile.mkdtemp(prefix="novix-mac-")
    result: Dict[str, Any] = {"ran": False, "passed": None, "buildOk": None, "testOk": None}
    try:
        ok, out = clone(job, directory)
        if not ok:
            result["reason"] = "the repository could not be cloned"
            result["log"] = scrub(tail(out), secrets)
            api.post_result(job, result)
            return
        ok, out = apply_patch(directory, str(job.get("diff") or ""))
        if not ok:
            result["reason"] = "the drafted patch did not apply to this clone"
            result["log"] = scrub(tail(out), secrets)
            api.post_result(job, result)
            return

        shots: List[dict] = []
        shot_reason = ""
        reverted = False
        if "shots" in work:
            # Pictures FIRST: they need the patched tree and then revert it, and a
            # build afterwards would then be building the customer's own code. The
            # patch is re-applied below if a build is also wanted.
            shots, shot_reason, reverted = capture_shots(job, directory, api)

        if "build" in work:
            if reverted:
                # ONLY IF THE TREE REALLY WENT BACK. capture_shots reverts to take the
                # "before" picture, so the patch has to go back on or the build verdict
                # describes the customer's own code. But re-applying a patch that is
                # still there fails, and reporting that as "the patch could not be
                # re-applied" would lose a build verdict on a run where nothing was
                # wrong — which is why this reads the flag instead of `if shots`.
                ok, out = apply_patch(directory, str(job.get("diff") or ""))
                if not ok:
                    result["reason"] = "the patch could not be re-applied after the screenshots"
                    result["log"] = scrub(tail(out), secrets)
                    result["shots"] = shots
                    api.post_result(job, result)
                    return
            built = build_apple(job, directory)
            baseline = None
            attributable = True
            out_of_time = False
            if built["buildOk"] is False or built["testOk"] is False:
                # A RED REPOSITORY IS NOT A RED PATCH. Ticket 3cddab08's proposed
                # onboarding change touched two Swift files, while Xcode failed in
                # an untouched SupabaseManager.swift. The same error was already on
                # main, but the first runner built only the patched tree and blamed
                # the proposal. Compare only after a red run (a green patch needs no
                # second build), using the same target and commands on the reverted
                # clone. The verdict is about the DIFFERENCE, not repository health.
                left = budget_left(started, job)
                if left < MIN_BASELINE_SECONDS:
                    # NOT ENOUGH CLOCK LEFT, so the comparison is not started. An
                    # overrun does not cost the comparison, it costs the whole pass:
                    # the server refuses a result posted past its ceiling, requeues
                    # the job once and retires it the second time. Unproven with the
                    # reason is worth more than a verdict nobody receives.
                    attributable = None
                    out_of_time = True
                elif revert_patch(directory):
                    baseline = build_apple(
                        job, directory, deadline=time.monotonic() + left,
                    )
                    attributable = failure_attribution(built, baseline)
                    result["baselineBuildOk"] = baseline.get("buildOk")
                    result["baselineTestOk"] = baseline.get("testOk")
                else:
                    attributable = None

            result["buildOk"] = built["buildOk"] if attributable is True else None
            result["testOk"] = built["testOk"] if attributable is True else None
            result["toolchain"] = built["toolchain"]
            result["log"] = scrub(compared_log(built, baseline), secrets)
            if attributable is False:
                result["reason"] = (
                    "the repository's base ref already fails in Xcode, so this run "
                    "cannot attribute that failure to the patch"
                )
            elif attributable is None and (
                built["buildOk"] is False or built["testOk"] is False
            ):
                # Which of the two it was, because only one of them is answered by
                # raising NOVIX_MAC_RUNNER_JOB_TTL_SECONDS.
                result["reason"] = (
                    "the patched tree failed in Xcode, and this job ran out of time "
                    "to build the base ref for comparison"
                    if out_of_time else
                    "the patched tree failed in Xcode, but the base ref could not be "
                    "verified for comparison"
                )
            # ONE FALSE ANYWHERE IS A FAIL; anything unproven leaves the whole verdict
            # unproven. Never coerce a null into a pass.
            if result["buildOk"] is False or result["testOk"] is False:
                result["passed"] = False
            elif result["buildOk"] is True:
                result["passed"] = True
            result["ran"] = built["buildOk"] is not None

        if shots:
            result["shots"] = shots
            result["ran"] = True
        if shot_reason and not result.get("reason"):
            result["reason"] = shot_reason
        api.post_result(job, result)
        log(f"job {job.get('jobId')} finished: passed={result['passed']} shots={len(shots)}")
    except Exception as exc:
        log(f"job {job.get('jobId')} blew up: {exc.__class__.__name__}")
        result["reason"] = f"the runner failed: {exc.__class__.__name__}"
        api.post_result(job, result)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# -- the loop ---------------------------------------------------------------------


# How long to leave a capability that is OFF before asking the machine again. Ten
# minutes, because the thing being waited for is a person at a keyboard running
# `sudo safaridriver --enable` and ticking a menu item, and neither the Novix card
# nor this log should make them wonder for longer than a coffee.
RECHECK_OFF_SECONDS = 600


def recheck_capabilities(caps: dict) -> List[str]:
    """Re-probe the capabilities that are currently OFF, in place. Returns what flipped.

    ONLY THE ONES THAT ARE OFF, and the asymmetry is the whole design. `safari_ready`
    proves itself by starting a real safaridriver session and taking a WebDriver
    session off it — that is the only honest test, and it is also a browser window on
    somebody's daily machine, so running it every ten minutes against a capability
    already known to work would be rude and would prove nothing. Off-to-on is the
    direction a person is actively waiting on; on-to-off is a machine breaking, which
    the job itself reports.

    IN PLACE, because `main` hands this same dict to the heartbeat thread AND to
    `api.claim`. Rebinding it here would update the check-in and leave the claim
    still asking for jobs on the old answer."""
    flipped: List[str] = []
    if not caps.get("safari"):
        if safari_ready():
            caps["safari"] = True
            flipped.append("safari")
    if not caps.get("xcode"):
        version = xcode_version()
        if version:
            caps["xcode"] = version
            flipped.append("xcode")
    return flipped


def heartbeat_forever(api: Api, caps: dict, every: List[int], stop: threading.Event) -> None:
    """Check in on a timer, WHATEVER ELSE IS HAPPENING.

    Its own thread on purpose. The check-in is the only thing that makes the Safari
    card light up in Novix, and a runner that beat only between jobs would read as a
    Mac that had gone away for the whole five minutes of an xcodebuild — which is
    exactly when it is most obviously present.

    IT ALSO RE-ASKS THE MACHINE, and that is a fix rather than a nicety. Capabilities
    used to be probed once at startup and the same dict sent forever, so switching
    Safari automation on did nothing at all until somebody restarted the runner —
    while the Novix card, this script's own log and `runner/README.md` all said it
    would come on by itself at the next check-in. Three surfaces promising something
    no code did is the exact silent failure this project keeps paying for; the promise
    is the reasonable half, so the code moved."""
    waited = 0.0
    while not stop.is_set():
        api.checkin(caps)
        pause = max(15, every[0])
        stop.wait(pause)
        waited += pause
        if waited >= RECHECK_OFF_SECONDS:
            waited = 0.0
            for name in recheck_capabilities(caps):
                log(f"{name} is available now; the next check-in says so.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Novix's Apple verification jobs on this Mac.")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--url", default=os.environ.get("NOVIX_URL", "https://app.getnovix.ai"))
    parser.add_argument("--token", default=os.environ.get("NOVIX_MAC_RUNNER_TOKEN", ""))
    parser.add_argument("--label", default=os.environ.get("NOVIX_MAC_RUNNER_LABEL", ""))
    parser.add_argument("--once", action="store_true", help="Take at most one job, then exit.")
    parser.add_argument(
        "--worker", default="",
        help="Name a second, third... runner on this same Mac, so several jobs run at once.",
    )
    args = parser.parse_args()

    if not args.token:
        log("NOVIX_MAC_RUNNER_TOKEN is not set. Nothing to do.")
        return 2
    if platform.system() != "Darwin":
        log("This runner only makes sense on macOS: it exists to do what Linux cannot.")
        return 2

    # UPDATE BEFORE ASKING FOR WORK, and only here in the main thread. Replacing the
    # process from the heartbeat thread could kill an xcodebuild halfway through and
    # turn a healthy patch into an expired, unproven job. Every later check below is
    # also between claims, so an update can wait at most one bounded job.
    update_check = [0.0]
    maybe_self_update(update_check, force=True)

    # SEVERAL WORKERS ON ONE MAC NEED SEPARATE IDS. `machine_id()` is per MACHINE, so
    # two agents on the same always-on mini would share one registry row: each
    # check-in overwrites the other's, the health block reports one runner while two
    # are working, and a worker that dies is invisible because its twin keeps the row
    # fresh. Claiming was never the problem — that takes the ticket row's lock — so
    # this is about being able to SEE the fleet, which is the half that matters when
    # the machine is expected to run unattended for months. No `--worker` reproduces
    # today's id exactly, so a single runner is untouched.
    runner_id = machine_id()
    worker = re.sub(r"[^a-z0-9]+", "-", str(args.worker or "").strip().lower()).strip("-")
    label = args.label or socket.gethostname()
    if worker:
        runner_id = f"{runner_id}-{worker}"
        label = f"{label} ({worker})"
    api = Api(args.url, args.token, runner_id, label)

    caps = capabilities()
    log(f"{label} ({runner_id}): xcode={caps['xcode']} safari={caps['safari']}")
    if not caps["xcode"]:
        log("Xcode is not installed, so no build job will be offered to this Mac.")
    if not caps["safari"]:
        log("Safari automation is off. Run: sudo safaridriver --enable, then tick "
            "Develop > Allow Remote Automation in Safari. This runner re-checks every "
            f"{RECHECK_OFF_SECONDS // 60} minutes, so there is no need to restart it.")

    first = api.checkin(caps)
    if first is None:
        log("The first check-in was refused. Check the url and the token.")
        return 1
    beat = [int(first.get("heartbeatSeconds") or 60)]
    log(f"connected to {args.url}; beating every {beat[0]}s")

    stop = threading.Event()
    thread = threading.Thread(target=heartbeat_forever, args=(api, caps, beat, stop), daemon=True)
    thread.start()
    low_disk = False
    try:
        while True:
            maybe_self_update(update_check)
            # ASKED EVERY PASS, not once at startup: this Mac is somebody's daily
            # machine and the space that vanishes is usually theirs, not ours.
            free = free_disk_gb()
            if free < MIN_FREE_DISK_GB:
                if not low_disk:
                    log(f"only {free:.0f}GB free, holding off until there is "
                        f"{MIN_FREE_DISK_GB}GB. Apple jobs will expire unproven, "
                        "which is never read as a failing build.")
                    low_disk = True
                if args.once:
                    return 0
                time.sleep(POLL_IDLE_SECONDS)
                continue
            if low_disk:
                log(f"{free:.0f}GB free again, taking jobs")
                low_disk = False
            job = api.claim(caps)
            if job:
                handle(job, api)
                if args.once:
                    return 0
                continue
            if args.once:
                log("nothing waiting")
                return 0
            time.sleep(POLL_IDLE_SECONDS)
    except KeyboardInterrupt:
        return 0
    finally:
        stop.set()


def _find_key(node: Any, key: str) -> Optional[str]:
    """The first value of ``key`` anywhere in a parsed plist. Recursive on purpose.

    ``ioreg -a``'s SHAPE MOVES BETWEEN macOS RELEASES — a list of dicts on some, a
    single dict whose real payload hangs off ``IORegistryEntryChildren`` on others
    (measured on macOS 26, where a fixed ``data[0]["IOPlatformUUID"]`` found nothing
    and the runner silently fell back to the hostname). Searching for the key is
    version-proof in a way that any fixed path is not."""
    if isinstance(node, dict):
        if isinstance(node.get(key), str):
            return node[key]
        for value in node.values():
            found = _find_key(value, key)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_key(item, key)
            if found:
                return found
    return None


def machine_id() -> str:
    """A stable id for this Mac, so its row is updated rather than multiplied.

    The hardware UUID, which survives a rename and a reinstall. Falls back to the
    hostname — which is what a person recognises, but is NOT unique: two Macs both
    called "Mac" would share one row and each check-in would overwrite the other's.
    That is survivable (the row only answers "a Mac is connected") and is why the
    UUID is tried first."""
    code, out = run(["ioreg", "-d2", "-c", "IOPlatformExpertDevice", "-a"], timeout=30)
    if code == 0 and out:
        try:
            uid = _find_key(plistlib.loads(out.encode()), "IOPlatformUUID")
            if uid:
                return f"mac-{str(uid).lower().replace('-', '')[:16]}"
        except Exception:
            pass
    return f"mac-{socket.gethostname().lower().replace('.', '-')[:32]}"


if __name__ == "__main__":
    sys.exit(main())

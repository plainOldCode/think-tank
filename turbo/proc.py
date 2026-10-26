"""Process and git ports. Tests pass fakes; these are the real ones.

Git commands are a list, never a shell string. The bearer for a webhook
is not read here.
"""

import os
import signal
import subprocess
import tempfile


def parse_porcelain(text: str) -> list[str]:
    paths = []
    for line in text.splitlines():
        if len(line) < 4:
            # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
            continue
        path = line[3:]
        if " -> " in path:
            # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip().strip('"'))
    return paths


class SubprocessProc:
    """One headless child per spawn. No terminal session and no keystroke injection."""

    def __init__(self):
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
        self._procs: dict[int, subprocess.Popen] = {}
        self._files: dict[int, str] = {}
        self._n = 0

    def spawn(self, argv: list[str]) -> int:
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
        self._n += 1
        handle = self._n
        fd, name = tempfile.mkstemp(prefix="turbo-out-")
        os.close(fd)
        stream = open(name, "w", encoding="utf-8")
        try:
            proc = subprocess.Popen(argv, stdout=stream, stderr=subprocess.STDOUT)
        finally:
            stream.close()
        self._procs[handle] = proc
        self._files[handle] = name
        return handle

    def poll(self, handle: int) -> int | None:
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
        return self._procs[handle].poll()

    def interrupt(self, handle: int) -> None:
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
        proc = self._procs[handle]
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)

    def output(self, handle: int) -> str:
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
        try:
            with open(self._files[handle], encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
            return ""


class GitCli:
    def __init__(self, cwd: str, run=None):
        self.cwd = cwd
        self.run = run or subprocess.run

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess:
        return self.run(argv, cwd=self.cwd, capture_output=True, text=True, check=True)

    def changed_paths(self) -> list[str]:
        done = self._run(["git", "status", "--porcelain"])
        return parse_porcelain(done.stdout or "")

    def commit_all(self, message: str) -> str:
        self._run(["git", "add", "-A"])
        self._run(["git", "commit", "-m", message])
        done = self._run(["git", "rev-parse", "HEAD"])
        return (done.stdout or "").strip()

    def open_pr(self, title: str, body: str, base: str) -> dict:
        self._run(["git", "push", "-u", "origin", "HEAD"])
        done = self._run(["git", "rev-parse", "HEAD"])
        created = self._run([
            "gh", "pr", "create", "--title", title, "--body", body, "--base", base,
        ])
        url = ""
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; untaken branch, only unit-tested)
        if created.stdout:
            lines = [line.strip() for line in created.stdout.splitlines() if line.strip()]
            url = lines[-1] if lines else ""
        tail = url.rstrip("/").split("/")[-1] if url else ""
        number = int(tail) if tail.isdigit() else None
        return {
            "provider": "github",
            "url": url,
            "number": number,
            "base": base,
            "sha": (done.stdout or "").strip(),
        }

    def merge_pr(self, number: int, sha: str) -> None:
        # The server checks the sha before this runs. gh merges that pull request.
        if not sha:
            raise RuntimeError("merge requires the submitted sha")
        self._run(["gh", "pr", "merge", str(number), "--merge"])

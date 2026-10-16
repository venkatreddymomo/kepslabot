"""Admin UI build + Vite dev-server management.

Relocated verbatim from main.py (behavior-preserving). In development we run
Vite for HMR; in all modes we make sure admin_ui/dist is fresh so the admin
route works even if Vite fails to start.
"""
import os
import subprocess
import sys as _sys
from pathlib import Path

# Repo root (this module lives in <root>/bootstrap/).
_BASE_DIR = Path(__file__).resolve().parent.parent
_ADMIN_UI_DIR = (_BASE_DIR / "admin_ui").resolve()
VITE_DEV_PORT = 8080


def ensure_admin_dist_fresh() -> None:
    """Build admin_ui/dist when missing or older than admin_ui/src.

    Vite dev server is best-effort: it runs npm run dev async and may fail to
    start or compile (broken TSX, port clash, missing deps). If Vite fails the
    admin route falls back to admin_ui/dist/index.html — so dist MUST be fresh.

    This runs synchronously before uvicorn starts, building only when needed
    (typical: skipped on a warm tree, ~30s on a cold tree)."""
    if not (_ADMIN_UI_DIR / "package.json").exists():
        print(f"admin_ui not found at {_ADMIN_UI_DIR}; skipping dist build.")
        return

    src_dir = _ADMIN_UI_DIR / "src"
    dist_index = _ADMIN_UI_DIR / "dist" / "index.html"
    needs_build = False

    if not dist_index.exists():
        print("admin_ui/dist missing — building...")
        needs_build = True
    elif src_dir.exists():
        dist_mtime = dist_index.stat().st_mtime
        for src_file in src_dir.rglob("*"):
            try:
                if src_file.is_file() and src_file.stat().st_mtime > dist_mtime:
                    print(f"admin_ui/src changed since last build ({src_file.relative_to(_ADMIN_UI_DIR)}) — rebuilding...")
                    needs_build = True
                    break
            except OSError:
                continue

    if not needs_build:
        return

    npm_cmd = "npm.cmd" if _sys.platform == "win32" else "npm"
    try:
        result = subprocess.run(
            [npm_cmd, "run", "build"],
            cwd=str(_ADMIN_UI_DIR),
            timeout=300,
            capture_output=True,
        )
    except FileNotFoundError:
        print(f"admin_ui build skipped: '{npm_cmd}' not found in PATH.")
        return
    except subprocess.TimeoutExpired:
        print("admin_ui build timed out after 5 minutes.")
        return
    except Exception as exc:
        print(f"admin_ui build error: {exc}")
        return

    if result.returncode != 0:
        print(f"admin_ui build FAILED (exit {result.returncode}). stderr:")
        try:
            stderr_text = result.stderr.decode("utf-8", errors="replace")
        except Exception:
            stderr_text = ""
        print(stderr_text[:3000] if stderr_text else "(no stderr captured)")
        return

    print("admin_ui build complete.")


def start_vite_process() -> "subprocess.Popen[bytes] | None":
    """Start Vite dev server for admin_ui live reload (development only)."""
    npm_cmd = "npm.cmd" if _sys.platform == "win32" else "npm"
    if not (_ADMIN_UI_DIR / "package.json").exists():
        print(f"admin_ui not found at {_ADMIN_UI_DIR}; skipping Vite dev server.")
        return None
    try:
        process = subprocess.Popen(
            [npm_cmd, "run", "dev"],
            cwd=str(_ADMIN_UI_DIR),
            env=dict(os.environ),
        )
        print(f"Vite dev server started (pid={process.pid}) — admin UI served with HMR")
        return process
    except Exception as exc:
        print(f"Vite dev server auto-start failed: {exc}")
        return None


def stop_vite_process(process: "subprocess.Popen[bytes] | None") -> None:
    """Terminate Vite dev server process."""
    if process is None:
        return
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=10)
        print("Vite dev server stopped")
    except subprocess.TimeoutExpired:
        process.kill()
    except Exception:
        pass

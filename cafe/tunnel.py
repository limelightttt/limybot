"""A temporary https address for running on a laptop: Telegram opens the Mini App only over https. If PUBLIC_URL is
empty and cloudflared is installed, it gets a free quick tunnel (https://<words>.trycloudflare.com → this machine).
The address changes on every start, which is fine for a demo; a server has its own domain instead."""

import asyncio
import re
import shutil

QUICK_TUNNEL = re.compile(r"https://[-a-z0-9]+\.trycloudflare\.com")
WAIT = 30  # seconds for cloudflared to report the address


def is_quick_tunnel(url: str) -> bool:
    """An old quick-tunnel address left in .env never works again: treat it as no address."""
    return bool(QUICK_TUNNEL.fullmatch(url.rstrip("/")))


def needs_tunnel(url: str) -> bool:
    """No real https address of our own: empty, http://localhost (also the default without .env), or an old quick
    tunnel — then a laptop run gets a fresh tunnel."""
    return not url.startswith("https://") or is_quick_tunnel(url)


async def open_tunnel(port: int) -> tuple[asyncio.subprocess.Process, str] | None:
    """Starts cloudflared for http://127.0.0.1:<port>; (process, https address), or None without cloudflared."""
    exe = shutil.which("cloudflared")
    if not exe:
        return None
    proc = await asyncio.create_subprocess_exec(
        exe,
        "tunnel",
        "--no-autoupdate",
        "--url",
        f"http://127.0.0.1:{port}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        url = await asyncio.wait_for(_address(proc.stderr), WAIT)
    except TimeoutError:
        url = None
    if not url:
        await close_tunnel(proc)
        return None
    asyncio.create_task(_drain(proc.stderr))  # cloudflared stalls once its log pipe is full
    return proc, url


async def close_tunnel(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        proc.terminate()
        await proc.wait()


async def _address(log: asyncio.StreamReader) -> str | None:
    while line := await log.readline():
        if m := QUICK_TUNNEL.search(line.decode(errors="replace")):
            return m[0]
    return None  # cloudflared exited


async def _drain(log: asyncio.StreamReader) -> None:
    while await log.readline():
        pass

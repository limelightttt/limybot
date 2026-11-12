"""Starting limybot: the temporary https tunnel, the summary printed for the operator, friendly refusals."""

import socket
import stat

import pytest

from cafe.__main__ import check_token
from cafe.runner import Runner, port_is_free
from cafe.store import OPERATOR, OPS
from cafe.template import slug_for
from cafe.tokens import TokenError, bot_profile
from cafe.tunnel import close_tunnel, is_quick_tunnel, needs_tunnel, open_tunnel


def fake_cloudflared(tmp_path, monkeypatch, script: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "cloudflared"
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bin_dir))


async def test_tunnel_reports_its_address(tmp_path, monkeypatch):
    fake_cloudflared(
        tmp_path,
        monkeypatch,
        'echo "INF Requesting new quick Tunnel on trycloudflare.com..." >&2\n'
        'echo "INF |  https://brave-new-words.trycloudflare.com   |" >&2\n'
        "exec sleep 30\n",
    )
    proc, url = await open_tunnel(8000)
    assert url == "https://brave-new-words.trycloudflare.com" and proc.returncode is None
    await close_tunnel(proc)
    assert proc.returncode is not None


async def test_no_tunnel_without_cloudflared_or_an_address(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing installed
    assert await open_tunnel(8000) is None
    fake_cloudflared(tmp_path, monkeypatch, 'echo "ERR failed to request quick Tunnel" >&2\n')
    assert await open_tunnel(8000) is None


def test_old_tunnel_addresses_are_not_kept():
    assert is_quick_tunnel("https://spreading-fly-protest-cross.trycloudflare.com/")
    assert not is_quick_tunnel("https://admin.limybot.uz") and not is_quick_tunnel("")


def test_a_tunnel_unless_there_is_a_real_https_address():
    for url in ("", "http://localhost:8000", "https://old-words.trycloudflare.com"):
        assert needs_tunnel(url), url
    assert not needs_tunnel("https://admin.limybot.uz")


def test_summary_tells_the_operator_what_works(store, demo, settings):
    runner = Runner(store, settings)
    settings.public_url = "https://brave-new-words.trycloudflare.com"
    text = runner.summary()
    assert "/ops/login/" in text and "Demo Coffee" in text and "временный адрес" in text
    token = text.split("/ops/login/")[1].split()[0]
    assert store.consume_login(token) == (OPS, OPERATOR)
    runner.problems[demo.id] = ("rejected", demo.bot_token)
    assert runner.state(demo.id) == "rejected" and "не принял токен" in runner.summary()
    settings.public_url = "http://localhost:8000"
    assert "cloudflared" in runner.summary()


def test_the_panel_link_for_this_machine(store, settings):
    link = Runner(store, settings).panel_link()
    assert link.startswith(f"http://localhost:{settings.web_port}/ops/login/")
    assert store.consume_login(link.rsplit("/", 1)[1]) == (OPS, OPERATOR)


def test_summary_without_cafes(store, settings):
    assert "Подключить заведение" in Runner(store, settings).summary()


def test_busy_port_is_noticed():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        assert not port_is_free("127.0.0.1", s.getsockname()[1])


async def test_token_mistakes_are_explained():
    with pytest.raises(TokenError, match="Это не токен бота"):
        await bot_profile("ВАШ_ТОКЕН")  # pasted the placeholder from the instructions


def test_cli_explains_a_bad_token_instead_of_a_traceback():
    with pytest.raises(SystemExit, match="Это не токен бота"):
        check_token("ВАШ_ТОКЕН")


def test_short_names_from_bot_usernames(store, demo):
    assert slug_for("Limy_Coffee_bot") == "limy_coffee" and slug_for("marrymebot") == "marryme"
    assert store.free_slug("demo") == "demo-2" and store.free_slug("tea") == "tea"


def test_the_service_bot_is_run_like_a_cafe_bot(store, demo, settings):
    runner = Runner(store, settings)
    assert runner.token(OPS) is None and runner.token(demo.id) == demo.bot_token
    store.set_meta("ops_bot_token", "9:ops")
    runner.problems[OPS] = ("rejected", "9:ops")
    assert runner.state(OPS) == "rejected"
    store.set_meta("ops_bot_token", "9:new")  # a new token: tried again
    assert runner.state(OPS) == "starting"

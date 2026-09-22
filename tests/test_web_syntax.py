"""Parse each console page the way a browser does.

A SyntaxError in a classic <script> is not a runtime error you can catch --
the entire script is discarded before its first line runs. The page still
returns HTTP 200 and still renders its markup, so the failure looks exactly
like "the screens load but show no data", with nothing in the server log.

That is what a duplicate top-level `const api` did: console.js declared it
globally, every page re-declared it while destructuring window.Console, and
all three screens went blank.

So: concatenate console.js with each page's inline script -- one shared
scope, as the browser sees it -- and check it parses.
"""
import pathlib
import re
import shutil
import subprocess

import pytest

WEB = pathlib.Path(__file__).resolve().parent.parent / "web"
PAGES = ("run", "configuration", "history", "login")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("page", PAGES)
def test_page_parses(page, tmp_path):
    shared = (WEB / "console.js").read_text(encoding="utf-8")
    html = (WEB / f"{page}.html").read_text(encoding="utf-8")
    inline = re.findall(r"<script>(.*?)</script>", html, re.S)

    combined = tmp_path / f"{page}.js"
    combined.write_text(shared + "\n" + "\n".join(inline), encoding="utf-8")

    result = subprocess.run(
        ["node", "--check", str(combined)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"{page}.html: {result.stderr}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_console_js_leaks_nothing_global():
    """console.js must export only window.Console.

    Anything else at top level shares scope with every page script and will
    collide the moment a page destructures the same name.
    """
    source = (WEB / "console.js").read_text(encoding="utf-8")
    leaked = [
        line for line in source.splitlines()
        if re.match(r"^(const|let|var|function|async function)\s", line)
    ]
    assert not leaked, f"console.js leaks globals: {leaked}"

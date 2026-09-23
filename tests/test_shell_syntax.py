"""Static checks for the seven-page console shell.

The shell moved its script out of the page and into web/shell/*.js, which
defeats the checks in test_web_syntax.py -- those read inline <script> blocks,
and console.html has none. The same three bug classes still apply, and all
three are invisible from the server:

  * a SyntaxError discards the whole module before its first line runs,
  * $("id") on markup that does not exist throws on load,
  * a Console helper used without importing it is a ReferenceError.

These cost three rounds of "the screens load but show no data" the last time
they went unchecked, so they are checked per module here rather than per page.
"""
import pathlib
import re
import shutil
import subprocess

import pytest

WEB = pathlib.Path(__file__).resolve().parent.parent / "web"
SHELL = WEB / "shell"
PAGE = WEB / "console.html"

MODULES = sorted(p.name for p in SHELL.glob("*.js"))


def test_shell_modules_exist():
    """The page loads these by name; a rename that misses the <script> tag
    produces a 404 and a silently missing feature, not an error."""
    html = PAGE.read_text(encoding="utf-8")
    referenced = set(re.findall(r'src="/static/shell/([^"]+)"', html))
    assert referenced, "console.html loads no shell modules"
    missing = referenced - set(MODULES)
    assert not missing, f"console.html loads modules that do not exist: {sorted(missing)}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_shell_parses_as_one_scope(tmp_path):
    """Concatenated in load order, the way the browser sees them.

    Each module is an IIFE, so nothing should collide -- but that is the
    property being tested, not an assumption to rely on. A top-level `const`
    that escaped an IIFE in two modules is a SyntaxError that takes the whole
    console down.
    """
    html = PAGE.read_text(encoding="utf-8")
    order = re.findall(r'src="/static/(?:shell/)?([^"]+\.js)"', html)

    parts = []
    for name in order:
        path = WEB / name if (WEB / name).exists() else SHELL / pathlib.Path(name).name
        parts.append(path.read_text(encoding="utf-8"))

    combined = tmp_path / "shell.js"
    combined.write_text("\n".join(parts), encoding="utf-8")

    result = subprocess.run(
        ["node", "--check", str(combined)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", MODULES)
def test_module_leaks_nothing_global(module):
    """Every shell module wraps itself in an IIFE.

    One that does not shares scope with all the others, and the collision
    only shows up once a second module happens to pick the same name.
    """
    source = (SHELL / module).read_text(encoding="utf-8")
    leaked = [
        line for line in source.splitlines()
        if re.match(r"^(const|let|var|function|async function)\s", line)
    ]
    assert not leaked, f"{module} declares globals: {leaked}"


@pytest.mark.parametrize("module", MODULES)
def test_every_element_id_referenced_exists(module):
    """$("id") against markup that is not in console.html throws on load.

    The error reads "Cannot read properties of null (reading
    'addEventListener')" and names a line, nothing more. It is what removing
    markup while leaving its driving code behind produces every time.
    """
    html = PAGE.read_text(encoding="utf-8")
    source = (SHELL / module).read_text(encoding="utf-8")

    declared = set(re.findall(r'\bid="([^"]+)"', html))
    referenced = set(re.findall(r'\$\("([^"]+)"\)', source))
    referenced |= set(re.findall(r'getElementById\("([^"]+)"\)', source))

    # Ids the module creates at runtime and addresses through a template
    # literal are out of scope for a static check. So are the ones it renders
    # into a container itself -- those exist by the time they are read.
    dynamic = {r for r in referenced if "$" in r or "{" in r}
    rendered = set(re.findall(r'id="([a-z0-9-]+)"', source))

    missing = referenced - declared - dynamic - rendered
    assert not missing, f"{module} references ids not in console.html: {sorted(missing)}"


@pytest.mark.parametrize("module", MODULES)
def test_console_helpers_are_imported(module):
    """A helper called without being pulled off window.Console is a
    ReferenceError -- the module parses, the panel just stays blank."""
    helpers = {
        "api", "fmt", "duration", "pill", "esc", "showError",
        "clearError", "streamRun", "pager", "whoami", "mountUser",
    }
    source = (SHELL / module).read_text(encoding="utf-8")

    m = re.search(r"const \{ ([^}]+) \} = window\.Console;", source)
    imported = {x.strip() for x in m.group(1).split(",")} if m else set()

    local = set(re.findall(r"function (\w+)\s*\(", source))
    local |= set(re.findall(r"const (\w+) = \(", source))

    called = set(re.findall(r"(?<![.\w])(" + "|".join(helpers) + r")\s*\(", source))
    missing = called - local - imported
    assert not missing, (
        f"{module} calls {sorted(missing)} without taking them off window.Console"
    )


def test_every_route_has_a_page_and_a_module():
    """A rail button pointing at a route with no section behind it shows a
    blank screen with no error at all -- the router hides every page and
    reveals none."""
    html = PAGE.read_text(encoding="utf-8")
    app = (SHELL / "app.js").read_text(encoding="utf-8")

    pages = set(re.findall(r'data-page="([^"]+)"', html))
    routes = set(re.findall(r'^\s*"([a-z-]+)":\s*\{ label:', app, re.M))

    assert routes, "app.js declares no routes"
    assert routes == pages, (
        f"routes and pages disagree — routes only: {sorted(routes - pages)}, "
        f"pages only: {sorted(pages - routes)}"
    )

    # Every rail destination must be one of those routes.
    rail = set(re.findall(r'class="rail-btn" data-route="([^"]+)"', html))
    assert rail <= routes, f"rail points at unknown routes: {sorted(rail - routes)}"


def test_run_detail_modal_is_present():
    """The run-detail dialog was carried over deliberately. Its markup and
    its module have to travel together: either alone is a blank dialog."""
    html = PAGE.read_text(encoding="utf-8")
    for element in ("rd-title", "rd-meta", "rd-metrics", "rd-overview",
                    "rd-groups", "rd-failures", "rd-console", "rd-config"):
        assert f'id="{element}"' in html, f"run-detail modal is missing #{element}"
    assert "rundetail.js" in html

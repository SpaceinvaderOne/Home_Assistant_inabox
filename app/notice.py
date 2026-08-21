"""The WHATVERSION gate: what the container does when the Docker
template that started it does not match this image's own major version.
"""

import html
import http.server
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

NoticeKind = Literal["old", "absent", "unrecognised"]

PORTS = (8123, 9123)

VM_UNTOUCHED_SENTENCE = (
    "The Home Assistant VM is untouched and still running. "
    "Deleting this container does not touch the VM."
)


def classify_whatversion(value: str | None) -> Literal["match", "old", "absent", "unrecognised"]:
    """Sort a raw WHATVERSION env value into one of the four cases."""
    if value == "3":
        return "match"
    if value == "2":
        return "old"
    if value is None:
        return "absent"
    return "unrecognised"


@dataclass(frozen=True, slots=True)
class NoticeContent:
    """One notice kind's facts, plain text, ordered. `points` always
    includes VM_UNTOUCHED_SENTENCE verbatim -- render_html marks that one
    entry out for prominence by identity, rather than duplicating it as a
    separate field, so it can never drift from the sentence every other
    kind also carries."""

    kind: NoticeKind
    heading: str
    points: tuple[str, ...]


def notice_content(kind: NoticeKind, raw_value: str | None) -> NoticeContent:
    """The facts for one notice kind. `raw_value` is threaded through
    uniformly (even where a kind ignores it) so callers never need a
    special case -- only "unrecognised" actually quotes it back."""
    if kind == "old":
        return NoticeContent(
            kind="old",
            heading="This container updated to v3, but its template is still v2's",
            points=(
                "This container was updated to Home Assistant inabox v3, but the "
                "Docker template that started it is still the old v2 template.",
                VM_UNTOUCHED_SENTENCE,
                'Install "Home Assistant inabox v3" from Community Applications, '
                "then delete this old container.",
                "To stay on v2 exactly as it was, change this template's Repository "
                "field to spaceinvaderone/ha_inabox:2 and restart the container.",
                "Once you have moved to v3, this container's old appdata folder is "
                "no longer used. It can be deleted later; nothing needs it.",
            ),
        )
    if kind == "absent":
        return NoticeContent(
            kind="absent",
            heading="This container does not know which template started it",
            points=(
                "This container could not find a WHATVERSION environment variable, "
                "so it does not know which template version started it.",
                VM_UNTOUCHED_SENTENCE,
                "Add -e WHATVERSION=3 to the run command (or the matching setting "
                "in a compose file) and restart the container.",
            ),
        )
    return NoticeContent(
        kind="unrecognised",
        heading="This container's WHATVERSION is not one it recognises",
        points=(
            f"WHATVERSION is set to {raw_value!r}, which this container does not "
            "recognise -- it expects 3.",
            VM_UNTOUCHED_SENTENCE,
            "Set WHATVERSION=3 and restart the container.",
        ),
    )


def render_html(content: NoticeContent) -> str:
    """One self-contained page: inline CSS only, no external assets of any
    kind, dark, matching app/web/static/app.css's own palette rather than
    inventing a second one.
    """
    paragraphs = []
    for point in content.points:
        css_class = ' class="safe"' if point == VM_UNTOUCHED_SENTENCE else ""
        paragraphs.append(f"<p{css_class}>{html.escape(point)}</p>")
    body = "\n".join(paragraphs)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Home Assistant in a box</title>
<style>
  :root {{
    --brand: #41BDF5;
    --ink:   #0b0f14;
    --pane:  #121820;
    --line:  #232e3a;
    --tx:    #e6edf5;
    --tx2:   #8b9bb0;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font: 16px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--ink);
    color: var(--tx);
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 2.5rem 1.5rem;
  }}
  main {{
    max-width: 640px;
    background: var(--pane);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 2rem 2.2rem;
  }}
  h1 {{
    font-size: 1.3rem;
    font-weight: 650;
    letter-spacing: -0.01em;
    margin-bottom: 1.2rem;
    color: var(--tx);
  }}
  p {{ color: var(--tx2); margin: 0.9rem 0; }}
  p.safe {{
    color: var(--tx);
    background: rgba(65, 189, 245, 0.08);
    border-left: 3px solid var(--brand);
    padding: 0.7rem 0.9rem;
    border-radius: 6px;
  }}
</style>
</head>
<body>
<main>
<h1>{html.escape(content.heading)}</h1>
{body}
</main>
</body>
</html>
"""


def render_text(content: NoticeContent) -> str:
    """The same facts as render_html, framed for docker logs -- "view logs"
    on an old template's WebUI-less container is the only other place a
    confused user is likely to look."""
    rule = "=" * 70
    points = "\n\n".join(content.points)
    return (
        f"{rule}\n"
        f"HOME ASSISTANT IN A BOX -- NOTICE MODE\n"
        f"{content.heading}\n"
        f"{rule}\n\n"
        f"{points}\n\n"
        f"{rule}"
    )


def notice_handler_class(page_bytes: bytes) -> type[http.server.BaseHTTPRequestHandler]:
    """A request handler that returns `page_bytes` for any path and any of
    GET/HEAD/POST/PUT/DELETE -- an old bookmark to any URL, or a health
    check using any method, must land on the page rather than a 404 or a
    501. HEAD omits the body, matching HTTP's own definition of the verb;
    every other method returns it in full.
    """

    class NoticeHandler(http.server.BaseHTTPRequestHandler):
        def _respond(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page_bytes)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(page_bytes)

        do_GET = _respond
        do_HEAD = _respond
        do_POST = _respond
        do_PUT = _respond
        do_DELETE = _respond

        def log_message(self, format: str, *args) -> None:
            pass

    return NoticeHandler


def build_servers(
    page_bytes: bytes,
    *,
    ports: tuple[int, ...] = PORTS,
    server_class: Callable[..., object] = http.server.ThreadingHTTPServer,
) -> list:
    """One server per port, both serving the identical page. `server_class`
    is a seam for tests: the default is a real stdlib server, but a test
    injects a recording stand-in so "both ports requested" can be proven
    from the construction calls themselves, never a real bind."""
    handler_class = notice_handler_class(page_bytes)
    return [server_class(("0.0.0.0", port), handler_class) for port in ports]


def _install_sigterm_handler(stop_event: threading.Event):
    """Return the previous handler so the caller can restore it -- this
    process's SIGTERM handling should not outlive one call to
    run_notice_server, in production or in a test that fires SIGTERM at
    itself to prove the handler works."""

    def _on_sigterm(signum, frame) -> None:
        stop_event.set()

    return signal.signal(signal.SIGTERM, _on_sigterm)


def run_notice_server(
    kind: NoticeKind,
    raw_value: str | None,
    *,
    server_class: Callable[..., object] = http.server.ThreadingHTTPServer,
    wait: Callable[[], None] | None = None,
) -> None:
    """Serve the notice page on both PORTS until told to stop."""
    content = notice_content(kind, raw_value)
    print(render_text(content))
    sys.stdout.flush()

    servers = build_servers(render_html(content).encode("utf-8"), server_class=server_class)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()

    stop_event = threading.Event()
    previous_handler = _install_sigterm_handler(stop_event)
    try:
        (wait or stop_event.wait)()
    finally:
        for server in servers:
            server.shutdown()
        for server in servers:
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)
        signal.signal(signal.SIGTERM, previous_handler)

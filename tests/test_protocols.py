"""The section-visibility protocol layer (catalogue/domain/protocols.py) and its web wiring.

A protocol is a named gate; 'default' is always visible, 'local' needs a host (loopback) request,
'desktop' needs a desktop-class client. The same names gate the client-rendered nav (mirrored in
library-core.js) — these tests pin the canonical Python side + the settings-section wiring.
"""
from catalogue.services import protocols


def test_default_protocol_always_visible():
    assert protocols.is_visible("default", {}) is True
    assert protocols.is_visible("default", {"local": False, "desktop": False}) is True


def test_unknown_or_none_protocol_falls_back_to_default():
    assert protocols.is_visible("nope", {}) is True
    assert protocols.is_visible(None, {}) is True


def test_local_protocol_needs_host_context():
    assert protocols.is_visible("local", {"local": True}) is True
    assert protocols.is_visible("local", {"local": False}) is False
    assert protocols.is_visible("local", {}) is False


def test_desktop_protocol_needs_desktop_context():
    assert protocols.is_visible("desktop", {"desktop": True}) is True
    assert protocols.is_visible("desktop", {"desktop": False}) is False
    assert protocols.is_visible("desktop", {}) is False


def test_mount_roots_section_gated_by_local_protocol(tmp_path):
    """The /settings mount-roots section uses protocol 'local': shown on the host, hidden remote."""
    from catalogue.webui.web import create_app
    c = create_app(str(tmp_path / "t.db")).test_client()                    # isolated tmp DB
    local = c.get("/settings").get_data(as_text=True)                       # loopback by default
    remote = c.get("/settings", environ_overrides={"REMOTE_ADDR": "10.0.0.9"}).get_data(as_text=True)
    assert "Library mount roots" in local
    assert "Library mount roots" not in remote
    assert "Device preferences" in remote                                   # default-protocol section stays

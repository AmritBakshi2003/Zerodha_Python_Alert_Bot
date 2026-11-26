"""F13 — documentation must not drift back into the stale claims from the audit.

These are cheap text checks that fail in CI if a future edit reintroduces a
claim we already corrected: RSI momentum confirmation, Docker as a supported
deployment, "3 websockets per user", vague "arrives in a later update" markers,
or a missing AGENTS.md / live-session checklist.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_agents_md_exists_with_hard_rules() -> None:
    text = _read(ROOT / "AGENTS.md")
    assert "Alerts only" in text
    assert "No secrets in git or in logs" in text
    assert "One phase per branch" in text


def test_roadmap_does_not_claim_rsi_momentum_confirmation() -> None:
    text = _read(DOCS / "ROADMAP.md")
    assert "momentum confirmation" not in text.lower()


def test_roadmap_does_not_advertise_docker_deployment() -> None:
    text = _read(DOCS / "ROADMAP.md")
    assert "Containerized Deployment (Docker Compose)" not in text
    assert "docker-compose.yml for headless" not in text
    assert "RULED OUT" in text


def test_roadmap_links_the_authoritative_status_register() -> None:
    text = _read(DOCS / "ROADMAP.md")
    assert "IMPLEMENTATION.md" in text


def test_no_vague_later_update_markers() -> None:
    for path in DOCS.glob("*.md"):
        assert "arrives in a later update" not in _read(path).lower(), path.name


def test_live_feed_quota_is_per_api_key() -> None:
    text = _read(DOCS / "live-feed.md")
    assert "per API key" in text
    assert "per user" not in text.lower()


def test_live_session_checklist_exists() -> None:
    text = _read(DOCS / "live-feed.md")
    assert "First Live Session Verification Checklist" in text
    assert "UNVERIFIED" in text

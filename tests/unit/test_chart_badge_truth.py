"""N1/F5 — the frontend must render the honest feed badges.

On main the chart badge node could show a plain green ``LIVE`` while the feed
reported ``stale`` (the JS ``updateLiveBadge`` had no branch for that state,
and the payload badge was hard-coded "LIVE" for connecting/stale/closed too).
These are static-text guarantees: they fail on main and pin the fix.

F15 moved the truth table out of ``chart.js`` into the DOM-free ``badge.js``
so Node can test it; these checks follow the logic to its new home.
"""

from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[2] / "src" / "kite_alerts" / "ui" / "static"


class TestBadgeStateHandlesEveryFeedState:
    def test_badge_js_has_a_branch_for_every_feed_state(self) -> None:
        js = (STATIC_DIR / "badge.js").read_text(encoding="utf-8")
        for state in ("live", "connecting", "market_closed", "token_expired", "stale", "no_plan"):
            assert f'feedState === "{state}"' in js, f"badgeState ignores the {state!r} state"

    def test_badge_js_never_claims_live_without_fresh_ticks(self) -> None:
        js = (STATIC_DIR / "badge.js").read_text(encoding="utf-8")
        # Both the "stale" feed state and the "live but no fresh tick" path
        # return this descriptor; it must never carry the LIVE badge.
        descriptor = js.split("function staleInfo", 1)[1].split("}", 1)[0]
        assert '"LIVE"' not in descriptor, "a stale feed must not render the LIVE badge"
        assert 'badge: "Stale"' in descriptor, "a stale feed must render the Stale badge"


class TestBadgeStylesExist:
    def test_css_styles_the_honest_badge_labels(self) -> None:
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        for badge in ("CONNECTING", "STALE", "CLOSED", "EXPIRED", "NO PLAN", "OFF"):
            assert f'data-badge="{badge}"' in css, f"no style for the {badge!r} badge"

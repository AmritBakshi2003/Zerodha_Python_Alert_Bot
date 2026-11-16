"""N1/F5 — the frontend must render the honest feed badges.

On main the chart badge node could show a plain green ``LIVE`` while the feed
reported ``stale`` (the JS ``updateLiveBadge`` had no branch for that state,
and the payload badge was hard-coded "LIVE" for connecting/stale/closed too).
These are static-text guarantees: they fail on main and pin the fix.
"""

from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[2] / "src" / "kite_alerts" / "ui" / "static"


class TestUpdateLiveBadgeHandlesEveryState:
    def test_chart_js_has_a_branch_for_every_feed_state(self) -> None:
        js = (STATIC_DIR / "chart.js").read_text(encoding="utf-8")
        for state in ("live", "connecting", "market_closed", "token_expired", "stale", "no_plan"):
            assert f'st === "{state}"' in js, f"updateLiveBadge ignores the {state!r} state"

    def test_chart_js_never_claims_live_without_fresh_ticks(self) -> None:
        js = (STATIC_DIR / "chart.js").read_text(encoding="utf-8")
        stale_branch = js.split('st === "stale"', 1)[1].split("} else if", 1)[0]
        assert '"LIVE"' not in stale_branch, "a stale feed must not render the LIVE badge"


class TestBadgeStylesExist:
    def test_css_styles_the_honest_badge_labels(self) -> None:
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        for badge in ("CONNECTING", "STALE", "CLOSED", "EXPIRED", "NO PLAN", "OFF"):
            assert f'data-badge="{badge}"' in css, f"no style for the {badge!r} badge"

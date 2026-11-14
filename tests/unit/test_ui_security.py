"""Dashboard security: loopback only, Host validation, CSP, same-origin POST."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kite_alerts.cli.main import build_parser, main
from kite_alerts.core.errors import ConfigError
from kite_alerts.ui.app import create_app
from kite_alerts.ui.security import (
    CHART_STYLE_SHA256,
    CONTENT_SECURITY_POLICY,
    host_header_allowed,
    is_loopback,
    validate_bind_host,
)
from kite_alerts.ui.server import (
    _open_when_ready,
    dashboard_url,
    run_ui,
    validate_port,
)
from unit.ui_support import (
    BASE_URL,
    FakeNotifierFactory,
    make_client,
    make_context,
    make_settings,
)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = make_settings(tmp_path / "alerts.db")
    context = make_context(settings, notifier_factory=FakeNotifierFactory([("console", None)]))
    with make_client(context) as test_client:
        yield test_client


class TestLoopback:
    @pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost", "::1", "[::1]"])
    def test_loopback_names_are_allowed(self, host: str) -> None:
        assert is_loopback(host)
        assert validate_bind_host(host) == host

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", "", "::"])
    def test_non_loopback_is_refused_with_a_clear_message(self, host: str) -> None:
        assert not is_loopback(host)
        with pytest.raises(ConfigError) as excinfo:
            validate_bind_host(host)
        message = str(excinfo.value)
        assert "only listens on this computer" in message
        assert "--host 127.0.0.1" in message

    def test_cli_refuses_a_public_host(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--env-file", str(tmp_path / "missing.env"), "ui", "--host", "0.0.0.0"])
        assert code == 2
        assert "loopback" in capsys.readouterr().err

    def test_invalid_port(self) -> None:
        with pytest.raises(ConfigError):
            validate_port(0)
        with pytest.raises(ConfigError):
            validate_port(70000)

    @pytest.mark.parametrize(
        ("header", "allowed"),
        [
            ("127.0.0.1:8765", True),
            ("localhost:8765", True),
            ("[::1]:8765", True),
            ("evil.example:8765", False),
            ("127.0.0.1.evil.example", False),
            ("", False),
        ],
    )
    def test_host_header_validation(self, header: str, allowed: bool) -> None:
        assert host_header_allowed(header) is allowed


class TestHeadersAndOrigins:
    def test_dns_rebinding_host_is_rejected(self, tmp_path: Path) -> None:
        context = make_context(make_settings(tmp_path / "alerts.db"))
        app = create_app(context)
        with TestClient(app, base_url="http://evil.example:8765") as evil:
            for path in ("/", "/api/status", "/api/demo/timeline"):
                response = evil.get(path)
                assert response.status_code == 400
                assert "unexpected host name" in response.json()["detail"]

    def test_localhost_host_header_is_accepted(self, tmp_path: Path) -> None:
        context = make_context(make_settings(tmp_path / "alerts.db"))
        with make_client(context, base_url="http://localhost:8765") as local:
            assert local.get("/api/status").status_code == 200

    def test_restrictive_headers_on_every_response(self, client: TestClient) -> None:
        response = client.get("/api/status")
        assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert "default-src 'none'" in CONTENT_SECURITY_POLICY
        script_directive = next(
            part for part in CONTENT_SECURITY_POLICY.split(";") if "script-src" in part
        )
        assert "'self'" in script_directive
        assert "unsafe-inline" not in script_directive
        assert "unsafe-eval" not in script_directive
        # The vendored chart positions canvases with style attributes; CSP3 lets
        # us allow those without allowing arbitrary inline <style> blocks or
        # scripts. Its one static stylesheet is allowed by hash.
        assert "style-src-attr 'unsafe-inline'" in CONTENT_SECURITY_POLICY
        assert "style-src-elem 'self' '" in CONTENT_SECURITY_POLICY
        assert CHART_STYLE_SHA256 in CONTENT_SECURITY_POLICY
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cache-control"] == "no-store"

    def test_no_permissive_cors(self, client: TestClient) -> None:
        response = client.get(
            "/api/status",
            headers={"Origin": "https://evil.example"},
        )
        assert "access-control-allow-origin" not in response.headers
        preflight = client.options(
            "/api/test-alert",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in preflight.headers

    def test_cross_site_test_alert_is_rejected(self, client: TestClient) -> None:
        foreign = client.post("/api/test-alert", headers={"Origin": "https://evil.example"})
        assert foreign.status_code == 403
        assert "only accepted from the dashboard" in foreign.json()["detail"]

        fetch_metadata = client.post("/api/test-alert", headers={"Sec-Fetch-Site": "cross-site"})
        assert fetch_metadata.status_code == 403

    def test_same_origin_test_alert_is_allowed(self, client: TestClient) -> None:
        response = client.post(
            "/api/test-alert",
            headers={"Origin": BASE_URL, "Sec-Fetch-Site": "same-origin"},
        )
        assert response.status_code == 200
        assert response.json()["results"][0]["channel"] == "console"


class TestStaticAssets:
    def test_index_is_served_locally(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        body = response.text
        assert "Kite Alerts" in body
        assert "never places orders" in body
        assert "/static/app.js" in body
        assert 'src="http' not in body

    def test_chart_library_is_vendored(self, client: TestClient) -> None:
        response = client.get(
            "/static/vendor/lightweight-charts/lightweight-charts.standalone.production.js"
        )
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]
        assert "Lightweight Charts" in response.text[:400]
        assert client.get("/static/styles.css").status_code == 200
        assert client.get("/static/favicon.svg").status_code == 200

    def test_licence_and_notice_are_served(self, client: TestClient) -> None:
        licence = client.get("/license")
        assert licence.status_code == 200
        assert licence.headers["content-type"].startswith("text/plain")
        assert "Apache License" in licence.text
        notice = client.get("/notice")
        assert notice.status_code == 200
        assert "TradingView" in notice.text

    def test_no_innerhtml_and_no_cdn(self, client: TestClient) -> None:
        script = client.get("/static/app.js").text
        assert "innerHTML" not in script
        for forbidden in ("cdn.", "unpkg", "jsdelivr", "googleapis", "fonts.gstatic"):
            assert forbidden not in script
            assert forbidden not in client.get("/").text
            assert forbidden not in client.get("/static/styles.css").text


class TestCliAndBrowser:
    def test_ui_subcommand_defaults(self) -> None:
        args = build_parser().parse_args(["ui"])
        assert args.host == "127.0.0.1"
        assert args.port == 8765
        assert args.open_browser is False
        assert args.db is None

    def test_ui_subcommand_flags(self, tmp_path: Path) -> None:
        db = tmp_path / "custom.db"
        args = build_parser().parse_args(
            ["ui", "--host", "localhost", "--port", "9000", "--open", "--db", str(db)]
        )
        assert (args.host, args.port, args.open_browser, args.db) == ("localhost", 9000, True, db)

    def test_missing_ui_extra_is_explained(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setitem(sys.modules, "kite_alerts.ui.server", None)
        assert main(["ui"]) == 2
        error = capsys.readouterr().err
        assert 'pip install -e ".[ui]"' in error

    def test_dashboard_url_uses_ipv4_for_localhost(self) -> None:
        assert dashboard_url("localhost", 8765) == "http://127.0.0.1:8765/"
        assert dashboard_url("127.0.0.1", 8765) == "http://127.0.0.1:8765/"

    def test_browser_opens_only_when_the_port_answers(self) -> None:
        opened: list[str] = []
        url = "http://127.0.0.1:8765/"
        _open_when_ready(
            url,
            opened.append,
            host="127.0.0.1",
            port=8765,
            listening=lambda _host, _port: True,
        )
        assert opened == [url]

        never: list[str] = []
        _open_when_ready(
            url,
            never.append,
            host="127.0.0.1",
            port=8765,
            timeout=0.2,
            sleep=lambda _seconds: None,
            listening=lambda _host, _port: False,
        )
        assert never == []

    def test_run_ui_passes_host_and_port_to_uvicorn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        captured: dict[str, object] = {}

        def fake_run(app: object, **kwargs: object) -> None:
            captured["app"] = app
            captured.update(kwargs)

        monkeypatch.setattr("uvicorn.run", fake_run)
        code = run_ui(
            port=8899,
            env_file=tmp_path / "missing.env",
            db_path=tmp_path / "alerts.db",
        )
        assert code == 0
        assert captured["host"] == "127.0.0.1"
        assert captured["port"] == 8899
        assert captured["access_log"] is False
        output = capsys.readouterr().out
        assert "8899" in output
        assert "Alerts only" in output

    def test_run_ui_refuses_a_public_host(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            run_ui(host="10.0.0.5", env_file=tmp_path / "missing.env")

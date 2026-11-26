"""Workflow-shape tests for F9 (release dry-run) and F10 (CI Python matrix).

These do not run GitHub Actions. They pin the YAML contract the audit found
broken or missing, so a later edit cannot quietly remove the dry-run path, make
a publish step unconditional, or drop a supported interpreter.

``on:`` is a YAML 1.1 boolean, so PyYAML loads the key as ``True``; the helpers
below normalise that instead of hiding it in every test.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

#: Publish commands that must never run on a dry-run dispatch.
PUBLISH_COMMANDS = ("gh release create", "twine upload", "pypi")

_ACTION_RE = re.compile(r"^(?P<action>[\w.-]+/[\w.-]+)@(?P<version>v[\w.]+)$")


def _load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{name} did not load as a mapping"
    return data


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict), "workflow has no trigger mapping"
    return triggers


def _jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "workflow has no jobs mapping"
    assert jobs, "workflow has no jobs"
    return jobs


def _steps(workflow: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (job_name, step)
        for job_name, job in _jobs(workflow).items()
        for step in job.get("steps", [])
    ]


def _publish_steps(workflow: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (job_name, step)
        for job_name, step in _steps(workflow)
        if any(command in str(step.get("run", "")) for command in PUBLISH_COMMANDS)
    ]


# --------------------------------------------------------------------- F9


def test_release_can_be_dispatched_as_a_dry_run() -> None:
    triggers = _triggers(_load("release.yml"))
    assert "workflow_dispatch" in triggers, "release.yml has no manual dry-run trigger"
    dispatch = triggers["workflow_dispatch"]
    assert isinstance(dispatch, dict)
    inputs = dispatch.get("inputs")
    assert isinstance(inputs, dict)
    dry_run = inputs.get("dry_run")
    assert isinstance(dry_run, dict), "workflow_dispatch has no dry_run input"
    assert dry_run.get("type") == "boolean"
    assert dry_run.get("default") is True


def test_release_still_starts_on_version_tags() -> None:
    triggers = _triggers(_load("release.yml"))
    assert triggers["push"]["tags"] == ["v*"]


def test_every_publish_step_is_guarded_against_dry_run() -> None:
    publish_steps = _publish_steps(_load("release.yml"))
    assert publish_steps, "expected at least one publish step in release.yml"
    for job_name, step in publish_steps:
        condition = str(step.get("if", ""))
        assert "github.event_name == 'push'" in condition, (
            f"{job_name}/{step.get('name', 'publish step')} would also run on a "
            f"workflow_dispatch dry run (if: {condition!r})"
        )


def test_artifact_actions_use_one_version_each() -> None:
    versions: dict[str, set[str]] = {}
    for path in WORKFLOWS.glob("*.yml"):
        for _job_name, step in _steps(_load(path.name)):
            match = _ACTION_RE.match(str(step.get("uses", "")))
            if not match:
                continue
            action = match.group("action")
            if action in {"actions/upload-artifact", "actions/download-artifact"}:
                versions.setdefault(action, set()).add(match.group("version"))
    assert versions, "no artifact actions found"
    for action, found in versions.items():
        assert len(found) == 1, f"{action} is pinned to more than one version: {sorted(found)}"


# --------------------------------------------------------------------- F10


def _matrix_rows(job: dict[str, Any]) -> list[dict[str, Any]]:
    matrix = job["strategy"]["matrix"]
    assert isinstance(matrix, dict)
    include = matrix.get("include")
    if include:
        assert isinstance(include, list)
        return [dict(row) for row in include]
    versions = matrix.get("python-version")
    assert isinstance(versions, list)
    return [{"python-version": version, "experimental": False} for version in versions]


def test_ci_matrix_covers_every_supported_python() -> None:
    workflow = _load("ci.yml")
    job = _jobs(workflow)["test"]
    rows = _matrix_rows(job)
    versions = {str(row["python-version"]) for row in rows}
    assert versions == {"3.10", "3.11", "3.12", "3.13", "3.14"}
    experimental = {str(row["python-version"]) for row in rows if row.get("experimental")}
    assert experimental == {
        "3.13",
        "3.14",
    }, "the owner runs 3.14, so 3.13/3.14 must be in the matrix (experimental is fine)"
    guard = str(job.get("continue-on-error", ""))
    assert "matrix.experimental" in guard, (
        "experimental interpreters must report without blocking the build "
        f"(continue-on-error: {guard!r})"
    )

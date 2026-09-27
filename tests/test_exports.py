"""T2: CSV export at every stage, and the live progress dashboard."""

import csv
import io

import pytest

from doggfather.services.exports import KINDS, safe_cell

SLUG = "sample-hack-2026"


def rows_of(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_every_export_is_csv_for_organizers(as_role, kind):
    response = as_role("organizer").get(f"/api/events/evt_01/export/{kind}.csv")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    header = response.text.splitlines()[0]
    assert "," in header


def test_results_export_has_all_projects_and_methods(as_role):
    rows = rows_of(as_role("organizer").get("/api/events/evt_01/export/results.csv").text)
    assert rows[0][:3] == ["rank", "project_id", "title"]
    assert {"raw_mean", "zscore", "bias_model", "bt_theta"} <= set(rows[0])
    assert len(rows) == 42  # header + 41 projects


def test_scores_export_has_one_row_per_scorecard(as_role):
    rows = rows_of(as_role("organizer").get("/api/events/evt_01/export/scores.csv").text)
    assert rows[0][:5] == ["judge_id", "judge_name", "project_id", "project_title", "track"]
    assert len(rows) == 127


@pytest.mark.parametrize("role", [None, "participant", "judge_a"])
def test_exports_are_organizer_only(as_role, role):
    assert as_role(role).get("/api/events/evt_01/export/scores.csv").status_code in (401, 403)


def test_unknown_export_is_404(as_role):
    assert as_role("organizer").get("/api/events/evt_01/export/secrets.csv").status_code == 404


def test_formula_cells_are_neutralised(as_role, conn):
    conn.execute("UPDATE projects SET title = '=HYPERLINK(\"http://evil\",\"x\")' WHERE id = 'prj_01'")
    text = as_role("organizer").get("/api/events/evt_01/export/projects.csv").text
    assert "'=HYPERLINK" in text
    assert safe_cell("-3.5") == "-3.5"  # real numbers stay numbers
    assert safe_cell("@SUM(A1)") == "'@SUM(A1)"


def test_progress_snapshot_flags_outstanding_work(as_role):
    snap = as_role("organizer").get("/api/events/evt_01/progress").json()
    assert snap["totals"]["assigned"] == 126 + 8
    assert snap["totals"]["pending"] == 8
    assert snap["totals"]["projects_below_target"] == 8  # the fixture's unfinished batches
    stuck = [j for j in snap["judges"] if j["pending"]]
    assert stuck and all(j["abandoned_batches"] == ["B-001"] or j["done"] for j in stuck)


@pytest.mark.parametrize("role", [None, "participant", "judge_a"])
def test_progress_is_organizer_only(as_role, role):
    assert as_role(role).get("/api/events/evt_01/progress").status_code in (401, 403)


def test_organizer_pages_render(as_role):
    org = as_role("organizer")
    for page in ("progress", "exports"):
        assert org.get(f"/organize/{SLUG}/{page}").status_code == 200

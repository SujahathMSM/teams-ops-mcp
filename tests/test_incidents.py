from __future__ import annotations

import pytest

from app.tools.incidents import IncidentSearchError, search_incidents


async def test_incidents_search_finds_seeded_deadlock(setup_test_db: None) -> None:
    result = await search_incidents("postgres deadlock", limit=5)
    assert result["match_count"] >= 1
    top = result["incidents"][0]
    assert top["incident_key"] == "INC-1008"
    assert "runbook_steps" in top
    assert top["relevance_score"] > 0


async def test_incidents_search_matches_exact_incident_key(setup_test_db: None) -> None:
    result = await search_incidents("INC-1016", limit=5)
    assert result["match_count"] >= 1
    assert result["incidents"][0]["incident_key"] == "INC-1016"


async def test_incidents_search_returns_empty_for_unmatched_query(setup_test_db: None) -> None:
    result = await search_incidents("zzznonexistentincident999", limit=5)
    assert result["match_count"] == 0
    assert result["incidents"] == []


async def test_incidents_search_validates_inputs(setup_test_db: None) -> None:
    with pytest.raises(IncidentSearchError, match="cannot be empty"):
        await search_incidents("   ")

    with pytest.raises(IncidentSearchError, match="between 1 and 20"):
        await search_incidents("redis", limit=25)

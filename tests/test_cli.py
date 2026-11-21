from __future__ import annotations

import pytest

from app.auth import get_user_by_token
from app.cli import _deactivate_user
from tests.conftest import TestUser


async def test_deactivate_user_revokes_token(
    viewer_user: TestUser,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert await get_user_by_token(viewer_user.raw_token) is not None

    exit_code = await _deactivate_user("alice")

    assert exit_code == 0
    assert "User deactivated" in capsys.readouterr().out
    assert await get_user_by_token(viewer_user.raw_token) is None


async def test_deactivate_unknown_user_fails(
    clean_db: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = await _deactivate_user("ghost")

    assert exit_code == 1
    assert "not found" in capsys.readouterr().err

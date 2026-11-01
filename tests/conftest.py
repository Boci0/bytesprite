import pytest


@pytest.fixture(autouse=True)
def _no_explorer_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    # Drop tests would otherwise open real Explorer windows on pytest's temp folders
    import src.ui.pet_window as pet_window

    monkeypatch.setattr(pet_window, "open_folder_in_background", lambda folder: None)


@pytest.fixture(autouse=True)
def _no_real_skills(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # The user's own AI chat skills would otherwise leak into system prompts under test
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))

import pytest


@pytest.fixture(autouse=True)
def _no_explorer_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    # Drop tests would otherwise open real Explorer windows on pytest's temp folders
    import src.ui.pet_window as pet_window

    monkeypatch.setattr(pet_window, "open_folder_in_background", lambda folder: None)

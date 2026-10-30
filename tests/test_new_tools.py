"""Unit, security, and UI integration tests for FileForgePlugin and ClipVaultPlugin."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
import time
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QMimeData, QPointF, Qt, QUrl
from PyQt6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QImage,
)
from PyQt6.QtWidgets import QApplication

from src.engine.models import PetState, PetStats
from src.engine.pet_state import PetStateMachine
from src.main import ByteSpriteApp
from src.storage.storage_manager import StorageManager
from src.tools.base import ToolPlugin
from src.tools.clip_vault import ClipVaultPlugin
from src.tools.file_forge import (
    FileForgePlugin,
    ForgeMode,
    compress_to_tar_xz,
    compress_to_zip,
    convert_data_file,
    convert_image_file,
    extract_archive_safe,
    get_wheel_options_for_paths,
    process_paths_sync,
)
from src.ui.pet_window import PetWindow


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


class TestFileForgeCompressionAndSecurity:
    def test_plugin_implements_tool_protocol(self) -> None:
        forge = FileForgePlugin()
        try:
            assert isinstance(forge, ToolPlugin)
            assert forge.tool_id == "forge"
            hud = forge.get_hud_data()
            assert hud.badge_left == "FILE.FRG"
            assert len(hud.bars) == 3
        finally:
            forge.close()

    def test_zip_compression_uses_stored_bypass_for_precompressed_and_deflate_for_text(
        self, tmp_path: Path
    ) -> None:
        src_dir = tmp_path / "project_bundle"
        src_dir.mkdir()
        (src_dir / "empty_sub").mkdir()
        (src_dir / "readme.txt").write_text("ByteSprite " * 400, encoding="utf-8")
        (src_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x12\x34" * 200)

        res = compress_to_zip([src_dir])
        assert res.ok is True
        assert len(res.output_paths) == 1
        archive = res.output_paths[0]
        assert archive.suffix == ".zip"
        assert archive.exists()

        with zipfile.ZipFile(archive, "r") as zf:
            info_map = {zi.filename: zi for zi in zf.infolist()}
            assert info_map["readme.txt"].compress_type == zipfile.ZIP_DEFLATED
            assert info_map["photo.jpg"].compress_type == zipfile.ZIP_STORED
            assert "empty_sub/" in info_map

        # Collision-safe second run creates _1.zip without overwriting
        res2 = compress_to_zip([src_dir])
        assert res2.ok is True
        assert res2.output_paths[0] != archive
        assert res2.output_paths[0].name.endswith("_1.zip")

    def test_tar_xz_ultra_compression_and_safe_roundtrip(self, tmp_path: Path) -> None:
        src_dir = tmp_path / "logs"
        src_dir.mkdir()
        payload = "INFO 2026-10-30 high-frequency telemetry sample line\n" * 600
        (src_dir / "app.log").write_text(payload, encoding="utf-8")

        comp = compress_to_tar_xz([src_dir])
        assert comp.ok is True
        txz_path = comp.output_paths[0]
        assert txz_path.name == "logs.tar.xz"
        assert comp.saved_percent > 80.0

        ext = extract_archive_safe(txz_path)
        assert ext.ok is True
        extracted_dir = ext.output_paths[0]
        assert extracted_dir.name == "logs_1"
        assert (extracted_dir / "app.log").read_text(encoding="utf-8") == payload

    def test_single_gzip_stream_decompression(self, tmp_path: Path) -> None:
        gz_file = tmp_path / "dump.sql.gz"
        raw = b"SELECT 1;\n" * 200
        with gzip.open(gz_file, "wb") as f:
            f.write(raw)

        res = process_paths_sync([gz_file], ForgeMode.AUTO)
        assert res.ok is True
        assert res.output_paths[0].name == "dump.sql"
        assert res.output_paths[0].read_bytes() == raw

    def test_zip_slip_path_traversal_is_blocked_atomically(self, tmp_path: Path) -> None:
        evil_zip = tmp_path / "malicious.zip"
        outside_victim = tmp_path / "escaped.txt"
        with zipfile.ZipFile(evil_zip, "w") as zf:
            zf.writestr("safe.txt", "benign")
            zf.writestr("../escaped.txt", "pwned")

        res = extract_archive_safe(evil_zip)
        assert res.ok is False
        assert res.toast_text == "UNSAFE!"
        assert not outside_victim.exists()
        # Staging folder must be completely wiped on rollback
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".forge_")]
        assert leftovers == []

    def test_tar_symlink_and_traversal_blocked(self, tmp_path: Path) -> None:
        evil_tar = tmp_path / "bad_link.tar"
        with tarfile.open(evil_tar, "w") as tf:
            sym = tarfile.TarInfo(name="sneaky_link")
            sym.type = tarfile.SYMTYPE
            sym.linkname = "/etc/passwd"
            tf.addfile(sym)

        res = extract_archive_safe(evil_tar)
        assert res.ok is False
        assert res.toast_text == "UNSAFE!"

    def test_decompression_bomb_ceiling_aborts_and_cleans_up(self, tmp_path: Path) -> None:
        bomb_zip = tmp_path / "bomb.zip"
        with zipfile.ZipFile(bomb_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("zeros.bin", b"\x00" * 16_384)

        res = extract_archive_safe(bomb_zip, max_extract_bytes=2048)
        assert res.ok is False
        assert res.toast_text == "UNSAFE!"
        assert not (tmp_path / "bomb").exists()


class TestFileForgeImageAndDataConversion:
    def test_png_to_webp_jpg_and_ico_conversions(
        self, qapp: QApplication, tmp_path: Path
    ) -> None:
        img = QImage(64, 64, QImage.Format.Format_RGBA8888)
        img.fill(QColor(0, 0, 0, 0))
        for y in range(16, 48):
            for x in range(16, 48):
                img.setPixelColor(x, y, QColor("#41A6F6"))
        png_path = tmp_path / "sprite.png"
        assert img.save(str(png_path), "PNG")

        # AUTO mode converts PNG -> optimized WebP
        auto_res = process_paths_sync([png_path], ForgeMode.AUTO)
        assert auto_res.ok is True
        assert auto_res.output_paths[0].suffix == ".webp"
        assert auto_res.output_paths[0].stat().st_size > 0

        # RGBA PNG -> JPG flattens transparent pixels onto white (#FFFFFF)
        jpg_res = convert_image_file(png_path, "jpg")
        assert jpg_res.ok is True
        jpg_img = QImage(str(jpg_res.output_paths[0]))
        corner = jpg_img.pixelColor(2, 2)
        assert corner.red() > 245 and corner.green() > 245 and corner.blue() > 245

        # PNG -> multi-resolution Windows .ico
        ico_res = convert_image_file(png_path, "ico")
        assert ico_res.ok is True
        raw_ico = ico_res.output_paths[0].read_bytes()
        assert raw_ico[:6] == b"\x00\x00\x01\x00\x06\x00"

    def test_json_csv_and_toml_conversions(self, tmp_path: Path) -> None:
        json_path = tmp_path / "users.json"
        records = [
            {"id": "001", "name": "Ada", "role": "eng"},
            {"id": "002", "name": "Linus", "role": "ops"},
        ]
        json_path.write_text(json.dumps(records, indent=2), encoding="utf-8")

        # JSON -> CSV
        csv_res = convert_data_file(json_path)
        assert csv_res.ok is True
        csv_path = csv_res.output_paths[0]
        assert csv_path.suffix == ".csv"
        assert "Ada" in csv_path.read_text(encoding="utf-8")

        # CSV (with UTF-8 BOM) -> JSON preserves leading zeros in "001"
        bom_csv = tmp_path / "excel_export.csv"
        bom_csv.write_bytes(b"\xef\xbb\xbf" + csv_path.read_bytes())
        back_res = convert_data_file(bom_csv)
        assert back_res.ok is True
        reloaded = json.loads(back_res.output_paths[0].read_text(encoding="utf-8"))
        assert reloaded == records

        # TOML -> JSON
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            'title = "ByteSprite"\nworkers = 1\nrelease = 2026-10-30\n',
            encoding="utf-8",
        )
        toml_res = convert_data_file(toml_path)
        assert toml_res.ok is True
        parsed_toml = json.loads(toml_res.output_paths[0].read_text(encoding="utf-8"))
        assert parsed_toml["title"] == "ByteSprite"
        assert parsed_toml["workers"] == 1
        assert parsed_toml["release"] == "2026-10-30"


class TestClipVaultPlugin:
    def test_ring_deduplication_bounds_privacy_and_transforms(self, tmp_path: Path) -> None:
        box = [""]
        paths_box: list[Path] = []

        vault = ClipVaultPlugin(
            read_text=lambda: box[0],
            write_text=lambda s: box.__setitem__(0, s),
            read_paths=lambda: paths_box,
        )
        assert isinstance(vault, ToolPlugin)

        # Privacy marker blocks password manager captures
        assert (
            vault.capture_text(
                "super-secret-password",
                mime_formats=("ExcludeClipboardContentFromMonitorProcessing",),
            )
            is False
        )
        assert len(vault.entries) == 0

        # Oversized (> 32 KiB) text ignored to cap RAM
        assert vault.capture_text("x" * 40_000) is False
        assert len(vault.entries) == 0

        # Bounded 9-slot ring + O(1) promotion on duplicate copy
        for i in range(12):
            vault.capture_text(f"item-{i}")
        assert len(vault.entries) == ClipVaultPlugin.MAX_SLOTS == 9
        assert vault.entries[0].text == "item-11"

        vault.capture_text("item-5")
        assert vault.entries[0].text == "item-5"
        assert sum(1 for e in vault.entries if e.text == "item-5") == 1

        # Persistence never leaks raw clipboard text
        serialized = vault.to_dict()
        assert "entries" not in serialized
        assert "item-5" not in json.dumps(serialized)

        # JSON format <-> minify toggle
        box[0] = '{"alpha":1,"beta":[2,3]}'
        ok, toast, _ = vault.execute_action("clipvault:json_toggle")
        assert ok and toast == "JSON FMT"
        assert "\n  " in box[0]
        ok, toast, _ = vault.execute_action("clipvault:json_toggle")
        assert ok and toast == "JSON MIN"
        assert box[0] == '{"alpha":1,"beta":[2,3]}'

        # Plain-text clean transform
        box[0] = "  hello   \r\nworld\t\t\r\n\r\n\r\n\r\ntail  "
        ok, toast, _ = vault.execute_action("clipvault:clean_text")
        assert ok and toast == "CLEANED"
        assert box[0] == "hello\nworld\n\ntail"

        # Base64 encode <-> decode reversible toggle
        box[0] = "ByteSprite Pixel Companion"
        ok, toast, _ = vault.execute_action("clipvault:b64_toggle")
        assert ok and toast == "B64 ENC"
        assert box[0] != "ByteSprite Pixel Companion"
        ok, toast, _ = vault.execute_action("clipvault:b64_toggle")
        assert ok and toast == "B64 DEC"
        assert box[0] == "ByteSprite Pixel Companion"

        # Streaming file SHA-256 digest
        sample_bin = tmp_path / "firmware.bin"
        sample_bin.write_bytes(b"bytesprite-binary-payload")
        expected_sha = hashlib.sha256(b"bytesprite-binary-payload").hexdigest()
        paths_box.append(sample_bin)
        ok, toast, _ = vault.execute_action("clipvault:sha256")
        assert ok and toast == "FILE SHA"
        assert box[0] == expected_sha


class TestDragDropAndMenuIntegration:
    def test_drop_file_onto_pet_window_runs_async_forge_and_shows_submenus(
        self, qapp: QApplication, tmp_path: Path
    ) -> None:
        storage = StorageManager(tmp_path / "app_state.json")
        controller = ByteSpriteApp(app=qapp, storage=storage, enable_tray=False)
        try:
            win: PetWindow = controller.window
            assert win.acceptDrops() is True

            # Verify Context Menu organizes File Forge and Clip Vault into clean submenus
            menu = win.build_context_menu()
            try:
                assert FileForgePlugin.SUBMENU_NAME in menu.plugin_submenus
                assert ClipVaultPlugin.SUBMENU_NAME in menu.plugin_submenus
                assert "forge:mode:txz" in menu.plugin_actions
                assert "clipvault:json_toggle" in menu.plugin_actions
            finally:
                menu.deleteLater()

            # Drop a local text file directly onto PetWindow
            sample = tmp_path / "notes.txt"
            sample.write_text("compress me via drag and drop!\n" * 250, encoding="utf-8")

            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(sample))])
            drop_evt = QDropEvent(
                QPointF(win.sprite_center_point()),
                Qt.DropAction.CopyAction,
                mime,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            win.dropEvent(drop_evt)

            assert controller.state_machine.active_tool_id == "forge"
            # Wait briefly for single-worker thread to complete compression
            forge = controller.state_machine.forge_plugin
            assert forge is not None
            deadline = time.monotonic() + 3.0
            while forge.busy and time.monotonic() < deadline:
                time.sleep(0.02)

            assert not forge.busy
            controller._on_simulation_tick()
            assert (tmp_path / "notes.zip").exists()
            assert forge.completed_jobs == 1
            # Once notification is drained, adaptive timer returns to idle 2000ms cadence
            assert controller.sim_timer.interval() == 2_000
        finally:
            controller.shutdown()

    def test_hovering_file_spawns_radial_wheel_for_exact_format_or_skip_cancel(
        self, qapp: QApplication, tmp_path: Path
    ) -> None:
        storage = StorageManager(tmp_path / "wheel_state.json")
        controller = ByteSpriteApp(app=qapp, storage=storage, enable_tray=False)
        try:
            win: PetWindow = controller.window
            win.move(300, 300)
            win.show()
            qapp.processEvents()

            img = QImage(32, 32, QImage.Format.Format_RGBA8888)
            img.fill(QColor("#38B764"))
            png_file = tmp_path / "badge.png"
            assert img.save(str(png_file), "PNG")

            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(png_file))])

            # 1. Hovering file over PetWindow spawns the context-aware radial wheel
            enter_evt = QDragEnterEvent(
                win.sprite_center_point(),
                Qt.DropAction.CopyAction,
                mime,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            win.dragEnterEvent(enter_evt)

            wheel = win.drop_wheel
            assert wheel is not None
            assert wheel.isVisible() is True

            modes = [opt.mode for opt in wheel.options]
            assert ForgeMode.WEBP in modes
            assert ForgeMode.PNG in modes
            assert ForgeMode.JPG in modes
            assert ForgeMode.ICO in modes
            assert ForgeMode.BMP in modes
            assert ForgeMode.ZIP in modes
            assert ForgeMode.TAR_XZ in modes

            # Every slice center point maps 1:1 to its own index; hub maps to -1 (SKIP)
            for idx in range(len(wheel.options)):
                pt = wheel.slice_center_point(idx)
                assert wheel.hit_test_option_index(QPointF(pt)) == idx
            assert wheel.hit_test_option_index(QPointF(wheel.hub_center_point())) == -1

            # 2. Aim cursor onto the ICO slice and render wheel cleanly
            ico_idx = modes.index(ForgeMode.ICO)
            ico_pt = wheel.slice_center_point(ico_idx)
            move_evt = QDragMoveEvent(
                ico_pt,
                Qt.DropAction.CopyAction,
                mime,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            wheel.dragMoveEvent(move_evt)
            assert wheel.hovered_index == ico_idx
            assert wheel.selected_option() is not None
            assert wheel.selected_option().mode == ForgeMode.ICO  # type: ignore[union-attr]

            canvas = QImage(
                wheel.width(),
                wheel.height(),
                QImage.Format.Format_ARGB32_Premultiplied,
            )
            canvas.fill(0)
            wheel.render(canvas)
            assert canvas.pixelColor(int(wheel.CENTER_X), int(wheel.CENTER_Y)).alpha() > 0

            # 3. Drop on ICO slice -> dismisses wheel and creates badge.ico (not .webp)
            drop_ico = QDropEvent(
                QPointF(ico_pt),
                Qt.DropAction.CopyAction,
                mime,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            wheel.dropEvent(drop_ico)
            assert wheel.isVisible() is False

            forge = controller.state_machine.forge_plugin
            assert forge is not None
            deadline = time.monotonic() + 3.0
            while forge.busy and time.monotonic() < deadline:
                time.sleep(0.02)
            controller._on_simulation_tick()

            assert (tmp_path / "badge.ico").exists()
            assert not (tmp_path / "badge.webp").exists()
            assert forge.completed_jobs == 1

            # 4. Hovering a JSON file and dropping on center SKIP hub performs NO conversion
            json_file = tmp_path / "items.json"
            json_file.write_text('[{"a": 1}]', encoding="utf-8")
            mime_json = QMimeData()
            mime_json.setUrls([QUrl.fromLocalFile(str(json_file))])

            win.dragEnterEvent(
                QDragEnterEvent(
                    win.sprite_center_point(),
                    Qt.DropAction.CopyAction,
                    mime_json,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
            )
            assert wheel.isVisible() is True
            json_modes = [opt.mode for opt in wheel.options]
            assert ForgeMode.CSV in json_modes
            assert ForgeMode.TSV in json_modes
            assert ForgeMode.JSON_MIN in json_modes

            wheel.dropEvent(
                QDropEvent(
                    QPointF(wheel.hub_center_point()),
                    Qt.DropAction.CopyAction,
                    mime_json,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
            )
            assert wheel.isVisible() is False
            assert forge.completed_jobs == 1
            assert not (tmp_path / "items.csv").exists()
            assert not (tmp_path / "items.zip").exists()

            # 5. Dragging out of wheel (dragLeaveEvent) dismisses immediately
            win.dragEnterEvent(
                QDragEnterEvent(
                    win.sprite_center_point(),
                    Qt.DropAction.CopyAction,
                    mime_json,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
            )
            assert wheel.isVisible() is True
            wheel.dragLeaveEvent(QDragLeaveEvent())
            assert wheel.isVisible() is False
        finally:
            controller.shutdown()

    def test_wheel_explicit_modes_tgz_gzip_tsv_clean_and_sha256(
        self, tmp_path: Path
    ) -> None:
        script = tmp_path / "deploy.py"
        script.write_text("print('hi')   \r\nx = 1\t\r\n", encoding="utf-8")

        opts = [o.mode for o in get_wheel_options_for_paths([script])]
        assert ForgeMode.GZIP in opts
        assert ForgeMode.TAR_GZ in opts
        assert ForgeMode.CLEAN_TEXT in opts
        assert ForgeMode.SHA256 in opts

        clean_res = process_paths_sync([script], ForgeMode.CLEAN_TEXT)
        assert clean_res.ok is True
        assert clean_res.output_paths[0].read_bytes() == b"print('hi')\nx = 1\n"

        gz_res = process_paths_sync([script], ForgeMode.GZIP)
        assert gz_res.ok is True
        assert gz_res.output_paths[0].name == "deploy.py.gz"

        tgz_res = process_paths_sync([script], ForgeMode.TAR_GZ)
        assert tgz_res.ok is True
        assert tgz_res.output_paths[0].name == "deploy.tar.gz"

        sha_res = process_paths_sync([script], ForgeMode.SHA256)
        assert sha_res.ok is True
        assert sha_res.output_paths[0].name == "deploy.py.sha256"
        expected = hashlib.sha256(script.read_bytes()).hexdigest()
        assert expected in sha_res.output_paths[0].read_text(encoding="utf-8")

        csv_in = tmp_path / "grid.csv"
        csv_in.write_text("k,v\nalpha,10\n", encoding="utf-8")
        tsv_res = process_paths_sync([csv_in], ForgeMode.TSV)
        assert tsv_res.ok is True
        assert tsv_res.output_paths[0].suffix == ".tsv"
        assert "alpha\t10" in tsv_res.output_paths[0].read_text(encoding="utf-8")

    def test_codebase_free_of_em_dashes(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        for folder in ("src", "tests"):
            for py_file in (repo_root / folder).rglob("*.py"):
                if py_file.name == "test_new_tools.py":
                    continue
                content = py_file.read_text(encoding="utf-8")
                assert "\u2014" not in content, f"Em dash found in {py_file}"


def test_clip_vault_pins_survive_rotation_and_clear() -> None:
    written: list[str] = []
    vault = ClipVaultPlugin(read_text=lambda: "", write_text=written.append)
    vault.capture_text("keep me")
    assert vault.execute_action("clipvault:pin")[1] == "PINNED"
    assert vault.execute_action("clipvault:pin")[1] == "PINNED"  # same digest not pinned twice
    assert len(vault.pins) == 1
    for i in range(ClipVaultPlugin.MAX_SLOTS + 1):
        vault.capture_text(f"filler {i}")
    vault.execute_action("clipvault:clear")
    assert vault.execute_action("clipvault:pinned:0")[0] is True
    assert written[-1] == "keep me"
    assert vault.get_hud_data().detail.startswith("Latest: keep me")
    vault.execute_action("clipvault:clear_pins")
    assert vault.pins == ()


def test_dev_transforms() -> None:
    from src.tools.clip_vault import (
        convert_case,
        decode_jwt,
        toggle_string_escape,
        toggle_timestamp,
        toggle_url_encoding,
    )

    import base64 as b64

    def seg(obj: dict) -> str:
        return b64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    token = f"{seg({'alg': 'HS256'})}.{seg({'sub': 'ab'})}.sig"
    assert json.loads(decode_jwt(token)) == {"header": {"alg": "HS256"}, "payload": {"sub": "ab"}}
    with pytest.raises(ValueError):
        decode_jwt("not.a")

    enc, badge = toggle_url_encoding("a b&c=d")
    assert (enc, badge) == ("a%20b%26c%3Dd", "URL ENC")
    assert toggle_url_encoding(enc) == ("a b&c=d", "URL DEC")

    assert toggle_timestamp("0000000000")[0] == "1970-01-01T00:00:00+00:00"
    assert toggle_timestamp("1700000000000")[0].startswith("2023-11-14T22:13:20")
    assert toggle_timestamp("2023-11-14T22:13:20Z") == ("1700000000", "EPOCH")
    with pytest.raises(ValueError):
        toggle_timestamp("hello")

    assert convert_case("parseHTTPResponse code", "snake") == "parse_http_response_code"
    assert convert_case("user-id_value", "camel") == "userIdValue"
    assert convert_case("MyClassName", "kebab") == "my-class-name"

    escaped, badge = toggle_string_escape('say "hi"\n')
    assert (escaped, badge) == ('"say \\"hi\\"\\n"', "ESCAPED")
    assert toggle_string_escape(escaped) == ('say "hi"\n', "UNESCAPED")


def test_dev_transform_actions_write_clipboard() -> None:
    written: list[str] = []
    vault = ClipVaultPlugin(read_text=lambda: "hello world", write_text=written.append)
    assert vault.execute_action("clipvault:dev:camel")[1] == "CAMEL"
    assert written[-1] == "helloWorld"
    assert vault.execute_action("clipvault:dev:jwt")[1] == "NO MATCH"
    assert vault.execute_action("clipvault:dev:uuid")[1] == "UUID"
    assert len(written[-1]) == 36
    assert any(i.submenu == ClipVaultPlugin.DEV_SUBMENU for i in vault.get_menu_items())


def test_parse_git_status() -> None:
    from src.tools.project import parse_git_status

    out = (
        "# branch.oid abc\n# branch.head main\n# branch.upstream origin/main\n"
        "# branch.ab +2 -1\n1 .M N... 100644 100644 100644 a b src/x.py\n? new.txt\n"
    )
    st = parse_git_status(out)
    assert (st.branch, st.changed, st.ahead, st.behind) == ("main", 2, 2, 1)
    st = parse_git_status("# branch.oid abc\n# branch.head (detached)\n")
    assert (st.branch, st.changed, st.ahead, st.behind) == ("detached", 0, 0, 0)


def test_project_plugin_persists_and_reads_git(tmp_path: Path) -> None:
    from src.tools.project import ProjectPlugin

    plugin = ProjectPlugin()
    assert plugin.get_hud_data().detail == "Pick a project folder"
    for i in range(ProjectPlugin.MAX_RECENT + 2):
        plugin.set_repo(tmp_path / f"p{i}")
    assert len(plugin.recent) == ProjectPlugin.MAX_RECENT
    assert plugin.recent[0] == str(tmp_path / f"p{ProjectPlugin.MAX_RECENT + 1}")

    restored = ProjectPlugin()
    restored.load_dict(plugin.to_dict())
    assert restored.repo == plugin.repo and restored.recent == plugin.recent

    # This checkout is a git repo: tick until the background git call lands
    repo = ProjectPlugin()
    repo.set_repo(Path(__file__).resolve().parents[1])
    if repo._git is None:
        pytest.skip("git not installed")
    deadline = time.monotonic() + 10
    while repo.status is None and time.monotonic() < deadline:
        repo.tick()
        time.sleep(0.05)
    assert repo.status is not None and repo.status.branch
    repo.close()


def test_port_kill_guards() -> None:
    from src.tools.ports import can_kill, kill_pid, list_listening_ports
    from src.tools.system_monitor import SystemMonitorPlugin

    assert not can_kill(0) and not can_kill(4) and not can_kill(os.getpid())
    assert kill_pid(os.getpid()) is False
    ports = list_listening_ports()
    assert isinstance(ports, list)
    assert all(1024 <= p.port < 49152 for p in ports)
    ok, toast, _ = SystemMonitorPlugin().execute_action("sysmon:kill:99999999:1")
    assert (ok, toast) == (True, "PORT FREE")

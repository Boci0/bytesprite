import pytest

from src.tools.macros import MAX_MACROS, MacroBook, describe, parse_recipe


def test_parse_recipe_steps_and_args():
    assert parse_recipe(" region > text: Explain this > ask ") == [("region", ""), ("text", "Explain this"), ("ask", "")]
    assert parse_recipe("open:https://x.com/?q={text}") == [("open", "https://x.com/?q={text}")]
    assert parse_recipe("tool:pomodoro:toggle") == [("tool", "pomodoro:toggle")]


@pytest.mark.parametrize("bad", ["", " > ", "nope", "text", "ocr:x", "open:ftp://x", "tool:macro:1", "tool:macrokey:1", "tool:update:check", "x" * 601])
def test_parse_recipe_rejects(bad):
    with pytest.raises(ValueError):
        parse_recipe(bad)


def test_describe_reads_like_english():
    assert describe("region > ocr > search") == "Snip a region, then read its text, then Google it"
    assert describe("bogus") == "unknown step 'bogus'"


def test_book_save_find_rename_remove_and_double_click():
    book = MacroBook()
    assert [m.name for m in book.macros] == ["Snap Ask"]
    assert book.save("OCR Search", "region > ocr > search") == "ADDED"
    assert book.save("OCR Search", "region") == "NAME TAKEN"
    assert book.numbered("2").name == "OCR Search" and book.numbered("9") is None and book.find("2") is None
    book.double_click = "OCR Search"
    assert book.save("Find", "region > ocr > search", old_name="OCR Search") == "SAVED"
    assert book.double_click == "Find" and book.find("Find") is not None
    with pytest.raises(ValueError):
        book.save("X", "bogus")
    assert book.remove("Find") and book.double_click == ""
    for i in range(MAX_MACROS - 1):
        book.save(f"m{i}", "copy")
    assert book.save("over", "copy") == "TOO MANY"


def test_book_round_trip_drops_bad_rows():
    book = MacroBook()
    book.save("A", "screen > ask")
    book.double_click = "A"
    again = MacroBook()
    again.load_dict(book.to_dict())
    assert [(m.name, m.recipe) for m in again.macros] == [(m.name, m.recipe) for m in book.macros]
    assert again.double_click == "A"
    again.load_dict({"list": [["B", "bogus"], ["C", "copy"], ["C", "save"], "junk"], "double_click": "B"})
    assert [m.name for m in again.macros] == ["C"] and again.double_click == ""
    fresh = MacroBook()
    fresh.load_dict({"list": []})
    assert fresh.macros == []  # deleting the defaults sticks


def test_gt_inside_text_stays_text_and_numbers_never_match_names():
    assert parse_recipe("text:use > carefully > ask") == [("text", "use > carefully"), ("ask", "")]
    book = MacroBook()
    book.macros = []
    book.save("A", "copy")
    book.save("1", "save")
    assert book.numbered("1").name == "A" and book.find("1").name == "1"

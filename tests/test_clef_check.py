"""Offline checks for scripts/clef_check.py's mark and scoring logic (no GPU, no model)."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "clef_check", Path(__file__).resolve().parent.parent / "scripts" / "clef_check.py")
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


def test_ocr_marks_reading_order_and_filtering():
    items = [
        (300, 10, 360, 30, "Sign in", 0.9),
        (10, 12, 80, 30, "Home", 0.95),
        (10, 100, 200, 130, "Search", 0.8),
        (400, 100, 420, 120, "x", 0.2),     # low score: dropped
        (500, 100, 520, 120, "   ", 0.99),  # blank: dropped
    ]
    marks = cc.ocr_marks(items)
    assert [m["text"] for m in marks] == ["Home", "Sign in", "Search"]
    assert [m["id"] for m in marks] == ["1", "2", "3"]


def test_ocr_marks_same_line_with_uneven_tops_reads_left_to_right():
    items = [(1150, 8, 1250, 34, "Sign in", 0.9), (20, 14, 90, 36, "Home", 0.9)]
    assert [m["text"] for m in cc.ocr_marks(items)] == ["Home", "Sign in"]


def test_ocr_marks_cap_keeps_largest():
    items = [(0, 0, 10, 10, "small", 0.9), (0, 50, 200, 90, "big", 0.9)]
    assert [m["text"] for m in cc.ocr_marks(items, max_marks=1)] == ["big"]


def test_grid_skips_cells_with_text_and_numbers_after_ocr():
    taken = [{"id": "1", "box": (10, 10, 40, 30), "text": "File"}]
    grid = cc.grid_marks(200, 100, cols=2, rows=2, taken=taken, start=2)
    assert len(grid) == 3  # top-left cell holds the OCR box centre
    assert [g["id"] for g in grid] == ["2", "3", "4"]
    assert grid[0]["box"] == (100, 0, 200, 50)


def test_grid_keeps_only_busy_cells():
    grid = cc.grid_marks(200, 100, cols=2, rows=2, taken=[], start=1, busy=lambda box: box[1] == 50)
    assert [g["box"] for g in grid] == [(0, 50, 100, 100), (100, 50, 200, 100)]


def test_mark_at_with_padding():
    marks = [{"id": "1", "box": (10, 10, 50, 30), "text": "a"}, {"id": "2", "box": (0, 0, 100, 100), "text": ""}]
    assert cc.mark_at(marks, 20, 20) == ["1", "2"]
    assert cc.mark_at(marks, 53, 20) == ["1", "2"]   # within 4 px pad
    assert cc.mark_at(marks, 60, 20) == ["2"]
    assert cc.mark_at(marks, 300, 300) == []


def test_valid_probs():
    assert cc.valid_probs({"a": 0.25, "b": 0.75}, ["a", "b"])
    assert not cc.valid_probs({"a": float("nan"), "b": 0.5}, ["a", "b"])
    assert not cc.valid_probs({"a": 0.5, "b": 0.4}, ["a", "b"])
    assert not cc.valid_probs({"a": 1.0}, ["a", "b"])


def test_parse_choice():
    assert cc.parse_choice("Box 12", {"3": "", "12": ""}) == "12"
    assert cc.parse_choice("Login.", cc.PAGES) == "login"
    assert cc.parse_choice("no idea", {"1": ""}) is None


def test_click_question_lists_every_mark():
    marks = [{"id": "1", "box": (0, 0, 1, 1), "text": "Search"}, {"id": "2", "box": (0, 0, 1, 1), "text": ""}]
    q = cc.click_question("the Search button", marks)
    assert set(q["criteria"]) == {"1", "2"}
    assert "Search" in q["criteria"]["1"] and "icon" in q["criteria"]["2"]

"""scripts/self_improve.py: the loop keeps a better set of notes, drops a worse one, and stays in its folder."""

import importlib.util
import sys
from pathlib import Path

from src.tools.ai_assist import AiReply

_spec = importlib.util.spec_from_file_location("self_improve", Path(__file__).parents[1] / "scripts" / "self_improve.py")
si = sys.modules["self_improve"] = importlib.util.module_from_spec(_spec)  # dataclasses look it up there
_spec.loader.exec_module(si)

TASKS = [
    si.Task("fix", "Fix teh.", {"a.txt": "teh\n"},
            lambda r, _t: "" if (r / "a.txt").read_text("utf-8") == "the\n" else "not fixed"),
    si.Task("say", "Say ok.", {}, lambda _r, t: "" if t == "ok" else "wrong"),
]


def fake_ask(proposals):
    """A model that solves "fix" only once its system prompt carries the note GOOD; proposes from `proposals`."""
    def ask(base_url, key, model, messages, host=None, web=True):
        if host is None:
            return AiReply(text=proposals.pop(0), cost_estimate=0.001)
        system, prompt = messages[0]["content"], messages[-1]["content"]
        if "Fix teh" in prompt and "GOOD" in system:
            print(host.run("edit_file", {"path": "a.txt", "old_text": "teh", "new_text": "the"}))
        return AiReply(text="ok", total_tokens=100, cost_estimate=0.001,
                       tools=[("edit_file(a.txt)", "ok")] if "GOOD" in system else [])
    return ask


def test_keeps_better_notes_and_drops_worse(tmp_path: Path) -> None:
    ask = fake_ask(["- BAD habit", "- GOOD habit\nnot a note line\n- GOOD habit"])
    lessons, best = si.improve([], 3, 1.0, ask, "u", "k", "m", tmp_path, TASKS, log=lambda _s: None)
    assert lessons == ["GOOD habit"]  # the BAD round was dropped, the duplicate merged
    assert best.score == 2


def test_improver_never_sees_held_out_tasks(tmp_path: Path) -> None:
    tasks = [si.Task(f"t{i}", f"secret prompt {i}", {}, lambda _r, _t: "fail") for i in range(3)]
    seen = []
    ask = lambda b, k, m, msgs, host=None, web=True: (seen.append(msgs[-1]["content"]), AiReply(text="- x"))[1]  # noqa: E731
    si.improve([], 1, 1.0, ask, "u", "k", "m", tmp_path, tasks, log=lambda _s: None)
    report = next(s for s in seen if "FAILURES" in s)
    assert "secret prompt 0" in report and "secret prompt 2" not in report


def test_flaky_pass_does_not_count(tmp_path: Path) -> None:
    runs = iter(["ok", "no"])
    ask = lambda *a, host=None, web=True: AiReply(text=next(runs))  # noqa: E731
    result = si.run_suite([], ask, "u", "k", "m", tmp_path, TASKS[1:], repeats=2)
    assert result.score == 0


def test_report_shows_every_run_and_recheck(tmp_path: Path) -> None:
    history = []
    ask = fake_ask(["- BAD habit", "- GOOD <habit>"])
    si.improve([], 3, 1.0, ask, "u", "k", "m", tmp_path, TASKS, log=lambda _s: None, history=history, confirm=True)
    assert [h["label"] for h in history] == ["baseline", "round 1", "round 2", "recheck: old notes", "recheck: new notes"]
    assert [h["kept"] for h in history] == [True, False, True, None, None]
    page = si.report_html(history, TASKS, "m")
    assert "GOOD &lt;habit&gt;" in page and "<habit>" not in page  # model text is escaped
    assert page.count("<tr><th>") >= 5 + len(TASKS)


def test_lucky_gain_is_undone_by_recheck(tmp_path: Path) -> None:
    good = fake_ask(["- GOOD habit"])
    calls = {"n": 0}

    def ask(b, k, m, msgs, host=None, web=True):  # notes help only on their first pass: a lucky round
        if host is not None and "GOOD" in msgs[0]["content"]:
            calls["n"] += 1
            if calls["n"] > len(TASKS):
                return AiReply(text="no")
        return good(b, k, m, msgs, host=host, web=web)
    history = []
    lessons, _ = si.improve([], 1, 1.0, ask, "u", "k", "m", tmp_path, TASKS, log=lambda _s: None, history=history,
                            confirm=True)
    assert lessons == [] and history[-1]["kept"] is False


def test_stops_on_cost_cap(tmp_path: Path) -> None:
    lessons, best = si.improve([], 3, 0.0, fake_ask([]), "u", "k", "m", tmp_path, TASKS, log=lambda _s: None)
    assert lessons == [] and best.score == 1


def test_host_refuses_paths_outside_folder(tmp_path: Path) -> None:
    host = si.BenchHost(tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    assert host.run("read_file", {"path": str(tmp_path / "x.txt")}).startswith("error: stay inside")
    assert host.run("edit_files", {"edits": [{"path": "../x.txt", "old_text": "", "new_text": "x"}]}).startswith("error: stay")
    assert host.run("run_command", {"command": "dir"}).startswith("error")
    assert not (tmp_path / "x.txt").exists()


def test_save_replaces_only_tagged_notes(tmp_path: Path) -> None:
    mem = tmp_path / "memory.md"
    mem.write_text("# mine\n- user note\n- [harness] old\n", encoding="utf-8")
    si.save_to_memory(mem, ["new one"])
    assert mem.read_text("utf-8") == "# mine\n- user note\n- [harness] new one\n"


def test_no_task_passes_untouched(tmp_path: Path) -> None:
    for task in si.TASKS:  # a check that passes on the starting files and an empty answer tests nothing
        folder = tmp_path / task.name
        for rel, text in task.files.items():
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_bytes(text.encode("utf-8"))
        folder.mkdir(exist_ok=True)
        assert task.check(folder, "") != "", task.name


def test_hard_task_checks_pass_on_correct_files(tmp_path: Path) -> None:
    by_name = {t.name: t for t in si.TASKS}
    (tmp_path / "shapes.py").write_bytes(si._CLASSES.replace("return 'meow'", "return 'roar'").replace(
        "return 'roar'", "return 'meow'", 1).encode())
    assert by_name["twin_methods"].check(tmp_path, "") == ""
    (tmp_path / "package.json").write_bytes(si._PKG.replace(
        '"chalk": {\n      "version": "2.0.0"', '"chalk": {\n      "version": "5.3.0"').encode())
    assert by_name["nested_json"].check(tmp_path, "") == ""
    files = {"utils.py": "def keep():\n    return 1\n", "helpers.py": "def slugify(s):\n    return s\n",
             "a.py": "from helpers import slugify\n", "b.py": "from utils import keep\nfrom helpers import slugify\n",
             "c.py": si._MOVE["c.py"]}
    for rel, text in files.items():
        (tmp_path / rel).write_bytes(text.encode())
    assert by_name["move_func"].check(tmp_path, "") == ""
    assert by_name["count_defs"].check(tmp_path, "There are 18 functions.") == ""


def _put(root: Path, files: dict) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(text.encode())


def test_agentic_and_design_checks_pass_on_correct_work(tmp_path: Path) -> None:
    by_name = {t.name: t for t in si.TASKS}
    _put(tmp_path, {**si._CHAIN, "env/overrides.json": '{"port": 9000}\n'})
    assert by_name["trace_config"].check(tmp_path, "It used 8500 from overrides.json") == ""
    _put(tmp_path, {"stats.py": si._STATS.replace("    return sum", "    if not xs:\n        return 0.0\n    return sum")})
    assert by_name["fix_from_log"].check(tmp_path, "") == ""
    _put(tmp_path, {"models/user.py": "class User:\n    name = ''\n", "models/order.py": "class Order:\n    total = 0\n",
                    "models/invoice.py": "class Invoice:\n    paid = False\n",
                    "app.py": "from models.user import User\nfrom models.order import Order\n"
                              "from models.invoice import Invoice\n"})
    assert by_name["split_module"].check(tmp_path, "") == ""
    plan = '<div class="plan"><button>Buy</button></div>'
    _put(tmp_path, {"index.html": '<meta name="viewport" content="width=device-width"><style>:root{--bg:#fff;--fg:#000}'
                                  '@media (prefers-color-scheme: dark){:root{--bg:#000}} .p{flex-wrap:wrap}</style>'
                                  + plan * 3 + '<img src="a.png" alt="x">'})
    assert by_name["design_page"].check(tmp_path, "") == ""
    css = (":root { --white: #ffffff; --ink: #1a1a1a; --blue: #3366ff; --card: #f4f4f4; --line: #dddddd; }\n"
           "body { background: var(--white); color: var(--ink); }\na { color: var(--blue); }\n"
           ".card { background: var(--card); border: 1px solid var(--line); }\n")
    _put(tmp_path, {"style.css": css})
    assert by_name["design_tokens"].check(tmp_path, "") == ""
    _put(tmp_path, {"theme.css": si._THEME.replace("#a0c4f0", "#0a1f3d")})
    assert by_name["design_contrast"].check(tmp_path, "") == ""
    assert round(si.contrast("#ffffff", "#000000"), 1) == 21.0


def test_real_task_checks_pass_on_correct_answers(tmp_path: Path) -> None:
    by_name = {t.name: t for t in si.TASKS}
    assert by_name["count_todo"].check(tmp_path, "3") == ""
    assert by_name["count_todo"].check(tmp_path, "There are 3 of 4") != ""
    assert by_name["which_file"].check(tmp_path, "models/billing.py") == ""

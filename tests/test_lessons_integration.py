"""Integration tests for the lesson store inside the plugin.

Covers the `jev_lessons` tool and the hook gate that stops an action a kick
lesson matches. The invariant throughout: with enforcement off (the default),
nothing is stopped and no catch is counted; only an enforcing mode blocks.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LITERAL = "rm -rf /tmp/jevs-decision-store"
PARAPHRASE = "clear out the temporary decision folder with a recursive delete"


@pytest.fixture
def lessons(monkeypatch, tmp_path):
    """The lessons module with its ledger writes redirected into tmp_path."""
    import ledger
    monkeypatch.setattr(ledger, "get_hermes_home", lambda: str(tmp_path))
    import lessons as module
    monkeypatch.setattr(module, "append_ledger", ledger.append)
    return module


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    import ledger
    import closed_loop
    import supervision as supervision_module
    import lessons as lessons_module

    monkeypatch.setattr(ledger, "get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(closed_loop, "get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(supervision_module, "append_ledger", ledger.append)
    monkeypatch.setattr(lessons_module, "append_ledger", ledger.append)

    spec = importlib.util.spec_from_file_location("jev_lessons_audit", ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(module, "_secret", lambda: "synthetic")
    monkeypatch.setattr(module, "_request", lambda *a, **k: {"answers": {}})
    monkeypatch.setattr(module, "_write_shadow_record", lambda record: None)

    supervision_module._DEFAULT = supervision_module.Supervision(supervision_module.SupervisionConfig())
    lessons_module._DEFAULT = lessons_module.LessonStore(root=str(tmp_path / "jev"))
    return module


@pytest.fixture
def hooked(plugin, monkeypatch):
    monkeypatch.setenv("JEV_ENABLE_HOOKS", "1")
    return plugin


def _call(plugin, **payload):
    return json.loads(plugin.jev_lessons_handler(payload))


# -- tool surface --------------------------------------------------------


def test_add_then_list_a_lesson(plugin):
    added = _call(plugin, action="add", text="Check the port is free", detect="a start with no port check")
    assert added["success"] is True
    assert added["lesson"]["id"] == "L1"
    listed = _call(plugin, action="list")
    assert listed["count"] == 1
    assert listed["lessons"][0]["text"] == "Check the port is free"


def test_add_requires_text(plugin):
    assert "error" in _call(plugin, action="add", detect="something")


def test_get_returns_a_lesson_and_null_for_a_miss(plugin):
    _call(plugin, action="add", text="Check the port is free")
    assert _call(plugin, action="get", lesson_id="L1")["lesson"]["id"] == "L1"
    assert _call(plugin, action="get", lesson_id="L404")["lesson"] is None


def test_edit_corrects_a_wrong_escalation(plugin):
    for _ in range(3):
        _call(plugin, action="add", text="Save output with Out-File and utf8", detect="a bare > redirect")
    assert _call(plugin, action="get", lesson_id="L1")["lesson"]["severity"] == "kick"
    fixed = _call(plugin, action="edit", lesson_id="L1", severity="nudge", escapes=0)
    assert fixed["lesson"]["severity"] == "nudge"
    assert fixed["lesson"]["escapes"] == 0


def test_owner_rule_starts_as_a_kick(plugin):
    added = _call(plugin, action="add", text="Never loosen a test", source="owner", detect="a weakened assertion")
    assert added["lesson"]["severity"] == "kick"


def test_retire_and_sweep(plugin):
    _call(plugin, action="add", text="Check the port is free")
    assert _call(plugin, action="retire", lesson_id="L1")["lesson"]["status"] == "retired"
    assert _call(plugin, action="list")["count"] == 0


def test_sweep_reports_what_it_retired(plugin):
    _call(plugin, action="add", text="Check the port is free")
    for _ in range(40):
        _call(plugin, action="surfaced", lesson_ids=["L1"])
    swept = _call(plugin, action="sweep")
    assert swept["retired"] == ["L1"]
    assert swept["stats"]["active"] == 0


def test_candidates_requires_action_text(plugin):
    assert "error" in _call(plugin, action="candidates")


def test_candidates_returns_a_shortlist_note(plugin):
    _call(plugin, action="add", text="Check the port is free before starting the server")
    result = _call(plugin, action="candidates", action_text="Check the port is free before starting the server")
    assert result["count"] >= 1
    assert "not a verdict" in result["note"]


def test_caught_and_surfaced_require_ids(plugin):
    assert "error" in _call(plugin, action="caught")
    assert "error" in _call(plugin, action="surfaced")


def test_export_and_import_round_trip(plugin, tmp_path):
    import lessons as lessons_module
    _call(plugin, action="add", text="Check the port is free", detect="a start with no check")
    _call(plugin, action="caught", lesson_ids=["L1"])
    pack = _call(plugin, action="export", **{"from": "kit"})["pack"]
    assert pack["from"] == "kit" and len(pack["lessons"]) == 1

    other = lessons_module.LessonStore(root=str(tmp_path / "second"))
    imported = other.import_pack(pack)
    assert imported["count"] == 1
    assert other.get(imported["imported"][0]).catches == 0


def test_import_requires_a_pack_object(plugin):
    assert "error" in _call(plugin, action="import")


def test_stats(plugin):
    _call(plugin, action="add", text="Check the port is free")
    _call(plugin, action="add", text="Never loosen a test", source="owner")
    stats = _call(plugin, action="stats")["stats"]
    assert stats["active"] == 2 and stats["owner_rules"] == 1


def test_unknown_action_is_reported(plugin):
    assert _call(plugin, action="explode")["error"] == "unknown action"


# -- hook gate -----------------------------------------------------------


def test_no_lesson_means_no_directive(hooked):
    assert hooked._on_pre_tool_call("terminal", {"command": "ls"}, session_id="s1") is None


def test_shadow_mode_never_blocks_on_a_kick_lesson(hooked):
    hooked.jev_lessons_handler({"action": "add", "text": LITERAL, "detect": LITERAL, "source": "owner"})
    assert hooked._on_pre_tool_call("terminal", {"command": LITERAL}, session_id="s1") is None


def test_shadow_mode_records_what_enforcement_would_have_done(hooked, tmp_path):
    hooked.jev_lessons_handler({"action": "add", "text": LITERAL, "detect": LITERAL, "source": "owner"})
    hooked._on_pre_tool_call("terminal", {"command": LITERAL}, session_id="s1")
    ledger_text = (tmp_path / "logs" / "jev-ledger.jsonl").read_text()
    assert "lesson_would_kick" in ledger_text
    lesson = json.loads(hooked.jev_lessons_handler({"action": "get", "lesson_id": "L1"}))["lesson"]
    assert lesson["catches"] == 0, "nothing was caught while enforcement was off"


def test_enforcing_mode_blocks_on_a_kick_lesson(hooked):
    hooked.jev_lessons_handler({"action": "add", "text": LITERAL, "detect": LITERAL, "source": "owner"})
    hooked._supervision.default_supervision().configure(mode="correct_next")
    directive = hooked._on_pre_tool_call("terminal", {"command": LITERAL}, session_id="s1")
    assert directive["action"] == "block"
    assert "L1" in directive["message"]
    assert LITERAL in directive["message"]


def test_enforcing_mode_counts_the_catch(hooked):
    hooked.jev_lessons_handler({"action": "add", "text": LITERAL, "detect": LITERAL, "source": "owner"})
    hooked._supervision.default_supervision().configure(mode="correct_next")
    hooked._on_pre_tool_call("terminal", {"command": LITERAL}, session_id="s1")
    lesson = json.loads(hooked.jev_lessons_handler({"action": "get", "lesson_id": "L1"}))["lesson"]
    assert lesson["catches"] == 1
    assert lesson["surfaced"] == 1


def test_a_nudge_lesson_never_blocks(hooked):
    hooked.jev_lessons_handler({"action": "add", "text": LITERAL, "detect": LITERAL})
    hooked._supervision.default_supervision().configure(mode="correct_next")
    assert hooked._on_pre_tool_call("terminal", {"command": LITERAL}, session_id="s1") is None


def test_a_paraphrase_is_now_blocked_locally(hooked):
    """The gap that used to be documented as a miss is closed for named families."""
    hooked.jev_lessons_handler({"action": "add", "text": LITERAL, "detect": LITERAL, "source": "owner"})
    hooked._supervision.default_supervision().configure(mode="correct_next")
    directive = hooked._on_pre_tool_call("terminal", {"command": PARAPHRASE}, session_id="s1")
    assert directive is not None and directive["action"] == "block"
    assert "lesson" in directive["message"].lower()


def test_a_paraphrase_still_stops_nothing_in_shadow(hooked):
    """Wider reach must not widen authority: shadow still blocks nothing."""
    hooked.jev_lessons_handler({"action": "add", "text": LITERAL, "detect": LITERAL, "source": "owner"})
    assert hooked._on_pre_tool_call("terminal", {"command": PARAPHRASE}, session_id="s1") is None


def test_a_distant_paraphrase_slips_through_the_local_gate(hooked):
    """The residual limit at the hook level: an unnamed rephrasing is not caught."""
    hooked.jev_lessons_handler({"action": "add", "text": LITERAL, "detect": LITERAL, "source": "owner"})
    hooked._supervision.default_supervision().configure(mode="correct_next")
    distant = "get rid of the scratch area used for the decisions work"
    assert hooked._on_pre_tool_call("terminal", {"command": distant}, session_id="s1") is None


def test_lesson_gate_runs_before_the_repeated_failure_control(hooked):
    """A known mistake blocks on the first attempt, with no failure history."""
    hooked.jev_lessons_handler({"action": "add", "text": LITERAL, "detect": LITERAL, "source": "owner"})
    hooked._supervision.default_supervision().configure(mode="correct_next")
    directive = hooked._on_pre_tool_call("terminal", {"command": LITERAL}, session_id="s1")
    assert directive is not None and directive["action"] == "block"
    assert hooked._supervision.default_supervision().status()["metrics"].get("controls_created", 0) == 0


def test_hooks_stay_inert_without_the_opt_in(plugin, monkeypatch):
    monkeypatch.delenv("JEV_ENABLE_HOOKS", raising=False)
    plugin.jev_lessons_handler({"action": "add", "text": LITERAL, "detect": LITERAL, "source": "owner"})
    assert plugin._on_pre_tool_call("terminal", {"command": LITERAL}, session_id="s1") is None


# -- hardening: the gate must never veto ---------------------------------
#
# Hermes resolves a raising or slow pre_tool_call callback as a BLOCK on the
# user's tool call. These tests pin the abstain behaviour, because a guard that
# errors must pass the tool through rather than stop the user's work.


def test_gate_abstains_when_the_store_raises(hooked, monkeypatch):
    import lessons as lessons_module
    store = lessons_module.default_store()
    monkeypatch.setattr(type(store), "local_kicks", lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone")))
    assert hooked._on_pre_tool_call("terminal", {"command": "ls"}, session_id="s1") is None


def test_gate_failure_is_counted_and_visible(hooked, monkeypatch):
    import lessons as lessons_module
    store = lessons_module.default_store()
    monkeypatch.setattr(type(store), "local_kicks", lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone")))
    hooked._on_pre_tool_call("terminal", {"command": "ls"}, session_id="s1")
    monkeypatch.undo()
    stats = _call(hooked, action="stats")
    assert stats["gate"]["count"] >= 1
    assert "OSError" in stats["gate"]["last"]


def test_gate_abstains_on_a_corrupt_store_file(hooked, tmp_path):
    store_dir = tmp_path / "jev"
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "lessons.json").write_text("{ this is not json")
    assert hooked._on_pre_tool_call("terminal", {"command": "ls"}, session_id="s1") is None


def test_gate_abstains_on_a_hostile_store_file(hooked, tmp_path):
    store_dir = tmp_path / "jev"
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "lessons.json").write_text(json.dumps([
        "not a dict",
        {"id": "L1", "text": "valid", "detect": "valid", "severity": "kick", "tags": 5, "catches": "x"},
        {"text": "no id at all"},
        42,
    ]))
    assert hooked._on_pre_tool_call("terminal", {"command": "ls"}, session_id="s1") is None


def test_a_hostile_row_does_not_lose_the_valid_ones(hooked, tmp_path):
    import lessons as lessons_module
    store_dir = tmp_path / "jev"
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "lessons.json").write_text(json.dumps([
        "garbage",
        {"id": "L1", "text": "Keep the tmp store tidy", "detect": "an untidy tmp store", "severity": "catastrophic",
         "tags": "not a list", "surfaced": "nonsense"},
    ]))
    lessons_module._DEFAULT = lessons_module.LessonStore(root=str(store_dir))
    listed = _call(hooked, action="list")
    assert listed["count"] == 1
    lesson = listed["lessons"][0]
    assert lesson["severity"] == "nudge", "an unknown severity is normalised, not trusted"
    assert lesson["tags"] == []
    assert lesson["surfaced"] == 0


def test_a_read_only_store_directory_does_not_block_tools(hooked, tmp_path, monkeypatch):
    import lessons as lessons_module
    store_dir = tmp_path / "jev"
    store_dir.mkdir(parents=True, exist_ok=True)
    store = lessons_module.LessonStore(root=str(store_dir))
    store.add(text=LITERAL, detect=LITERAL, source="owner")
    lessons_module._DEFAULT = store
    hooked._supervision.default_supervision().configure(mode="correct_next")
    monkeypatch.setattr(type(store), "_save", lambda self: (_ for _ in ()).throw(OSError("read only")))
    # An enforcing gate that cannot record must still not crash the host.
    directive = hooked._on_pre_tool_call("terminal", {"command": LITERAL}, session_id="s1")
    assert directive is None or directive.get("action") == "block"


# -- cross-process safety ------------------------------------------------


def test_a_stale_in_memory_copy_does_not_drop_another_writers_lesson(lessons, tmp_path):
    """Two Hermes processes share one store; a write must not clobber the other's."""
    root = str(tmp_path / "jev")
    first = lessons.LessonStore(root=root)
    second = lessons.LessonStore(root=root)

    first.add(text="Check the port is free before starting the server")
    second.all()  # second loads now, before first writes again
    first.add(text="Money in Decimal, never a bare float")
    second.add(text="Never loosen a test to make it pass")

    reloaded = lessons.LessonStore(root=root)
    assert len(reloaded.all()) == 3


# -- registration --------------------------------------------------------


def test_register_declares_the_lessons_tool(plugin):
    registered = {}

    class Context:
        def register_tool(self, name, toolset, schema, handler, description=""):
            registered[name] = toolset

        def register_hook(self, name, callback):
            registered[f"hook:{name}"] = callback

    plugin.register(Context())
    assert registered["jev_lessons"] == "jev"
    assert sum(1 for key in registered if key.startswith("hook:")) == 3
    assert len([k for k in registered if not k.startswith("hook:")]) == 9


def test_plugin_yaml_declares_nine_tools():
    manifest = (ROOT / "plugin.yaml").read_text()
    for tool in (
        "jev_decide", "jev_workflow", "jev_ledger", "jev_gateway",
        "jev_ingest", "jev_loop", "jev_supervision", "jev_live", "jev_lessons",
    ):
        assert tool in manifest, f"{tool} missing from plugin.yaml"
    assert manifest.count("- pre_tool_call") == 1, "only one pre_tool_call owner is allowed"

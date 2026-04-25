import json
from pathlib import Path

from maxwell_daemon.core.template_store import TemplateStore


def test_template_store_loads_builtins() -> None:
    store = TemplateStore()
    templates = store.list_templates()
    assert len(templates) >= 2

    t = store.get_template("audit-repo-todos")
    assert t is not None
    assert t.name == "Audit Repo TODOs"
    assert t.parameters[0].name == "repo"


def test_template_store_loads_from_disk(tmp_path: Path) -> None:
    custom_template = {
        "id": "custom-1",
        "name": "Custom 1",
        "description": "My custom template",
        "prompt_template": "Do custom things to {{ repo }}",
        "parameters": [{"name": "repo", "type": "repo", "required": True}],
    }

    (tmp_path / "custom-1.json").write_text(json.dumps(custom_template))

    store = TemplateStore(tmp_path)
    t = store.get_template("custom-1")
    assert t is not None
    assert t.name == "Custom 1"


def test_template_render() -> None:
    store = TemplateStore()
    t = store.get_template("audit-repo-todos")
    assert t is not None

    rendered = t.render({"repo": "D-sorganization/Maxwell-Daemon"})
    assert "D-sorganization/Maxwell-Daemon" in rendered
    assert "{{" not in rendered

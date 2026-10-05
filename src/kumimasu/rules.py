from __future__ import annotations

from importlib import resources
from pathlib import Path

import yaml

from .model import Rule

PACKAGED = "default_rules.yaml"


def parse_rules(text: str, where: str) -> list[Rule]:
    """A rules file is a YAML list; an item is the rule text, or {text, on} for a rule that is kept but off."""
    data = yaml.safe_load(text) or []
    out = []
    if isinstance(data, list):
        for x in data:
            if isinstance(x, str) and x.strip():
                out.append(Rule(text=x.strip()))
            elif isinstance(x, dict) and isinstance(x.get("text"), str):
                out.append(Rule(text=x["text"].strip(), on=bool(x.get("on", True))))
            else:
                break
        else:
            return out
    raise ValueError(f"{where}: rules must be a YAML list of strings (or {{text, on}})")


def dump_rules(rules: list[Rule]) -> str:
    rows = [r.text if r.on else {"text": r.text, "on": False} for r in rules]
    return yaml.safe_dump(rows, allow_unicode=True, sort_keys=False, width=1000)


def packaged_rules() -> list[Rule]:
    return parse_rules(resources.files("kumimasu").joinpath(PACKAGED).read_text(encoding="utf-8"), PACKAGED)


def default_rules(path: Path | None = None) -> list[Rule]:
    if path is None:
        return packaged_rules()
    return parse_rules(path.read_text(encoding="utf-8"), str(path))

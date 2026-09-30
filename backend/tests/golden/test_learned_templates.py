"""Offline mining over the sample corpus reproduces the seeded templates, and every sample
command matches one of them."""
import json

from app import settings
from app.ingest.detector import detect
from app.mapping.compiler import build_index
from app.mapping.matcher import match_config
from app.parsing.flattener import flatten_config
from app.templating.hints import LearnerHints
from app.templating.lexer import Lexer
from app.templating.miner import TemplateLearner, assign_profiles
from app.templating.signature import choose_profile

LEARNED = settings.SEED_DIR / "learned" / "templates.json"


def _corpus():
    parsed = []
    for folder in sorted(settings.SAMPLES_DIR.iterdir()):
        if folder.name == "junos":                  # new-vendor sample, not mined (task Q2)
            continue
        for path in sorted(f for f in folder.rglob("*") if f.is_file()):
            text = path.read_text(encoding="utf-8", errors="replace")
            vendor, dialect = detect(text)
            assert vendor["name"] == folder.name, path
            parsed.append((path, dialect, flatten_config(text, dialect, vendor), vendor["name"]))
    return parsed


def test_learn_reproduces_seed_templates_and_matches_everything():
    hints = LearnerHints()
    parsed = _corpus()
    profiles = assign_profiles(parsed, False, hints.cluster_threshold)
    learner = TemplateLearner(hints)
    for name, p in profiles.items():
        learner.learn_profile(name, [(str(f), d, c) for f, d, c in p["members"]])
    data = learner.to_json(profiles)

    seeded = json.loads(LEARNED.read_text())
    assert {t["id"] for t in data["templates"]} == {t["id"] for t in seeded["templates"]}

    index = build_index(data["templates"])
    sigs = {n: (p["dialect"], set(p["signature"])) for n, p in data["profiles"].items()}
    lexer = Lexer(hints.ifname_prefixes)
    for path, dialect, cmds, vendor in parsed:
        profile = choose_profile(dialect, cmds, vendor, sigs)
        missed = [c.path_str for c, t, _, _ in match_config(cmds, index, profile, lexer) if not t]
        assert not missed, f"{path}: {missed[:3]}"

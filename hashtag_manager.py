"""
Hashtag A/B testing manager.

Rotates through HASHTAG_SETS round-robin while in "testing" phase, so each
post uses the next set in line. Counts are tracked in hashtag_state.json
(committed back to the repo after every post, same pattern as posted.json).

Each set carries a "fingerprint" tag that's unique to that set only — this
lets analyze_hashtags.py identify, from Publer's post text alone, which set
a given published post used (no need to rely on Publer's label system).

Once every set has POSTS_PER_SET posts *and* Publer has insight data for
them, analyze_hashtags.py calls lock_winner() and every post from then on
uses that single winning set.
"""
import json
from pathlib import Path

STATE_PATH = Path("hashtag_state.json")
POSTS_PER_SET = 10

HASHTAG_SETS = {
    "broad_reach": {
        "fingerprint": "#clipprodaily",
        "tags": [
            "#fyp", "#foryou", "#foryoupage", "#viral", "#trending",
            "#justchatting", "#twitch", "#twitchclips", "#clipprodaily",
        ],
    },
    "niche_community": {
        "fingerprint": "#twitchmoments",
        "tags": [
            "#twitch", "#twitchclips", "#twitchmoments", "#justchatting",
            "#streamerclips", "#streamersoftiktok", "#twitchfails", "#IRL",
        ],
    },
    "reaction_mood": {
        "fingerprint": "#justchattingclips",
        "tags": [
            "#fyp", "#viral", "#funny", "#crazy", "#omg", "#justchatting",
            "#justchattingclips", "#nochill", "#streamermoments",
        ],
    },
}

SET_ORDER = list(HASHTAG_SETS.keys())


def _default_state() -> dict:
    return {
        "phase": "testing",   # "testing" or "locked"
        "winner": None,
        "counts": {name: 0 for name in SET_ORDER},
        "next_index": 0,
    }


def load_state() -> dict:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
        state.setdefault("counts", {})
        for name in SET_ORDER:
            state["counts"].setdefault(name, 0)
        state.setdefault("phase", "testing")
        state.setdefault("winner", None)
        state.setdefault("next_index", 0)
        return state
    return _default_state()


def save_state(state: dict):
    STATE_PATH.write_text(json.dumps(state, indent=2))


def get_active_set() -> tuple[str, dict]:
    """
    Returns (set_name, set_dict) to use for the *next* post.
    Round-robins through sets while testing; always returns the locked
    winner once analyze_hashtags.py has picked one.
    """
    state = load_state()
    if state["phase"] == "locked" and state["winner"]:
        name = state["winner"]
        return name, HASHTAG_SETS[name]

    name = SET_ORDER[state["next_index"] % len(SET_ORDER)]
    return name, HASHTAG_SETS[name]


def record_post(set_name: str):
    """Call once, after a successful post, to advance rotation + counters."""
    state = load_state()
    if state["phase"] == "locked":
        return
    state["counts"][set_name] = state["counts"].get(set_name, 0) + 1
    state["next_index"] = (SET_ORDER.index(set_name) + 1) % len(SET_ORDER)
    save_state(state)


def testing_complete() -> bool:
    state = load_state()
    if state["phase"] == "locked":
        return True
    return all(state["counts"].get(name, 0) >= POSTS_PER_SET for name in SET_ORDER)


def lock_winner(set_name: str):
    state = load_state()
    state["phase"] = "locked"
    state["winner"] = set_name
    save_state(state)

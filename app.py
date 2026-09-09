"""
Back to Hogwarts — House Cup Night
A live, multiplayer event engine for one host screen (shared over a call)
plus one device per player.

Design principles this version is built on:
  * EVERY player controls their own thing on the shared screen. No
    aggregate "tap to fill a bar" — you fly your own broom, you throw your
    own ingredient, you stun your own goblin, and your name is on screen.
  * Outcomes are earned, never rolled. A Quidditch match is won on goals
    and the Snitch, both of which come from someone actually flying there.
  * Per-player stats are recorded all night so the closing certificate can
    name real people for real things.

Protocol out to clients:
  {"type":"state", ...}  full snapshot, on every discrete change
  {"type":"f", ...}      compact real-time frame at TICK_HZ during a
                         live minigame (positions etc). Small on purpose;
                         clients merge and never rebuild the DOM from it.
Protocol in from clients:
  {"type":"player_action","action":"move","payload":{"dx":..,"dy":..}}
  {"type":"player_action","action":"act"}            (catch/throw/cast)
  {"type":"player_action","action":"brew","payload":{"order":[..]}} (potions)
  {"type":"player_action","action":"rune","payload":{"r":..}}   (gringotts)
  {"type":"player_action","action":"zap","payload":{"g":..}}    (gringotts)
  {"type":"player_action","action":"clank"}                     (dragon)
  {"type":"player_action","action":"owl_answer","payload":{"q":..,"choice":..}}
  {"type":"host_action","action":...}
"""
import asyncio
import json
import math
import os
import random
import time
from typing import Optional

from starlette.applications import Starlette
from starlette.routing import Route, WebSocketRoute
from starlette.responses import HTMLResponse, JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

HOST_PIN = os.environ.get("HOST_PIN", "9110")

# Let people who aren't on the fixed roster join and be auto-sorted into the
# emptiest house. On for rehearsals and for playing with friends; set
# ALLOW_GUESTS=0 for the real night if you want the roster locked down.
ALLOW_GUESTS = os.environ.get("ALLOW_GUESTS", "1") not in ("0", "false", "False", "")

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
TICK_HZ = 10
DT = 1.0 / TICK_HZ

# Quidditch
FIELD_W, FIELD_H = 1600, 900
ACCEL = 1500.0
MAX_SPEED = 430.0
DRAG = 0.90
CATCH_R = 34
THROW_SPEED = 980.0
QUAFFLE_DRAG = 0.985
BLUDGER_R = 30
STUN_SECONDS = 2.2
SNITCH_R = 26
SNITCH_AT = 30          # seconds remaining when the Snitch is released
QUID_SECONDS = 100
GOAL_POINTS = 10
SNITCH_POINTS = 150

# Potions
POTION_CORRECT_POINTS = 50      # to your house for a correct brew
POTION_FIRST_BONUS = 25         # extra, for the first correct brew of a round
SORT_COUNTDOWN = 3              # the Hat's pause before every house is revealed

# Gringotts
CAGE_TARGET = 14         # rune casts needed per cage
GOBLIN_INTERVAL = 2.6
GOBLIN_SPEED = 46.0
GOBLIN_DAMAGE = 2
RUNE_ROTATE = 3.4        # how often the called rune changes
DRAGON_SECONDS = 16
DRAGON_TARGET = 150      # clanks needed to drive it back
GRINGOTTS_SECONDS = 210

RUNES = ["ᚠ", "ᚦ", "ᛉ", "ᛊ", "ᛟ", "ᛞ"]
GOBLIN_LETTERS = list("QWERASDFZXCV")

# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------
HOUSES = ["Gryffindor", "Slytherin", "Ravenclaw", "Hufflepuff"]
HOUSE_MEMBERS = {
    "Gryffindor": ["Ayush", "Het", "Gauri", "Chirag", "Geetika", "Utkarsh", "Riapreet"],
    "Slytherin": ["Poorva", "Chinmay", "Siddhant", "Jayesh", "Riya Aggarwal", "Mehul", "Sahil"],
    "Ravenclaw": ["Hridam", "Charan", "Kartik", "Asmi", "Shagun", "Sameer", "Sasanka"],
    "Hufflepuff": ["Anushka", "Suhas", "Archit", "Sayali", "Parikshith", "Anamika"],
}
NAME_TO_HOUSE = {}
for _h, _m in HOUSE_MEMBERS.items():
    for _n in _m:
        NAME_TO_HOUSE[_n.strip().lower()] = _h
ALL_NAMES = [n for m in HOUSE_MEMBERS.values() for n in m]
GUESTS: dict[str, str] = {}          # guest display name -> house

random.seed(20260911)

# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------
# Potions — three brews. Each is an ordering puzzle: a shelf of ingredients,
# a handful of clues, and a cauldron that takes exactly N of them in the right
# order. `rule` next to each clue is the machine-readable form of the same
# sentence; test_games.py brute-forces every permutation to prove the clues
# admit exactly one answer, so a puzzle can never ship ambiguous or unfair.
POTIONS = [
    {
        "name": "Draught of Clean Deploys",
        "story": "Three deploys have failed this morning. The Professor is not sympathetic.",
        "slots": 4,
        "seconds": 100,
        "shelf": [
            {"n": "Dependency Array", "icon": "🧷"},     # 0
            {"n": "Heap Dump", "icon": "🫙"},            # 1
            {"n": "needs:", "icon": "🔗"},               # 2
            {"n": "IAM Policy", "icon": "🛡️"},           # 3
            {"n": "Intern's Tears", "icon": "😢"},       # 4
        ],
        "answer": [0, 2, 1, 3],
        "clues": [
            {"text": "Intern's Tears have no place in a serious brew.", "rule": ["exclude", 4]},
            {"text": "Begin with the ward that stops an effect looping forever.", "rule": ["first", 0]},
            {"text": "Finish with the ward that decides who may touch what.", "rule": ["last", 3]},
            {"text": "What one rite waits on another for goes in before anything you'd read after a crash.",
             "rule": ["before", 2, 1]},
        ],
    },
    {
        "name": "Elixir of Steady State",
        "story": "The cluster claims to be healthy. The Professor's eyebrow says otherwise.",
        "slots": 4,
        "seconds": 120,
        "shelf": [
            {"n": "Moonstone", "icon": "🌙"},            # 0
            {"n": "Phoenix Feather", "icon": "🪶"},      # 1
            {"n": "Mandrake Root", "icon": "🌿"},        # 2
            {"n": "Snake Venom", "icon": "🐍"},          # 3
            {"n": "Fairy Wing", "icon": "🧚"},           # 4
            {"n": "Bezoar", "icon": "🪨"},               # 5
        ],
        "answer": [1, 0, 5, 2],
        "clues": [
            {"text": "No brew of mine has ever contained Fairy Wing.", "rule": ["exclude", 4]},
            {"text": "Snake Venom stays in its jar tonight.", "rule": ["exclude", 3]},
            {"text": "Moonstone follows the Phoenix Feather at once, or the whole thing curdles.",
             "rule": ["immediately_before", 1, 0]},
            {"text": "Mandrake Root screams last, as it always does.", "rule": ["last", 2]},
            {"text": "A Bezoar is no use first — it must undo something already in the pot.",
             "rule": ["notfirst", 5]},
        ],
    },
    {
        "name": "The Draught of Advancement",
        "story": "The final brew. Get this right and the year is behind you; get it wrong and it is not.",
        "slots": 5,
        "seconds": 150,
        "shelf": [
            {"n": "OutOfSync", "icon": "🌀"},            # 0
            {"n": "CrashLoop Tears", "icon": "💀"},      # 1
            {"n": "Rollback Salts", "icon": "🧂"},       # 2
            {"n": "Moonstone", "icon": "🌙"},            # 3
            {"n": "Green Build", "icon": "✅"},          # 4
            {"n": "Force Push", "icon": "💥"},           # 5
            {"n": "Bezoar", "icon": "🪨"},               # 6
        ],
        "answer": [1, 0, 6, 2, 4],
        "clues": [
            {"text": "Nothing good has ever come of a Force Push. Leave it.", "rule": ["exclude", 5]},
            {"text": "Moonstone belongs to the other elixir; it spoils this one.", "rule": ["exclude", 3]},
            {"text": "A Green Build is the last thing any of us want to see.", "rule": ["last", 4]},
            {"text": "Trouble is noticed before it is named: the tears go in before the drift.",
             "rule": ["before", 1, 0]},
            {"text": "The drift must be seen before it is neutralised — Bezoar comes straight after it.",
             "rule": ["immediately_before", 0, 6]},
            {"text": "Salts are the second-to-last resort, quite literally.", "rule": ["pos", 2, 3]},
            {"text": "Begin where it hurts.", "rule": ["first", 1]},
        ],
    },
]


# ---------------------------------------------------------------------------
# O.W.L.s — a written paper. Everyone answers the same twelve questions at
# their own pace inside one timer; nobody is told whether they were right
# until the results are posted, so the Hall can't shout the answers.
# ---------------------------------------------------------------------------
OWL_SECONDS = 200
OWL_POINTS = 15            # to your house, per correct answer
OWL_PERFECT_BONUS = 50     # for a flawless paper
OWL_BANDS = [              # (minimum correct out of 10, grade, letter)
    (9, "Outstanding", "O"),
    (7, "Exceeds Expectations", "E"),
    (5, "Acceptable", "A"),
    (3, "Poor", "P"),
    (0, "Troll", "T"),
]

OWLS = [
    {"topic": "Java",
     "q": "Two String variables hold the same text. What does `==` actually compare?",
     "choices": ["Whether they are the same object in memory", "Whether the characters match",
                 "Their lengths", "Their hash codes"], "correct": 0},
    {"topic": "Java",
     "q": "What does the garbage collector spare you from doing by hand?",
     "choices": ["Releasing memory you have finished with", "Compiling your classes",
                 "Starting threads", "Checking types"], "correct": 0},

    {"topic": "React",
     "q": "Why does React want a stable `key` on each item in a list?",
     "choices": ["To tell which item is which between renders", "To sort the list",
                 "To style alternate rows", "To make the list accessible"], "correct": 0},
    {"topic": "React",
     "q": "You call `setCount(count + 1)` twice in one handler and the count only goes up by one. Why?",
     "choices": ["Both calls read the same stale `count` from that render",
                 "React batches only odd-numbered updates", "The second call throws silently",
                 "State updates are always ignored in handlers"], "correct": 0},

    {"topic": "Kubernetes",
     "q": "A Pod is best described as…",
     "choices": ["One or more containers sharing a network and storage — the smallest deployable unit",
                 "A physical machine in the cluster", "A YAML file", "A container registry"], "correct": 0},
    {"topic": "Kubernetes",
     "q": "A Deployment's job is to…",
     "choices": ["Keep the declared number of replicas running and roll out changes",
                 "Build your container image", "Store your secrets", "Route external DNS"], "correct": 0},

    {"topic": "AWS",
     "q": "S3 is…",
     "choices": ["Object storage", "A relational database", "A virtual machine", "A load balancer"],
     "correct": 0},
    {"topic": "AWS",
     "q": "The point of an IAM role, rather than an IAM user, is that…",
     "choices": ["It is assumed temporarily, so there are no long-lived credentials to leak",
                 "It costs less", "It works in only one region", "It can log in to the console"], "correct": 0},

    {"topic": "LLM basics",
     "q": "A token is roughly…",
     "choices": ["A chunk of text — often part of a word — that the model reads and writes in",
                 "One English word, always", "A single character", "A request to the API"], "correct": 0},
    {"topic": "MCP",
     "q": "The Model Context Protocol exists to…",
     "choices": ["Give models a standard way to reach tools and data, whoever built them",
                 "Compress prompts before sending them", "Replace HTTP inside data centres",
                 "Rank which model is best for a task"], "correct": 0},
]

# Every question above is written with its correct answer first, which would
# make the whole paper "always pick A". Deal the correct positions round-robin
# over A/B/C/D and shuffle the distractors around them — seeded, so the paper
# is identical on every worker and across a restart, and deliberately balanced
# rather than merely random (a random shuffle can still clump).
_owl_rng = random.Random(90911)
_slots = []
while len(_slots) < len(OWLS):
    _four = [0, 1, 2, 3]
    _owl_rng.shuffle(_four)
    _slots.extend(_four)
for _q, _target in zip(OWLS, _slots):
    _right = _q["choices"][_q["correct"]]
    _rest = [c for c in _q["choices"] if c != _right]
    _owl_rng.shuffle(_rest)
    _rest.insert(_target, _right)
    _q["choices"] = _rest
    _q["correct"] = _target
del _owl_rng, _slots, _four, _q, _target, _right, _rest


TRIO = [
    {"id": "harry", "name": "Harry", "colour": "#a3121e"},
    {"id": "ron", "name": "Ron", "colour": "#e07b28"},
    {"id": "hermione", "name": "Hermione", "colour": "#8a5ad0"},
]

# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------
CONNECTIONS: dict[WebSocket, dict] = {}   # ws -> {role, name, house, pid}
PLAYERS: dict[int, dict] = {}             # pid -> {name, house, ws}
INPUT: dict[int, dict] = {}               # pid -> {dx, dy}
NEXT_PID = 1
LOCK = asyncio.Lock()

def pid_for(name: str) -> int:
    """Stable per-name id, so a reconnect keeps its broom and its stats."""
    global NEXT_PID
    for pid, p in PLAYERS.items():
        if p["name"] == name:
            return pid
    pid = NEXT_PID
    NEXT_PID += 1
    return pid


def stats_for(name: str) -> dict:
    return STATE["stats"].setdefault(name, {
        "goals": 0, "snitch": 0, "assists": 0, "ingredients": 0,
        "runes": 0, "goblins": 0, "clanks": 0, "rescues": 0,
    })


def bump(name: str, key: str, n: int = 1):
    if not name:
        return
    stats_for(name)[key] = stats_for(name).get(key, 0) + n


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def fresh_quidditch():
    return {
        "bracket": None,
        "match": None,
        "matchState": "idle",     # idle | countdown | live | done
        "houses": [],
        "score": {},
        "endsAt": None,
        "startsAt": None,
        "snitchOut": False,
        "events": [],
        "flash": None,
        "winners": {},
        "finalScore": {},
        "bodies": {},             # pid -> {x,y,vx,vy,fx,fy,stun,house}
        "quaffle": {"x": FIELD_W / 2, "y": FIELD_H / 2, "vx": 0.0, "vy": 0.0, "carrier": None, "cool": 0.0},
        "bludgers": [],
        "snitch": {"x": FIELD_W / 2, "y": 200.0, "vx": 0.0, "vy": 0.0},
    }


def fresh_potions():
    return {
        "state": "idle",        # idle | live | resolved
        "index": -1,
        "endsAt": None,
        "brews": {},            # name -> {"order": [...], "ok": bool, "ts": float}
        "first": None,          # who got it right first this round
        "solved": {h: 0 for h in HOUSES},   # correct brews per house, all rounds
        "spoiled": {h: 0 for h in HOUSES},  # exploded cauldrons per house
        "events": [],           # recent brews, for the host screen animation
    }


def fresh_owls():
    return {
        "state": "idle",     # idle | live | graded
        "endsAt": None,
        "papers": {},        # name -> {"picked": {qIndex: choice}, "finishedAt": float|None}
        "results": None,     # name -> {"score": n, "grade": str, "band": "O"|..., "house": h}
    }


def fresh_gringotts():
    return {
        "state": "idle",        # idle | live | dragon | done
        "endsAt": None,
        "cages": [
            {"id": t["id"], "name": t["name"], "colour": t["colour"], "progress": 0, "free": False}
            for t in TRIO
        ],
        "rune": RUNES[0],
        "runeAt": 0.0,
        "goblins": [],          # {id, x, y, cage, letter, stunned}
        "nextGoblin": 0.0,
        "gid": 1,
        "clanks": 0,
        "dragonTarget": DRAGON_TARGET,
        "dragonEndsAt": None,
        "events": [],
        "flash": None,
        "freed": 0,
    }


def fresh_state():
    return {
        "phase": "lobby",   # lobby | sorting | potions | gringotts | owls | quidditch | housecup
        "points": {h: 0 for h in HOUSES},
        "point_log": [],
        "stats": {},
        "sorting": {"state": "idle", "endsAt": None},
        "potions": fresh_potions(),
        "gringotts": fresh_gringotts(),
        "owls": fresh_owls(),
        "quidditch": fresh_quidditch(),
        "housecup": {"revealed": False},
    }


STATE = fresh_state()
RT_GEN = 0        # bumped whenever a realtime loop should be superseded
FLASH_SEQ = 0


def award(house: str, amount: int, reason: str):
    if house in STATE["points"]:
        STATE["points"][house] += amount
        STATE["point_log"].append({"house": house, "amount": amount, "reason": reason})


def flash(which: str, kind: str, house: str = "", extra=None):
    global FLASH_SEQ
    FLASH_SEQ += 1
    STATE[which]["flash"] = {"kind": kind, "house": house, "n": FLASH_SEQ, "extra": extra}


def say(which: str, text: str, kind: str = "info"):
    STATE[which]["events"].append({"text": text, "kind": kind, "t": time.time()})
    del STATE[which]["events"][:-8]


# ---------------------------------------------------------------------------
# Broadcasting
# ---------------------------------------------------------------------------
async def _send_all(payload: str):
    dead = []
    for ws in list(CONNECTIONS.keys()):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        drop_connection(ws)


def drop_connection(ws):
    info = CONNECTIONS.pop(ws, None)
    if info and info.get("pid") in PLAYERS and PLAYERS[info["pid"]].get("ws") is ws:
        PLAYERS[info["pid"]]["ws"] = None


async def broadcast():
    """Each socket gets a snapshot built for *that* person: during the O.W.L.s
    a player receives only their own paper back, never anyone else's."""
    dead = []
    for ws, info in list(CONNECTIONS.items()):
        try:
            payload = json.dumps({"type": "state", "state": public_state(info.get("name"))})
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        drop_connection(ws)


async def broadcast_frame():
    await _send_all(json.dumps({"type": "f", "f": frame(), "points": STATE["points"]}))


def roster_public():
    out = {}
    for ws, info in CONNECTIONS.items():
        if info.get("name"):
            out[info["name"]] = {"house": info.get("house"), "connected": True, "pid": info.get("pid")}
    return out


def public_state(for_name: Optional[str] = None):
    q = STATE["quidditch"]
    p = STATE["potions"]
    g = STATE["gringotts"]
    o = STATE["owls"]
    return {
        "phase": STATE["phase"],
        "points": dict(STATE["points"]),
        "stats": STATE["stats"],
        "sorting": STATE["sorting"],
        "houseTally": house_tally(),
        "sortedRoster": ({h: HOUSE_MEMBERS[h] + [n for n, gh in GUESTS.items() if gh == h] for h in HOUSES}
                         if STATE["sorting"]["state"] == "revealed" else None),
        "roster": roster_public(),
        "houseMembers": HOUSE_MEMBERS,
        "guestMembers": {h: [n for n, gh in GUESTS.items() if gh == h] for h in HOUSES},
        "allowGuests": ALLOW_GUESTS,
        "players": {str(pid): {"name": v["name"], "house": v["house"]} for pid, v in PLAYERS.items()},
        "potions": {
            "state": p["state"], "index": p["index"], "endsAt": p["endsAt"],
            "solved": p["solved"], "spoiled": p["spoiled"], "first": p["first"],
            "brews": p["brews"], "events": p["events"][-12:], "total": len(POTIONS),
            "puzzle": ({
                "name": POTIONS[p["index"]]["name"],
                "story": POTIONS[p["index"]]["story"],
                "slots": POTIONS[p["index"]]["slots"],
                "shelf": POTIONS[p["index"]]["shelf"],
                "clues": [c["text"] for c in POTIONS[p["index"]]["clues"]],
                "answer": (POTIONS[p["index"]]["answer"] if p["state"] == "resolved" else None),
            } if 0 <= p["index"] < len(POTIONS) else None),
        },
        "gringotts": {
            "state": g["state"], "cages": g["cages"], "rune": g["rune"], "runes": RUNES,
            "endsAt": g["endsAt"], "clanks": g["clanks"], "dragonTarget": g.get("dragonTarget", DRAGON_TARGET),
            "dragonEndsAt": g["dragonEndsAt"], "events": g["events"][-4:], "freed": g["freed"],
            "cageTarget": CAGE_TARGET,
        },
        "owls": {
            "state": o["state"], "endsAt": o["endsAt"], "total": len(OWLS),
            "results": o["results"], "seconds": OWL_SECONDS,
            "progress": {n: len(p["picked"]) for n, p in o["papers"].items()},
            "finished": [n for n, p in o["papers"].items() if p["finishedAt"]],
            # a player only ever receives their OWN paper back
            "mine": ({"picked": o["papers"][for_name]["picked"],
                      "finishedAt": o["papers"][for_name]["finishedAt"],
                      "result": (o["results"] or {}).get(for_name)}
                     if for_name and for_name in o["papers"] else
                     ({"picked": {}, "finishedAt": None, "result": None} if for_name else None)),
            "paper": ([{"topic": q2["topic"], "q": q2["q"], "choices": q2["choices"]} for q2 in OWLS]
                      if o["state"] in ("live", "graded") else None),
            "answers": ([q2["correct"] for q2 in OWLS] if o["state"] == "graded" else None),
        },
        "quidditch": {
            "bracket": q["bracket"], "match": q["match"], "matchState": q["matchState"],
            "houses": q["houses"], "score": q["score"], "endsAt": q["endsAt"],
            "startsAt": q["startsAt"], "snitchOut": q["snitchOut"],
            "events": q["events"][-4:], "winners": q["winners"], "finalScore": q["finalScore"],
            "field": [FIELD_W, FIELD_H], "snitchPoints": SNITCH_POINTS,
        },
        "housecup": dict(STATE["housecup"]),
        "certificate": certificate_data() if STATE["housecup"]["revealed"] else None,
    }


def frame():
    """Compact realtime frame for whichever minigame is live."""
    ph = STATE["phase"]
    if ph == "quidditch":
        q = STATE["quidditch"]
        return {
            "ph": "quid",
            "st": q["matchState"],
            "p": [[pid, int(b["x"]), int(b["y"]), round(max(0.0, b["stun"] - time.time()), 1)]
                  for pid, b in q["bodies"].items()],
            "q": [int(q["quaffle"]["x"]), int(q["quaffle"]["y"]), q["quaffle"]["carrier"]],
            "b": [[int(b["x"]), int(b["y"])] for b in q["bludgers"]],
            "s": ([int(q["snitch"]["x"]), int(q["snitch"]["y"])] if q["snitchOut"] else None),
            "sc": q["score"],
            "tl": max(0.0, round((q["endsAt"] or 0) - time.time(), 1)),
            "ev": q["events"][-3:],
            "fl": q["flash"],
        }
    if ph == "gringotts":
        g = STATE["gringotts"]
        return {
            "ph": "grin",
            "st": g["state"],
            "cages": g["cages"],
            "rune": g["rune"],
            "gob": [[gg["id"], int(gg["x"]), int(gg["y"]), gg["letter"], gg["cage"], round(gg["stunned"], 1)]
                    for gg in g["goblins"]],
            "tl": max(0.0, round((g["endsAt"] or 0) - time.time(), 1)),
            "clanks": g["clanks"],
            "dtl": (max(0.0, round((g["dragonEndsAt"] or 0) - time.time(), 1)) if g["dragonEndsAt"] else None),
            "dragonTarget": g.get("dragonTarget", DRAGON_TARGET),
            "ev": g["events"][-3:],
            "fl": g["flash"],
            "freed": g["freed"],
        }
    if ph == "owls":
        o = STATE["owls"]
        return {
            "ph": "owl",
            "st": o["state"],
            "tl": max(0.0, round((o["endsAt"] or 0) - time.time(), 1)),
            "prog": {n: len(p2["picked"]) for n, p2 in o["papers"].items()},
            "done": [n for n, p2 in o["papers"].items() if p2["finishedAt"]],
            "total": len(OWLS),
        }
    if ph == "potions":
        p = STATE["potions"]
        return {
            "ph": "pot",
            "st": p["state"],
            "solved": p["solved"],
            "spoiled": p["spoiled"],
            "tl": max(0.0, round((p["endsAt"] or 0) - time.time(), 1)),
            "ev": p["events"][-10:],
            "n": len(p["brews"]),
        }
    return {"ph": ph}


def house_tally():
    """How big each house is once the Hat has spoken (0 before that)."""
    if STATE["sorting"]["state"] != "revealed":
        return {h: 0 for h in HOUSES}
    return {h: len(HOUSE_MEMBERS[h]) + sum(1 for g in GUESTS.values() if g == h) for h in HOUSES}


def emptiest_house() -> str:
    """The house with the fewest people actually connected right now (ties
    broken by the smaller squad), so a handful of guests spread out."""
    live = {h: 0 for h in HOUSES}
    for info in CONNECTIONS.values():
        if info.get("house") in live:
            live[info["house"]] += 1
    total = {h: len(HOUSE_MEMBERS[h]) + sum(1 for g in GUESTS.values() if g == h) for h in HOUSES}
    return sorted(HOUSES, key=lambda h: (live[h], total[h]))[0]


def register_guest(raw: str):
    """Clean up a typed-in name and give it a house. Returns (name, house)
    or (None, None) if the name is unusable."""
    name = " ".join((raw or "").split())[:18].strip()
    if not name or not ALLOW_GUESTS:
        return None, None
    if name.lower() in NAME_TO_HOUSE:                 # already known
        return resolve_name(name)
    house = emptiest_house()
    GUESTS[name] = house
    NAME_TO_HOUSE[name.lower()] = house               # so scoring finds them
    return name, house


def resolve_name(raw: Optional[str]):
    if not raw:
        return None, None
    key = raw.strip().lower()
    house = NAME_TO_HOUSE.get(key)
    if not house:
        return None, None
    for n in ALL_NAMES:
        if n.strip().lower() == key:
            return n, house
    for n in GUESTS:
        if n.strip().lower() == key:
            return n, house
    return raw, house


# ===========================================================================
# QUIDDITCH — every player flies their own broom
# ===========================================================================
def hoops(side: int):
    """The three goal hoops for one end. side 0 = left goal (defended by
    houses[0]), 1 = right. Spread in x as well as height so they read as
    three separate posts rather than one totem pole — the client draws from
    these same numbers, so the picture and the scoring can never disagree."""
    if side == 0:
        return [(118, 330, 50), (196, 226, 56), (274, 344, 44)]
    return [(FIELD_W - 118, 330, 50), (FIELD_W - 196, 226, 56), (FIELD_W - 274, 344, 44)]


def spawn_bodies():
    q = STATE["quidditch"]
    q["bodies"] = {}
    for pid, p in PLAYERS.items():
        if p["house"] in q["houses"]:
            place_body(pid, p["house"])


def place_body(pid: int, house: str):
    q = STATE["quidditch"]
    side = 0 if house == q["houses"][0] else 1
    n = len([1 for b in q["bodies"].values() if b["house"] == house])
    x = 380 + (n % 3) * 90 if side == 0 else FIELD_W - 380 - (n % 3) * 90
    y = 220 + (n // 3) * 130 + random.uniform(-30, 30)
    q["bodies"][pid] = {
        "x": float(x), "y": float(min(FIELD_H - 60, y)), "vx": 0.0, "vy": 0.0,
        "fx": 1.0 if side == 0 else -1.0, "fy": 0.0, "stun": 0.0, "house": house,
    }


def start_quid_match(match: str):
    global RT_GEN
    q = STATE["quidditch"]
    b = q["bracket"]
    if not b:
        return None
    pair = {"semi1": b["semi1"], "semi2": b["semi2"], "final": b.get("finalists") or []}.get(match)
    if not pair or len(pair) != 2:
        return None
    RT_GEN += 1
    now = time.time()
    a, c = pair
    q["match"] = match
    q["matchState"] = "countdown"
    q["houses"] = [a, c]
    q["score"] = {a: 0, c: 0}
    q["startsAt"] = now + 3
    q["endsAt"] = now + 3 + QUID_SECONDS
    q["snitchOut"] = False
    q["events"] = []
    q["flash"] = None
    q["quaffle"] = {"x": FIELD_W / 2, "y": FIELD_H / 2, "vx": 0.0, "vy": 0.0, "carrier": None, "cool": 0.0}
    q["bludgers"] = [
        {"x": 500.0, "y": 260.0, "vx": 210.0, "vy": 140.0},
        {"x": 1100.0, "y": 640.0, "vx": -190.0, "vy": -160.0},
    ]
    q["snitch"] = {"x": FIELD_W / 2, "y": 240.0, "vx": 120.0, "vy": 60.0}
    spawn_bodies()
    say("quidditch", "Brooms up!", "info")
    return RT_GEN


def step_quidditch():
    q = STATE["quidditch"]
    now = time.time()
    if q["matchState"] == "countdown":
        if now < q["startsAt"]:
            return True
        q["matchState"] = "live"
        say("quidditch", "And they're away!", "info")
    if q["matchState"] != "live":
        return False

    # --- players
    for pid, b in q["bodies"].items():
        if b["stun"] > now:
            b["vx"] *= 0.86
            b["vy"] *= 0.86
        else:
            inp = INPUT.get(pid) or {}
            dx, dy = float(inp.get("dx", 0) or 0), float(inp.get("dy", 0) or 0)
            m = math.hypot(dx, dy)
            if m > 1:
                dx, dy = dx / m, dy / m
            if m > 0.05:
                b["fx"], b["fy"] = dx, dy
            b["vx"] += dx * ACCEL * DT
            b["vy"] += dy * ACCEL * DT
        b["vx"] *= DRAG
        b["vy"] *= DRAG
        sp = math.hypot(b["vx"], b["vy"])
        if sp > MAX_SPEED:
            b["vx"] *= MAX_SPEED / sp
            b["vy"] *= MAX_SPEED / sp
        b["x"] = min(FIELD_W - 40, max(40, b["x"] + b["vx"] * DT))
        b["y"] = min(FIELD_H - 40, max(40, b["y"] + b["vy"] * DT))

    # --- quaffle
    qf = q["quaffle"]
    qf["cool"] = max(0.0, qf["cool"] - DT)
    if qf["carrier"] is not None:
        c = q["bodies"].get(qf["carrier"])
        if not c or c["stun"] > now:
            qf["carrier"] = None
        else:
            qf["x"] = c["x"] + c["fx"] * 34
            qf["y"] = c["y"] + c["fy"] * 34
            qf["vx"] = qf["vy"] = 0.0
    else:
        qf["x"] += qf["vx"] * DT
        qf["y"] += qf["vy"] * DT
        qf["vx"] *= QUAFFLE_DRAG
        qf["vy"] *= QUAFFLE_DRAG
        if qf["x"] < 30 or qf["x"] > FIELD_W - 30:
            qf["vx"] *= -0.6
            qf["x"] = min(FIELD_W - 30, max(30, qf["x"]))
        if qf["y"] < 30 or qf["y"] > FIELD_H - 30:
            qf["vy"] *= -0.6
            qf["y"] = min(FIELD_H - 30, max(30, qf["y"]))
        # goals: the ball must pass through a hoop of the defending side
        for side in (0, 1):
            for (hx, hy, hr) in hoops(side):
                if math.hypot(qf["x"] - hx, qf["y"] - hy) < hr * 0.8:
                    scorer = q["houses"][1] if side == 0 else q["houses"][0]
                    q["score"][scorer] = q["score"].get(scorer, 0) + GOAL_POINTS
                    thrower = qf.get("last")
                    tname = PLAYERS.get(thrower, {}).get("name") if thrower else None
                    if tname and NAME_TO_HOUSE.get(tname.lower()) == scorer:
                        bump(tname, "goals")
                        say("quidditch", f"GOAL — {tname} puts it through for {scorer}!", "goal")
                    else:
                        say("quidditch", f"GOAL for {scorer}!", "goal")
                    flash("quidditch", "goal", scorer)
                    qf.update({"x": FIELD_W / 2, "y": FIELD_H / 2, "vx": 0.0, "vy": 0.0, "carrier": None, "cool": 0.6, "last": None})
                    break
        # pick-ups
        if qf["carrier"] is None and qf["cool"] <= 0:
            best, bestd = None, 1e9
            for pid, b in q["bodies"].items():
                if b["stun"] > now:
                    continue
                d = math.hypot(b["x"] - qf["x"], b["y"] - qf["y"])
                if d < CATCH_R and d < bestd:
                    best, bestd = pid, d
            if best is not None:
                qf["carrier"] = best
                nm = PLAYERS.get(best, {}).get("name")
                if nm:
                    say("quidditch", f"{nm} takes the Quaffle!", "catch")

    # --- bludgers
    for bl in q["bludgers"]:
        bl["x"] += bl["vx"] * DT
        bl["y"] += bl["vy"] * DT
        if bl["x"] < 40 or bl["x"] > FIELD_W - 40:
            bl["vx"] *= -1
            bl["x"] = min(FIELD_W - 40, max(40, bl["x"]))
        if bl["y"] < 40 or bl["y"] > FIELD_H - 40:
            bl["vy"] *= -1
            bl["y"] = min(FIELD_H - 40, max(40, bl["y"]))
        for pid, b in q["bodies"].items():
            if b["stun"] > now:
                continue
            if math.hypot(b["x"] - bl["x"], b["y"] - bl["y"]) < BLUDGER_R + 18:
                b["stun"] = now + STUN_SECONDS
                b["vx"] = bl["vx"] * 0.7
                b["vy"] = bl["vy"] * 0.7
                if qf["carrier"] == pid:
                    qf["carrier"] = None
                    qf["vx"], qf["vy"] = bl["vx"] * 0.5, bl["vy"] * 0.5
                    qf["cool"] = 0.5
                nm = PLAYERS.get(pid, {}).get("name")
                say("quidditch", f"BLUDGER! {nm} is knocked off the play!" if nm else "BLUDGER!", "bludger")
                flash("quidditch", "bludger", b["house"])

    # --- snitch
    remaining = (q["endsAt"] or now) - now
    if not q["snitchOut"] and remaining <= SNITCH_AT:
        q["snitchOut"] = True
        say("quidditch", "THE GOLDEN SNITCH IS OUT — 150 to whoever catches it!", "snitch_out")
        flash("quidditch", "snitch_out")
    if q["snitchOut"]:
        s = q["snitch"]
        # flee the nearest broom
        near, nd = None, 1e9
        for pid, b in q["bodies"].items():
            d = math.hypot(b["x"] - s["x"], b["y"] - s["y"])
            if d < nd:
                near, nd = b, d
        ax = ay = 0.0
        if near and nd < 320:
            ax = (s["x"] - near["x"]) / max(1.0, nd) * 520
            ay = (s["y"] - near["y"]) / max(1.0, nd) * 520
        s["vx"] += (ax + random.uniform(-260, 260)) * DT
        s["vy"] += (ay + random.uniform(-260, 260)) * DT
        sp = math.hypot(s["vx"], s["vy"])
        if sp > 400:
            s["vx"] *= 400 / sp
            s["vy"] *= 400 / sp
        s["x"] += s["vx"] * DT
        s["y"] += s["vy"] * DT
        if s["x"] < 60 or s["x"] > FIELD_W - 60:
            s["vx"] *= -1
            s["x"] = min(FIELD_W - 60, max(60, s["x"]))
        if s["y"] < 60 or s["y"] > FIELD_H - 60:
            s["vy"] *= -1
            s["y"] = min(FIELD_H - 60, max(60, s["y"]))
        for pid, b in q["bodies"].items():
            if b["stun"] > now:
                continue
            if math.hypot(b["x"] - s["x"], b["y"] - s["y"]) < SNITCH_R + 20:
                house = b["house"]
                q["score"][house] = q["score"].get(house, 0) + SNITCH_POINTS
                nm = PLAYERS.get(pid, {}).get("name")
                if nm:
                    bump(nm, "snitch")
                say("quidditch", f"{nm} CATCHES THE SNITCH! That's the match!" if nm else "SNITCH CAUGHT!", "snitch")
                flash("quidditch", "snitch", house)
                finish_quid_match()
                return False

    if remaining <= 0:
        finish_quid_match()
        return False
    return True


def quid_act(pid: int):
    """Space / tap: throw if carrying, otherwise a short grab lunge."""
    q = STATE["quidditch"]
    if q["matchState"] != "live":
        return
    b = q["bodies"].get(pid)
    if not b or b["stun"] > time.time():
        return
    qf = q["quaffle"]
    if qf["carrier"] == pid:
        fx, fy = b["fx"], b["fy"]
        m = math.hypot(fx, fy) or 1.0
        qf["carrier"] = None
        qf["last"] = pid
        qf["x"] = b["x"] + fx / m * 40
        qf["y"] = b["y"] + fy / m * 40
        qf["vx"] = fx / m * THROW_SPEED + b["vx"] * 0.3
        qf["vy"] = fy / m * THROW_SPEED + b["vy"] * 0.3
        qf["cool"] = 0.45
        nm = PLAYERS.get(pid, {}).get("name")
        if nm:
            bump(nm, "assists")
    else:
        b["vx"] += b["fx"] * 240
        b["vy"] += b["fy"] * 240


def finish_quid_match():
    q = STATE["quidditch"]
    match = q["match"]
    if not match or q["matchState"] == "done":
        return
    a, c = q["houses"]
    sa, sc = q["score"].get(a, 0), q["score"].get(c, 0)
    if sa == sc:
        say("quidditch", "Level on points — sudden gold to the first goal next time. Called a draw.", "info")
        winner = a if random.random() < 0.5 else c
    else:
        winner = a if sa > sc else c
    loser = c if winner == a else a
    q["matchState"] = "done"
    q["winners"][match] = winner
    q["finalScore"][match] = {a: sa, c: sc}
    b = q["bracket"]
    if match in ("semi1", "semi2"):
        b.setdefault("finalists", [])
        if winner not in b["finalists"]:
            b["finalists"].insert(0, winner) if match == "semi1" else b["finalists"].append(winner)
        award(winner, 300, "Quidditch: semifinal win")
        award(loser, 100, "Quidditch: semifinal — well flown")
    else:
        award(winner, 600, "Quidditch: FINAL — Cup-clinching victory")
        award(loser, 250, "Quidditch: runner-up")


# ===========================================================================
# POTIONS — a clue-driven ordering puzzle, brewed individually
# ===========================================================================
def potions_start_step(idx: int):
    global RT_GEN
    p = STATE["potions"]
    if not (0 <= idx < len(POTIONS)):
        return None
    RT_GEN += 1
    p["state"] = "live"
    p["index"] = idx
    p["endsAt"] = time.time() + POTIONS[idx]["seconds"]
    p["brews"] = {}
    p["first"] = None
    p["events"] = []
    return RT_GEN


def potions_brew(name: str, house: str, order):
    """One attempt each. Returns True if the cauldron accepted the attempt."""
    p = STATE["potions"]
    if p["state"] != "live" or not (0 <= p["index"] < len(POTIONS)):
        return False
    if name in p["brews"]:
        return False
    puz = POTIONS[p["index"]]
    try:
        order = [int(i) for i in order]
    except (TypeError, ValueError):
        return False
    if len(order) != puz["slots"] or len(set(order)) != len(order):
        return False
    if any(i < 0 or i >= len(puz["shelf"]) for i in order):
        return False

    ok = order == puz["answer"]
    p["brews"][name] = {"order": order, "ok": ok, "ts": time.time()}
    p["events"].append({"name": name, "house": house, "ok": ok, "t": time.time()})
    del p["events"][:-20]

    if ok:
        p["solved"][house] = p["solved"].get(house, 0) + 1
        award(house, POTION_CORRECT_POINTS, f"Potions: brewed the {puz['name']}")
        bump(name, "brews")
        if p["first"] is None:
            p["first"] = name
            award(house, POTION_FIRST_BONUS, f"Potions: first to brew the {puz['name']}")
            bump(name, "firstbrew")
    else:
        p["spoiled"][house] = p["spoiled"].get(house, 0) + 1
    return True


def potions_resolve():
    p = STATE["potions"]
    if p["state"] == "live":
        p["state"] = "resolved"


# ===========================================================================
# O.W.L.s — Ordinary Wizarding Levels
# ===========================================================================
def owls_start():
    global RT_GEN
    RT_GEN += 1
    o = STATE["owls"]
    o["state"] = "live"
    o["endsAt"] = time.time() + OWL_SECONDS
    o["papers"] = {}
    o["results"] = None
    return RT_GEN


def owl_answer(name: str, house: str, q_index: int, choice: int):
    """Record one answer. Silent — nobody is told right or wrong until the
    results are posted, so the Hall can't shout the answers to each other."""
    o = STATE["owls"]
    if o["state"] != "live":
        return False
    try:
        q_index, choice = int(q_index), int(choice)
    except (TypeError, ValueError):
        return False
    if not (0 <= q_index < len(OWLS)) or not (0 <= choice < len(OWLS[q_index]["choices"])):
        return False
    paper = o["papers"].setdefault(name, {"picked": {}, "finishedAt": None})
    if paper["finishedAt"] or str(q_index) in paper["picked"]:
        return False
    paper["picked"][str(q_index)] = choice
    if len(paper["picked"]) >= len(OWLS):
        paper["finishedAt"] = time.time()
    return True


def owl_grade(score: int):
    for need, grade, band in OWL_BANDS:
        if score >= need:
            return grade, band
    return "Troll", "T"


def owls_post_results():
    """Mark every paper, award the houses, and record the grades."""
    o = STATE["owls"]
    if o["state"] == "graded":
        return
    o["state"] = "graded"
    results = {}
    for name, paper in o["papers"].items():
        house = NAME_TO_HOUSE.get(name.strip().lower())
        score = sum(1 for i, q in enumerate(OWLS) if paper["picked"].get(str(i)) == q["correct"])
        grade, band = owl_grade(score)
        results[name] = {"score": score, "total": len(OWLS), "grade": grade,
                         "band": band, "house": house,
                         "finishedAt": paper["finishedAt"]}
        if house:
            award(house, score * OWL_POINTS, f"O.W.L.s: {name} scored {score}/{len(OWLS)}")
            if score == len(OWLS):
                award(house, OWL_PERFECT_BONUS, f"O.W.L.s: {name} sat a flawless paper")
        bump(name, "owls", score)
        if score == len(OWLS):
            bump(name, "owlperfect")
    o["results"] = results


# ===========================================================================
# GRINGOTTS — free the trio from the vault
# ===========================================================================
def gringotts_start():
    global RT_GEN
    RT_GEN += 1
    g = STATE["gringotts"]
    now = time.time()
    g.update(fresh_gringotts())
    g["state"] = "live"
    g["endsAt"] = now + GRINGOTTS_SECONDS
    g["rune"] = random.choice(RUNES)
    g["runeAt"] = now
    g["nextGoblin"] = now + 2.0
    say("gringotts", "Down in the vaults: three cages, and something moving in the dark.", "info")
    return RT_GEN


def step_gringotts():
    g = STATE["gringotts"]
    now = time.time()
    if g["state"] not in ("live", "dragon"):
        return False

    if g["state"] == "live":
        if now - g["runeAt"] > RUNE_ROTATE:
            choices = [r for r in RUNES if r != g["rune"]]
            g["rune"] = random.choice(choices)
            g["runeAt"] = now

        if now >= g["nextGoblin"]:
            g["nextGoblin"] = now + max(1.1, GOBLIN_INTERVAL - g["freed"] * 0.4)
            cage = random.randrange(3)
            g["goblins"].append({
                "id": g["gid"], "x": random.choice([-40.0, 1040.0]),
                # lane height per cage — MUST match CAGE_Y in static/index.html
                "y": 140.0 + cage * 152 + random.uniform(-18, 18),
                "cage": cage, "letter": random.choice(GOBLIN_LETTERS), "stunned": 0.0,
            })
            g["gid"] += 1

        for gob in list(g["goblins"]):
            if gob["stunned"] > 0:
                gob["stunned"] -= DT
                if gob["stunned"] <= 0:
                    g["goblins"].remove(gob)
                continue
            target_x = 500.0
            gob["x"] += (GOBLIN_SPEED * DT) * (1 if gob["x"] < target_x else -1)
            if abs(gob["x"] - target_x) < 26:
                cage = g["cages"][gob["cage"]]
                if not cage["free"]:
                    cage["progress"] = max(0, cage["progress"] - GOBLIN_DAMAGE)
                    say("gringotts", f"A goblin rattles {cage['name']}'s cage — the lock slips back!", "hit")
                    flash("gringotts", "hit", "", cage["id"])
                g["goblins"].remove(gob)

        if all(c["free"] for c in g["cages"]) and g["state"] == "live":
            g["state"] = "dragon"
            g["dragonEndsAt"] = now + DRAGON_SECONDS
            # scale with who is actually here: ~6 clanks each, floor of 40,
            # so a 4-person rehearsal isn't asked for 150
            heads = max(1, sum(1 for i in CONNECTIONS.values() if i.get("role") == "player"))
            g["dragonTarget"] = max(40, min(DRAGON_TARGET, heads * 6))
            g["clanks"] = 0
            g["goblins"] = []
            say("gringotts", "All three are free — but the guard dragon has woken. CLANKERS! Everyone, together!", "dragon")
            flash("gringotts", "dragon")
            return True

        if now >= (g["endsAt"] or now):
            gringotts_finish(timeout=True)
            return False
        return True

    # dragon phase
    if now >= (g["dragonEndsAt"] or now):
        gringotts_finish(timeout=False)
        return False
    return True


def gringotts_rune(name: str, house: str, rune: str):
    g = STATE["gringotts"]
    if g["state"] != "live" or rune != g["rune"]:
        return False
    cage = next((c for c in g["cages"] if not c["free"]), None)
    if not cage:
        return False
    cage["progress"] += 1
    bump(name, "runes")
    award(house, 6, "Gringotts: rune cast")
    if cage["progress"] >= CAGE_TARGET:
        cage["free"] = True
        g["freed"] += 1
        bump(name, "rescues")
        award(house, 150, f"Gringotts: freed {cage['name']}")
        say("gringotts", f"{cage['name']} is free! {name} broke the last rune.", "free")
        flash("gringotts", "free", house, cage["id"])
    return True


def gringotts_zap(name: str, house: str, gid: int):
    g = STATE["gringotts"]
    if g["state"] != "live":
        return False
    for gob in g["goblins"]:
        if gob["id"] == gid and gob["stunned"] <= 0:
            gob["stunned"] = 0.6
            bump(name, "goblins")
            award(house, 10, "Gringotts: goblin stunned")
            flash("gringotts", "zap", house, gid)
            return True
    return False


def gringotts_clank(name: str, house: str):
    g = STATE["gringotts"]
    if g["state"] != "dragon":
        return False
    g["clanks"] += 1
    bump(name, "clanks")
    if g["clanks"] == g.get("dragonTarget", DRAGON_TARGET):
        say("gringotts", "The dragon recoils — the way out is clear! Everyone, onto its back!", "dragon")
        flash("gringotts", "dragon_win")
    return True


def gringotts_finish(timeout: bool):
    g = STATE["gringotts"]
    g["state"] = "done"
    freed = sum(1 for c in g["cages"] if c["free"])
    if freed == 3 and g["clanks"] >= g.get("dragonTarget", DRAGON_TARGET):
        say("gringotts", "The vault is empty, the trio are safe, and the dragon is somebody else's problem.", "info")
        for h in HOUSES:
            award(h, 80, "Gringotts: escaped on dragonback")
    elif freed == 3:
        say("gringotts", "All three are out — singed, but out.", "info")
    else:
        say("gringotts", f"The vault seals with {3 - freed} still inside. The trio will not forget who tried.", "info")


# ===========================================================================
# Certificate (the shareable finale)
# ===========================================================================
def certificate_data():
    pts = STATE["points"]
    order = sorted(pts.items(), key=lambda kv: -kv[1])
    st = STATE["stats"]

    def top(key, label, unit):
        best = [(n, v.get(key, 0)) for n, v in st.items() if v.get(key, 0) > 0]
        best.sort(key=lambda kv: -kv[1])
        if not best:
            return None
        return {"label": label, "name": best[0][0], "value": best[0][1], "unit": unit,
                "house": NAME_TO_HOUSE.get(best[0][0].lower())}

    honours = [h for h in [
        top("goals", "Top Scorer", "goals"),
        top("snitch", "Seeker of the Night", "Snitch caught"),
        top("brews", "Master Brewer", "potions brewed"),
        top("owls", "Top of the Year", f"O.W.L.s out of {len(OWLS)}"),
        top("firstbrew", "Fastest Cauldron", "first to brew"),
        top("runes", "Rune-Breaker", "runes cast"),
        top("goblins", "Goblin's Bane", "goblins stunned"),
        top("rescues", "Cage-Breaker", "rescues"),
        top("clanks", "Loudest Clanker", "clanks"),
    ] if h]

    return {
        "date": "11 September 2026",
        "champion": order[0][0] if order else None,
        "standings": [{"house": h, "points": p} for h, p in order],
        "houseMembers": HOUSE_MEMBERS,
        "guestMembers": {h: [n for n, gh in GUESTS.items() if gh == h] for h in HOUSES},
        "allowGuests": ALLOW_GUESTS,
        "honours": honours,
        "quidditch": {
            "winners": STATE["quidditch"]["winners"],
            "finalScore": STATE["quidditch"]["finalScore"],
        },
        "gringotts": {
            "freed": [c["name"] for c in STATE["gringotts"]["cages"] if c["free"]],
            "clanks": STATE["gringotts"]["clanks"],
        },
        "potions": {"solved": STATE["potions"]["solved"], "brewed": len(STATE["potions"]["brews"])},
        "owls": STATE["owls"]["results"],
    }


# ===========================================================================
# Realtime loop
# ===========================================================================
async def sorting_reveal_after(delay: float):
    """The Hat's pause. Everyone counts down together, then every house is
    revealed at once."""
    await asyncio.sleep(delay)
    async with LOCK:
        s = STATE["sorting"]
        if s["state"] == "counting":
            s["state"] = "revealed"
            s["endsAt"] = None
            await broadcast()


async def rt_loop(gen: int):
    while True:
        await asyncio.sleep(DT)
        async with LOCK:
            if gen != RT_GEN:
                return
            ph = STATE["phase"]
            alive = False
            if ph == "quidditch":
                alive = step_quidditch()
            elif ph == "gringotts":
                alive = step_gringotts()
            elif ph == "owls":
                o = STATE["owls"]
                if o["state"] == "live":
                    if time.time() >= (o["endsAt"] or 0):
                        owls_post_results()
                        await broadcast()
                        return
                    alive = True
            elif ph == "potions":
                p = STATE["potions"]
                if p["state"] == "live":
                    if time.time() >= (p["endsAt"] or 0):
                        potions_resolve()
                        await broadcast()
                        return
                    alive = True
            if alive:
                await broadcast_frame()
            else:
                await broadcast()
                return


# ===========================================================================
# Host + player actions
# ===========================================================================
async def host_action(action: str, payload: dict):
    global RT_GEN
    if action == "goto_phase":
        RT_GEN += 1
        STATE["phase"] = payload.get("phase", STATE["phase"])

    elif action == "sorting_start":
        s = STATE["sorting"]
        if s["state"] != "counting":
            s["state"] = "counting"
            s["endsAt"] = time.time() + SORT_COUNTDOWN
            asyncio.create_task(sorting_reveal_after(SORT_COUNTDOWN))

    elif action == "potions_start_step":
        idx = payload.get("index")
        if idx is None:
            idx = STATE["potions"]["step"] + 1
        gen = potions_start_step(idx)
        if gen:
            asyncio.create_task(rt_loop(gen))

    elif action == "potions_resolve":
        potions_resolve()

    elif action == "owls_start":
        gen = owls_start()
        asyncio.create_task(rt_loop(gen))

    elif action == "owls_post":
        owls_post_results()

    elif action == "gringotts_start":
        gen = gringotts_start()
        asyncio.create_task(rt_loop(gen))

    elif action == "gringotts_end":
        gringotts_finish(timeout=True)

    elif action == "quidditch_draw":
        hs = HOUSES[:]
        random.shuffle(hs)
        STATE["quidditch"] = fresh_quidditch()
        STATE["quidditch"]["bracket"] = {"semi1": [hs[0], hs[1]], "semi2": [hs[2], hs[3]], "finalists": []}

    elif action == "quidditch_start_match":
        gen = start_quid_match(payload.get("match"))
        if gen:
            asyncio.create_task(rt_loop(gen))

    elif action == "quidditch_force_end":
        if STATE["quidditch"]["matchState"] in ("countdown", "live"):
            finish_quid_match()

    elif action == "housecup_reveal":
        STATE["housecup"]["revealed"] = True

    await broadcast()


async def player_action(pid: int, name: str, house: str, action: str, payload: dict):
    """Returns True when a full-state broadcast is warranted (frames cover
    the realtime stuff)."""
    if action == "move":
        INPUT[pid] = {"dx": payload.get("dx", 0), "dy": payload.get("dy", 0)}
        return False
    if action == "act":
        if STATE["phase"] == "quidditch":
            quid_act(pid)
        return False
    if action == "brew":
        return potions_brew(name, house, payload.get("order") or [])
    if action == "owl_answer":
        return owl_answer(name, house, payload.get("q"), payload.get("choice"))
    if action == "rune":
        gringotts_rune(name, house, str(payload.get("r", "")))
        return False
    if action == "zap":
        try:
            gringotts_zap(name, house, int(payload.get("g", -1)))
        except (TypeError, ValueError):
            pass
        return False
    if action == "clank":
        gringotts_clank(name, house)
        return False
    return False


# ===========================================================================
# WebSocket
# ===========================================================================
async def ws_endpoint(websocket: WebSocket):
    role = websocket.query_params.get("role", "player")
    name = websocket.query_params.get("name")
    pin = websocket.query_params.get("pin")
    await websocket.accept()

    canonical, house, pid = None, None, None
    if role == "host" and pin != HOST_PIN:
        await websocket.send_text(json.dumps({"type": "error", "message": "Wrong host PIN."}))
        await websocket.close()
        return
    if role == "player":
        canonical, house = resolve_name(name)
        if not canonical and websocket.query_params.get("guest") == "1":
            canonical, house = register_guest(name)
        if not canonical:
            msg = ("Name not recognized. Pick your name from the list"
                   + (", or join as a guest." if ALLOW_GUESTS else "."))
            await websocket.send_text(json.dumps({"type": "error", "message": msg}))
            await websocket.close()
            return
        pid = pid_for(canonical)
        PLAYERS[pid] = {"name": canonical, "house": house, "ws": websocket}
        stats_for(canonical)
        q = STATE["quidditch"]
        if STATE["phase"] == "quidditch" and q["matchState"] in ("countdown", "live") and house in q["houses"] and pid not in q["bodies"]:
            place_body(pid, house)

    CONNECTIONS[websocket] = {"role": role, "name": canonical, "house": house, "pid": pid}
    await websocket.send_text(json.dumps({"type": "welcome", "role": role, "name": canonical, "house": house, "pid": pid}))
    await broadcast()
    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")
            async with LOCK:
                if mtype == "host_action" and role == "host":
                    await host_action(msg.get("action"), msg.get("payload") or {})
                elif mtype == "player_action" and role == "player" and canonical:
                    if await player_action(pid, canonical, house, msg.get("action"), msg.get("payload") or {}):
                        await broadcast()
    except WebSocketDisconnect:
        drop_connection(websocket)
        try:
            await broadcast()
        except Exception:
            pass
    except Exception:
        drop_connection(websocket)
        try:
            await broadcast()
        except Exception:
            pass


# The page is normally read from static/index.html. `build_bundle.py` also
# bakes a copy into EMBEDDED_INDEX at the bottom of this file, so app.py can
# be uploaded on its own — three files, no folders. Whichever is present
# wins, with the file on disk taking precedence so it stays editable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_INDEX_PATH = os.path.join(_HERE, "static", "index.html")


def load_index() -> str:
    if os.path.exists(_INDEX_PATH):
        with open(_INDEX_PATH, encoding="utf-8") as fh:
            return fh.read()
    if EMBEDDED_INDEX.strip():
        return EMBEDDED_INDEX
    raise RuntimeError(
        "No page to serve: static/index.html is missing and this app.py has no "
        "embedded copy. Upload static/index.html alongside app.py, or use the "
        "bundled app.py from build_bundle.py."
    )


async def index(request):
    return HTMLResponse(load_index())


async def api_names(request):
    return JSONResponse({"names": ALL_NAMES})


async def api_reset(request):
    global STATE, RT_GEN
    if request.query_params.get("pin") != HOST_PIN:
        return JSONResponse({"ok": False, "error": "wrong pin"}, status_code=403)
    RT_GEN += 1
    STATE = fresh_state()
    INPUT.clear()
    GUESTS.clear()
    for k in [k for k, v in NAME_TO_HOUSE.items() if k not in {n.lower() for n in ALL_NAMES}]:
        NAME_TO_HOUSE.pop(k, None)
    await broadcast()
    return JSONResponse({"ok": True})


app = Starlette(routes=[
    Route("/", index),
    Route("/api/names", api_names),
    Route("/api/reset", api_reset),
    WebSocketRoute("/ws", ws_endpoint),
])

# Filled in by build_bundle.py — leave the empty default in the source copy.
EMBEDDED_INDEX = ""

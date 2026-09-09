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
SNITCH_AT = 40          # seconds remaining when the Snitch is released
QUID_SECONDS = 180      # 3 minutes — long enough to learn the broom, then play
QUID_COUNTDOWN = 5      # brooms-up pause before the whistle
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
    q["startsAt"] = now + QUID_COUNTDOWN
    q["endsAt"] = now + QUID_COUNTDOWN + QUID_SECONDS
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
EMBEDDED_INDEX = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, user-scalable=no" />
<title>Back to Hogwarts — House Cup Night</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@500;700;900&family=Cinzel+Decorative:wght@700;900&family=Spectral:ital,wght@0,400;0,600;1,400&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/canvas-confetti/1.9.2/confetti.browser.min.js"></script>
<style>
:root{
  --stone-950:#08070d; --stone-900:#11101a; --stone-800:#1a1826; --stone-700:#252235; --stone-600:#332e46;
  --parchment:#ecdfc2; --parchment-hi:#f7edd4; --ink:#1c1710; --ink-soft:#453c2c;
  --bone:#d9cfae; --mist:#8b83a3;
  --gold:#d4af37; --gold-hi:#f5dd8a; --ember:#e0663a; --arcane:#7b5cff; --moss:#3f6b4a; --danger:#c0392b;
  --radius:14px; color-scheme: dark;
}
*{box-sizing:border-box; -webkit-tap-highlight-color:transparent;}
html,body{margin:0;padding:0;min-height:100%;background:var(--stone-950);}
body{
  font-family:'Spectral',serif; color:var(--parchment); min-height:100vh; overflow-x:hidden;
  background:
    radial-gradient(ellipse at 50% -10%, rgba(123,92,255,.20), transparent 55%),
    radial-gradient(ellipse at 85% 110%, rgba(224,102,58,.13), transparent 50%),
    linear-gradient(180deg, var(--stone-950), var(--stone-900) 45%, #0b0912 100%);
}
h1,h2,h3{font-family:'Cinzel',serif; letter-spacing:.02em; margin:0;}
.deco{font-family:'Cinzel Decorative',serif;}
.mono{font-family:'JetBrains Mono',monospace;}
#app{min-height:100vh; display:flex; flex-direction:column;}
.screen{flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; padding:30px 16px 78px; animation:fadein .4s ease both;}
@keyframes fadein{from{opacity:0; transform:translateY(8px);} to{opacity:1; transform:none;}}
.card{background:linear-gradient(180deg, var(--stone-800), var(--stone-900)); border:1px solid rgba(212,175,55,.22);
  border-radius:var(--radius); box-shadow:0 20px 60px rgba(0,0,0,.5); padding:24px; max-width:780px; width:100%;}
.parchment-card{background:linear-gradient(180deg, var(--parchment-hi), var(--parchment)); color:var(--ink);
  border-radius:var(--radius); padding:26px; max-width:520px; width:100%; box-shadow:0 24px 70px rgba(0,0,0,.55);}
.eyebrow{text-transform:uppercase; letter-spacing:.2em; font-size:.7rem; color:var(--gold-hi); font-weight:600; margin:0 0 6px; font-family:'Cinzel',serif;}
.title-xl{font-size:clamp(1.7rem,4.4vw,2.9rem); margin:0 0 8px; color:var(--gold-hi); text-shadow:0 2px 30px rgba(212,175,55,.35);}
.sub{color:var(--mist); font-size:.96rem; margin:0 0 16px;}
.small{font-size:.84rem; color:var(--mist);} .center{text-align:center;} .wide{max-width:1180px;}
.narration{font-style:italic; color:var(--bone); line-height:1.62;}
.btn{font-family:'Cinzel',serif; font-weight:700; letter-spacing:.04em; font-size:.92rem;
  background:linear-gradient(180deg,var(--gold-hi),var(--gold)); color:#241a04; border:none; padding:13px 24px;
  border-radius:10px; cursor:pointer; box-shadow:0 6px 18px rgba(212,175,55,.26); transition:transform .12s;}
.btn:hover{transform:translateY(-1px);} .btn:active{transform:translateY(0);}
.btn.ghost{background:transparent; color:var(--parchment); border:1px solid rgba(217,207,174,.3); box-shadow:none;}
.btn.big{font-size:1.02rem; padding:16px 28px;}
.row{display:flex; gap:12px; flex-wrap:wrap; justify-content:center; align-items:center;}
.stack{display:flex; flex-direction:column; gap:13px;}
input[type=text],select{font-family:'Spectral',serif; font-size:1.05rem; padding:12px 14px; border-radius:10px;
  border:1px solid rgba(0,0,0,.25); background:#fff; color:#1c1710; width:100%;}
.house-pill{padding:4px 13px; border-radius:999px; font-family:'Cinzel',serif; font-weight:700; font-size:.78rem; display:inline-flex; align-items:center; gap:6px; white-space:nowrap;}
.house-Gryffindor{background:linear-gradient(90deg,#7f0d17,#4d070d); color:#f0cf72; border:1px solid #d4af37;}
.house-Slytherin{background:linear-gradient(90deg,#0d3b1e,#062210); color:#b9d9b6; border:1px solid #7d9a79;}
.house-Ravenclaw{background:linear-gradient(90deg,#0e2a5e,#071733); color:#e7cd93; border:1px solid #a9803c;}
.house-Hufflepuff{background:linear-gradient(90deg,#c99512,#8a6408); color:#fff3cd; border:1px solid #ffd75e;}
.topbar{position:fixed; top:0; left:0; right:0; display:flex; justify-content:space-between; align-items:center; padding:8px 12px; z-index:60; font-size:.76rem; color:var(--mist); pointer-events:none;}
.topbar > *{pointer-events:auto;}
.chip-btn{background:rgba(0,0,0,.45); border:1px solid rgba(217,207,174,.2); color:var(--parchment); border-radius:8px; padding:6px 10px; cursor:pointer; font-size:.78rem;}
.scoreboard{position:fixed; bottom:0; left:0; right:0; display:flex; z-index:50; border-top:1px solid rgba(212,175,55,.22); background:rgba(8,7,13,.92); backdrop-filter:blur(8px);}
.scoreboard .seg{flex:1; text-align:center; padding:6px 3px; font-family:'Cinzel',serif; font-size:.68rem;}
.scoreboard .seg b{display:block; font-size:.98rem; color:var(--gold-hi); font-family:'JetBrains Mono',monospace;}
.flashwash{position:fixed; inset:0; pointer-events:none; z-index:90; opacity:0;}
.flashwash.go{animation:wash .7s ease-out;}
@keyframes wash{0%{opacity:.7;}100%{opacity:0;}}
canvas.stage{display:block; width:100%; height:auto; background:#05050b; border-radius:14px;}
.stage-wrap{position:relative; width:100%; max-width:1180px; border:1px solid rgba(212,175,55,.26); border-radius:16px; overflow:hidden; box-shadow:0 26px 70px rgba(0,0,0,.6);}
.hud{position:absolute; inset:0; pointer-events:none;}
.hud-top{position:absolute; top:12px; left:14px; right:14px; display:flex; justify-content:space-between; gap:10px; align-items:flex-start;}
.hud-bot{position:absolute; bottom:12px; left:14px; right:14px;}
.panel{background:rgba(6,6,12,.74); border:1px solid rgba(212,175,55,.3); border-radius:11px; padding:7px 13px; backdrop-filter:blur(6px);}
.clock{font-family:'JetBrains Mono',monospace; font-size:1.4rem; color:var(--gold-hi);}
.clock.urgent{color:#ff7a5c; animation:tick 1s steps(2) infinite;}
@keyframes tick{0%,100%{opacity:1;}50%{opacity:.4;}}
.ticker{background:rgba(6,6,12,.74); border-left:3px solid var(--gold); border-radius:8px; padding:7px 11px; font-size:.9rem; min-height:46px;}
.ticker div{opacity:.5;} .ticker div:last-child{opacity:1; font-weight:600; color:var(--parchment-hi);}
.big-rune{font-size:clamp(3rem,9vw,6rem); line-height:1; color:var(--gold-hi); text-shadow:0 0 40px rgba(245,221,138,.6);}

/* ---------- sorting ---------- */
.hat{width:180px;height:160px;filter:drop-shadow(0 14px 30px rgba(0,0,0,.6));}
.hat.thinking{animation:hatwobble .85s ease-in-out infinite;}
@keyframes hatwobble{0%,100%{transform:rotate(-5deg);}25%{transform:rotate(4deg) translateY(-6px);}50%{transform:rotate(-3deg);}75%{transform:rotate(6deg) translateY(-4px);}}
.hat-stage{display:flex;flex-direction:column;align-items:center;gap:6px;min-height:340px;justify-content:center;}
.sorting-name{font-family:'Cinzel',serif;font-weight:700;font-size:clamp(1.4rem,4vw,2.4rem);color:var(--parchment-hi);}
.thinking-dots span{animation:blink 1.1s infinite;font-size:1.5rem;color:var(--gold-hi);}
.thinking-dots span:nth-child(2){animation-delay:.2s;} .thinking-dots span:nth-child(3){animation-delay:.4s;}
@keyframes blink{0%,80%,100%{opacity:.15;}40%{opacity:1;}}
.house-shout{font-family:'Cinzel Decorative',serif;font-weight:900;font-size:clamp(2.1rem,8.4vw,5rem);line-height:1;animation:shout .7s cubic-bezier(.2,1.5,.4,1) both;text-shadow:0 0 46px currentColor;}
@keyframes shout{0%{transform:scale(.3) rotate(-6deg);opacity:0;filter:blur(8px);}60%{transform:scale(1.12);opacity:1;filter:blur(0);}100%{transform:scale(1);}}
.tally-row{display:flex;gap:10px;flex-wrap:wrap;justify-content:center;margin-top:18px;}
.tally{min-width:112px;border-radius:12px;padding:9px 11px;text-align:center;border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.04);}
.tally b{display:block;font-family:'JetBrains Mono',monospace;font-size:1.3rem;margin-top:3px;}

.sorted-name{font-size:.95rem;font-weight:600;opacity:0;animation:sortIn .5s cubic-bezier(.2,1.4,.4,1) forwards;}
@keyframes sortIn{from{opacity:0;transform:translateY(10px) scale(.94);}to{opacity:1;transform:none;}}
.clue-card{background:linear-gradient(180deg,#f6ecd2,#e8dab8);color:#2a2010;border-radius:12px;padding:14px 16px;width:100%;
  box-shadow:0 12px 34px rgba(0,0,0,.45);border:1px solid rgba(90,60,20,.3);}
.clue-card .eyebrow{color:#7a5a10;}
.clue{font-size:.94rem;line-height:1.5;padding:5px 0;border-bottom:1px dotted rgba(90,60,20,.28);}
.clue:last-child{border-bottom:none;}
.cauldron-list{width:100%;background:rgba(255,255,255,.04);border:1px solid rgba(217,207,174,.16);border-radius:12px;padding:8px;}
.slot-row{display:flex;align-items:center;gap:10px;padding:8px;border-radius:8px;background:rgba(0,0,0,.28);margin-bottom:6px;font-size:.94rem;}
.slot-row:last-child{margin-bottom:0;}
.slot-row .slot-n{width:22px;height:22px;border-radius:50%;background:var(--gold);color:#241a04;font-weight:700;
  display:flex;align-items:center;justify-content:center;font-size:.78rem;flex:0 0 auto;font-family:'JetBrains Mono',monospace;}
.slot-row.right{border:1px solid #5ee88f;} .slot-row.wrong{border:1px solid var(--danger);opacity:.75;}
.slot-row span:nth-child(2){flex:1;}

/* ---------- controllers (player device) ---------- */
.ctrl-wrap{width:100%;max-width:460px;display:flex;flex-direction:column;gap:14px;align-items:center;}
.jar-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;width:100%;}
.jar{background:linear-gradient(180deg,var(--stone-700),var(--stone-800));border:1px solid rgba(217,207,174,.2);
  border-radius:16px;padding:16px 12px;cursor:pointer;text-align:center;font-family:'Spectral',serif;color:var(--parchment);transition:transform .1s;}
.jar:active{transform:scale(.96);}
.jar .ic{font-size:2rem;display:block;margin-bottom:6px;}
.jar .nm{font-size:.92rem;font-weight:600;}
.jar.thrown{opacity:.35;border-color:var(--gold);} .jar.ok{border-color:#5ee88f;background:linear-gradient(180deg,rgba(63,140,84,.5),var(--stone-800));}
.jar.bad{border-color:var(--danger);opacity:.5;}
.rune-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;width:100%;}
.rune-btn{font-size:2.1rem;padding:14px 0;border-radius:14px;background:linear-gradient(180deg,#2b2740,#1b1828);
  border:1px solid rgba(217,207,174,.22);color:var(--gold-hi);cursor:pointer;font-family:'JetBrains Mono',monospace;}
.rune-btn:active{transform:scale(.95);background:linear-gradient(180deg,#4a3f6b,#2b2740);}
.rune-btn.hit{animation:runehit .45s ease;}
@keyframes runehit{0%{box-shadow:0 0 0 0 rgba(94,232,143,.9);}100%{box-shadow:0 0 0 22px rgba(94,232,143,0);}}
.rune-btn.called{border-color:var(--gold-hi);color:#241a04;background:linear-gradient(180deg,#fff0bd,var(--gold));
  box-shadow:0 0 26px rgba(245,221,138,.55);}
.rune-call{display:flex;flex-direction:column;align-items:center;gap:2px;padding:6px 22px;border-radius:16px;
  border:1px solid rgba(212,175,55,.35);background:rgba(20,17,32,.75);}
.rune-call .big-rune{font-size:clamp(2.6rem,13vw,4rem);}
.rune-call .big-rune.turn{animation:runeturn .35s ease;}
@keyframes runeturn{0%{transform:scale(.6);opacity:.2;}100%{transform:scale(1);opacity:1;}}
.gob-grid{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;}
.gob-btn{min-width:56px;padding:12px 10px;border-radius:12px;background:linear-gradient(180deg,#4a2b18,#2a1710);
  border:1px solid #7a4a22;color:#ffd9a8;font-family:'JetBrains Mono',monospace;font-weight:700;font-size:1.2rem;cursor:pointer;}
.gob-btn:active{transform:scale(.94);}
.stick{width:190px;height:190px;border-radius:50%;background:radial-gradient(circle at 50% 50%,rgba(255,255,255,.07),rgba(255,255,255,.02));
  border:2px solid rgba(217,207,174,.25);position:relative;touch-action:none;flex:0 0 auto;}
.stick .knob{position:absolute;width:74px;height:74px;border-radius:50%;left:58px;top:58px;
  background:linear-gradient(180deg,var(--gold-hi),var(--gold));box-shadow:0 6px 18px rgba(0,0,0,.5);}
.act-btn{width:130px;height:130px;border-radius:50%;border:none;font-family:'Cinzel',serif;font-weight:900;font-size:1rem;
  color:#241a04;background:linear-gradient(180deg,#fff0bd,var(--gold));box-shadow:0 8px 0 #8a5a12,0 16px 28px rgba(0,0,0,.4);cursor:pointer;}
.act-btn:active{transform:translateY(6px);box-shadow:0 2px 0 #8a5a12;}
.clank-btn{width:100%;max-width:340px;padding:34px 18px;border-radius:22px;border:none;font-family:'Cinzel',serif;font-weight:900;
  font-size:1.4rem;color:#2a1206;background:linear-gradient(180deg,#ffd08a,#e0663a);box-shadow:0 10px 0 #7a2f13,0 18px 30px rgba(0,0,0,.45);cursor:pointer;}
.clank-btn:active{transform:translateY(7px);box-shadow:0 3px 0 #7a2f13;}
.meter{height:14px;border-radius:99px;background:rgba(255,255,255,.09);overflow:hidden;border:1px solid rgba(255,255,255,.12);}
.meter i{display:block;height:100%;background:linear-gradient(90deg,var(--ember),var(--gold-hi));transition:width .2s;}
.you-badge{font-family:'Cinzel',serif;font-weight:700;color:var(--gold-hi);}

/* ---------- lobby: one unsorted hall, no houses yet ---------- */
.arrivals{display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:6px 14px;}
.arrival{display:flex; align-items:center; gap:8px; font-size:.88rem; opacity:.32; padding:3px 2px;}
.arrival.here{opacity:1;}
.arrival .dot{width:7px;height:7px;border-radius:50%;flex:0 0 auto;}

/* ---------- the shared stage, mirrored onto a player's own device ----------
   --ar is the canvas aspect ratio; capping the WIDTH by (height * ar) keeps
   the picture undistorted while guaranteeing the controls stay on screen. */
.stage-wrap.mirror{max-width:min(100%, calc(var(--mh,40vh) * var(--ar,1.78))); margin:0 auto;}
.stage-wrap.mirror .hud-top{top:6px;left:8px;right:8px;}
.stage-wrap.mirror .hud-bot{bottom:6px;left:8px;right:8px;}
.stage-wrap.mirror .panel{padding:4px 9px;}
.stage-wrap.mirror .clock{font-size:1.05rem;}
.stage-wrap.mirror .ticker{font-size:.72rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; opacity:.85;}
/* a nudge that only appears on a phone held upright, where the pitch is
   width-limited and turning sideways genuinely doubles it */
.rotate-hint{display:none;}
@media (max-width:560px) and (orientation:portrait){ .rotate-hint{display:block;} }
@media (max-height:760px){
  .stick{width:150px;height:150px;} .stick .knob{width:58px;height:58px;}
  .act-btn{width:104px;height:104px;font-size:.82rem;}
  .clank-btn{padding:24px 18px;}
  .stage-wrap.mirror{--mh:32vh;}
}

.exam-paper{background:linear-gradient(180deg,#f6ecd2,#e8dab8);color:#241c0e;border-radius:12px;padding:14px 16px;width:100%;
  box-shadow:0 12px 34px rgba(0,0,0,.45);border:1px solid rgba(90,60,20,.3);}
.exam-q{font-size:1.06rem;line-height:1.5;margin-top:4px;font-weight:600;}
.exam-progress{height:8px;border-radius:99px;background:rgba(255,255,255,.09);overflow:hidden;width:100%;border:1px solid rgba(255,255,255,.1);}
.exam-progress i{display:block;height:100%;background:linear-gradient(90deg,var(--moss),var(--gold-hi));transition:width .3s;}
.owl-grade{font-family:'Cinzel Decorative',serif;font-weight:900;font-size:clamp(3.4rem,14vw,6rem);line-height:1;text-shadow:0 0 44px currentColor;}
.band-O{color:#f5dd8a;} .band-E{color:#9ad9a0;} .band-A{color:#e7cd93;} .band-P{color:#e0904a;} .band-T{color:#d05a4a;}
.review{margin-top:14px;text-align:left;max-height:44vh;overflow-y:auto;}
.review-row{padding:8px 10px;border-radius:8px;margin-bottom:6px;background:rgba(255,255,255,.04);font-size:.9rem;line-height:1.4;}
.review-row.right{border-left:3px solid #5ee88f;} .review-row.wrong{border-left:3px solid var(--danger);}
.review-row.skipped{border-left:3px solid var(--mist);opacity:.75;}

/* ---------- certificate ---------- */
.cert{background:linear-gradient(180deg,#f6ecd2,#e8dab8); color:#241c0e; width:100%; max-width:820px; padding:44px 48px;
  border-radius:6px; box-shadow:0 30px 80px rgba(0,0,0,.6); position:relative; font-family:'Spectral',serif;}
.cert:before{content:''; position:absolute; inset:14px; border:2px solid rgba(90,60,20,.35); border-radius:3px; pointer-events:none;}
.cert h1{font-family:'Cinzel Decorative',serif; font-size:2.1rem; text-align:center; color:#3a2a0c;}
.cert h2{font-family:'Cinzel',serif; font-size:1rem; letter-spacing:.18em; text-transform:uppercase; color:#6b4f1c; margin:26px 0 10px; border-bottom:1px solid rgba(90,60,20,.25); padding-bottom:4px;}
.cert .champ{text-align:center; font-family:'Cinzel Decorative',serif; font-size:2.6rem; margin:6px 0;}
.cert table{width:100%; border-collapse:collapse; font-size:.94rem;}
.cert td{padding:5px 4px; border-bottom:1px dotted rgba(90,60,20,.25);}
.cert td.n{text-align:right; font-family:'JetBrains Mono',monospace; font-weight:700;}
.cert .hon{display:flex; justify-content:space-between; gap:10px; padding:5px 0; border-bottom:1px dotted rgba(90,60,20,.22); font-size:.94rem;}
.cert .rosters{display:grid; grid-template-columns:1fr 1fr; gap:14px 24px; font-size:.86rem;}
.cert .sig{margin-top:30px; text-align:center; font-style:italic; color:#5b431a;}
@media print{
  body{background:#fff !important;}
  .topbar,.scoreboard,.no-print{display:none !important;}
  .screen{padding:0 !important;}
  .cert{box-shadow:none; max-width:100%; border-radius:0; background:#fff;}
  @page{size:A4; margin:12mm;}
}
</style>
</head>
<body>
<div class="topbar no-print">
  <span id="connStatus" class="mono small"></span>
  <span style="display:flex; gap:8px;">
    <button class="chip-btn" id="whoBtn" style="display:none" onclick="notYou()"></button>
    <button class="chip-btn" id="muteBtn" onclick="toggleMute()">🔊 Sound</button>
  </span>
</div>
<div id="flashwash" class="flashwash"></div>
<div id="app"><div class="screen"><div class="card center"><p>Lighting the candles…</p></div></div></div>
<div id="scoreboard" class="scoreboard no-print" style="display:none;"></div>

<script>
/* ========================= AUDIO ========================= */
let audioCtx=null, muted=false, noiseBuf=null;
function ensureAudio(){
  if(!audioCtx){ try{ audioCtx=new (window.AudioContext||window.webkitAudioContext)(); }catch(e){ return; } }
  if(audioCtx.state==='suspended') audioCtx.resume();
  if(!noiseBuf){ noiseBuf=audioCtx.createBuffer(1,audioCtx.sampleRate*2,audioCtx.sampleRate);
    const d=noiseBuf.getChannelData(0); for(let i=0;i<d.length;i++) d[i]=Math.random()*2-1; }
}
function tone(f,st,dur,type,vol,endF){
  if(muted||!audioCtx) return;
  const t0=audioCtx.currentTime+st, o=audioCtx.createOscillator(), g=audioCtx.createGain();
  o.type=type||'sine'; o.frequency.setValueAtTime(f,t0);
  if(endF) o.frequency.exponentialRampToValueAtTime(Math.max(20,endF),t0+dur);
  g.gain.setValueAtTime(.0001,t0); g.gain.linearRampToValueAtTime(vol==null?.16:vol,t0+Math.min(.03,dur*.3));
  g.gain.exponentialRampToValueAtTime(.0001,t0+dur);
  o.connect(g); g.connect(audioCtx.destination); o.start(t0); o.stop(t0+dur+.04);
}
function crowd(dur,vol,freq){
  if(muted||!audioCtx||!noiseBuf) return;
  const t0=audioCtx.currentTime, s=audioCtx.createBufferSource(); s.buffer=noiseBuf; s.loop=true;
  const f=audioCtx.createBiquadFilter(); f.type='bandpass'; f.frequency.value=freq||750; f.Q.value=.7;
  const g=audioCtx.createGain(); g.gain.setValueAtTime(.0001,t0);
  g.gain.linearRampToValueAtTime(vol||.13,t0+dur*.28); g.gain.exponentialRampToValueAtTime(.0001,t0+dur);
  s.connect(f); f.connect(g); g.connect(audioCtx.destination); s.start(t0); s.stop(t0+dur+.05);
}
const sfx={
  click(){ tone(430,0,.05,'square',.07); },
  ok(){ tone(660,0,.1,'triangle',.18); tone(950,.08,.16,'triangle',.15); },
  bad(){ tone(200,0,.2,'sawtooth',.13); tone(140,.1,.24,'sawtooth',.11); },
  whoosh(){ tone(190,0,.34,'sine',.11,70); },
  whistle(){ tone(1900,0,.09,'square',.09); tone(2300,.11,.13,'square',.09); },
  goal(){ tone(180,0,.5,'sawtooth',.16,320); tone(360,.06,.5,'square',.09); crowd(1.9,.19); },
  catchB(){ tone(520,0,.07,'triangle',.1); },
  thud(){ tone(90,0,.24,'sine',.19,40); },
  zap(){ tone(1500,0,.06,'square',.1,400); tone(700,.05,.12,'sawtooth',.07); },
  brew(){ tone(320,0,.3,'sine',.1,520); crowd(.6,.06,1400); },
  smoke(){ tone(150,0,.4,'sawtooth',.1,60); },
  free(){ [523,659,784,1047].forEach((f,i)=>tone(f,i*.09,.32,'triangle',.17)); crowd(1.4,.14); },
  dragon(){ tone(70,0,1.1,'sawtooth',.22,40); crowd(1.6,.2,300); },
  clank(){ tone(1200+Math.random()*400,0,.05,'square',.07); tone(300,.02,.1,'square',.05); },
  snitch(){ [1046,1318,1568,2093].forEach((f,i)=>tone(f,i*.08,.5,'triangle',.15)); crowd(2.4,.2); },
  fanfare(){ [523,659,784,1047,1318].forEach((f,i)=>tone(f,i*.12,.4,'triangle',.18)); crowd(2.6,.16); },
  hat(){ tone(320,0,.5,'sine',.09,240); },
  shout(){ tone(300,0,.14,'triangle',.15,600); tone(700,.12,.4,'triangle',.13); crowd(1.3,.15); },
};
function toggleMute(){ muted=!muted; document.getElementById('muteBtn').textContent=muted?'🔇 Muted':'🔊 Sound'; }

/* ========================= CONNECTION ========================= */
const app=document.getElementById('app'), scoreboardEl=document.getElementById('scoreboard');
const connStatusEl=document.getElementById('connStatus'), washEl=document.getElementById('flashwash');
let ws=null, S=null, FR=null, myRole=null, myName=null, myHouse=null, myPid=null;
let scaffold='', lastSortIdx=-99, sortPhase='idle', lastFlash={}, lastPotState='', lastGrinState='';
const HOUSES=['Gryffindor','Slytherin','Ravenclaw','Hufflepuff'];
const GLOW={Gryffindor:'#f0cf72',Slytherin:'#b9d9b6',Ravenclaw:'#e7cd93',Hufflepuff:'#ffd75e'};
const BAR ={Gryffindor:'#a3121e',Slytherin:'#1d6b36',Ravenclaw:'#1b4a9e',Hufflepuff:'#e0ae1f'};
const DARK={Gryffindor:'#5a0a10',Slytherin:'#062210',Ravenclaw:'#071733',Hufflepuff:'#7a5806'};

function wsUrl(p){ return (location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+'/ws?'+new URLSearchParams(p); }
function connectAsHost(pin){ myRole='host'; ws=new WebSocket(wsUrl({role:'host',pin})); wire(()=>{ localStorage.setItem('hp_role','host'); localStorage.setItem('hp_pin',pin); }); }
function connectAsPlayer(n,guest){
  myRole='player'; myName=n;
  const q={role:'player',name:n}; if(guest) q.guest='1';
  ws=new WebSocket(wsUrl(q));
  wire(()=>{ localStorage.setItem('hp_role','player'); localStorage.setItem('hp_name',n); if(guest) localStorage.setItem('hp_guest','1'); });
}
function wire(onWelcome){
  connStatusEl.textContent='connecting…';
  ws.onopen=()=>{ connStatusEl.textContent='● live'; };
  ws.onclose=()=>{ connStatusEl.textContent='○ reconnecting…';
    setTimeout(()=>{ if(myRole==='host') connectAsHost(localStorage.getItem('hp_pin')); else if(myRole==='player') connectAsPlayer(myName); },1400); };
  ws.onerror=()=>{};
  ws.onmessage=(ev)=>{
    const m=JSON.parse(ev.data);
    if(m.type==='welcome'){ if(m.role==='player'){ myHouse=m.house; myPid=m.pid; } onWelcome&&onWelcome(); updateWho(); }
    else if(m.type==='error'){ showError(m.message); }
    else if(m.type==='state'){ S=m.state; FR=null; render(); }
    else if(m.type==='f'){ if(!S) return; FR=m.f; if(m.points) S.points=m.points; onFrame(); }
  };
}
function send(t,a,p){ if(ws&&ws.readyState===1) ws.send(JSON.stringify({type:t,action:a,payload:p||{}})); }
function act(a,p){ send('player_action',a,p); }
function updateWho(){
  const b=document.getElementById('whoBtn'); if(!b) return;
  if(myRole==='host'){ b.textContent='🎙 Host — not you?'; b.style.display='inline-block'; }
  else if(myRole==='player'){ b.textContent='👤 '+myName+' — not you?'; b.style.display='inline-block'; }
}
function notYou(){ if(!confirm('Switch this device to a different role or name?')) return; try{localStorage.clear();}catch(e){} location.reload(); }
function wash(c){ washEl.style.background=`radial-gradient(ellipse at center,${c},transparent 70%)`; washEl.classList.remove('go'); void washEl.offsetWidth; washEl.classList.add('go'); }
function burst(cols,n){ if(typeof confetti==='function') confetti({particleCount:n||120,spread:80,origin:{y:.55},colors:cols||['#d4af37','#f5dd8a']}); }

/* ========================= BOOT ========================= */
async function boot(){
  const r=localStorage.getItem('hp_role');
  if(r==='host'&&localStorage.getItem('hp_pin')) return connectAsHost(localStorage.getItem('hp_pin'));
  if(r==='player'&&localStorage.getItem('hp_name')){ myName=localStorage.getItem('hp_name'); return connectAsPlayer(myName, localStorage.getItem('hp_guest')==='1'); }
  rolePicker();
}
async function rolePicker(){
  let names=[]; try{ names=(await (await fetch('/api/names')).json()).names; }catch(e){}
  window.__names=names;
  app.innerHTML=`<div class="screen"><div class="parchment-card stack center">
    <div class="deco" style="font-size:1.4rem;color:var(--ink)">⚡ Back to Hogwarts</div>
    <h1 class="title-xl" style="color:var(--ink);text-shadow:none">House Cup Night</h1>
    <p class="sub" style="color:var(--ink-soft)">How are you joining tonight?</p>
    <div class="row"><button class="btn" onclick="pickPlayer()">I'm a Player</button>
    <button class="btn ghost" style="color:var(--ink);border-color:var(--ink-soft)" onclick="pickHost()">I'm the Host</button></div>
  </div></div>`;
}
function pickPlayer(){
  app.innerHTML=`<div class="screen"><div class="parchment-card stack">
    <h2 style="color:var(--ink)">Find your name</h2>
    <select id="nameSel"><option value="">— choose your name —</option>
      ${(window.__names||[]).map(n=>`<option value="${n}">${n}</option>`).join('')}</select>
    <button class="btn" onclick="okPlayer()">Enter the Great Hall</button>
    <div style="text-align:center;font-size:.85rem;color:var(--ink-soft)">— or —</div>
    <input type="text" id="guestName" placeholder="Not on the list? Type your name" maxlength="18" />
    <button class="btn ghost" style="color:var(--ink);border-color:var(--ink-soft)" onclick="okGuest()">Join as a guest</button>
    <button class="btn ghost" style="color:var(--ink);border-color:var(--ink-soft)" onclick="rolePicker()">Back</button></div></div>`;
  const gi=document.getElementById('guestName');
  if(gi) gi.addEventListener('keydown',(e)=>{ if(e.key==='Enter') okGuest(); });
}
function okGuest(){
  const v=(document.getElementById('guestName')||{}).value;
  if(!v||!v.trim()) return;
  ensureAudio(); connectAsPlayer(v.trim(), true);
}
function okPlayer(){ const v=document.getElementById('nameSel').value; if(!v) return; ensureAudio(); connectAsPlayer(v); }
function pickHost(){
  app.innerHTML=`<div class="screen"><div class="parchment-card stack">
    <h2 style="color:var(--ink)">Host access</h2><input type="text" id="pinInput" placeholder="Host PIN" />
    <button class="btn" onclick="okHost()">Take the podium</button>
    <button class="btn ghost" style="color:var(--ink);border-color:var(--ink-soft)" onclick="rolePicker()">Back</button></div></div>`;
}
function okHost(){ const v=document.getElementById('pinInput').value.trim(); if(!v) return; ensureAudio(); connectAsHost(v); }
function showError(m){ app.innerHTML=`<div class="screen"><div class="parchment-card center" style="color:var(--ink)"><p><b>${m}</b></p><button class="btn" onclick="localStorage.clear();location.reload()">Start over</button></div></div>`; }

/* ========================= RENDER DISPATCH ========================= */
function scoreboardRender(){
  if(!S||S.phase==='lobby'){ scoreboardEl.style.display='none'; return; }
  scoreboardEl.style.display='flex';
  scoreboardEl.innerHTML=HOUSES.map(h=>`<div class="seg"><span class="house-pill house-${h}" style="font-size:.6rem">${h}</span><b>${S.points[h]}</b></div>`).join('');
}
const PHASES=[['lobby','Lobby'],['sorting','Sorting'],['potions','Potions'],['gringotts','Gringotts'],['owls','O.W.L.s'],['quidditch','Quidditch'],['housecup','Great Hall']];
function phaseJumper(){
  if(myRole!=='host') return '';
  return `<div class="row no-print" style="margin-top:22px;gap:6px;opacity:.65">
    <span class="small" style="margin-right:4px">jump to:</span>
    ${PHASES.map(([k,l])=>`<button class="chip-btn" ${S.phase===k?'style="border-color:var(--gold);color:var(--gold-hi)"':''}
      onclick="jumpTo('${k}')">${l}</button>`).join('')}</div>`;
}
function jumpTo(k){
  if(S.phase===k) return;
  sfx.whoosh(); send('host_action','goto_phase',{phase:k});
}
function render(){
  if(!S) return;
  scoreboardRender();
  const p=S.phase;
  if(p==='lobby') viewLobby();
  else if(p==='sorting') viewSorting();
  else if(p==='potions') viewPotions();
  else if(p==='gringotts') viewGringotts();
  else if(p==='owls') viewOwls();
  else if(p==='quidditch') viewQuidditch();
  else if(p==='housecup') viewHouseCup();
}
function onFrame(){
  scoreboardRender();
  const want=scaffoldKey();
  if(want!==scaffold) return render();
  if(FR.ph==='owl') owlHud();
  else if(FR.ph==='quid') quidHud();
  else if(FR.ph==='grin') grinHud();
  else if(FR.ph==='pot') potHud();
  handleFlashes();
}
function scaffoldKey(){
  if(!S) return '';
  const p=S.phase;
  if(p==='quidditch') return ['q',S.quidditch.match,FR?FR.st:S.quidditch.matchState,S.quidditch.bracket?'b':'n',(S.quidditch.bracket&&(S.quidditch.bracket.finalists||[]).length)||0,myRole,inMatch()?'in':'out'].join('|');
  if(p==='gringotts') return ['g',FR?FR.st:S.gringotts.state,myRole].join('|');
  if(p==='potions') return ['p',S.potions.state,S.potions.step,myRole].join('|');
  return [p,myRole].join('|');
}
function inMatch(){ const h=(S&&S.quidditch.houses)||[]; return myRole==='player'&&(myHouse===h[0]||myHouse===h[1]); }

/* ========================= LOBBY ========================= */
function viewLobby(){
  scaffold=scaffoldKey(); stopLoops();
  const r=S.roster||{}, mem=S.houseMembers||{}, all=Object.values(mem).flat();
  const here=Object.values(r).filter(x=>x.connected).length;
  app.innerHTML=`<div class="screen wide">
    <div class="eyebrow">Uptime Crew presents</div>
    <h1 class="title-xl deco">Back to Hogwarts</h1>
    <p class="sub">House Cup Night — the candles are lit, the Hall is filling.</p>
    <p class="mono" style="color:var(--gold-hi);font-size:1.25rem">${here} / ${all.length} arrived</p>
    <div class="card wide">
      <div class="eyebrow center">The Hall — unsorted</div>
      <p class="small center" style="margin:2px 0 12px">Nobody has a house yet. The Hat hasn't spoken.</p>
      <div class="arrivals">
      ${(()=>{ const guests=Object.values(S.guestMembers||{}).flat();
        const names=all.slice().sort((x,y)=>x.localeCompare(y))
                      .concat(guests.slice().sort((x,y)=>x.localeCompare(y)));
        return names.map(n=>{
          const on=!!(r[n]&&r[n].connected), guest=!all.includes(n);
          return `<div class="arrival${on?' here':''}">
            <span class="dot" style="background:${on?'#5ee88f':'#4a4658'};box-shadow:${on?'0 0 8px #5ee88f':'none'}"></span>${n}${guest?' <span class="small" style="opacity:.55">· guest</span>':''}</div>`;}).join('');
      })()}
      </div>
    </div>
    ${myRole==='host'?`<div class="row" style="margin-top:18px"><button class="btn big" onclick="sfx.whoosh();send('host_action','goto_phase',{phase:'sorting'})">Begin the Sorting Ceremony</button></div>`
      :`<p class="small" style="margin-top:14px">You're in. Keep this tab open — it becomes your wand, your ladle and your broom. 🕯️</p>`}
  ${phaseJumper()}
  </div>`;
}

/* ========================= SORTING — everyone at once ========================= */
function hatSvg(cls){ return `<svg class="hat ${cls||''}" viewBox="0 0 200 180">
  <defs><linearGradient id="hg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" stop-color="#7a6140"/><stop offset="45%" stop-color="#54422a"/><stop offset="100%" stop-color="#2e2412"/></linearGradient></defs>
  <ellipse cx="100" cy="152" rx="80" ry="17" fill="#241c0d"/><ellipse cx="100" cy="147" rx="80" ry="16" fill="#3a2d18"/>
  <path d="M64 148 Q 66 98, 92 58 Q 112 28, 156 20 Q 134 56, 126 98 Q 119 130, 128 148 Z" fill="url(#hg)" stroke="#1f1708" stroke-width="2"/>
  <path d="M67 130 Q 96 120, 123 128" stroke="#2a2010" stroke-width="7" fill="none" opacity=".85"/>
  <path d="M74 108 Q 96 100, 118 106" stroke="#241b0c" stroke-width="3" fill="none" opacity=".7"/>
  <path d="M78 116 q 8 -7 17 -2" stroke="#120d05" stroke-width="4" fill="none" stroke-linecap="round"/>
  <path d="M103 114 q 8 -6 16 -1" stroke="#120d05" stroke-width="4" fill="none" stroke-linecap="round"/>
  <path d="M76 138 Q 98 148, 120 136" stroke="#150f06" stroke-width="4.5" fill="none" stroke-linecap="round"/></svg>`; }
let sortTick=null;
function viewSorting(){
  scaffold=scaffoldKey(); stopLoops(); clearInterval(sortTick);
  const st=S.sorting.state, tally=S.houseTally||{}, roster=S.sortedRoster;
  const myHouseNow = myRole==='player' ? myHouse : null;

  let stage;
  if(st==='idle'){
    stage=`<div class="hat-stage">${hatSvg()}
      <p class="narration center" style="max-width:540px">A patched, fraying hat waits on a three-legged stool.
      Tonight it will not call you up one by one — it will look at the whole Hall at once, and decide.</p></div>`;
  } else if(st==='counting'){
    stage=`<div class="hat-stage">${hatSvg('thinking')}
      <p class="sorting-name" style="font-size:1.3rem">Hmmmmm…</p>
      <div id="sortCount" class="deco" style="font-size:clamp(5rem,18vw,11rem);line-height:1;color:var(--gold-hi);text-shadow:0 0 60px rgba(212,175,55,.6)">3</div>
      <p class="small">${myRole==='player'?'the Hat is deciding your fate, '+myName+'…':'the Hat is deciding all of your fates…'}</p></div>`;
  } else {
    if(myRole==='player'){
      stage=`<div class="hat-stage" style="background:radial-gradient(ellipse at 50% 45%,${GLOW[myHouseNow]}22,transparent 62%)">
        <div style="transform:scale(.55);margin-bottom:-34px;opacity:.9">${hatSvg()}</div>
        <div class="sorting-name" style="opacity:.8;font-size:1.2rem">${myName}, you are</div>
        <div class="house-shout" style="color:${GLOW[myHouseNow]}">${myHouseNow}!</div>
        <div style="width:min(420px,80%);height:3px;border-radius:99px;background:linear-gradient(90deg,transparent,${GLOW[myHouseNow]},transparent)"></div>
        <p style="color:var(--gold-hi);font-family:'Cinzel',serif;margin-top:10px">Welcome home. 🎉</p></div>`;
    } else {
      stage=`<div style="width:100%">
        <p class="center deco" style="font-size:1.5rem;color:var(--gold-hi);margin-bottom:14px">The Hat has spoken.</p>
        <div class="row" style="align-items:flex-start;gap:14px">
        ${HOUSES.map((h,hi)=>`<div style="flex:1;min-width:180px">
          <div class="house-pill house-${h}" style="width:100%;justify-content:center">${h}</div>
          <div style="margin-top:8px;display:flex;flex-direction:column;gap:5px">
          ${((roster&&roster[h])||[]).map((n,ni)=>`<div class="sorted-name" style="animation-delay:${(hi*60+ni*70)}ms;color:${GLOW[h]}">${n}</div>`).join('')}
          </div></div>`).join('')}
        </div></div>`;
    }
  }

  app.innerHTML=`<div class="screen wide">
    ${(myRole==='player'&&st!=='idle')?'':`<div class="eyebrow">Chapter One</div><h1 class="title-xl deco">The Sorting Ceremony</h1>`}
    ${st==='revealed'?`<p class="sub">${Object.values(tally).reduce((a,b)=>a+b,0)} witches and wizards, four houses.</p>`:''}
    <div class="card wide center" style="min-height:400px;display:flex;align-items:center;justify-content:center">${stage}</div>
    ${st==='revealed'?`<div class="tally-row">${HOUSES.map(h=>`<div class="tally" style="border-color:${GLOW[h]}44">
      <span class="house-pill house-${h}" style="font-size:.6rem">${h}</span><b style="color:${GLOW[h]}">${tally[h]||0}</b></div>`).join('')}</div>`:''}
    ${myRole==='host'?`<div class="row" style="margin-top:18px">
      ${st==='idle'?`<button class="btn big" onclick="sfx.hat();send('host_action','sorting_start')">Let the Hat decide 🎩</button>`:''}
      ${st==='revealed'?`<button class="btn big" onclick="sfx.whoosh();send('host_action','goto_phase',{phase:'potions'})">To Potions Class</button>`:''}
    </div>`:''}
    ${phaseJumper()}
  </div>`;

  if(st==='counting'){
    sfx.hat();
    const el=document.getElementById('sortCount');
    const paint=()=>{
      if(!el) return;
      const left=Math.max(0,Math.ceil((S.sorting.endsAt||0)-Date.now()/1000));
      const shown=Math.max(1,left);
      if(el.textContent!==String(shown)){
        el.textContent=shown;
        el.style.animation='none'; void el.offsetWidth; el.style.animation='shout .5s cubic-bezier(.2,1.5,.4,1) both';
        sfx.click();
      }
    };
    paint(); sortTick=setInterval(paint,120);
  }
  if(st==='revealed'&&!window.__sortCelebrated){
    window.__sortCelebrated=1;
    sfx.shout();
    if(myRole==='player'){ wash(GLOW[myHouseNow]+'55'); burst([GLOW[myHouseNow],BAR[myHouseNow],'#fff'],120); }
    else { burst([GLOW.Gryffindor,GLOW.Slytherin,GLOW.Ravenclaw,GLOW.Hufflepuff],200); }
  }
}

/* ========================= POTIONS — brew from the clues ========================= */
let potCv=null,potCx=null,potRaf=null,potFly=[],potSeen=0,potTick=null;
let cauldron=[];             // shelf indices this player has dropped in, in order

function viewPotions(){
  scaffold=scaffoldKey(); stopLoops(); clearInterval(potTick);
  const p=S.potions, puz=p.puzzle, host=myRole==='host';
  const mine = myRole==='player' ? (p.brews||{})[myName] : null;
  if(!puz || p.index!==window.__potIndex){ window.__potIndex=p.index; cauldron=[]; }

  let ctrl='';
  if(myRole==='player'&&puz){
    if(p.state==='live'&&!mine){
      ctrl=`<div class="ctrl-wrap">
        <div class="clue-card">
          <div class="eyebrow" style="margin-bottom:6px">The Professor's clues</div>
          ${puz.clues.map(c=>`<div class="clue">${c}</div>`).join('')}
        </div>
        <p class="small center">Drop <b>${puz.slots}</b> ingredients into your cauldron — order matters. One attempt.</p>
        <div class="jar-grid">${puz.shelf.map((j,i)=>`<div class="jar ${cauldron.includes(i)?'thrown':''}" onclick="dropIn(${i})">
          <span class="ic">${j.icon}</span><span class="nm">${j.n}</span></div>`).join('')}</div>
        <div class="cauldron-list" id="cauldronList">
          ${cauldron.length?cauldron.map((i,slot)=>`<div class="slot-row">
              <span class="slot-n">${slot+1}</span>
              <span>${puz.shelf[i].icon} ${puz.shelf[i].n}</span>
              <button class="chip-btn" onclick="takeOut(${i})">remove</button></div>`).join('')
            :`<p class="small center" style="margin:6px 0">🫕 the cauldron is empty…</p>`}
        </div>
        <button class="btn big" style="width:100%" ${cauldron.length===puz.slots?'':'disabled'} onclick="brew()">
          ${cauldron.length===puz.slots?'🔥 BREW IT':`add ${puz.slots-cauldron.length} more`}</button>
      </div>`;
    } else if(mine){
      const ans=puz.answer;
      ctrl=`<div class="ctrl-wrap center">
        <div class="card" style="padding:18px">
          <div class="deco" style="font-size:1.5rem;color:${mine.ok?'#7be0a4':'var(--danger)'}">
            ${mine.ok?'✨ It shimmers gold.':'💥 Your cauldron explodes.'}</div>
          <p class="small">${mine.ok?`+${50} to ${myHouse}${p.first===myName?' — and you were first, +25':''}`:'No points this round.'}</p>
          <div class="cauldron-list" style="margin-top:10px">
            ${mine.order.map((i,slot)=>`<div class="slot-row ${ans?(ans[slot]===i?'right':'wrong'):''}">
              <span class="slot-n">${slot+1}</span><span>${puz.shelf[i].icon} ${puz.shelf[i].n}</span></div>`).join('')}
          </div>
          ${ans?`<p class="small" style="margin-top:10px">The Professor's order was:<br>
            <b style="color:var(--gold-hi)">${ans.map(i=>puz.shelf[i].n).join(' → ')}</b></p>`:
            `<p class="small" style="margin-top:10px">Sit tight — the answer comes when time is called.</p>`}
        </div></div>`;
    } else {
      ctrl=`<p class="small center">Wait for the Professor to set the next brew.</p>`;
    }
  }

  app.innerHTML=`<div class="screen wide">
    ${(myRole==='player'&&puz)
      ? `<h2 style="color:var(--gold-hi);font-size:1.15rem;margin-bottom:2px">${puz.name}</h2>
         <p class="small center" style="max-width:440px;margin-bottom:10px">${puz.story}</p>`
      : `<div class="eyebrow">Chapter Two</div><h1 class="title-xl deco">Potions Class</h1>
         <p class="sub">Read the clues. Work out the order. One attempt each.</p>`}
    ${host?`<div class="stage-wrap"><canvas id="potCv" class="stage" width="1200" height="560"></canvas>
      <div class="hud"><div class="hud-top">
        <div class="panel" style="max-width:64%">
          <div class="eyebrow" id="potName">Potions</div>
          <div id="potClues" style="font-size:.94rem;line-height:1.5"></div></div>
        <div style="text-align:right">
          <div class="panel clock" id="potClock">--</div>
          <div class="panel small" id="potCount" style="margin-top:6px">0 brewed</div></div>
      </div></div></div>`:''}
    ${ctrl}
    ${host?potHostCtrl(p):''}
    ${phaseJumper()}
  </div>`;
  if(host){ potFly=[]; potSeen=0; startPot(); }
  if(p.state==='resolved'&&lastPotState!=='resolved'){
    const anyOk=Object.values(p.brews||{}).some(b=>b.ok);
    if(anyOk){ sfx.brew(); } else { sfx.smoke(); }
  }
  if(mine&&window.__lastBrewShown!==(p.index+':'+myName)){
    window.__lastBrewShown=p.index+':'+myName;
    if(mine.ok){ sfx.ok(); wash('rgba(94,232,143,.35)'); burst(['#7be0a4','#f5dd8a'],90); }
    else { sfx.smoke(); wash('rgba(192,57,43,.35)'); }
  }
  lastPotState=p.state;
  potHud();
  if(p.state==='live') potTick=setInterval(potHud,250);
}
function dropIn(i){
  const puz=S.potions.puzzle; if(!puz) return;
  if(cauldron.includes(i)||cauldron.length>=puz.slots) return;
  sfx.click(); cauldron.push(i); viewPotions();
}
function takeOut(i){ sfx.click(); cauldron=cauldron.filter(x=>x!==i); viewPotions(); }
function brew(){
  const puz=S.potions.puzzle; if(!puz||cauldron.length!==puz.slots) return;
  sfx.brew(); act('brew',{order:cauldron});
}
function potHostCtrl(p){
  const next=(p.index==null?-1:p.index)+1, has=next<(p.total||3);
  return `<div class="row" style="margin-top:16px">
    ${p.index<0?`<button class="btn big" onclick="sfx.whistle();send('host_action','potions_start_step',{index:0})">Set the first brew</button>`:''}
    ${p.state==='live'?`<button class="btn" onclick="send('host_action','potions_resolve')">Call time &amp; reveal</button>`:''}
    ${p.state==='resolved'&&has?`<button class="btn big" onclick="sfx.whistle();send('host_action','potions_start_step',{index:${next}})">Next brew</button>`:''}
    ${p.state==='resolved'&&!has?`<button class="btn big" onclick="sfx.whoosh();send('host_action','goto_phase',{phase:'gringotts'})">Down to Gringotts →</button>`:''}
  </div>`;
}
function potHud(){
  const p=S.potions, puz=p.puzzle;
  const nm=document.getElementById('potName'), cl=document.getElementById('potClues');
  if(nm) nm.textContent=puz?`Brew ${p.index+1} of ${p.total} · ${puz.name}`:'Potions';
  if(cl) cl.innerHTML=puz?puz.clues.map(c=>`<div>• ${c}</div>`).join('')
    +(p.state==='resolved'&&puz.answer?`<div style="margin-top:6px;color:var(--gold-hi)"><b>Answer: ${puz.answer.map(i=>puz.shelf[i].n).join(' → ')}</b></div>`:'')
    :'<div>The Professor unrolls a very long scroll…</div>';
  const c=document.getElementById('potClock');
  if(c){
    const left=(FR&&FR.tl!=null)?FR.tl:Math.max(0,(p.endsAt||0)-Date.now()/1000);
    c.textContent=p.state==='live'?(Math.floor(left/60)+':'+String(Math.floor(left%60)).padStart(2,'0')):'—';
    c.className='panel clock'+(p.state==='live'&&left<20?' urgent':'');
  }
  const cn=document.getElementById('potCount');
  if(cn){ const n=(FR&&FR.n!=null)?FR.n:Object.keys(p.brews||{}).length;
    cn.textContent=n+' cauldron'+(n===1?'':'s')+' in'; }
  const evs=(FR&&FR.ev)||p.events||[];
  for(let i=potSeen;i<evs.length;i++){ const e=evs[i]; potFly.push({name:e.name,house:e.house,ok:e.ok,t:performance.now()}); }
  potSeen=evs.length;
}

function houseOfName(n){
  if(!S) return null;
  for(const h of HOUSES){
    if((((S.houseMembers||{})[h])||[]).includes(n)) return h;
    if((((S.guestMembers||{})[h])||[]).includes(n)) return h;
  }
  return null;
}
function startPot(){
  potCv=document.getElementById('potCv'); if(!potCv) return; potCx=potCv.getContext('2d');
  stopPot(); loopPot();
}
function stopPot(){ if(potRaf){ cancelAnimationFrame(potRaf); potRaf=null; } }
function loopPot(){
  potRaf=requestAnimationFrame(loopPot);
  const cx=potCx; if(!cx||!S) return;
  const p=S.potions, W=1200, H=560, now=performance.now(), t=now/1000;
  const prog=(FR&&FR.solved)||p.solved||{}, smoke=(FR&&FR.spoiled)||p.spoiled||{};
  const roundOk={Gryffindor:0,Slytherin:0,Ravenclaw:0,Hufflepuff:0};
  Object.entries(p.brews||{}).forEach(([nm,b])=>{ const h=houseOfName(nm); if(h&&b.ok) roundOk[h]++; });
  // dungeon backdrop
  const g=cx.createLinearGradient(0,0,0,H);
  g.addColorStop(0,'#0b0912'); g.addColorStop(.55,'#15111e'); g.addColorStop(1,'#08060d');
  cx.fillStyle=g; cx.fillRect(0,0,W,H);
  for(let r=0;r<5;r++) for(let c2=0;c2<11;c2++){   // stone wall
    cx.strokeStyle='rgba(255,255,255,.022)'; cx.lineWidth=1;
    cx.strokeRect(c2*112+((r%2)?56:0),r*84,112,84);
  }
  // shelf of dusty jars along the back wall
  cx.fillStyle='#1a1524'; cx.fillRect(0,150,W,10);
  for(let i=0;i<26;i++){
    const jx=24+i*46, hh=16+((i*7)%14);
    cx.fillStyle=`rgba(${100+((i*13)%50)},${84+((i*17)%40)},${60+((i*11)%60)},.34)`;
    cx.fillRect(jx,150-hh,18,hh);
    cx.fillStyle='rgba(255,255,255,.035)'; cx.fillRect(jx,150-hh,5,hh);
    cx.fillStyle='rgba(90,64,40,.5)'; cx.fillRect(jx+3,150-hh-4,12,4);
  }
  // wall torches
  [110,W-110].forEach((x,i)=>{
    const fl=1+Math.sin(t*7+i)*.22;
    const rg=cx.createRadialGradient(x,120,3,x,120,86*fl);
    rg.addColorStop(0,'rgba(255,170,70,.16)'); rg.addColorStop(1,'rgba(255,120,40,0)');
    cx.fillStyle=rg; cx.beginPath(); cx.arc(x,120,86*fl,0,7); cx.fill();
  });
  // the bench itself, with legs
  cx.fillStyle='#221b2c'; cx.fillRect(0,432,W,26);
  cx.fillStyle='#2b2237'; cx.fillRect(0,426,W,8);
  cx.fillStyle='rgba(12,9,18,.75)';
  [70,W/2-14,W-96].forEach(x=>cx.fillRect(x,458,20,H-458));

  HOUSES.forEach((h,i)=>{
    const x=152+i*298, base=430;              // cauldron sits on the bench
    const size=(((S.houseMembers||{})[h])||[]).length+((((S.guestMembers||{})[h])||[]).length);
    const lvl=Math.min(1,(prog[h]||0)/Math.max(3,size));
    const rimY=base-96, rimRX=66;
    // fire underneath
    const fg=cx.createRadialGradient(x,base-4,2,x,base-4,64);
    fg.addColorStop(0,'rgba(255,150,60,.55)'); fg.addColorStop(1,'rgba(255,120,40,0)');
    cx.fillStyle=fg; cx.beginPath(); cx.arc(x,base-4,64,0,7); cx.fill();
    for(let f=0;f<5;f++){
      const ph=((t*2.2+f*0.2)%1);
      cx.globalAlpha=(1-ph)*.8; cx.fillStyle=f%2?'#ffb03a':'#ff7326';
      cx.beginPath(); cx.ellipse(x-22+f*11,base-8-ph*20,4,8+ph*6,0,0,7); cx.fill();
    }
    cx.globalAlpha=1;
    // the potion surface first, so the opaque pot can be drawn over its front
    // lip — level rises toward the rim as the brew comes together
    const surfY=rimY+52-lvl*46;
    const surfRX=rimRX*(0.62+0.3*lvl);
    const liq=cx.createRadialGradient(x,surfY,2,x,surfY,surfRX);
    liq.addColorStop(0,GLOW[h]); liq.addColorStop(.55,BAR[h]); liq.addColorStop(1,DARK[h]);
    cx.fillStyle=liq;
    cx.beginPath(); cx.ellipse(x,surfY,surfRX,surfRX*0.26,0,0,7); cx.fill();
    // glow off the surface, up the inside of the pot
    const up=cx.createRadialGradient(x,surfY,4,x,surfY,90);
    up.addColorStop(0,BAR[h]+'aa'); up.addColorStop(1,'rgba(0,0,0,0)');
    cx.fillStyle=up; cx.beginPath(); cx.ellipse(x,surfY-16,surfRX+16,44,0,0,7); cx.fill();
    // bubbles breaking the surface
    for(let bI=0;bI<5;bI++){
      const ph=(t*0.8+bI*0.31+i*0.13)%1;
      cx.globalAlpha=(1-ph)*(0.3+lvl*0.65); cx.fillStyle=GLOW[h];
      cx.beginPath(); cx.arc(x-surfRX*0.6+bI*(surfRX*0.3)+Math.sin(t*2+bI)*6, surfY-ph*48, 2.5+ph*6,0,7); cx.fill();
    }
    // steam
    for(let sI=0;sI<4;sI++){
      const ph=(t*0.36+sI*0.25)%1;
      cx.globalAlpha=(1-ph)*0.3*(0.3+lvl); cx.fillStyle=GLOW[h];
      cx.beginPath(); cx.arc(x+Math.sin(ph*5+sI*2)*22, surfY-24-ph*120, 10+ph*26,0,7); cx.fill();
    }
    cx.globalAlpha=1;
    // opaque iron pot (front half only, so the surface stays visible)
    const body=cx.createLinearGradient(x-rimRX,rimY,x+rimRX,base);
    body.addColorStop(0,'#221c2e'); body.addColorStop(.4,'#0f0c16'); body.addColorStop(1,'#1c1727');
    cx.fillStyle=body;
    cx.beginPath();
    cx.moveTo(x-rimRX,rimY);
    cx.bezierCurveTo(x-rimRX-8,base-14,x-rimRX*0.5,base-2,x,base-2);
    cx.bezierCurveTo(x+rimRX*0.5,base-2,x+rimRX+8,base-14,x+rimRX,rimY);
    cx.bezierCurveTo(x+rimRX*0.4,rimY+30,x-rimRX*0.4,rimY+30,x-rimRX,rimY);
    cx.closePath(); cx.fill();
    // three iron feet + a belly band
    cx.fillStyle='#0c0a12';
    [-30,0,30].forEach(o=>cx.fillRect(x+o-5,base-6,10,10));
    cx.strokeStyle='rgba(255,255,255,.05)'; cx.lineWidth=6;
    cx.beginPath(); cx.ellipse(x,base-46,rimRX*0.86,20,0,.15,Math.PI-.15); cx.stroke();
    // rim, drawn last so it caps the pot cleanly
    cx.strokeStyle='#463d5c'; cx.lineWidth=8;
    cx.beginPath(); cx.ellipse(x,rimY,rimRX,15,0,0,7); cx.stroke();
    cx.strokeStyle='#736688'; cx.lineWidth=2.5;
    cx.beginPath(); cx.ellipse(x,rimY,rimRX,15,0,0,7); cx.stroke();
    cx.strokeStyle=GLOW[h]; cx.globalAlpha=.35+lvl*.4; cx.lineWidth=2;
    cx.beginPath(); cx.ellipse(x,rimY,rimRX-4,12,0,Math.PI*1.05,Math.PI*1.95); cx.stroke(); cx.globalAlpha=1;
    // the vial that fills as the draught comes together
    const vx=x+rimRX+22, vy=base-40;
    cx.strokeStyle='rgba(255,255,255,.35)'; cx.lineWidth=2;
    cx.beginPath(); cx.moveTo(vx-9,vy-58); cx.lineTo(vx-9,vy-14);
    cx.quadraticCurveTo(vx-9,vy,vx,vy); cx.quadraticCurveTo(vx+9,vy,vx+9,vy-14);
    cx.lineTo(vx+9,vy-58); cx.stroke();
    cx.fillStyle=GLOW[h]; cx.globalAlpha=.85;
    const vh=Math.max(2,44*lvl);
    cx.fillRect(vx-7,vy-2-vh,14,vh); cx.globalAlpha=1;
    // labels
    cx.font='700 16px Cinzel, serif'; cx.textAlign='center'; cx.fillStyle=GLOW[h];
    cx.fillText(h.toUpperCase(),x,base+44);
    cx.font='700 12px JetBrains Mono, monospace'; cx.fillStyle='rgba(255,255,255,.5)';
    cx.fillText((prog[h]||0)+' brewed'+((smoke[h]||0)?'   ·   '+smoke[h]+' spoiled':''),x,base+62);
    // resolve feedback
    if(p.state==='resolved'){
      const r={ok:(roundOk[h]||0)>0, jar:(smoke[h]||0)?1:null};
      if(r.jar!=null&&!r.ok){
        for(let sI=0;sI<7;sI++){ const ph=((t*0.45+sI*0.15)%1);
          cx.globalAlpha=(1-ph)*.55; cx.fillStyle='#090710';
          cx.beginPath(); cx.arc(x+Math.sin(sI*2+t)*26, rimY-10-ph*150, 14+ph*30,0,7); cx.fill(); }
        cx.globalAlpha=1;
      } else if(r.ok){
        cx.globalAlpha=.45+Math.sin(t*4)*.2;
        cx.strokeStyle=GLOW[h]; cx.lineWidth=4;
        cx.beginPath(); cx.ellipse(x,rimY,rimRX+12,22,0,0,7); cx.stroke(); cx.globalAlpha=1;
      }
    }
  });
  // flying jars
  potFly=potFly.filter(f=>now-f.t<2000);
  potFly.forEach(f=>{
    const age=(now-f.t)/2000, hi=HOUSES.indexOf(f.house); if(hi<0) return;
    const tx=152+hi*298, ty=334;
    const sx=W/2+((f.name.length*137)%700-350), sy=H+50;
    const x=sx+(tx-sx)*age, y=sy+(ty-sy)*age-Math.sin(age*Math.PI)*150;
    cx.globalAlpha=.25; cx.strokeStyle=(f.ok?GLOW[f.house]:'#ff8a72'); cx.lineWidth=2;
    cx.beginPath(); cx.moveTo(x,y); cx.lineTo(x-(tx-sx)*0.06,y+26); cx.stroke(); cx.globalAlpha=1;
    cx.save(); cx.translate(x,y); cx.rotate(age*(f.ok?3:8));
    cx.font='32px serif'; cx.textAlign='center'; cx.textBaseline='middle';
    cx.fillText(f.ok?'🧪':'💥',0,0);
    cx.restore(); cx.textBaseline='alphabetic';
    cx.globalAlpha=Math.max(0,1-age*1.15);
    cx.font='700 13px Cinzel, serif'; cx.textAlign='center';
    const w=cx.measureText(f.name).width+12;
    cx.fillStyle='rgba(6,6,12,.75)'; cx.fillRect(x-w/2,y-40,w,19);
    cx.fillStyle=f.ok?(GLOW[f.house]||'#fff'):'#ff8a72'; cx.fillText(f.name,x,y-26);
    cx.globalAlpha=1;
  });
  // count
  if(p.state==='resolved'&&p.puzzle&&p.puzzle.answer){
    cx.font='700 15px Cinzel, serif'; cx.textAlign='left'; cx.fillStyle=GLOW.Hufflepuff;
    cx.fillText('Answer: '+p.puzzle.answer.map(i=>p.puzzle.shelf[i].n).join('  →  '),20,H-18);
  }
}


/* ========================= O.W.L.s — the written paper ========================= */
let owlCv=null,owlCx=null,owlRaf=null,owlTick=null,owlQ=0;
function viewOwls(){
  scaffold=scaffoldKey(); stopLoops(); clearInterval(owlTick);
  const o=S.owls, st=o.state, host=myRole==='host';
  const mine=o.mine||{picked:{},finishedAt:null,result:null};
  const paper=o.paper;

  let ctrl='';
  if(myRole==='player'){
    if(st==='idle'){
      ctrl=`<p class="small center">Quills down. The Professor hasn't handed out the papers yet.</p>`;
    } else if(st==='live'&&!mine.finishedAt&&paper){
      const answered=Object.keys(mine.picked||{}).length;
      while(owlQ<paper.length&&(mine.picked||{})[String(owlQ)]!==undefined) owlQ++;
      const q=paper[Math.min(owlQ,paper.length-1)];
      ctrl=`<div class="ctrl-wrap">
        <div class="exam-progress"><i style="width:${(answered/paper.length)*100}%"></i></div>
        <p class="small center" style="margin:0">Question ${Math.min(owlQ+1,paper.length)} of ${paper.length} · ${q.topic}
          &nbsp;·&nbsp; <span class="mono" id="owlClock">--</span></p>
        <div class="exam-paper">
          <div class="eyebrow" style="color:#7a5a10">${q.topic}</div>
          <div class="exam-q">${q.q}</div>
        </div>
        ${q.choices.map((c,i)=>`<button class="choice-btn" onclick="answerOwl(${owlQ},${i})">
          <b style="color:var(--gold-hi)">${String.fromCharCode(65+i)}</b> &nbsp; ${c}</button>`).join('')}
        <p class="small center">No going back, and no marks until the results are posted.</p>
      </div>`;
    } else if(st==='live'&&mine.finishedAt){
      ctrl=`<div class="ctrl-wrap center"><div class="card">
        <div class="deco" style="font-size:1.5rem;color:var(--gold-hi)">🪶 Quill down.</div>
        <p class="small">Your paper is in. The Professor marks them when time is called.</p></div></div>`;
    } else if(st==='graded'){
      const r=mine.result;
      ctrl=`<div class="ctrl-wrap center"><div class="card">
        ${r?`<div class="owl-grade band-${r.band}">${r.band}</div>
          <div class="deco" style="font-size:1.35rem">${r.grade}</div>
          <p class="mono" style="color:var(--gold-hi);font-size:1.1rem">${r.score} / ${r.total}</p>
          <p class="small">+${r.score*15} to ${r.house}${r.score===r.total?' — and 50 more for a flawless paper':''}</p>`
        :`<p class="small">You didn't sit this one.</p>`}
        ${paper&&o.answers?`<div class="review">${paper.map((q,i)=>{
          const pick=(mine.picked||{})[String(i)], ok=pick===o.answers[i];
          return `<div class="review-row ${pick===undefined?'skipped':(ok?'right':'wrong')}">
            <b>${i+1}.</b> ${q.q}<br>
            <span class="small">${pick===undefined?'— left blank —':(ok?'✓ '+q.choices[pick]:'✗ you: '+q.choices[pick])}</span>
            ${ok?'':`<br><span class="small" style="color:#7be0a4">✓ ${q.choices[o.answers[i]]}</span>`}</div>`;
        }).join('')}</div>`:''}
      </div></div>`;
    }
  }

  app.innerHTML=`<div class="screen wide">
    ${(myRole==='player'&&st==='live')
      ? `<h2 style="color:var(--gold-hi);font-size:1.1rem;margin-bottom:6px">O.W.L. Examination</h2>`
      : `<div class="eyebrow">Chapter Four</div><h1 class="title-xl deco">Ordinary Wizarding Levels</h1>
         <p class="sub">Eighteen questions. Java, React, Kubernetes, AWS, LLMs and MCP. Quills ready.</p>`}
    ${host?`<div class="stage-wrap"><canvas id="owlCv" class="stage" width="1200" height="620"></canvas>
      <div class="hud"><div class="hud-top">
        <div class="panel"><div class="eyebrow" style="margin:0">The Great Hall — examinations</div>
          <div id="owlSub" style="font-size:.95rem">papers not yet handed out</div></div>
        <div class="panel clock" id="owlHostClock">--</div></div></div></div>`:''}
    ${ctrl}
    ${host?owlHostCtrl(o):''}
    ${phaseJumper()}
  </div>`;

  if(host) startOwl();
  if(st==='live'){ owlTick=setInterval(owlHud,250); }
  owlHud();
  if(st==='graded'&&window.__owlShown!==1){
    window.__owlShown=1;
    const r=mine.result;
    if(r&&r.band==='O'){ sfx.fanfare(); burst(['#f5dd8a','#fff'],160); }
    else if(r){ sfx.ok(); }
  }
  if(st!=='graded') window.__owlShown=0;
}
function owlHostCtrl(o){
  return `<div class="row" style="margin-top:16px">
    ${o.state==='idle'?`<button class="btn big" onclick="sfx.whistle();send('host_action','owls_start')">Hand out the papers</button>`:''}
    ${o.state==='live'?`<button class="btn" onclick="send('host_action','owls_post')">Quills down &amp; post results</button>`:''}
    ${o.state==='graded'?`<button class="btn big" onclick="sfx.whoosh();send('host_action','goto_phase',{phase:'quidditch'})">To the Quidditch Pitch →</button>`:''}
  </div>`;
}
function answerOwl(q,choice){ if(myRole!=='player') return; sfx.click(); owlQ=q+1; act('owl_answer',{q,choice}); }
function owlHud(){
  const o=S.owls;
  const left=(FR&&FR.tl!=null)?FR.tl:Math.max(0,(o.endsAt||0)-Date.now()/1000);
  const fmt=Math.floor(left/60)+':'+String(Math.floor(left%60)).padStart(2,'0');
  const c=document.getElementById('owlClock'); if(c) c.textContent=o.state==='live'?fmt:'—';
  const hc=document.getElementById('owlHostClock');
  if(hc){ hc.textContent=o.state==='live'?fmt:(o.state==='graded'?'marked':'—');
    hc.className='panel clock'+(o.state==='live'&&left<45?' urgent':''); }
  const sub=document.getElementById('owlSub');
  if(sub){
    const prog=(FR&&FR.prog)||o.progress||{}, done=((FR&&FR.done)||o.finished||[]).length;
    const sitting=Object.keys(prog).length, total=(FR&&FR.total)||o.total||18;
    const answered=Object.values(prog).reduce((a,b)=>a+b,0);
    sub.textContent = o.state==='graded'
      ? 'results posted on the noticeboard'
      : `${sitting} sitting · ${done} finished · ${answered} / ${sitting*total||total} answers written`;
  }
}
function startOwl(){ owlCv=document.getElementById('owlCv'); if(!owlCv) return; owlCx=owlCv.getContext('2d'); stopOwl(); loopOwl(); }
function stopOwl(){ if(owlRaf){ cancelAnimationFrame(owlRaf); owlRaf=null; } }
function loopOwl(){
  owlRaf=requestAnimationFrame(loopOwl);
  const cx=owlCx; if(!cx||!S) return;
  const W=1200,H=620,t=performance.now()/1000;
  const o=S.owls, st=o.state;
  const prog=(FR&&FR.prog)||o.progress||{}, doneList=(FR&&FR.done)||o.finished||[];
  const total=(FR&&FR.total)||o.total||18;

  const g=cx.createLinearGradient(0,0,0,H);
  g.addColorStop(0,'#0b0a14'); g.addColorStop(.5,'#171326'); g.addColorStop(1,'#0a0810');
  cx.fillStyle=g; cx.fillRect(0,0,W,H);
  // tall windows + floating candles
  for(let i=0;i<4;i++){ const x=140+i*310;
    cx.fillStyle='rgba(80,90,160,.09)';
    cx.beginPath(); cx.moveTo(x-46,250); cx.lineTo(x-46,110); cx.quadraticCurveTo(x,44,x+46,110); cx.lineTo(x+46,250); cx.closePath(); cx.fill(); }
  for(let i=0;i<26;i++){ const x=40+((i*173)%(W-80)), y=54+((i*89)%110)+Math.sin(t*1.1+i)*5;
    cx.fillStyle='#efe0b4'; cx.fillRect(x-1.5,y,3,10);
    const fl=1+Math.sin(t*9+i)*.35;
    cx.fillStyle='rgba(255,226,150,.9)'; cx.beginPath(); cx.ellipse(x,y-5,2,4.2*fl,0,0,7); cx.fill();
    const rg=cx.createRadialGradient(x,y-5,1,x,y-5,12*fl);
    rg.addColorStop(0,'rgba(255,215,130,.45)'); rg.addColorStop(1,'rgba(255,180,80,0)');
    cx.fillStyle=rg; cx.beginPath(); cx.arc(x,y-5,12*fl,0,7); cx.fill(); }

  // every candidate gets a desk, in house colour, with their name and paper
  const names=[];
  HOUSES.forEach(h=>{
    (((S.houseMembers||{})[h])||[]).concat((((S.guestMembers||{})[h])||[])).forEach(n=>{
      if(prog[n]!==undefined || st!=='idle') names.push({n,h});
    });
  });
  const list = names.length?names:HOUSES.flatMap(h=>(((S.houseMembers||{})[h])||[]).map(n=>({n,h})));
  const cols=Math.min(7,Math.max(4,Math.ceil(Math.sqrt(list.length*1.6))));
  const rows=Math.ceil(list.length/cols);
  const dw=Math.min(150,(W-80)/cols), dh=Math.min(84,(H-300)/Math.max(1,rows));
  const x0=(W-cols*dw)/2, y0=250;
  list.forEach((p,i)=>{
    const cxp=x0+(i%cols)*dw+dw/2, cy=y0+Math.floor(i/cols)*dh;
    const answered=prog[p.n]||0, finished=doneList.includes(p.n);
    const res=(o.results||{})[p.n];
    // desk
    cx.fillStyle='#241b2c'; cx.fillRect(cxp-dw*0.38,cy+22,dw*0.76,7);
    cx.fillStyle='#191322'; cx.fillRect(cxp-dw*0.30,cy+29,4,16); cx.fillRect(cxp+dw*0.26,cy+29,4,16);
    // parchment, filling as they answer
    cx.fillStyle='#e9dcbb'; cx.fillRect(cxp-20,cy-2,40,26);
    cx.fillStyle='rgba(0,0,0,.16)';
    const lines=Math.round((answered/total)*6);
    for(let l=0;l<lines;l++) cx.fillRect(cxp-16,cy+2+l*4,32,2);
    // quill, scratching while they still have questions left
    if(st==='live'&&!finished){
      const wob=Math.sin(t*9+i)*3;
      cx.strokeStyle='#d9cfae'; cx.lineWidth=2;
      cx.beginPath(); cx.moveTo(cxp+14+wob*0.2,cy+18); cx.lineTo(cxp+22+wob,cy-6); cx.stroke();
    }
    if(finished&&st==='live'){ cx.font='13px serif'; cx.textAlign='center'; cx.fillText('✔',cxp+26,cy+10); }
    // grade as a wax seal pressed on the corner of the paper
    if(res){
      const seal={O:'#e8c33a',E:'#5aa86a',A:'#8a7ad0',P:'#c9722a',T:'#a8342a'}[res.band]||'#888';
      const sx=cxp+22, sy=cy+4;
      cx.fillStyle='rgba(0,0,0,.45)'; cx.beginPath(); cx.arc(sx+1.5,sy+2,13,0,7); cx.fill();
      cx.fillStyle=seal; cx.beginPath(); cx.arc(sx,sy,13,0,7); cx.fill();
      cx.strokeStyle='rgba(255,255,255,.4)'; cx.lineWidth=1.4;
      cx.beginPath(); cx.arc(sx,sy,13,0,7); cx.stroke();
      cx.font='900 15px Cinzel, serif'; cx.textAlign='center'; cx.textBaseline='middle';
      cx.fillStyle=res.band==='O'?'#3a2a05':'#fff';
      cx.fillText(res.band,sx,sy+1);
      cx.textBaseline='alphabetic';
      cx.font='700 11px JetBrains Mono, monospace'; cx.textAlign='center';
      cx.fillStyle='rgba(255,255,255,.7)'; cx.fillText(res.score+'/'+res.total,cxp,cy+40);
    }
    // name plate
    cx.font='700 11px Cinzel, serif'; cx.textAlign='center';
    cx.fillStyle=GLOW[p.h]||'#fff';
    cx.fillText(p.n.length>12?p.n.slice(0,11)+'…':p.n,cxp,res?cy+52:cy+40);
  });

  if(st==='graded'){
    const byHouse={}; Object.values(o.results||{}).forEach(r=>{ if(r.house) byHouse[r.house]=(byHouse[r.house]||0)+r.score; });
    cx.fillStyle='rgba(6,6,12,.72)'; cx.fillRect(W/2-430,168,860,62);
    cx.strokeStyle='rgba(212,175,55,.35)'; cx.lineWidth=1.5; cx.strokeRect(W/2-430,168,860,62);
    cx.font='900 19px Cinzel, serif'; cx.textAlign='center';
    cx.fillStyle='#f5dd8a'; cx.fillText('RESULTS POSTED',W/2,192);
    const slot=860/4;
    HOUSES.forEach((h,i)=>{
      const hx=W/2-430+slot*i+slot/2;
      cx.font='700 12px Cinzel, serif'; cx.fillStyle=GLOW[h];
      cx.fillText(h.toUpperCase(),hx,212);
      cx.font='700 15px JetBrains Mono, monospace'; cx.fillStyle='#fff';
      cx.fillText(String(byHouse[h]||0),hx,228);
    });
  } else if(st==='idle'){
    cx.font='700 18px Cinzel, serif'; cx.textAlign='center'; cx.fillStyle='rgba(255,255,255,.5)';
    cx.fillText('Papers face down. Quills ready.',W/2,214);
  }
}

/* ========================= GRINGOTTS — free the trio ========================= */
let vCv=null,vCx=null,vRaf=null,zaps=[],lastRune='';
// same idea as pitchMini: the vault is mirrored onto phones, so labels grow
let vaultMini=false;
const CAGE_Y=(i)=>140+i*152+18;   // MUST match the goblin lane maths in app.py
const VY=18;
function viewGringotts(){
  scaffold=scaffoldKey(); stopLoops();
  const g=S.gringotts, st=(FR&&FR.st)||g.state, host=myRole==='host';
  let ctrl='';
  if(myRole==='player'){
    if(st==='live'){
      ctrl=`<div class="ctrl-wrap">
        <div class="rune-call"><span class="eyebrow" style="margin:0">Cast this rune</span>
          <span class="big-rune" id="runeCall">·</span></div>
        <div class="rune-grid">${(g.runes||[]).map(r=>`<button class="rune-btn" data-r="${r}" onclick="castRune('${r}')">${r}</button>`).join('')}</div>
        <div style="width:100%"><p class="small center" style="margin:6px 0">Goblins in the vault — tap to Stupefy (or press the key)</p>
        <div class="gob-grid" id="gobGrid"></div></div></div>`;
    } else if(st==='dragon'){
      ctrl=`<div class="ctrl-wrap"><p class="center" style="font-family:'Cinzel',serif;color:var(--ember)">THE DRAGON IS AWAKE — CLANKERS! ALL OF YOU!</p>
        <button class="clank-btn" id="clankBtn">🔔 CLANK</button>
        <div style="width:100%"><div class="meter"><i id="clankMeter" style="width:0%"></i></div>
        <p class="small center" id="clankMeta">—</p></div></div>`;
    } else ctrl=`<p class="small center">Wands away for a moment.</p>`;
  }
  app.innerHTML=`<div class="screen wide">
    ${(myRole==='player'&&(st==='live'||st==='dragon'))
      ? `<h2 style="color:var(--gold-hi);font-size:1.1rem;margin-bottom:6px">Gringotts — free the trio</h2>`
      : `<div class="eyebrow">Chapter Three</div><h1 class="title-xl deco">Gringotts — The Goblin Vault</h1>
         <p class="sub">Harry, Ron and Hermione are locked in the deep vaults. The Hall has one shot at them.</p>`}
    ${(host||st==='live'||st==='dragon')?`<div class="stage-wrap${host?'':' mirror'}" style="--ar:1.62">
      <canvas id="vCv" class="stage" width="1040" height="640"></canvas>
      <div class="hud">
        ${host?`<div style="position:absolute;top:10px;left:50%;transform:translateX(-50%);text-align:center">
          <div class="panel" style="padding:6px 26px" id="runePanel">
            <div class="eyebrow" style="margin:0">Everyone — cast this rune</div>
            <div class="big-rune" id="runeBig" style="font-size:clamp(2.6rem,6vw,4.4rem)">·</div>
          </div></div>`:''}
        <div style="position:absolute;top:${host?12:6}px;right:${host?14:8}px" class="panel clock" id="vClock">--</div>
        <div style="position:absolute;top:${host?12:6}px;left:${host?14:8}px" class="panel small" id="vFreed">0 / 3 free</div>
        <div class="hud-bot"><div class="ticker" id="vTicker"></div></div></div></div>`:''}
    ${ctrl}
    ${host?grinHostCtrl(g,st):''}
    ${phaseJumper()}
  </div>`;
  if(host||st==='live'||st==='dragon') startVault();
  if(myRole==='player'&&st==='dragon') wireClank();
  if(myRole==='player'&&st==='live') wireGoblinKeys();
  grinHud();
}
function grinHostCtrl(g,st){
  return `<div class="row" style="margin-top:16px">
    ${st==='idle'?`<button class="btn big" onclick="sfx.whoosh();send('host_action','gringotts_start')">Take the cart down</button>`:''}
    ${(st==='live'||st==='dragon')?`<button class="btn ghost" onclick="send('host_action','gringotts_end')">End the heist</button>`:''}
    ${st==='done'?`<button class="btn big" onclick="sfx.whoosh();send('host_action','goto_phase',{phase:'owls'})">To the O.W.L. examinations →</button>`:''}
  </div>`;
}
function castRune(r){ if(myRole!=='player') return; sfx.click();
  const b=document.querySelector(`.rune-btn[data-r="${r}"]`); if(b){ b.classList.remove('hit'); void b.offsetWidth; b.classList.add('hit'); }
  act('rune',{r}); }
function zapGoblin(id){ if(myRole!=='player') return; sfx.zap(); act('zap',{g:id}); }
function wireClank(){
  const b=document.getElementById('clankBtn'); if(!b||b.__w) return; b.__w=1;
  const go=(e)=>{ if(e)e.preventDefault(); sfx.clank(); act('clank'); if(navigator.vibrate){try{navigator.vibrate(10);}catch(_){}} };
  b.addEventListener('pointerdown',go);
  if(!window.__clankKeys){ window.__clankKeys=1;
    window.addEventListener('keydown',(e)=>{ if((e.code==='Space'||e.code==='Enter')&&S&&S.phase==='gringotts'&&((FR&&FR.st)||S.gringotts.state)==='dragon'&&myRole==='player'){ e.preventDefault(); sfx.clank(); act('clank'); } });
  }
}
function wireGoblinKeys(){
  if(window.__gobKeys) return; window.__gobKeys=1;
  window.addEventListener('keydown',(e)=>{
    if(myRole!=='player'||!S||S.phase!=='gringotts') return;
    if(((FR&&FR.st)||S.gringotts.state)!=='live') return;
    const k=(e.key||'').toUpperCase(); if(k.length!==1) return;
    const gob=((FR&&FR.gob)||[]).find(g=>g[3]===k&&g[5]<=0);
    if(gob){ e.preventDefault(); zapGoblin(gob[0]); }
  });
}
function grinHud(){
  const g=S.gringotts, st=(FR&&FR.st)||g.state;
  const rune=(FR&&FR.rune)||g.rune;
  const rb=document.getElementById('runeBig'); if(rb) rb.textContent=rune||'·';
  const rc=document.getElementById('runeCall');
  if(rc&&rc.textContent!==(rune||'·')){ rc.textContent=rune||'·';
    rc.classList.remove('turn'); void rc.offsetWidth; rc.classList.add('turn'); }
  // the matching key on the player's own pad lights up, so nobody has to
  // read the shared screen and find the button at the same time
  document.querySelectorAll('.rune-btn').forEach(b=>b.classList.toggle('called',b.dataset.r===rune));
  const rp=document.getElementById('runePanel'); if(rp) rp.style.display=(st==='dragon'?'none':'block');
  const fr=document.getElementById('vFreed');
  if(fr){ const n=(FR&&FR.freed!=null)?FR.freed:g.freed; fr.textContent=n+' / 3 free'; }
  if(rune&&rune!==lastRune){ lastRune=rune; }
  const c=document.getElementById('vClock');
  if(c){
    if(st==='dragon'&&FR&&FR.dtl!=null){ c.textContent='DRAGON '+Math.ceil(FR.dtl)+'s'; c.className='panel clock urgent'; }
    else { const tl=FR&&FR.tl!=null?FR.tl:Math.max(0,(g.endsAt||0)-Date.now()/1000);
      c.textContent=Math.floor(tl/60)+':'+String(Math.floor(tl%60)).padStart(2,'0');
      c.className='panel clock'+(tl<30?' urgent':''); }
  }
  const tk=document.getElementById('vTicker');
  if(tk){ const ev=((FR&&FR.ev)||g.events||[]).slice(myRole==='host'?-3:-1);
    tk.innerHTML=ev.map(e=>`<div>${e.text}</div>`).join('')||'<div>The cart rattles deeper…</div>'; }
  // player goblin buttons
  const grid=document.getElementById('gobGrid');
  if(grid&&FR&&FR.gob){
    const live=FR.gob.filter(x=>x[5]<=0);
    grid.innerHTML=live.length?live.map(x=>`<button class="gob-btn" onclick="zapGoblin(${x[0]})">${x[3]}</button>`).join('')
      :'<span class="small">none right now</span>';
  }
  const cm=document.getElementById('clankMeter'), cmeta=document.getElementById('clankMeta');
  if(cm){ const cl=(FR&&FR.clanks!=null)?FR.clanks:g.clanks,
          tgt=(FR&&FR.dragonTarget)||g.dragonTarget||150;
    cm.style.width=Math.min(100,(cl/tgt)*100)+'%';
    if(cmeta) cmeta.textContent=cl+' / '+tgt+' clanks'; }
  if(st==='dragon'&&lastGrinState!=='dragon'){ sfx.dragon(); wash('rgba(224,102,58,.45)'); }
  lastGrinState=st;
}
function startVault(){ vCv=document.getElementById('vCv'); if(!vCv) return; vCx=vCv.getContext('2d');
  vaultMini=myRole!=='host'; zaps=[]; stopVault(); loopVault(); }
function stopVault(){ if(vRaf){ cancelAnimationFrame(vRaf); vRaf=null; } }
function captive(cx,x,y,colour,name,free){
  // A robed silhouette — deliberately generic, identified by its name plate.
  cx.save(); cx.translate(x,y);
  if(free){ cx.globalAlpha=.9; }
  cx.fillStyle='#0d0a14';
  cx.beginPath(); cx.moveTo(0,-34); cx.quadraticCurveTo(20,-30,22,10); cx.lineTo(-22,10); cx.quadraticCurveTo(-20,-30,0,-34); cx.closePath(); cx.fill();
  cx.fillStyle=colour; cx.globalAlpha=(free?.9:.75);
  cx.beginPath(); cx.moveTo(0,-30); cx.quadraticCurveTo(9,-26,10,8); cx.lineTo(-10,8); cx.quadraticCurveTo(-9,-26,0,-30); cx.closePath(); cx.fill();
  cx.globalAlpha=1;
  cx.fillStyle='#0d0a14'; cx.beginPath(); cx.arc(0,-38,10,0,7); cx.fill();
  cx.fillStyle=free?'#ffe9a8':'#3a3348'; cx.beginPath(); cx.arc(0,-38,7,0,7); cx.fill();
  cx.restore();
}
let vaultSkip=false;
function loopVault(){
  vRaf=requestAnimationFrame(loopVault);
  if(vaultMini){ vaultSkip=!vaultSkip; if(vaultSkip) return; }
  const cx=vCx; if(!cx||!S) return;
  const W=1040,H=640,t=performance.now()/1000;
  const g=S.gringotts, st=(FR&&FR.st)||g.state;
  const cages=(FR&&FR.cages)||g.cages||[];
  const gob=(FR&&FR.gob)||[];
  // vault backdrop
  const bg=cx.createLinearGradient(0,0,0,H);
  bg.addColorStop(0,'#0a0812'); bg.addColorStop(.55,'#140f1c'); bg.addColorStop(1,'#080610');
  cx.fillStyle=bg; cx.fillRect(0,0,W,H);
  // stone blocks
  for(let r=0;r<9;r++) for(let c2=0;c2<13;c2++){
    cx.strokeStyle='rgba(255,255,255,.025)'; cx.lineWidth=1;
    cx.strokeRect(c2*80+((r%2)?40:0),r*72,80,72);
  }
  // piles of gold
  for(let i=0;i<26;i++){
    const x=40+((i*137)%(W-80)), y=H-30-((i*53)%22);
    cx.fillStyle=`rgba(${212-((i*7)%40)},${175-((i*5)%30)},55,.8)`;
    cx.beginPath(); cx.arc(x,y,5+((i*3)%4),0,7); cx.fill();
  }
  // torches, kept low and dim so they frame rather than flood
  [64,W-64].forEach((x,i)=>{
    const fl=1+Math.sin(t*7+i)*0.25;
    const rg=cx.createRadialGradient(x,470,3,x,470,110*fl);
    rg.addColorStop(0,'rgba(255,170,70,.28)'); rg.addColorStop(1,'rgba(255,120,40,0)');
    cx.fillStyle=rg; cx.beginPath(); cx.arc(x,470,110*fl,0,7); cx.fill();
    cx.fillStyle='#5a4530'; cx.fillRect(x-3,470,6,22);
    const ffl=1+Math.sin(t*9+i)*.3;
    cx.fillStyle='rgba(255,196,110,.95)';
    cx.beginPath(); cx.ellipse(x,462,4,9*ffl,0,0,7); cx.fill();
  });
  // vignette
  const vg=cx.createRadialGradient(W/2,H/2,180,W/2,H/2,W*0.62);
  vg.addColorStop(0,'rgba(0,0,0,0)'); vg.addColorStop(1,'rgba(0,0,0,.65)');
  cx.fillStyle=vg; cx.fillRect(0,0,W,H);
  // cages
  cages.forEach((c,i)=>{
    const cy=CAGE_Y(i), cxp=500, free=c.free;
    const rg=cx.createRadialGradient(cxp,cy,4,cxp,cy,130);
    rg.addColorStop(0,(free?'rgba(255,230,150,.26)':'rgba(123,92,255,.14)')); rg.addColorStop(1,'rgba(0,0,0,0)');
    cx.fillStyle=rg; cx.beginPath(); cx.arc(cxp,cy,130,0,7); cx.fill();
    cx.fillStyle='rgba(255,255,255,.05)'; cx.beginPath(); cx.ellipse(cxp,cy+44,72,11,0,0,7); cx.fill();
    captive(cx,cxp,cy+24,c.colour,c.name,free);
    if(!free){
      cx.strokeStyle='#6b6478'; cx.lineWidth=4; cx.strokeRect(cxp-56,cy-52,112,100);
      for(let b=1;b<7;b++){ cx.beginPath(); cx.moveTo(cxp-56+b*16,cy-52); cx.lineTo(cxp-56+b*16,cy+48); cx.stroke(); }
      cx.strokeStyle='#8a8299'; cx.lineWidth=2; cx.strokeRect(cxp-56,cy-52,112,100);
    } else {
      for(let sp=0;sp<8;sp++){ const ph=((performance.now()/1000*0.6+sp*0.12)%1);
        cx.globalAlpha=1-ph; cx.fillStyle='#ffe9a8';
        cx.beginPath(); cx.arc(cxp+Math.sin(sp*2+performance.now()/900)*48,cy+30-ph*76,2.5,0,7); cx.fill(); }
      cx.globalAlpha=1;
    }
    // name plate, inside the top of the cage so it can't collide with the row above
    const nfs=vaultMini?24:14, nph=vaultMini?32:21;
    cx.font=`700 ${nfs}px Cinzel, serif`; cx.textAlign='center';
    const label=free?(c.name+'  ✦ FREE'):c.name;
    const nw=cx.measureText(label).width+18;
    cx.fillStyle='rgba(6,6,12,.86)'; cx.fillRect(cxp-nw/2,cy-50,nw,nph);
    cx.strokeStyle=free?'#ffe9a8':'rgba(255,255,255,.2)'; cx.lineWidth=1.4;
    cx.strokeRect(cxp-nw/2,cy-50,nw,nph);
    cx.fillStyle=free?'#ffe9a8':c.colour; cx.fillText(label,cxp,cy-50+nph-(vaultMini?9:6));
    // lock progress, tucked under the bars
    if(!free){
      const pct=Math.min(1,c.progress/(g.cageTarget||14));
      cx.fillStyle='rgba(255,255,255,.13)'; cx.fillRect(cxp-56,cy+54,112,8);
      cx.fillStyle=GLOW.Hufflepuff; cx.fillRect(cxp-56,cy+54,112*pct,8);
      cx.font=`700 ${vaultMini?20:11}px JetBrains Mono, monospace`; cx.fillStyle='rgba(255,255,255,.7)';
      cx.fillText(c.progress+' / '+(g.cageTarget||14),cxp,cy+(vaultMini?82:74));
    }
  });
  // goblins
  gob.forEach(gg=>{
    const [id,x,y0,letter,cage,stunned]=gg;
    const y=y0+VY;
    cx.save(); cx.translate(x,y); cx.scale(1.25,1.25); cx.translate(0,0);
    const dir=x<500?1:-1;
    if(stunned>0){ cx.rotate(Math.sin(performance.now()/40)*0.5); cx.globalAlpha=.7; }
    // body
    cx.fillStyle='#4a3324'; cx.beginPath(); cx.ellipse(0,0,15,19,0,0,7); cx.fill();
    cx.fillStyle='#6b4a2c'; cx.beginPath(); cx.arc(0,-22,11,0,7); cx.fill();      // head
    cx.fillStyle='#6b4a2c';                                                        // ears
    cx.beginPath(); cx.moveTo(-10,-24); cx.lineTo(-22,-34); cx.lineTo(-8,-18); cx.closePath(); cx.fill();
    cx.beginPath(); cx.moveTo(10,-24); cx.lineTo(22,-34); cx.lineTo(8,-18); cx.closePath(); cx.fill();
    cx.fillStyle='#1a0f08'; cx.beginPath(); cx.arc(-4*dir,-23,2,0,7); cx.fill(); cx.beginPath(); cx.arc(4*dir,-23,2,0,7); cx.fill();
    cx.fillStyle='#8a5a34'; cx.beginPath(); cx.moveTo(0,-20); cx.lineTo(4*dir,-14); cx.lineTo(0,-14); cx.closePath(); cx.fill();
    // little pike
    cx.strokeStyle='#7d6b52'; cx.lineWidth=3; cx.beginPath(); cx.moveTo(14*dir,-12); cx.lineTo(20*dir,20); cx.stroke();
    cx.restore();
    // letter badge
    const bw=vaultMini?21:13, bh=vaultMini?34:22, by=y-(vaultMini?62:52);
    cx.fillStyle=stunned>0?'rgba(94,232,143,.9)':'rgba(8,7,13,.85)';
    cx.beginPath(); cx.roundRect ? cx.roundRect(x-bw,by,bw*2,bh,6) : cx.rect(x-bw,by,bw*2,bh); cx.fill();
    cx.strokeStyle=stunned>0?'#5ee88f':'#c9a227'; cx.lineWidth=1.6; cx.stroke();
    cx.font=`700 ${vaultMini?25:15}px JetBrains Mono, monospace`; cx.textAlign='center';
    cx.fillStyle=stunned>0?'#0a2a16':'#ffd9a8'; cx.fillText(letter,x,by+bh-(vaultMini?9:6));
  });
  // zap bolts
  zaps=zaps.filter(z=>performance.now()-z.t<260);
  zaps.forEach(z=>{
    const a=(performance.now()-z.t)/260;
    cx.strokeStyle=`rgba(190,255,220,${1-a})`; cx.lineWidth=4-3*a;
    cx.beginPath(); cx.moveTo(z.x,H);
    for(let i=1;i<=4;i++){ cx.lineTo(z.x+(Math.random()-.5)*30, H-(H-z.y)*(i/4)); }
    cx.stroke();
  });
  // dragon
  if(st==='dragon'){
    const cl=(FR&&FR.clanks)||0, tgt=(FR&&FR.dragonTarget)||g.dragonTarget||150, push=Math.min(1,cl/tgt);
    const dx=-120+ (1-push)*300;
    cx.save();
    cx.globalAlpha=.96;
    // neck sweeping in from off-screen, with spines
    cx.fillStyle='#241820';
    cx.beginPath();
    cx.moveTo(dx-200,H+40); cx.quadraticCurveTo(dx-40,H-140,dx+120,262);
    cx.quadraticCurveTo(dx+10,H-300,dx-200,H-60); cx.closePath(); cx.fill();
    cx.fillStyle='#3d2630';
    for(let sp=0;sp<7;sp++){
      const px=dx-150+sp*44, py=H-60-sp*46;
      cx.beginPath(); cx.moveTo(px,py); cx.lineTo(px-16,py-30); cx.lineTo(px+12,py-8); cx.closePath(); cx.fill();
    }
    // skull: brow, jaw, snout
    cx.fillStyle='#33212b';
    cx.beginPath();
    cx.moveTo(dx+80,236);                       // back of skull
    cx.quadraticCurveTo(dx+120,168,dx+206,176); // brow line
    cx.quadraticCurveTo(dx+286,186,dx+330,222); // snout top
    cx.lineTo(dx+300,240);                      // nostril step
    cx.quadraticCurveTo(dx+250,232,dx+214,244); // upper lip
    cx.quadraticCurveTo(dx+150,268,dx+92,272);  // cheek
    cx.closePath(); cx.fill();
    // lower jaw, slightly open
    cx.fillStyle='#2a1a23';
    cx.beginPath();
    cx.moveTo(dx+112,268); cx.quadraticCurveTo(dx+210,300,dx+306,264);
    cx.quadraticCurveTo(dx+220,282,dx+120,282); cx.closePath(); cx.fill();
    // teeth
    cx.fillStyle='#e8dcc4';
    for(let ti=0;ti<7;ti++){ const tx2=dx+150+ti*26;
      cx.beginPath(); cx.moveTo(tx2,252); cx.lineTo(tx2+7,252); cx.lineTo(tx2+3,268); cx.closePath(); cx.fill(); }
    // horns swept back
    cx.strokeStyle='#6b5344'; cx.lineWidth=13; cx.lineCap='round';
    cx.beginPath(); cx.moveTo(dx+126,186); cx.quadraticCurveTo(dx+70,120,dx+96,86); cx.stroke();
    cx.lineWidth=9;
    cx.beginPath(); cx.moveTo(dx+176,172); cx.quadraticCurveTo(dx+150,110,dx+178,84); cx.stroke();
    // eye, lit from within
    const eg=cx.createRadialGradient(dx+196,208,2,dx+196,208,30);
    eg.addColorStop(0,'#fff6cf'); eg.addColorStop(.35,'#ff8a2a'); eg.addColorStop(1,'rgba(120,20,0,0)');
    cx.fillStyle=eg; cx.beginPath(); cx.arc(dx+196,208,30,0,7); cx.fill();
    cx.fillStyle='#170403'; cx.beginPath(); cx.ellipse(dx+196,208,4.5,13,0,0,7); cx.fill();
    cx.strokeStyle='#1d1218'; cx.lineWidth=5;
    cx.beginPath(); cx.moveTo(dx+168,190); cx.quadraticCurveTo(dx+196,180,dx+224,192); cx.stroke();
    // nostril + smoke
    cx.fillStyle='#160c12'; cx.beginPath(); cx.ellipse(dx+306,226,7,5,0,0,7); cx.fill();
    for(let sm=0;sm<3;sm++){ const ph=((t*0.5+sm*0.33)%1);
      cx.globalAlpha=(1-ph)*.35; cx.fillStyle='#5a4a52';
      cx.beginPath(); cx.arc(dx+320+ph*70,220-ph*40,6+ph*16,0,7); cx.fill(); }
    cx.globalAlpha=1;
    // fire while the Hall is losing
    if(push<0.9){
      cx.globalAlpha=.45+Math.sin(t*10)*.2;
      const fgr=cx.createLinearGradient(dx+330,250,dx+620,250);
      fgr.addColorStop(0,'rgba(255,220,120,.95)'); fgr.addColorStop(.4,'rgba(255,140,40,.7)'); fgr.addColorStop(1,'rgba(255,60,0,0)');
      cx.fillStyle=fgr;
      cx.beginPath(); cx.moveTo(dx+320,244);
      cx.quadraticCurveTo(dx+480,258,dx+620,250);
      cx.quadraticCurveTo(dx+480,276,dx+320,272); cx.closePath(); cx.fill();
      cx.globalAlpha=1;
    }
    cx.restore();
    cx.font='900 26px Cinzel, serif'; cx.textAlign='center'; cx.fillStyle='#ffd08a';
    // low on the canvas: the cage rows own the middle, the ticker the floor
    cx.fillText('CLANKERS!  '+cl+' / '+tgt, W/2, 546);
    cx.fillStyle='rgba(255,255,255,.14)'; cx.fillRect(W/2-210,558,420,13);
    cx.fillStyle='#e0663a'; cx.fillRect(W/2-210,558,420*push,13);
  }
  if(st==='done'){
    cx.fillStyle='rgba(6,5,12,.6)'; cx.fillRect(0,0,W,H);
    const freed=cages.filter(c=>c.free).length;
    cx.font='900 34px Cinzel Decorative, serif'; cx.textAlign='center'; cx.fillStyle='#f5dd8a';
    cx.fillText(freed===3?'All three are out.':`${freed} of 3 made it out.`,W/2,H/2);
  }
}

/* ========================= QUIDDITCH — fly your own broom ========================= */
let pCv=null,pCx=null,pRaf=null,view={},moveVec={dx:0,dy:0},moveTimer=null,keyDown={},fxq=[],shake=0;
// true when the pitch is mirrored onto a phone — everything drawn on it needs
// to be bigger, because the same 1600px canvas is a few hundred pixels wide.
let pitchMini=false;
const FW=1600, FH=900;
function viewQuidditch(){
  scaffold=scaffoldKey(); stopLoops();
  const q=S.quidditch, st=(FR&&FR.st)||q.matchState, host=myRole==='host';
  const live=st==='countdown'||st==='live';
  const [a,b]=q.houses||[];
  let bracket='';
  if(q.bracket){
    const m=(k,l)=>{ const pr=k==='final'?(q.bracket.finalists||[]):q.bracket[k]; const w=q.winners[k]; const sc=q.finalScore[k];
      return `<div class="card" style="min-width:200px;max-width:250px;padding:12px"><div class="eyebrow">${l}</div>
        ${pr&&pr.length===2?`<div class="row" style="gap:6px"><span class="house-pill house-${pr[0]}">${pr[0]}</span>
          <span class="mono small">${sc?(sc[pr[0]]+' – '+sc[pr[1]]):'vs'}</span><span class="house-pill house-${pr[1]}">${pr[1]}</span></div>`
          :`<p class="small center">awaiting the semifinals</p>`}
        ${w?`<p class="center small" style="color:${GLOW[w]}">▲ ${w} advance</p>`:''}</div>`; };
    bracket=`<div class="row" style="margin-bottom:14px">${m('semi1','Semifinal 1')}${m('semi2','Semifinal 2')}${m('final','The Final')}</div>`;
  }
  let main='';
  if(live){
    // Host and every player watch the SAME pitch. On a phone the picture is
    // capped in height so the stick and the button stay under your thumbs.
    main=`<div class="stage-wrap${host?'':' mirror'}" style="--ar:1.78">
      <canvas id="pCv" class="stage" width="${FW}" height="${FH}"></canvas>
      <div class="hud"><div class="hud-top">
        <div class="panel row" style="gap:10px">
          <span class="house-pill house-${a}">${a}</span><span class="mono" id="scA" style="font-size:${host?'1.5rem':'1.15rem'};color:${GLOW[a]}">0</span>
          <span class="small">–</span><span class="mono" id="scB" style="font-size:${host?'1.5rem':'1.15rem'};color:${GLOW[b]}">0</span>
          <span class="house-pill house-${b}">${b}</span></div>
        <div class="panel clock" id="qClock">--</div></div>
        <div class="hud-bot"><div class="ticker" id="qTicker"></div></div></div></div>`;
  } else {
    main=`<div class="card center"><p class="narration" style="max-width:640px;margin:0 auto">
      ${!q.bracket?'Four houses. Two semifinals. One Final. Every player flies their own broom — the crowd cannot save you.'
        :(q.winners.final?`🏆 ${q.winners.final} win the Quidditch Cup`:'The next match is set. Brooms up.')}</p></div>`;
  }
  let ctrl='';
  if(myRole==='player'&&live){
    ctrl = inMatch()
      ? `<div class="row" style="margin-top:18px;gap:22px;justify-content:center">
           <div class="stick" id="stick"><div class="knob" id="knob"></div></div>
           <button class="act-btn" id="actBtn">CATCH<br>THROW</button></div>
         <p class="small center rotate-hint" style="margin-top:6px">↻ Turn your phone sideways for a bigger pitch</p>
         <p class="small center" style="margin-top:8px">Drag the stick or use WASD / arrows · SPACE to catch &amp; throw</p>
         <p class="small center" id="myStatus">—</p>`
      : `<div class="center" style="margin-top:12px"><button class="btn ghost" onclick="sfx.catchB()">📣 Cheer from the stands</button>
         <p class="small">${myHouse} isn't in this match — you're watching from the stands. Your turn comes.</p></div>`;
  }
  app.innerHTML=`<div class="screen wide">
    ${(myRole==='player'&&live)
      ? `<h2 style="color:var(--gold-hi);font-size:1.05rem;margin-bottom:6px">${inMatch()?'Quidditch — you\\'re flying. Your broom is the one ringed in gold.':'Quidditch — from the stands'}</h2>`
      : `<div class="eyebrow">Chapter Five</div><h1 class="title-xl deco">The Quidditch Cup</h1>`}
    ${live?'':`<p class="sub">${host?'Draw the matches, then send them up.':'Your broom appears here when your house is called.'}</p>`}
    ${(myRole==='player'&&live)?'':bracket}${main}${ctrl}
    ${host?quidHostCtrl(q,st):''}
    ${phaseJumper()}</div>`;
  if(live) startPitch();
  if(myRole==='player'&&live&&inMatch()) wireFlight();
  quidHud();
}
function quidHostCtrl(q,st){
  const inPlay=st==='countdown'||st==='live', w=q.winners||{};
  let b='';
  if(!q.bracket) b=`<button class="btn big" onclick="sfx.whoosh();send('host_action','quidditch_draw')">Draw the Matches</button>`;
  else if(inPlay) b=`<button class="btn ghost" onclick="send('host_action','quidditch_force_end')">End Match Now</button>`;
  else if(!w.semi1) b=`<button class="btn big" onclick="startMatch('semi1')">Start Semifinal 1</button>`;
  else if(!w.semi2) b=`<button class="btn big" onclick="startMatch('semi2')">Start Semifinal 2</button>`;
  else if(!w.final) b=`<button class="btn big" onclick="startMatch('final')">Start THE FINAL 🏆</button>`;
  else b=`<button class="btn big" onclick="sfx.whoosh();send('host_action','goto_phase',{phase:'housecup'})">To the Great Hall →</button>`;
  return `<div class="row" style="margin-top:18px">${b}</div>`;
}
function startMatch(m){ sfx.whistle(); send('host_action','quidditch_start_match',{match:m}); }

function wireFlight(){
  const stick=document.getElementById('stick'), knob=document.getElementById('knob'), btn=document.getElementById('actBtn');
  if(stick&&!stick.__w){
    stick.__w=1;
    const set=(e)=>{
      const r=stick.getBoundingClientRect();
      const p=e.touches?e.touches[0]:e;
      let dx=(p.clientX-(r.left+r.width/2))/(r.width/2), dy=(p.clientY-(r.top+r.height/2))/(r.height/2);
      const m=Math.hypot(dx,dy); if(m>1){ dx/=m; dy/=m; }
      moveVec={dx,dy};
      knobPlace(dx,dy);
    };
    const clear=()=>{ moveVec={dx:0,dy:0}; knobPlace(0,0); };
    knobPlace(0,0);
    stick.addEventListener('pointerdown',(e)=>{ stick.setPointerCapture(e.pointerId); set(e); });
    stick.addEventListener('pointermove',(e)=>{ if(e.pressure>0||e.buttons) set(e); });
    stick.addEventListener('pointerup',clear); stick.addEventListener('pointercancel',clear);
  }
  if(btn&&!btn.__w){ btn.__w=1; btn.addEventListener('pointerdown',(e)=>{ e.preventDefault(); doAct(); }); }
  if(!window.__flyKeys){
    window.__flyKeys=1;
    const K={ArrowUp:[0,-1],KeyW:[0,-1],ArrowDown:[0,1],KeyS:[0,1],ArrowLeft:[-1,0],KeyA:[-1,0],ArrowRight:[1,0],KeyD:[1,0]};
    window.addEventListener('keydown',(e)=>{
      if(myRole!=='player'||!S||S.phase!=='quidditch') return;
      if(e.code==='Space'){ e.preventDefault(); doAct(); return; }
      if(K[e.code]){ e.preventDefault(); keyDown[e.code]=1; applyKeys(); }
    });
    window.addEventListener('keyup',(e)=>{ if(K[e.code]){ delete keyDown[e.code]; applyKeys(); } });
    window.__K=K;
  }
  if(!moveTimer) moveTimer=setInterval(()=>{
    if(!S||S.phase!=='quidditch'||!inMatch()) return;
    act('move',{dx:+moveVec.dx.toFixed(2),dy:+moveVec.dy.toFixed(2)});
  },100);
}
// The stick shrinks on short screens, so the knob's rest position is measured
// rather than hard-coded.
function knobPlace(dx,dy){
  const s=document.getElementById('stick'), k=document.getElementById('knob');
  if(!s||!k) return;
  const c=(s.clientWidth-k.offsetWidth)/2, r=c*0.9;
  k.style.left=(c+dx*r)+'px'; k.style.top=(c+dy*r)+'px';
}
function applyKeys(){
  let dx=0,dy=0; const K=window.__K||{};
  Object.keys(keyDown).forEach(k=>{ if(K[k]){ dx+=K[k][0]; dy+=K[k][1]; } });
  const m=Math.hypot(dx,dy); if(m>1){ dx/=m; dy/=m; }
  moveVec={dx,dy};
  knobPlace(dx,dy);
}
function doAct(){ sfx.catchB(); act('act'); if(navigator.vibrate){try{navigator.vibrate(12);}catch(_){}}}

function quidHud(){
  const q=S.quidditch, [a,b]=q.houses||[];
  const sc=(FR&&FR.sc)||q.score||{};
  const A=document.getElementById('scA'), B=document.getElementById('scB');
  if(A) A.textContent=sc[a]||0; if(B) B.textContent=sc[b]||0;
  const c=document.getElementById('qClock');
  if(c){
    const st=(FR&&FR.st)||q.matchState;
    if(st==='countdown'){ const n=Math.max(1,Math.ceil((q.startsAt||0)-Date.now()/1000)); c.textContent='… '+n; c.className='panel clock'; }
    else { const tl=FR&&FR.tl!=null?FR.tl:Math.max(0,(q.endsAt||0)-Date.now()/1000);
      c.textContent=Math.floor(tl/60)+':'+String(Math.floor(tl%60)).padStart(2,'0');
      c.className='panel clock'+((q.snitchOut||tl<=30)?' urgent':''); }
  }
  const tk=document.getElementById('qTicker');
  if(tk){ const ev=((FR&&FR.ev)||q.events||[]).slice(myRole==='host'?-3:-1);
    tk.innerHTML=ev.map(e=>`<div>${e.text}</div>`).join('')||'<div>Brooms up…</div>'; }
  const ms=document.getElementById('myStatus');
  if(ms&&FR&&FR.p){
    const me=FR.p.find(x=>x[0]===myPid);
    const carrier=FR.q&&FR.q[2];
    if(!me) ms.textContent='—';
    else if(me[3]>0) ms.textContent='🌀 knocked off — back in '+me[3].toFixed(1)+'s';
    else if(carrier===myPid) ms.textContent='🔴 you have the Quaffle — SPACE to throw';
    else ms.textContent='fly to the Quaffle to take it';
  }
}
function handleFlashes(){
  const f=FR&&FR.fl; if(!f) return;
  const key=(FR.ph||'')+':'+f.n;
  if(lastFlash[key]) return; lastFlash[key]=1;
  if(FR.ph==='quid'){
    if(f.kind==='goal'){ sfx.goal(); wash((GLOW[f.house]||'#fff')+'66'); burst([GLOW[f.house],BAR[f.house],'#fff'],140); fxq.push({k:'goal',h:f.house,t:performance.now()}); }
    else if(f.kind==='bludger'){ sfx.thud(); shake=14; }
    else if(f.kind==='snitch_out'){ sfx.zap(); wash('rgba(245,221,138,.4)'); }
    else if(f.kind==='snitch'){ sfx.snitch(); wash('rgba(245,221,138,.75)'); burst(['#f5dd8a','#fff',GLOW[f.house]||'#f5dd8a'],240); }
  } else if(FR.ph==='grin'){
    if(f.kind==='zap'){ sfx.zap(); const gg=(FR.gob||[]).find(x=>x[0]===f.extra); if(gg) zaps.push({x:gg[1],y:gg[2],t:performance.now()}); }
    else if(f.kind==='free'){ sfx.free(); wash('rgba(255,233,168,.4)'); burst(['#ffe9a8','#fff'],150); }
    else if(f.kind==='hit'){ sfx.bad(); shake=8; }
    else if(f.kind==='dragon_win'){ sfx.fanfare(); burst(['#e0663a','#f5dd8a'],200); }
  }
}
function startPitch(){ pCv=document.getElementById('pCv'); if(!pCv) return; pCx=pCv.getContext('2d');
  pitchMini=myRole!=='host'; view={}; fxq=[]; stopPitch(); loopPitch(); }
function stopPitch(){ if(pRaf){ cancelAnimationFrame(pRaf); pRaf=null; } }
// Must mirror hoops() in app.py exactly — the server scores off these.
function hoopSet(side){
  return side===0 ? [[118,330,50],[196,226,56],[274,344,44]]
                  : [[FW-118,330,50],[FW-196,226,56],[FW-274,344,44]];
}
let pitchSkip=false;
function loopPitch(){
  pRaf=requestAnimationFrame(loopPitch);
  if(pitchMini){ pitchSkip=!pitchSkip; if(pitchSkip) return; }
  const cx=pCx; if(!cx||!S) return;
  const t=performance.now()/1000;
  const q=S.quidditch, [a,b]=q.houses||[];
  cx.save();
  if(shake>0){ cx.translate((Math.random()-.5)*shake,(Math.random()-.5)*shake); shake*=.86; if(shake<.4) shake=0; }
  drawSky(cx,t); drawGround(cx,t,a,b);
  [0,1].forEach(s=>hoopSet(s).forEach(h=>{
    cx.strokeStyle='#8a6a1e'; cx.lineWidth=8; cx.beginPath(); cx.moveTo(h[0],h[1]+h[2]); cx.lineTo(h[0],700); cx.stroke();
    cx.strokeStyle='#e8c33a'; cx.lineWidth=9; cx.beginPath(); cx.ellipse(h[0],h[1],h[2]*0.45,h[2],0,0,7); cx.stroke();
    cx.strokeStyle='rgba(255,240,180,.4)'; cx.lineWidth=3; cx.beginPath(); cx.ellipse(h[0],h[1],h[2]*0.45,h[2],0,0,7); cx.stroke();
  }));
  if(!FR||FR.ph!=='quid'){ cx.restore(); return; }
  // interpolate bodies
  const carrier=FR.q?FR.q[2]:null;
  (FR.p||[]).forEach(p=>{
    const [pid,x,y,stun]=p;
    if(!view[pid]) view[pid]={x,y};
    view[pid].x+=(x-view[pid].x)*0.35; view[pid].y+=(y-view[pid].y)*0.35;
    view[pid].stun=stun;
  });
  Object.keys(view).forEach(k=>{ if(!(FR.p||[]).some(p=>String(p[0])===k)) delete view[k]; });
  // bludgers
  const br=pitchMini?22:14;
  (FR.b||[]).forEach(bl=>{
    const g=cx.createRadialGradient(bl[0]-4,bl[1]-4,2,bl[0],bl[1],br+2);
    g.addColorStop(0,'#8a8a99'); g.addColorStop(1,'#14141b');
    cx.fillStyle=g; cx.beginPath(); cx.arc(bl[0],bl[1],br,0,7); cx.fill();
    if(pitchMini){ cx.strokeStyle='rgba(255,120,120,.5)'; cx.lineWidth=3;
      cx.beginPath(); cx.arc(bl[0],bl[1],br+7,0,7); cx.stroke(); }
  });
  // players
  (FR.p||[]).forEach(p=>{
    const pid=p[0], v=view[pid], pl=(S.players||{})[String(pid)]||{};
    if(!v) return;
    drawFlyer(cx,v.x,v.y,pl.house||a,pl.name||'',t,v.stun>0,pid===carrier,pid===myPid);
  });
  // quaffle
  if(FR.q){
    const x=FR.q[0],y=FR.q[1], qr=pitchMini?22:14;
    if(pitchMini){ const h=cx.createRadialGradient(x,y,2,x,y,64);
      h.addColorStop(0,'rgba(255,150,90,.55)'); h.addColorStop(1,'rgba(255,150,90,0)');
      cx.fillStyle=h; cx.beginPath(); cx.arc(x,y,64,0,7); cx.fill(); }
    const g=cx.createRadialGradient(x-4,y-4,1,x,y,qr+2);
    g.addColorStop(0,'#ffb28a'); g.addColorStop(1,'#a83b16');
    cx.fillStyle=g; cx.beginPath(); cx.arc(x,y,qr,0,7); cx.fill();
    cx.strokeStyle='rgba(0,0,0,.45)'; cx.lineWidth=2; cx.beginPath(); cx.arc(x,y,qr,0,7); cx.stroke();
    if(carrier==null){ cx.strokeStyle='rgba(255,255,255,.5)'; cx.lineWidth=pitchMini?4:2;
      cx.beginPath(); cx.arc(x,y,qr+10+Math.sin(t*6)*5,0,7); cx.stroke(); }
  }
  // snitch
  if(FR.s){
    const [x,y]=FR.s, k=pitchMini?1.7:1;
    const g=cx.createRadialGradient(x,y,1,x,y,40*k);
    g.addColorStop(0,'rgba(255,246,200,.95)'); g.addColorStop(1,'rgba(245,221,138,0)');
    cx.fillStyle=g; cx.beginPath(); cx.arc(x,y,40*k,0,7); cx.fill();
    cx.fillStyle='#ffe98a'; cx.beginPath(); cx.arc(x,y,9*k,0,7); cx.fill();
    const fl=Math.sin(t*26)*1.1;
    cx.strokeStyle='rgba(255,255,255,.85)'; cx.lineWidth=2.5*k;
    [-1,1].forEach(s=>{ cx.beginPath(); cx.ellipse(x+s*15*k,y-4*k,14*k,(5+fl*4)*k,s*.5,0,7); cx.stroke(); });
  }
  // goal fx
  fxq=fxq.filter(e=>performance.now()-e.t<1300);
  fxq.forEach(e=>{
    const age=(performance.now()-e.t)/1300;
    cx.save(); cx.globalAlpha=Math.max(0,1-age*1.3);
    cx.font='900 96px Cinzel Decorative, serif'; cx.textAlign='center';
    cx.fillStyle=GLOW[e.h]||'#fff'; cx.shadowColor=GLOW[e.h]||'#fff'; cx.shadowBlur=50;
    cx.fillText('GOAL!',FW/2,250-age*40); cx.restore();
  });
  if(FR.st==='countdown'){
    const n=Math.max(1,Math.ceil((q.startsAt||0)-Date.now()/1000));
    cx.font='900 200px Cinzel Decorative, serif'; cx.textAlign='center';
    cx.fillStyle='rgba(245,221,138,.9)'; cx.shadowColor='#f5dd8a'; cx.shadowBlur=60;
    cx.fillText(n,FW/2,FH/2+60); cx.shadowBlur=0;
  }
  cx.restore();
}
function drawSky(cx,t){
  const g=cx.createLinearGradient(0,0,0,FH);
  g.addColorStop(0,'#0a0a1c'); g.addColorStop(.45,'#1a1338'); g.addColorStop(.75,'#3a1f3f'); g.addColorStop(1,'#120c18');
  cx.fillStyle=g; cx.fillRect(0,0,FW,FH);
  cx.save(); const mg=cx.createRadialGradient(1380,120,5,1380,120,90);
  mg.addColorStop(0,'rgba(255,250,230,1)'); mg.addColorStop(1,'rgba(255,250,230,0)');
  cx.fillStyle=mg; cx.beginPath(); cx.arc(1380,120,90,0,7); cx.fill();
  cx.fillStyle='#fdf6dd'; cx.beginPath(); cx.arc(1380,120,36,0,7); cx.fill(); cx.restore();
  cx.fillStyle='#fff';
  for(let i=0;i<90;i++){ const x=(i*9173)%FW, y=(i*4177)%420;
    cx.globalAlpha=(.3+.7*Math.abs(Math.sin(t*1.1+i)))*.45; cx.fillRect(x,y,2,2); }
  cx.globalAlpha=1;
}
function tower(cx,x,base,w,h,t,seed){
  cx.fillStyle='#0b0916'; cx.fillRect(x-w/2,base-h,w,h);
  cx.beginPath(); cx.moveTo(x-w/2-6,base-h); cx.lineTo(x,base-h-w); cx.lineTo(x+w/2+6,base-h); cx.closePath(); cx.fill();
  for(let i=0;i<3;i++) for(let j=0;j<2;j++){
    const fy=base-h+18+i*26; if(fy>base-10) continue;
    const lit=.35+.65*Math.abs(Math.sin(t*.8+seed+i*2+j*3));
    cx.fillStyle=`rgba(255,190,90,${.2+lit*.6})`; cx.fillRect(x-w/4+j*(w/2)-3,fy,5,8);
  }
}
function drawGround(cx,t,a,b){
  tower(cx,80,720,52,190,t,1); tower(cx,150,720,34,132,t,2); tower(cx,214,720,42,162,t,3);
  cx.fillStyle='#0b0916'; cx.fillRect(50,690,190,40);
  tower(cx,FW-84,720,56,206,t,5); tower(cx,FW-158,720,36,146,t,6); tower(cx,FW-220,720,28,112,t,7);
  cx.fillStyle='#0b0916'; cx.fillRect(FW-260,694,200,36);
  cx.fillStyle='#0f2a1a'; cx.beginPath(); cx.ellipse(FW/2,900,FW*.66,190,0,0,7); cx.fill();
  for(let i=0;i<16;i++){ cx.fillStyle=i%2?'rgba(120,220,150,.04)':'rgba(0,0,0,.06)';
    cx.beginPath(); cx.moveTo(i*(FW/16),FH); cx.lineTo((i+1)*(FW/16),FH);
    cx.lineTo(FW/2+((i+1)/16-.5)*300,730); cx.lineTo(FW/2+(i/16-.5)*300,730); cx.closePath(); cx.fill(); }
  cx.fillStyle='#100c1c'; cx.beginPath(); cx.moveTo(0,748);
  for(let x=0;x<=FW;x+=90) cx.lineTo(x,738+((x/90)%2?20:0));
  cx.lineTo(FW,830); cx.lineTo(0,830); cx.closePath(); cx.fill();
  for(let i=0;i<14;i++){ const h=(i%2===0)?a:b; if(!h) break;
    const x=50+i*112, sw=Math.sin(t*1.5+i)*3;
    cx.fillStyle=BAR[h]; cx.globalAlpha=.85; cx.beginPath();
    cx.moveTo(x,742); cx.lineTo(x+30,742); cx.lineTo(x+30+sw,796); cx.lineTo(x+15+sw,786); cx.lineTo(x+sw,796);
    cx.closePath(); cx.fill(); cx.globalAlpha=1; }
  for(let i=0;i<380;i++){ const x=(i*97)%FW, row=i%4, y=754+row*17+Math.sin(t*3.4+i)*2.6;
    cx.fillStyle=['#5b4a68','#6d5a7c','#41364f','#7a6588'][i%4]; cx.globalAlpha=.75; cx.fillRect(x,y,5,7);
    if(i%9===0){ cx.fillStyle='rgba(255,220,150,.5)'; cx.fillRect(x,y-5,4,4); } cx.globalAlpha=1; }
  const fg=cx.createLinearGradient(0,690,0,780);
  fg.addColorStop(0,'rgba(120,110,160,0)'); fg.addColorStop(.6,'rgba(130,120,170,.14)'); fg.addColorStop(1,'rgba(120,110,160,0)');
  cx.fillStyle=fg; cx.fillRect(0,690,FW,90);
  [80,FW-80,FW/2,380,FW-380].forEach((x,i)=>{ const fl=1+Math.sin(t*7+i)*.22;
    const g=cx.createRadialGradient(x,726,3,x,726,60*fl);
    g.addColorStop(0,'rgba(255,175,75,.75)'); g.addColorStop(1,'rgba(255,120,40,0)');
    cx.fillStyle=g; cx.beginPath(); cx.arc(x,726,60*fl,0,7); cx.fill(); });
}
function drawFlyer(cx,x,y,house,name,t,stunned,carrier,isMe){
  const glow=GLOW[house]||'#fff', bar=BAR[house]||'#888', dark=DARK[house]||'#222';
  cx.save(); cx.translate(x,y);
  if(isMe){
    // On a phone the whole 1600px pitch is squeezed into a few hundred, so the
    // "that one is me" marker has to be loud: two gold rings and a chevron.
    cx.strokeStyle='#ffe98a'; cx.lineWidth=pitchMini?7:4;
    cx.globalAlpha=.75+Math.sin(t*4)*.25;
    cx.beginPath(); cx.arc(0,0,pitchMini?56:44,0,7); cx.stroke();
    cx.globalAlpha=.35; cx.lineWidth=pitchMini?3:2;
    cx.beginPath(); cx.arc(0,0,pitchMini?72:56,0,7); cx.stroke();
    cx.globalAlpha=1;
    const bob=Math.sin(t*5)*5, cs=pitchMini?26:16;
    cx.fillStyle='#ffe98a'; cx.beginPath();
    cx.moveTo(-cs,-(pitchMini?96:70)+bob); cx.lineTo(cs,-(pitchMini?96:70)+bob);
    cx.lineTo(0,-(pitchMini?66:50)+bob); cx.closePath(); cx.fill();
  }
  if(carrier){ const g=cx.createRadialGradient(0,0,3,0,0,60);
    g.addColorStop(0,'rgba(255,140,90,.5)'); g.addColorStop(1,'rgba(255,140,90,0)');
    cx.fillStyle=g; cx.beginPath(); cx.arc(0,0,60,0,7); cx.fill(); }
  cx.save();
  if(stunned) cx.rotate(Math.sin(performance.now()/50)*0.9);
  cx.scale(1.5,1.5);
  cx.strokeStyle='#7a5427'; cx.lineWidth=3.6; cx.lineCap='round';
  cx.beginPath(); cx.moveTo(-19,9); cx.lineTo(21,3); cx.stroke();
  cx.fillStyle='#a07c3c'; cx.beginPath(); cx.moveTo(-19,9); cx.lineTo(-33,2); cx.lineTo(-36,9); cx.lineTo(-32,16); cx.closePath(); cx.fill();
  cx.fillStyle=bar; cx.beginPath(); cx.moveTo(2,-13);
  cx.quadraticCurveTo(-16,-12+Math.sin(t*6)*3,-30,-2); cx.quadraticCurveTo(-16,2,-2,6); cx.closePath(); cx.fill();
  cx.fillStyle=dark; cx.beginPath(); cx.ellipse(3,-4,9,9.5,-.25,0,7); cx.fill();
  cx.strokeStyle=glow; cx.lineWidth=2.4; cx.beginPath(); cx.moveTo(-2,-9); cx.lineTo(7,1); cx.stroke();
  cx.fillStyle='#e8c9a0'; cx.beginPath(); cx.arc(9,-15,5.6,0,7); cx.fill();
  cx.fillStyle=dark; cx.beginPath(); cx.arc(7,-17,6.2,Math.PI*.85,Math.PI*2.05); cx.fill();
  cx.restore();
  if(stunned){ cx.fillStyle='#fff'; cx.font=(pitchMini?'700 30px':'700 18px')+' serif'; cx.textAlign='center'; cx.fillText('💫',0,pitchMini?-58:-42); }
  // On the mirrored pitch fourteen name plates at readable size would be a
  // wall of text, so only the two that matter are labelled: you, and whoever
  // has the Quaffle.
  if(pitchMini&&!isMe&&!carrier){ cx.restore(); return; }
  const fs=pitchMini?25:15, lbl=isMe?'YOU · '+name:name, ph=pitchMini?32:20, py=pitchMini?-56:-40;
  cx.font=`${isMe?900:700} ${fs}px Cinzel, serif`; cx.textAlign='center';
  cx.fillStyle=isMe?'rgba(60,42,0,.82)':'rgba(6,6,12,.62)';
  const w=cx.measureText(lbl).width+(pitchMini?18:12);
  cx.fillRect(-w/2,py,w,ph);
  cx.fillStyle=isMe?'#ffe98a':glow; cx.fillText(lbl,0,py+ph-(pitchMini?10:5));
  cx.restore();
}

/* ========================= GREAT HALL + CERTIFICATE ========================= */
let hallCv=null,hallCx=null,hallRaf=null;
function viewHouseCup(){
  scaffold=scaffoldKey(); stopLoops();
  const pts=S.points, order=Object.entries(pts).sort((x,y)=>y[1]-x[1]);
  const champ=order[0][0], revealed=S.housecup.revealed;
  app.innerHTML=`<div class="screen wide">
    <div class="eyebrow">Finale</div><h1 class="title-xl deco">The Great Hall</h1>
    <div class="stage-wrap"><canvas id="hallCv" class="stage" width="1200" height="600"></canvas>
      <div class="hud"><div class="hud-bot center">
        ${revealed?`<div class="panel" style="display:inline-block"><span class="deco" style="font-size:1.5rem;color:${GLOW[champ]}">${champ} win the House Cup</span></div>`
          :`<div class="panel" style="display:inline-block"><span class="small">the ceiling waits…</span></div>`}
      </div></div></div>
    ${revealed?`<div class="card wide" style="margin-top:16px">
      ${order.map(([h,p])=>`<div class="row" style="justify-content:space-between;margin-bottom:6px">
        <span class="house-pill house-${h}" style="min-width:120px;justify-content:center">${h}</span>
        <div style="flex:1;height:22px;border-radius:7px;background:rgba(255,255,255,.07);overflow:hidden;margin:0 10px">
          <div style="height:100%;width:${Math.max(4,(p/(order[0][1]||1))*100)}%;background:linear-gradient(90deg,${DARK[h]},${GLOW[h]});transition:width 1.4s cubic-bezier(.2,.9,.2,1)"></div></div>
        <b class="mono" style="min-width:64px;text-align:right;color:${GLOW[h]}">${p}</b></div>`).join('')}
    </div>`:''}
    <div class="row" style="margin-top:16px">
      ${myRole==='host'&&!revealed?`<button class="btn big" onclick="revealCup()">Light the Hall 🕯️</button>`:''}
      ${revealed?`<button class="btn" onclick="showCert()">Open the Commemorative Scroll</button>`:''}
    </div>
    ${phaseJumper()}</div>`;
  startHall();
  if(revealed&&!window.__cupCelebrated){ window.__cupCelebrated=1; sfx.fanfare();
    setTimeout(()=>burst([GLOW[champ],BAR[champ],'#fff'],260),200); setTimeout(()=>burst([GLOW[champ],'#fff'],200),900); }
}
function revealCup(){ sfx.fanfare(); send('host_action','housecup_reveal'); }
function startHall(){ hallCv=document.getElementById('hallCv'); if(!hallCv) return; hallCx=hallCv.getContext('2d'); stopHall(); loopHall(); }
function stopHall(){ if(hallRaf){ cancelAnimationFrame(hallRaf); hallRaf=null; } }
function loopHall(){
  hallRaf=requestAnimationFrame(loopHall);
  const cx=hallCx; if(!cx||!S) return;
  const W=1200,H=600,t=performance.now()/1000;
  const order=Object.entries(S.points).sort((a,b)=>b[1]-a[1]);
  const champ=order[0][0], revealed=S.housecup.revealed;
  const g=cx.createLinearGradient(0,0,0,H);
  g.addColorStop(0,'#0b0a14'); g.addColorStop(.55,'#171326'); g.addColorStop(1,'#0a0810');
  cx.fillStyle=g; cx.fillRect(0,0,W,H);
  // arched windows
  for(let i=0;i<4;i++){
    const x=110+i*330;
    cx.fillStyle='rgba(80,90,160,.10)';
    cx.beginPath(); cx.moveTo(x-52,330); cx.lineTo(x-52,150); cx.quadraticCurveTo(x,60,x+52,150); cx.lineTo(x+52,330); cx.closePath(); cx.fill();
    cx.strokeStyle='rgba(255,255,255,.06)'; cx.lineWidth=4; cx.stroke();
  }
  // banners: all four dim, or four of the champion's when lit
  for(let i=0;i<4;i++){
    const x=170+i*300, h=revealed?250:200, sw=Math.sin(t*1.2+i)*5;
    const house=revealed?champ:HOUSES[i];
    cx.fillStyle=BAR[house]; cx.globalAlpha=revealed?.95:.45;
    cx.beginPath(); cx.moveTo(x-46,70); cx.lineTo(x+46,70);
    cx.lineTo(x+46+sw,70+h); cx.lineTo(x+sw,70+h-26); cx.lineTo(x-46+sw,70+h); cx.closePath(); cx.fill();
    cx.globalAlpha=revealed?.9:.5; cx.strokeStyle=GLOW[house]; cx.lineWidth=3; cx.stroke();
    cx.globalAlpha=1;
    // house name down the banner, plus a simple heraldic chevron
    cx.save();
    cx.translate(x+sw*.4,96); cx.textAlign='center';
    cx.fillStyle=GLOW[house]; cx.globalAlpha=revealed?1:.55;
    cx.font='700 15px Cinzel, serif';
    const label=house.toUpperCase();
    for(let ci=0;ci<label.length;ci++) cx.fillText(label[ci],0,26+ci*19);
    cx.globalAlpha=revealed?.9:.4; cx.strokeStyle=GLOW[house]; cx.lineWidth=3;
    cx.beginPath(); cx.moveTo(-16,label.length*19+48); cx.lineTo(0,label.length*19+34); cx.lineTo(16,label.length*19+48); cx.stroke();
    cx.restore(); cx.globalAlpha=1;
  }
  // long tables
  [[70,468,1060,22],[140,528,920,20]].forEach(([tx,ty,tw,th],ti)=>{
    cx.fillStyle='#171122'; cx.fillRect(tx,ty,tw,th);
    cx.fillStyle='#241c31'; cx.fillRect(tx,ty-6,tw,7);
    cx.fillStyle='#120d1c';
    for(let lx=tx+30;lx<tx+tw;lx+=180) cx.fillRect(lx,ty+th,14,H-(ty+th));
    for(let gi=0;gi<12;gi++){                      // goblets catching the light
      const gx=tx+40+gi*(tw/12);
      cx.fillStyle='rgba(228,196,110,.55)'; cx.fillRect(gx,ty-16,5,10);
      cx.beginPath(); cx.ellipse(gx+2.5,ty-6,5,2.4,0,0,7); cx.fill();
    }
  });
  // floating candles
  const n=revealed?54:30;
  for(let i=0;i<n;i++){
    const x=54+((i*181)%(W-108)), y=64+((i*89)%150)+Math.sin(t*1.1+i*0.7)*6;
    cx.fillStyle='#efe0b4'; cx.fillRect(x-1.5,y,3,11);      // candle
    const fl=1+Math.sin(t*9+i)*.35;
    cx.fillStyle='rgba(255,226,150,.95)';
    cx.beginPath(); cx.ellipse(x,y-5,2.2,4.6*fl,0,0,7); cx.fill();
    const rg=cx.createRadialGradient(x,y-5,1,x,y-5,13*fl);
    rg.addColorStop(0,'rgba(255,215,130,.55)'); rg.addColorStop(1,'rgba(255,180,80,0)');
    cx.fillStyle=rg; cx.beginPath(); cx.arc(x,y-5,13*fl,0,7); cx.fill();
  }
  // the cup itself
  const cxp=W/2, cy=430;
  const glow=revealed?GLOW[champ]:'#6b6478';
  cx.save();
  if(revealed){ const rg=cx.createRadialGradient(cxp,cy-30,4,cxp,cy-30,150);
    rg.addColorStop(0,glow+'88'); rg.addColorStop(1,'rgba(0,0,0,0)');
    cx.fillStyle=rg; cx.beginPath(); cx.arc(cxp,cy-30,150,0,7); cx.fill(); }
  cx.fillStyle=revealed?'#e8c33a':'#4a4458';
  cx.beginPath(); cx.moveTo(cxp-42,cy-70); cx.quadraticCurveTo(cxp,cy+10,cxp+42,cy-70); cx.closePath(); cx.fill();
  cx.fillRect(cxp-7,cy-6,14,34); cx.fillRect(cxp-30,cy+28,60,10);
  cx.strokeStyle=revealed?'#fff3c4':'#5c5470'; cx.lineWidth=3;
  cx.beginPath(); cx.arc(cxp-46,cy-52,16,Math.PI*.4,Math.PI*1.6); cx.stroke();
  cx.beginPath(); cx.arc(cxp+46,cy-52,16,Math.PI*1.4,Math.PI*.6); cx.stroke();
  cx.restore();
}

function showCert(){
  const c=S.certificate; if(!c) return;
  stopLoops();
  scoreboardEl.style.display='none';   // it was covering the signature line
  const hon=c.honours||[];
  app.innerHTML=`<div class="screen">
    <div class="cert" id="certEl">
      <div class="center" style="font-family:'Cinzel',serif;letter-spacing:.24em;font-size:.72rem;color:#6b4f1c">UPTIME CREW · HOGWARTS SCHOOL OF SHIPPING &amp; SORCERY</div>
      <h1>The House Cup</h1>
      <p class="center" style="margin:0;color:#5b431a">${c.date}</p>
      <div class="champ" style="color:#7a5a10">${c.champion||'—'}</div>
      <p class="center" style="margin-top:-6px;font-family:'Cinzel',serif;letter-spacing:.1em;font-size:.8rem;color:#6b4f1c">CHAMPIONS OF THE NIGHT</p>
      <h2>Final Standings</h2>
      <table>${(c.standings||[]).map((s,i)=>`<tr><td>${i+1}. ${s.house}</td><td class="n">${s.points}</td></tr>`).join('')}</table>
      <h2>Honours</h2>
      ${hon.length?hon.map(h=>`<div class="hon"><span><b>${h.label}</b> — ${h.name}${h.house?` <i>(${h.house})</i>`:''}</span><span class="mono">${h.value} ${h.unit}</span></div>`).join('')
        :'<p class="small" style="color:#6b4f1c">No honours recorded.</p>'}
      <h2>The Quidditch Cup</h2>
      <p style="margin:4px 0">${(c.quidditch&&c.quidditch.winners&&c.quidditch.winners.final)?`<b>${c.quidditch.winners.final}</b> took the Final${(function(){const f=c.quidditch.finalScore&&c.quidditch.finalScore.final; if(!f) return '';const k=Object.keys(f); return k.length===2?` (${k[0]} ${f[k[0]]} – ${f[k[1]]} ${k[1]})`:'';})()}.`:'The Final went unplayed.'}</p>
      <h2>The Gringotts Heist</h2>
      <p style="margin:4px 0">${(c.gringotts.freed||[]).length===3?'Harry, Ron and Hermione were all brought out of the vault':((c.gringotts.freed||[]).length?'Freed from the vault: '+c.gringotts.freed.join(', '):'The vault kept its prisoners')}${c.gringotts.clanks?`, with ${c.gringotts.clanks} clanks raised against the dragon`:''}.</p>
      <h2>The Houses</h2>
      <div class="rosters">${HOUSES.map(h=>{
        const fixed=(c.houseMembers[h]||[]), guests=((S.guestMembers&&S.guestMembers[h])||[]);
        return `<div><b>${h}</b><br>${fixed.concat(guests).join(', ')||'—'}</div>`;}).join('')}</div>
      <div class="sig">“Mischief managed.”<br>— Sayeeda, Head of House</div>
    </div>
    <div class="row no-print" style="margin-top:18px">
      <button class="btn" onclick="window.print()">Save as PDF</button>
      <button class="btn ghost" onclick="render()">Back to the Hall</button>
    </div></div>`;
}

/* ========================= loop control ========================= */
function stopLoops(){ stopPitch(); stopVault(); stopPot(); stopHall(); stopOwl(); }
boot();
</script>
</body>
</html>
"""

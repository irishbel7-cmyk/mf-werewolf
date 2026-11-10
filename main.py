#!/usr/bin/env python3
"""Werewolf agent — deterministic heuristics, no LLM calls.
Protocol: POST {MAKEFUN_ENV_URL}/observe {} -> {done, pending:[{game, decision, options, max_chars, view}]}
For each pending: compute answer -> POST /act {game, decision, say/text or target}.
View: {day, phase, you:{seat,name,role,alive,partner}, log:[{seq,day,kind,text,...}]}
Decisions: speak {game,decision,text} | vote/kill/protect/inspect {game,decision,target}.
"""
import json, os, re, sys, time
from urllib import request as ureq

ENV = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
PROMPT_FILE = os.environ.get("MAKEFUN_PROMPT_FILE", "")

def post(path, body):
    req = ureq.Request(ENV + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with ureq.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())

# ---------------- per-game state ----------------
class Game:
    def __init__(self):
        self.claims = {}      # seat -> role claimed in speak
        self.seer_results = {}  # seat(inspector) -> {target: is_wolf} (from our own inspect)
        self.votes = {}       # day -> {voter: target}
        self.deaths = []      # revealed dead seats
        self.sus = {}         # seat -> suspicion score
        self.checked_wolves = set()   # seats our seer found to be wolves
        self.checked_clean = set()
        self.my_role = None
        self.my_seat = None
        self.partner = None
        self.last_protect = None
        self.claimed_seer = set()  # seats who claimed seer
        self.claimed_wolf = set()  # seats publicly revealed wolf role (dead) 

    def update_from_log(self, view):
        self.my_seat = view["you"]["seat"]
        self.my_role = view["you"]["role"]
        p = view["you"].get("partner")
        self.partner = p["seat"] if p else None
        for e in view.get("log", []):
            kind = e.get("kind", "")
            text = (e.get("text") or "").lower()
            # deaths: exile/kill reveals
            if kind in ("exile", "kill", "death") or ("exiled" in text or "killed" in text or "was a werewolf" in text):
                for w in self._seats_in(text):
                    if w not in self.deaths:
                        self.deaths.append(w)
                if "werewolf" in text and "was" in text:
                    for w in self._seats_in(text):
                        self.claimed_wolf.add(w)
            if kind == "vote" or "vote" in kind:
                pass  # votes are public as cast; the log entries carry text like "seat 3 voted seat 5"
                for m in _pairs(text):
                    voter, tgt = m
                    self.votes.setdefault(e.get("day", view.get("day", 0)), {})[voter] = tgt
            if kind == "speak" or kind == "statement":
                if "seer" in text:
                    s0 = self._speaker_hint(text)
                    if s0: self.claimed_seer.add(s0)
                if "checked seat" in text or "inspected seat" in text:
                    tgt, res = _check_claim(text)
                    if tgt:
                        self.claims[tgt] = res
            if "is a werewolf" in text or "is clean" in text or "not a werewolf" in text:
                m = re.search(r"seat (\d+) is (?:a )?(werewolf|villager|clean|not a werewolf)", text)
                if m:
                    tgt = int(m.group(1)); res = m.group(2)
                    if "werewolf" in res and "not" not in res:
                        self.checked_wolves.add(tgt)
                    else:
                        self.checked_clean.add(tgt)

    def _speaker_hint(self, text):
        # "seat N" nearest to start of statement
        import re
        m = re.search(r"seat (\d+)", text)
        return int(m.group(1)) if m else None

    def _seats_in(self, text):
        import re
        return [int(x) for x in re.findall(r"seat (\d+)", text)]

def _pairs(text):
    import re
    # "seat 3 voted seat 5"
    out = []
    for m in re.finditer(r"seat (\d+)\s+(?:voted|votes)\s+seat (\d+)", text):
        out.append((int(m.group(1)), int(m.group(2))))
    return out

def _check_claim(text):
    import re
    m = re.search(r"seat (\d+).*?(werewolf|villager|clean|wolf)", text)
    if m:
        return int(m.group(1)), m.group(2)
    return None, None

GAMES = {}  # game id -> Game

def gstate(gid, view):
    g = GAMES.setdefault(gid, Game())
    g.update_from_log(view)
    return g

def alive_seats(g, view, options):
    return options

def decide_speak(g, view, max_chars):
    seat, role = g.my_seat, g.my_role
    if role == "werewolf":
        # look innocent; misdirect to a suspicious villager
        targets = [s for s in g.sus if s != seat and s != g.partner]
        if g.checked_clean:
            t = sorted(g.checked_clean)[-1] if isinstance(g.checked_clean, set) else None
        tgt = max(g.sus, key=lambda s: g.sus[s]) if g.sus else None
        if tgt and tgt != seat and tgt != g.partner:
            return f"I think seat {tgt} is acting strange, their votes make no sense."
        return "No strong reads yet, let's hear more arguments."
    if role == "seer":
        if g.checked_wolves:
            w = sorted(g.checked_wolves)[0]
            return f"I am the seer. I checked seat {w} and they are a WEREWOLF. Vote them out."
        if g.checked_clean:
            c = sorted(g.checked_clean)[0]
            return f"I am the seer. Seat {c} is confirmed clean, protect them."
        return "I have information I will share when it matters most."
    if role == "doctor":
        return "I am a plain villager, but seat votes look suspicious today."
    # villager
    if g.claimed_seer:
        # counter-claim analysis: trust the one whose checks match outcomes
        return "One of the seer claims is a wolf lying. Watch who the votes defend."
    tgt = max(g.sus, key=lambda s: g.sus[s]) if g.sus else None
    if tgt and tgt != seat:
        return f"Seat {tgt} has been quiet and their votes look coordinated. I am watching them."
    return "No solid read yet; I am tracking every vote."

def decide_vote(g, view, options):
    seat, role = g.my_seat, g.my_role
    if role == "werewolf":
        # vote to exile a villager, prefer one the village already suspects; never partner
        cands = [s for s in options if s != seat and s != g.partner]
        if not cands: return None
        if g.sus:
            return max(cands, key=lambda s: g.sus.get(s, 0))
        return cands[0]
    if role == "seer" and g.checked_wolves:
        w = [s for s in g.checked_wolves if s in options]
        if w: return w[0]
    # village side: vote the most suspicious among options
    cands = [s for s in options if s != seat]
    if not cands: return None
    def score(s):
        sc = g.sus.get(s, 0)
        if s in g.checked_wolves: sc += 100
        if s in g.claimed_wolf: sc -= 1000
        # voted-against-seer-claims bonus
        return sc
    return max(cands, key=score)

def decide_kill(g, view, options):
    # we are werewolf: pick a villager, avoid doctor-likely; coordinate: pick same as partner would see
    cands = [s for s in options]
    # prefer seer claimants (real threat), then villagers
    for s in g.claimed_seer:
        if s in cands: return s
    # avoid voting targets we defended? simple: pick random-ish deterministic middle seat
    if not cands: return None
    cands.sort()
    return cands[len(cands)//2]

def decide_protect(g, view, options):
    # protect claimed seer if alive; else self? can't self. Never same as last night.
    cands = [s for s in options if s != g.my_seat and s != g.last_protect]
    if not cands: return None
    for s in g.claimed_seer:
        if s in cands: 
            g.last_protect = s
            return s
    pick = cands[0]
    g.last_protect = pick
    return pick

def decide_inspect(g, view, options):
    # inspect unchecked, most-suspicious first
    cands = [s for s in options if s not in g.checked_wolves and s not in g.checked_clean and s != g.my_seat]
    if not cands:
        cands = [s for s in options if s != g.my_seat]
    if not cands: return None
    if g.sus:
        cands.sort(key=lambda s: -g.sus.get(s, 0))
        pick = cands[0]
    else:
        pick = cands[0]
    # result will come through log next observe; optimistically mark clean for now is wrong, skip
    return pick

def handle(pending):
    gid = pending["game"]
    view = pending["view"]
    g = gstate(gid, view)
    d = pending["decision"]
    opts = pending.get("options") or []
    if d == "speak":
        text = decide_speak(g, view, pending.get("max_chars", 500))
        return {"game": gid, "decision": "speak", "text": text[:pending.get("max_chars", 500)]}
    if d == "vote":
        t = decide_vote(g, view, opts)
        if t in g.checked_wolves: pass
        return {"game": gid, "decision": "vote", "target": t}
    if d == "kill":
        return {"game": gid, "decision": "kill", "target": decide_kill(g, view, opts)}
    if d == "protect":
        return {"game": gid, "decision": "protect", "target": decide_protect(g, view, opts)}
    if d == "inspect":
        t = decide_inspect(g, view, opts)
        if t is not None:
            # remember we inspected; result arrives in log
            pass
        return {"game": gid, "decision": "inspect", "target": t}
    return {"game": gid, "decision": d, "target": None}

def main():
    if not ENV:
        print("MAKEFUN_ENV_URL missing", flush=True)
        sys.exit(1)
    print("werewolf heuristic agent starting", flush=True)
    while True:
        try:
            obs = post("/observe", {})
        except Exception as e:
            print("observe error:", e, flush=True)
            time.sleep(2)
            continue
        if obs.get("done"):
            print("done", flush=True)
            break
        for p in obs.get("pending", []):
            try:
                act = handle(p)
                r = post("/act", act)
                print(f"acted g{p['game']} {p['decision']} -> {json.dumps(act)[:120]}", flush=True)
            except Exception as e:
                print("act error:", e, flush=True)
        if not obs.get("pending"):
            time.sleep(0.5)

if __name__ == "__main__":
    main()

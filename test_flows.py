"""Offline regression test of the bot state machine — sends are mocked.
Covers the V5.6 combined opening card (fictional+ground+drop) + card-wired flow."""
import bot

SENT = []
bot.send_text = lambda u, t: SENT.append(("text", t[:70]))
bot.send_media = lambda u, p, prefer="image": SENT.append(("media", p.split("/")[-1][:40]))
bot.send_email = lambda s, b, f: SENT.append(("email", s[:50]))
bot.send_card = lambda u, title, desc, buttons, task_id=None: SENT.append(("card", title[:40]))
bot.send_multi_card = lambda u, title, desc, selects, submit_key, submit_text="": (
    SENT.append(("multicard", title[:40])) or True)
bot.bg = lambda fn, *a: fn(*a)  # run "async" legs inline so we can assert
bot.AF_AUTOFETCH = False        # manual-paste path (no xvfb subprocess in tests)
# hermetic: no DB reads or writes
bot.next_queued_entry = lambda: {"id": 999, "airline_name": "Test Airways", "registration": "T-999",
    "source_credit": "pom", "license": "CC-BY-NC-SA 4.0", "article_title": "t", "article_path": None, "image_path": None}
bot.next_fictional_entry = lambda: {"id": 998, "airline_name": "", "registration": "",
    "source_credit": None, "license": "CC-BY-NC-ND 4.0", "image_path": None}
bot.snapshot_entry = lambda rid: {"is_fictional": 0, "published_status": None}
bot.restore_entry = lambda rid, snap: SENT.append(("restore", f"{rid}"))
bot.mark_entry = lambda rid, st: SENT.append(("mark", f"{rid}->{st}"))
bot.mark_fictional = lambda rid: SENT.append(("mark", f"{rid}->fictional"))
def _leg(u, d, t):
    SENT.append(("PIPELINE", "deepseek+email+brew+article"))
    bot.SESS[u]["state"] = bot.S_PUBLISHED   # mirrors real leg tail
bot.leg_deepseek_and_rest = _leg
def _fleg(u, d):
    SENT.append(("FPIPELINE", "fictional synth+brew"))
    bot.SESS[u]["state"] = bot.S_FDONE       # mirrors real leg tail
bot.leg_fictional_synth = _fleg

U = "ZhangYiYang"


def drive(name, steps):
    """steps: list of ('text', msg) or ('combo', {answers}) or ('fn', callable)."""
    bot.SESS.clear(); SENT.clear()
    print(f"== {name} ==")
    for kind, val in steps:
        if kind == "text":
            r = bot.handle(U, val)
            print(f"  > {val!r:24s} -> {(r or chr(8709))[:60]}")
        elif kind == "combo":
            bot.apply_combo(U, val)
            print(f"  [combo submit] {val} -> state={bot.SESS[U]['state']}")
    for s in SENT:
        print(f"  [sent] {s}")


# the delayed-publish change: leg tail no longer sends the publish card; it sets
# S_PUBLISHED + await_brew, and brew_done fires the card. Mock the leg to mirror that.
def _leg2(u, d, t):
    SENT.append(("PIPELINE", "deepseek+refs+brew+article"))
    bot.SESS[u]["state"] = bot.S_PUBLISHED
    bot.SESS[u]["data"]["await_brew"] = (d.get("entry") or {}).get("id")
bot.leg_deepseek_and_rest = _leg2

# 1. REAL FLOW: article -> combo(fictional=no) -> paste -> (brew done) -> published=yes
drive("REAL FLOW", [("text", "article"),
                    ("combo", {"fictional": "no", "ground": "no", "drop": "yes"}),
                    ("text", "x" * 60)])
assert bot.SESS[U]["state"] == bot.S_PUBLISHED and bot.SESS[U]["data"].get("await_brew") == 999
bot.maybe_send_finish_card(U, 999)          # brew_done releases the held card
assert "await_brew" not in bot.SESS[U]["data"]
bot.handle(U, "yes")
assert bot.SESS[U]["state"] == bot.S_IDLE, bot.SESS[U]["state"]

# 2. COMBO fictional=yes -> tagged fictional, back to idle
drive("COMBO FICTIONAL", [("text", "article"),
                          ("combo", {"fictional": "yes", "ground": "no", "drop": "no"})])
assert bot.SESS[U]["state"] == bot.S_IDLE
assert any(s[0] == "mark" and "fictional" in s[1] for s in SENT)

# 3. COMBO reg correction (type a reg while combo card is up)
drive("COMBO REG EDIT", [("text", "article"), ("text", "B-2479")])
assert bot.SESS[U]["data"]["reg"] == "B-2479"
assert bot.SESS[U]["state"] == bot.S_COMBO

# 4. FA FLOW: fa -> fcombo submit(ground/drop) -> airline -> type -> synth
def _fleg2(u, d):
    SENT.append(("FPIPELINE", "fictional synth+brew"))
    bot.SESS[u]["state"] = bot.S_FDONE
    bot.SESS[u]["data"]["await_brew"] = (d.get("entry") or {}).get("id")
bot.leg_fictional_synth = _fleg2
bot.SESS.clear(); SENT.clear()
bot.handle(U, "fa")
assert bot.SESS[U]["state"] == bot.S_FCOMBO, bot.SESS[U]["state"]
bot.apply_fcombo(U, {"ground": "no", "drop": "yes"})   # -> S_FAIRLINE
assert bot.SESS[U]["state"] == bot.S_FAIRLINE
bot.handle(U, "Test Air")                              # set airline -> still S_FAIRLINE (confirm card)
bot.handle(U, "yes")                                   # -> S_FTYPE
assert bot.SESS[U]["state"] == bot.S_FTYPE
bot.handle(U, "Boeing 707-320B")                       # -> synth -> S_FDONE(+await_brew)
assert bot.SESS[U]["state"] == bot.S_FDONE
print("== FA FLOW ==  ok (fcombo -> airline -> type -> synth)")

# 5. KILL rolls back + returns to idle
drive("KILL", [("text", "article"), ("text", "kill")])
assert bot.SESS[U]["state"] == bot.S_IDLE
assert any(s[0] == "restore" for s in SENT)

# 6. SKIP -> discard mid-combo
drive("SKIP/DISCARD", [("text", "article"), ("text", "skip"), ("text", "yes")])
assert bot.SESS[U]["state"] == bot.S_IDLE
assert any(s[0] == "mark" and "discarded" in s[1] for s in SENT)

# 7. STATUS is reachable any time (mock the DB counts)
bot.status_text = lambda: "📊 status ok"
drive("STATUS", [("text", "article"), ("text", "status")])

# 8. numbers with/without brew
bot.LAST_BREW["id"] = None
drive("NUM no brew", [("text", "two")])
bot.LAST_BREW["id"] = 11
bot.leg_send_style = lambda u, b, n: SENT.append(("styles", f"brew {b} nums {n}"))
drive("NUM with brew", [("text", "1 3")])

print("ALL FLOW TESTS COMPLETED")

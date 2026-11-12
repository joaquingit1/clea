"""What the harness must do with what people write in Slack: one scenario per situation, each stating its intent.

A scenario posts messages into the simulated workspace (sim/world.py), runs the real engine over them (sim/harness.py)
and checks what Clea did: which cases exist, what it drafted, what it asked the user, and what left in their name.
The cast: Sam Rivera is the user. Ana and Bo are his teammates. Carla works in support, Dev on another team. Monitor
is a bot. Sam's install reads #team-checkout (team), #checkout-prs (PRs), #checkout-alerts (alerts) and #support-ops
(watched); it does not read #random, where only mentions and keywords reach it.

Scenarios run on the scripted model unless marked live: the script decides like a careless reader would (a question
mark is an ask), which is enough to prove the intake, the routing and the state machine. What Clea actually says is
judged in live mode, on the install's real provider.
"""
from __future__ import annotations

import re

from ap import db
from sim.harness import (ALERTS_CH, ANA, ANA_DM, BO, CARLA, CARLA_DM, DEV, DM, GROUP, ME, MONITOR, OPS_CH, PR_CH, RANDOM_CH,
                         TEAM_CH, Sim)

SCENARIOS: list = []


def scenario(group: str, intent: str, live: bool | str = False, config: dict | None = None):
    def deco(fn):
        SCENARIOS.append({"name": fn.__name__, "group": group, "intent": intent, "fn": fn, "live": live, "config": config})
        return fn
    return deco


class Checks:
    def __init__(self):
        self.failed: list[str] = []
        self.passed = 0

    def __call__(self, ok, what: str, got=None) -> bool:
        if ok:
            self.passed += 1
        else:
            self.failed.append(what + (f" (got: {got!r})" if got is not None else ""))
        return bool(ok)


def replies(s: Sim) -> list[dict]:
    return s.cases("REPLY")


def open_cases(s: Sim, kind: str | None = None) -> list[dict]:
    return [c for c in s.cases(kind) if c["status"] not in db.TERMINAL]


def tag(uid: str) -> str:
    return f"<@{uid}>"


# ---- A. direct messages ------------------------------------------------------------------------------------------------
@scenario("dm", "A question in a DM becomes one case with a draft, asked for approval; approving sends exactly that draft to that DM")
def dm_question(s: Sim, ok: Checks):
    s.world.post(ANA_DM, ANA, "hey Sam, can you check why the checkout total is wrong for coupon orders?")
    s.run()
    cases = replies(s)
    ok(len(cases) == 1 and cases[0]["status"] == "awaiting_approval", "one reply case waiting for approval", [(c["kind"], c["status"]) for c in s.cases()])
    asks = s.asks()
    ok(len(asks) == 1 and asks[0]["kind"] == "slack_send" and asks[0]["payload"]["channel"] == ANA_DM and not asks[0]["payload"].get("thread_ts"),
       "one ask: send in the DM with Ana, not in a thread", [(a["kind"], a["payload"]) for a in asks])
    ok(len(s.model_calls("reply")) == 1, "one drafting call", len(s.model_calls("reply")))
    ok(s.sent_to_others() == [], "nothing sent before the approval", s.sent_to_others())
    if not asks:
        return
    draft = asks[0]["payload"]["text"]
    s.approve(asks[0]["id"])
    out = s.sent_to_others()
    ok(len(out) == 1 and out[0]["channel"] == ANA_DM and out[0]["text"] == draft and not out[0].get("thread_ts"),
       "after approval: exactly the approved text, in that DM", out)
    ok(s.case(cases[0]["id"])["status"] == "done", "the case is done", s.case(cases[0]["id"])["status"])


@scenario("dm", "Several messages from one person in a row are one conversation: one case, one draft")
def dm_burst(s: Sim, ok: Checks):
    for text in ("hey Sam", "quick one", "the coupon total looks off in checkout", "can you take a look today?"):
        s.world.post(ANA_DM, ANA, text)
    s.run()
    ok(len(replies(s)) == 1, "one case for the burst", len(replies(s)))
    ok(len(s.model_calls("reply")) == 1, "one drafting call for the burst", len(s.model_calls("reply")))
    ok(len(s.asks()) == 1, "one ask", len(s.asks()))


@scenario("dm", "An acknowledgement, an emoji or a thank-you in a DM is not an ask: no case, no model call", live=True)
def dm_acknowledgements(s: Sim, ok: Checks):
    for text in ("ok", "thanks!", ":+1:", "👍", "great, thank you", "perfect", "lol", "got it", "gracias!", "dale"):
        s.world.post(ANA_DM, ANA, text)
        s.run()
    ok(len(s.cases()) == 0, "no case", [(c["kind"], c["title"]) for c in s.cases()])
    ok(len(s.model_calls()) == 0, "no model call", [c[0] for c in s.model_calls()])


@scenario("dm", "A bot writing in a DM is never an ask", live=True)
def dm_from_a_bot(s: Sim, ok: Checks):
    s.world.dm("D0SIMBOT001", MONITOR)
    s.world.post("D0SIMBOT001", MONITOR, "Reminder: can you fill in your timesheet?")
    s.run()
    ok(len(s.cases()) == 0 and len(s.model_calls()) == 0, "no case and no model call", [(c["kind"], c["title"]) for c in s.cases()])


@scenario("dm", "When the user answers the DM himself before approving, the case closes and nothing is sent in his name")
def dm_user_answers_first(s: Sim, ok: Checks):
    s.world.post(ANA_DM, ANA, "can you send me the link to the checkout dashboard?")
    s.run()
    ok(len(s.asks()) == 1, "an ask is waiting", len(s.asks()))
    s.world.post(ANA_DM, ME, "here you go: https://dash.example.com/checkout")
    s.wait(700)
    s.poll_approvals()
    s.run()
    ok(len(open_cases(s, "REPLY")) == 0, "no reply case left open", [(c["id"], c["status"]) for c in replies(s)])
    ok(len(s.asks()) == 0, "the ask is retired", s.asks())
    ok(s.sent_to_others() == [], "nothing sent by Clea", s.sent_to_others())
    ok(len(replies(s)) == 1, "still one case", len(replies(s)))


@scenario("dm", "A thank-you after the user answered does not reopen the conversation or cost a drafting call")
def dm_thanks_after_answer(s: Sim, ok: Checks):
    s.world.post(ANA_DM, ANA, "can you send me the link to the checkout dashboard?")
    s.run()
    s.world.post(ANA_DM, ME, "here: https://dash.example.com/checkout")
    s.wait(700)
    s.poll_approvals()
    s.run()
    before = len(s.model_calls("reply"))
    s.world.post(ANA_DM, ANA, "thanks a lot!")
    s.run()
    ok(len(replies(s)) == 1, "one case in total", len(replies(s)))
    ok(len(s.model_calls("reply")) == before, "no new drafting call", len(s.model_calls("reply")) - before)
    ok(len(s.asks()) == 0 and s.sent_to_others() == [], "no ask, nothing sent", (s.asks(), s.sent_to_others()))


@scenario("dm", "A new question in the same DM soon after continues the same case as a new round")
def dm_new_question_same_conversation(s: Sim, ok: Checks):
    s.world.post(ANA_DM, ANA, "can you send me the link to the checkout dashboard?")
    s.run()
    s.world.post(ANA_DM, ME, "here: https://dash.example.com/checkout")
    s.wait(700)
    s.poll_approvals()
    s.run()
    s.world.post(ANA_DM, ANA, "and do you know who owns the refunds view?")
    s.run()
    cases = replies(s)
    ok(len(cases) == 1, "still one case", len(cases))
    ok(cases and cases[0]["data"].get("round") == 2, "it is on its second round", cases and cases[0]["data"].get("round"))
    ok(len(s.asks()) == 1, "one open ask for the new question", len(s.asks()))
    ok(cases and cases[0]["status"] == "awaiting_approval", "waiting for his approval, not stuck", cases and (cases[0]["status"], cases[0].get("last_error")))


@scenario("dm", "Asking again with the same words after the earlier ask was retired still reaches the user")
def same_draft_twice(s: Sim, ok: Checks):
    same = lambda prompt, schema: {**s.model.fake(schema), "needs_reply": True, "why": "asked", "needs_facts": False, "checks": [],
                                   "draft": "Yes, it went out this morning.", "prod_problem": False, "someone_else_owns": False,
                                   "target": "thread", "report": "They ask whether the deploy went out.", "open_problem": "",
                                   "board_line": "Reply to Ana"}
    s.model.on("reply", same)
    s.model.on("report", lambda prompt, schema: {"headline": "Deploy question", "bullets": ["asked twice"], "need": "your approval",
                                                 "analysis": "", "text": "They ask whether the deploy went out.", "board_line": "Reply to Ana",
                                                 "detail": ""})
    s.world.post(ANA_DM, ANA, "did the checkout deploy go out?")
    s.run()
    s.world.post(ANA_DM, ME, "checking")
    s.wait(700)
    s.poll_approvals()
    s.run()
    s.world.post(ANA_DM, ANA, "sorry to insist: did the checkout deploy go out?")
    s.run()
    cases = replies(s)
    ok(len(cases) == 1 and cases[0]["status"] == "awaiting_approval", "one case, waiting for his approval",
       [(c["status"], (c.get("last_error") or "")[:140]) for c in cases])
    ok(len(s.asks()) == 1, "the ask is in front of him", len(s.asks()))


@scenario("dm", "With DM reading turned off, DMs are not read at all", config={"slack": {"scan_dms": False}})
def dm_reading_off(s: Sim, ok: Checks):
    s.world.post(ANA_DM, ANA, "can you check the checkout total?")
    s.run()
    ok(db.q1("SELECT count(*) c FROM events")["c"] == 0 and len(s.cases()) == 0, "no event, no case", db.q1("SELECT count(*) c FROM events")["c"])
    ok(not any(m == "conversations_history" and k.get("channel") == ANA_DM for m, k in s.world.calls), "the DM was never read")


# ---- B. mentions, groups, keywords -------------------------------------------------------------------------------------
@scenario("mention", "A mention in the team channel is answered in that message's thread")
def mention_in_team_channel(s: Sim, ok: Checks):
    ts = s.world.post(TEAM_CH, ANA, f"{tag(ME)} can you review the coupon rounding logic before we ship?")
    s.run()
    asks = s.asks()
    ok(len(replies(s)) == 1 and len(asks) == 1, "one case, one ask", (len(replies(s)), len(asks)))
    ok(asks and asks[0]["payload"]["channel"] == TEAM_CH and asks[0]["payload"].get("thread_ts") == ts, "the draft goes in that thread",
       asks and asks[0]["payload"])


@scenario("mention", "A mention in a channel Clea was not told to read still reaches it (found by search)")
def mention_in_unread_channel(s: Sim, ok: Checks):
    ts = s.world.post(RANDOM_CH, DEV, f"{tag(ME)} do you know if checkout supports gift cards?")
    s.run()
    asks = s.asks()
    ok(len(replies(s)) == 1, "one case", len(replies(s)))
    ok(asks and asks[0]["payload"]["channel"] == RANDOM_CH and asks[0]["payload"].get("thread_ts") == ts, "answered in its thread", asks and asks[0]["payload"])
    ok(not any(m == "conversations_history" and k.get("channel") == RANDOM_CH and "latest" not in k for m, k in s.world.calls),
       "the channel itself was not polled")


@scenario("mention", "A tag of the user's group is for the user")
def group_tag(s: Sim, ok: Checks):
    s.world.post(OPS_CH, CARLA, f"<!subteam^{GROUP}|@checkout-team> who can tell me why this order was charged twice?")
    s.run()
    ok(len(replies(s)) == 1 and len(s.asks()) == 1, "one case with an ask", (len(replies(s)), len(s.asks())))


@scenario("mention", "A message with the area's keyword in an unread channel is triaged; chatter is ignored, a problem becomes work")
def keyword_in_unread_channel(s: Sim, ok: Checks):
    s.world.post(RANDOM_CH, CARLA, "the new checkout page looks really nice")
    s.run()
    ok(len(s.cases()) == 0, "praise with the keyword opens nothing", [(c["kind"], c["title"]) for c in s.cases()])
    s.world.post(RANDOM_CH, CARLA, "checkout is broken for me, I get a 500 when I submit the order")
    s.ingest()
    tasks = s.cases("TASK") + s.cases("INCIDENT")
    ok(len(tasks) == 1, "the problem report opens one work case", [(c["kind"], c["status"]) for c in s.cases()])


@scenario("mention", "A message in an unread channel with no mention and no keyword is not Clea's business")
def unread_channel_plain(s: Sim, ok: Checks):
    s.world.post(RANDOM_CH, DEV, "anyone up for lunch? can someone book a table?")
    s.run()
    ok(db.q1("SELECT count(*) c FROM events")["c"] == 0 and len(s.cases()) == 0, "no event, no case", db.q1("SELECT count(*) c FROM events")["c"])


@scenario("mention", "A mention inside someone else's thread opens the case on that thread, not on the reply")
def mention_in_a_thread_reply(s: Sim, ok: Checks):
    root = s.world.post(OPS_CH, DEV, "customer reports the invoice PDF is blank")
    s.world.post(OPS_CH, CARLA, "same here for two more customers", thread_ts=root)
    s.run()
    reply = s.world.post(OPS_CH, CARLA, f"{tag(ME)} could this be related to the checkout change from yesterday?", thread_ts=root)
    s.run()
    cases = replies(s)
    ok(len(cases) == 1, "one reply case", [(c["kind"], c["thread_key"]) for c in s.cases()])
    ok(cases and cases[0]["thread_key"] == f"slack:{OPS_CH}:{root}", "keyed by the thread's root", cases and cases[0]["thread_key"])
    asks = s.asks()
    ok(asks and asks[0]["payload"].get("thread_ts") == root, "the draft goes in the thread", asks and asks[0]["payload"])
    ok(reply != root, "the mention was a reply in the thread")


@scenario("mention", "A channel the user marked as ignored is never read, mentions included", config={"ignored_channels": [OPS_CH]}, live=True)
def ignored_channel(s: Sim, ok: Checks):
    s.world.post(OPS_CH, CARLA, f"{tag(ME)} can you look at this checkout error?")
    s.run()
    ok(len(s.cases()) == 0 and db.q1("SELECT count(*) c FROM events")["c"] == 0, "no event, no case", [(c["kind"], c["title"]) for c in s.cases()])


@scenario("mention", "The user's own messages never become asks to himself")
def own_messages(s: Sim, ok: Checks):
    s.world.post(TEAM_CH, ME, "deploying the coupon fix now, can someone watch the dashboard?")
    s.world.post(RANDOM_CH, ME, f"note to self {tag(ME)}: check checkout metrics?")
    s.run()
    ok(len(replies(s)) == 0, "no reply case", [(c["kind"], c["title"]) for c in s.cases()])
    ok(len(s.model_calls("reply")) == 0, "no drafting call")


@scenario("mention", "Editing a message does not create a second case")
def edited_message(s: Sim, ok: Checks):
    ts = s.world.post(TEAM_CH, ANA, f"{tag(ME)} can you review the coupon logic?")
    s.run()
    s.world.edit(TEAM_CH, ts, f"{tag(ME)} can you review the coupon rounding logic today?")
    s.run()
    ok(len(replies(s)) == 1 and db.q1("SELECT count(*) c FROM events")["c"] == 1, "one event, one case",
       (len(replies(s)), db.q1("SELECT count(*) c FROM events")["c"]))


@scenario("mention", "A message seen by the channel poll, the thread poll and the search is one event")
def seen_by_every_path(s: Sim, ok: Checks):
    root = s.world.post(TEAM_CH, ANA, f"{tag(ME)} the checkout coupon test is flaky, can you look?")
    s.run()
    s.world.post(TEAM_CH, BO, f"{tag(ME)} it failed again on main, can you check?", thread_ts=root)
    for _ in range(3):
        s.run()
    n = db.q1("SELECT count(*) c FROM events")["c"]
    ok(n == 2, "two messages, two events", n)
    ok(len(replies(s)) == 1, "one case for the thread", len(replies(s)))


# ---- C. threads --------------------------------------------------------------------------------------------------------
@scenario("thread", "A reply in a thread the user started is for the user")
def thread_user_started(s: Sim, ok: Checks):
    root = s.world.post(TEAM_CH, ME, "I changed how coupons round in checkout, heads up")
    s.run()
    s.world.post(TEAM_CH, ANA, "does this also change the totals in old invoices?", thread_ts=root)
    s.run()
    asks = s.asks()
    ok(len(replies(s)) == 1, "one case", len(replies(s)))
    ok(asks and asks[0]["payload"]["channel"] == TEAM_CH and asks[0]["payload"].get("thread_ts") == root, "answered in his thread", asks and asks[0]["payload"])


@scenario("thread", "One conversation is one case: questions, his answers and chatter in a thread never multiply cases")
def one_thread_one_case(s: Sim, ok: Checks):
    root = s.world.post(OPS_CH, CARLA, f"{tag(ME)} a customer was charged twice at checkout, can you look?")
    s.run()
    s.world.post(OPS_CH, ME, "looking now", thread_ts=root)
    s.wait(700)
    s.poll_approvals()
    s.run()
    for text in ("thanks Sam!", "any update? the customer is asking again", "ok", "great"):
        s.world.post(OPS_CH, CARLA, text, thread_ts=root)
        s.run()
    s.world.post(OPS_CH, ME, "it was a double submit, refund issued", thread_ts=root)
    s.wait(700)
    s.poll_approvals()
    s.run()
    s.world.post(OPS_CH, DEV, "nice, should we add a guard for double submits?", thread_ts=root)
    s.run()
    cases = replies(s)
    ok(len(cases) == 1, "one reply case for the whole thread", [(c["id"], c["status"], c["thread_key"][-12:]) for c in cases])
    ok(len(s.model_calls("reply")) <= 3, "a drafting call only when something is asked of him", len(s.model_calls("reply")))


@scenario("thread", "A burst of messages in a thread is read once")
def thread_burst(s: Sim, ok: Checks):
    root = s.world.post(TEAM_CH, ME, "rolling out the new checkout button")
    s.run()
    for i, text in enumerate(("wait", "is the old flow still reachable?", "asking because support has docs for it",
                              "can you keep it for a week?", "or at least a redirect?", "can you let us know today?")):
        s.world.post(TEAM_CH, ANA if i % 2 == 0 else BO, text, thread_ts=root)
    s.run()
    ok(len(replies(s)) == 1 and len(s.model_calls("reply")) == 1, "one case, one drafting call", (len(replies(s)), len(s.model_calls("reply"))))
    ok(len(s.asks()) == 1, "one ask", len(s.asks()))


@scenario("thread", "When a teammate answers before the user approved, the standing draft is withdrawn and nothing is sent")
def teammate_answers_first(s: Sim, ok: Checks):
    root = s.world.post(TEAM_CH, CARLA, f"{tag(ME)} is the coupon bug fixed in production?")
    s.run()
    ok(len(s.asks()) == 1, "an ask is waiting", len(s.asks()))
    s.world.post(TEAM_CH, BO, "yes, it went out with this morning's deploy", thread_ts=root)
    s.run()
    s.wait(1300)
    s.poll_approvals()
    s.run()
    ok(len(s.asks()) == 0, "the ask is withdrawn", s.asks())
    ok(s.sent_to_others() == [], "nothing sent", s.sent_to_others())
    ok(len(replies(s)) == 1, "still one case", len(replies(s)))


@scenario("thread", "Questions for the user inside a work case's thread share one reply case")
def questions_in_a_work_thread(s: Sim, ok: Checks):
    root = s.world.post(TEAM_CH, ANA, "checkout is broken: the pay button fails with a 500 for saved cards")
    s.ingest()
    work = s.cases("TASK") + s.cases("INCIDENT")
    ok(len(work) == 1, "the report is one work case", [(c["kind"], c["status"]) for c in s.cases()])
    s.world.post(TEAM_CH, ANA, f"{tag(ME)} does this also hit new cards?", thread_ts=root)
    s.ingest()
    s.world.post(TEAM_CH, BO, f"{tag(ME)} and is there a workaround we can give support?", thread_ts=root)
    s.ingest()
    qs = [c for c in replies(s) if ":q" in c["thread_key"]]
    ok(len(qs) == 1, "one reply case for the thread's questions", [(c["id"], c["thread_key"][-20:]) for c in replies(s)])
    ok(len(s.cases("TASK") + s.cases("INCIDENT")) == 1, "still one work case")


# ---- D. channels ---------------------------------------------------------------------------------------------------------
@scenario("channel", "The same problem reported in two channels is one piece of work")
def same_report_two_channels(s: Sim, ok: Checks):
    s.world.post(TEAM_CH, ANA, "checkout is broken: 500 error when paying with saved cards")
    s.world.post(OPS_CH, CARLA, "checkout is broken: 500 error when paying with saved cards")
    s.ingest()
    work = open_cases(s, "TASK") + open_cases(s, "INCIDENT")
    ok(len(work) == 1, "one open work case", [(c["kind"], c["status"], c["title"][:40]) for c in s.cases()])


@scenario("channel", "A question in a watched channel that nobody answers for two hours is raised to the user")
def unanswered_question(s: Sim, ok: Checks):
    from ap import ingest
    # the triage model leaves it alone (a question to the room, not to him): the sweep is what raises it later
    s.model.on("agent-ap-triage", lambda prompt, schema: {"results": [
        {**r, "class": "IGNORE", "needs_joaquin": False} for r in s.model.triage(prompt, schema)["results"]]})
    s.world.post(OPS_CH, CARLA, "does anyone know if checkout refunds are automatic when an order is cancelled by the customer?")
    s.ingest()
    before = len(replies(s))
    s.wait(2 * 3600 + 120)
    ingest.unanswered_sweep()
    ok(len(replies(s)) == before + 1, "the sweep opens a reply case", (before, len(replies(s))))


@scenario("channel", "A question that a teammate answered is not raised")
def answered_question_not_raised(s: Sim, ok: Checks):
    from ap import ingest
    ts = s.world.post(OPS_CH, CARLA, "does anyone know if checkout refunds are automatic when an order is cancelled by the customer?")
    s.world.post(OPS_CH, ANA, "yes, within the hour", thread_ts=ts)
    s.ingest()
    s.wait(2 * 3600 + 120)
    n = len(s.cases())
    ingest.unanswered_sweep()
    ok(len(s.cases()) == n, "nothing new", [(c["kind"], c["title"][:40]) for c in s.cases()])


@scenario("channel", "A PR posted in the PR channel becomes one review; posting it again does not start another")
def pr_posted_for_review(s: Sim, ok: Checks):
    from ap import ingest
    s.world.pr("web", 12, "bo-chen", "fix(checkout): round coupons half up")
    s.world.post(PR_CH, BO, "small one: https://github.com/acme/web/pull/12")
    s.ingest()
    ingest.poll_github()
    reviews = s.cases("REVIEW")
    ok(len(reviews) == 1 and reviews[0]["repo"] == "web" and reviews[0]["pr"] == 12, "one review case for web#12", [(c["repo"], c["pr"]) for c in reviews])
    s.world.post(PR_CH, BO, "bump https://github.com/acme/web/pull/12")
    s.wait(700)
    s.ingest()
    ingest.poll_github()
    ok(len(s.cases("REVIEW")) == 1, "still one review case", len(s.cases("REVIEW")))


@scenario("channel", "A PR of a repo the install does not work in is not reviewed")
def pr_of_another_repo(s: Sim, ok: Checks):
    from ap import ingest
    s.world.post(PR_CH, DEV, "please look: https://github.com/acme/infra/pull/7")
    s.ingest()
    ingest.poll_github()
    ok(len(s.cases("REVIEW")) == 0, "no review case", len(s.cases("REVIEW")))


@scenario("channel", "The same alert firing again and again is at most one case")
def alert_storm(s: Sim, ok: Checks):
    for _ in range(6):
        s.world.post(ALERTS_CH, MONITOR, "[checkout-api][prod] Triggered: p95 latency above 2s on POST /checkout")
        s.ingest()
        s.wait(3600)
    ok(len(s.cases()) <= 1, "at most one case", [(c["kind"], c["status"], c["title"][:50]) for c in s.cases()])


@scenario("channel", "An install with no PR channel and no alerts channel runs its intake without errors",
          config={"pr_channel": "", "alerts_channel": "", "watched_channels": []})
def minimal_install(s: Sim, ok: Checks):
    s.world.post(TEAM_CH, ANA, f"{tag(ME)} can you look at the coupon bug?")
    s.run()
    ok(len(replies(s)) == 1, "the team channel still works", len(replies(s)))
    bad = [k for m, k in s.world.calls if m == "conversations_history" and not k.get("channel")]
    ok(not bad, "no read of an empty channel id", bad)


# ---- E. noise ------------------------------------------------------------------------------------------------------------
@scenario("noise", "Joins, bot posts, emoji and greetings in read channels never become cases", live=True)
def channel_noise(s: Sim, ok: Checks):
    s.world.post(TEAM_CH, DEV, "", subtype="channel_join")
    s.world.post(TEAM_CH, MONITOR, "Deploy finished: web@4f2a")
    for text in (":tada:", "good morning team", "🎉🎉", "hola equipo!", "+1"):
        s.world.post(TEAM_CH, ANA, text)
    s.run()
    ok(len(s.cases()) == 0, "no case", [(c["kind"], c["title"][:40]) for c in s.cases()])


@scenario("noise", "A very long message aimed at the user is handled like any other")
def very_long_message(s: Sim, ok: Checks):
    s.world.post(ANA_DM, ANA, "can you check this stack trace? " + ("at checkout.total(order.js:42) " * 400))
    s.run()
    ok(len(replies(s)) == 1 and len(s.asks()) == 1, "one case with an ask", (len(replies(s)), len(s.asks())))


@scenario("noise", "A message that was deleted before Clea got to it does not leave a stuck case")
def deleted_before_drafting(s: Sim, ok: Checks):
    ts = s.world.post(TEAM_CH, ANA, f"{tag(ME)} can you look at the coupon bug?")
    s.ingest()
    s.world.delete(TEAM_CH, ts)
    s.run()
    stuck = [c for c in s.cases() if c["status"] == "blocked" or (c.get("last_error") or "")]
    ok(not stuck, "no blocked or erroring case", [(c["status"], (c.get("last_error") or "")[:120]) for c in stuck])
    ok(s.sent_to_others() == [], "nothing sent")


# ---- F. what may leave -----------------------------------------------------------------------------------------------------
@scenario("safety", "Declining at the Touch ID dialog sends nothing and leaves the ask open")
def decline_touch_id(s: Sim, ok: Checks):
    s.world.post(ANA_DM, ANA, "can you confirm the checkout deploy went out?")
    s.run()
    asks = s.asks()
    try:
        s.approve(asks[0]["id"], touch_id="decline")
    except Exception:
        pass
    ok(s.sent_to_others() == [], "nothing sent", s.sent_to_others())
    ok(len(s.asks()) == 1, "the ask is still open", len(s.asks()))


@scenario("safety", "Rejecting a draft closes it and sends nothing")
def reject_draft(s: Sim, ok: Checks):
    s.world.post(ANA_DM, ANA, "can you confirm the checkout deploy went out?")
    s.run()
    s.decide(s.asks()[0]["id"], "reject")
    ok(s.sent_to_others() == [] and len(s.asks()) == 0, "nothing sent, no open ask", (s.sent_to_others(), s.asks()))


@scenario("safety", "Asking for changes produces a new draft and withdraws the old one")
def revise_draft(s: Sim, ok: Checks):
    s.world.post(ANA_DM, ANA, "can you confirm the checkout deploy went out?")
    s.run()
    first = s.asks()[0]
    s.decide(first["id"], "revise", "shorter, and say it was at 10am")
    s.run()
    asks = s.asks()
    ok(len(asks) == 1 and asks[0]["id"] != first["id"], "one new ask replaces the old", [a["id"] for a in asks])
    ok(any("shorter" in p for r, p in s.model_calls("reply")[1:]), "the redraft saw his request")
    ok(s.sent_to_others() == [], "nothing sent")


@scenario("safety", "His approving reaction on the ask in his own DM sends it; someone else's reaction does not")
def approve_by_reaction(s: Sim, ok: Checks):
    s.world.post(ANA_DM, ANA, "can you confirm the checkout deploy went out?")
    s.run()
    ask = s.asks()[0]
    s.world.react(DM, ask["ts"], ANA, "white_check_mark")
    s.poll_approvals()
    ok(s.sent_to_others() == [], "a teammate's reaction approves nothing", s.sent_to_others())
    s.world.react(DM, ask["ts"], ME, "white_check_mark")
    s.poll_approvals()
    out = s.sent_to_others()
    ok(len(out) == 1 and out[0]["channel"] == ANA_DM and out[0]["text"] == ask["payload"]["text"], "his reaction sends the approved text", out)


@scenario("safety", "In watch mode Clea drafts and reports to the user but can send nothing, even if approved", config={"autonomy": "watch", "mode": "shadow"})
def watch_mode(s: Sim, ok: Checks):
    s.world.post(ANA_DM, ANA, "can you confirm the checkout deploy went out?")
    s.run()
    ok(len(replies(s)) == 1, "the case exists", len(replies(s)))
    for a in s.asks():
        try:
            s.approve(a["id"])
        except Exception:
            pass
    ok(s.sent_to_others() == [], "nothing left the machine", s.sent_to_others())
    ok(all(o["channel"] == DM for o in s.world.outbox if o["kind"] == "slack_post"), "posts only in his own DM",
       [o for o in s.world.outbox if o.get("channel") != DM])


@scenario("safety", "A draft for someone else that names the assistant is never shown for sending")
def draft_names_the_assistant(s: Sim, ok: Checks):
    s.model.on("reply", lambda prompt, schema: {**s.model.fake(schema), "needs_reply": True, "why": "asked", "needs_facts": False, "checks": [],
                                                "draft": "Clea checked it for me: the deploy went out at 10.", "prod_problem": False,
                                                "someone_else_owns": False, "target": "thread", "report": "They ask about the deploy.",
                                                "open_problem": "", "board_line": "Reply to Ana"})
    s.model.on("draft", lambda prompt, schema: {"text": "I checked it: the deploy went out at 10."})
    s.world.post(ANA_DM, ANA, "can you confirm the checkout deploy went out?")
    s.run()
    asks = s.asks()
    bad = [a for a in asks if "clea" in (a["payload"].get("text") or "").lower()]
    ok(not bad, "no ask carries a draft that names Clea", [a["payload"]["text"] for a in bad])
    ok(len(asks) == 1 and asks[0]["payload"]["text"].startswith("I checked it"), "the ask carries the draft rewritten in his voice",
       [a["payload"].get("text") for a in asks])
    ok(len(s.model_calls("draft")) == 1, "one rewrite call", len(s.model_calls("draft")))
    ok(s.sent_to_others() == [], "nothing sent before his approval")
    if asks:
        s.approve(asks[0]["id"])
    s.run()
    sent = s.sent_to_others()
    ok(len(sent) == 1 and "clea" not in sent[0]["text"].lower(), "what goes out does not name the assistant", sent)


@scenario("safety", "A draft that still names the assistant after the rewrite reaches nobody")
def draft_keeps_naming_the_assistant(s: Sim, ok: Checks):
    s.model.on("reply", lambda prompt, schema: {**s.model.fake(schema), "needs_reply": True, "why": "asked", "needs_facts": False, "checks": [],
                                                "draft": "Clea checked it for me: the deploy went out at 10.", "prod_problem": False,
                                                "someone_else_owns": False, "target": "thread", "report": "They ask about the deploy.",
                                                "open_problem": "", "board_line": "Reply to Ana"})
    s.model.on("draft", lambda prompt, schema: {"text": "Clea says the deploy went out at 10."})
    s.world.post(ANA_DM, ANA, "can you confirm the checkout deploy went out?")
    s.run()
    ok(s.asks() == [], "no ask", [a["payload"].get("text") for a in s.asks()])
    ok(s.sent_to_others() == [], "nothing sent")


# ---- G. when things go wrong -------------------------------------------------------------------------------------------------
@scenario("resilience", "Slack rate-limiting a read delays it; the message is still read once")
def slack_rate_limit(s: Sim, ok: Checks):
    s.world.post(ANA_DM, ANA, "can you check the checkout total?")
    s.world.faults["conversations_history"] = ["ratelimited", "ratelimited"]
    s.run()
    s.run()
    ok(len(replies(s)) == 1 and db.q1("SELECT count(*) c FROM events")["c"] == 1, "one event, one case",
       (len(replies(s)), db.q1("SELECT count(*) c FROM events")["c"]))


@scenario("resilience", "Running the intake again and again over the same messages changes nothing")
def repeated_intake(s: Sim, ok: Checks):
    root = s.world.post(TEAM_CH, ANA, f"{tag(ME)} can you look at the coupon bug?")
    s.world.post(TEAM_CH, BO, "I can pair on it if useful", thread_ts=root)
    s.world.post(ANA_DM, ANA, "also, can you send me the dashboard link?")
    s.run()
    snap = (db.q1("SELECT count(*) c FROM events")["c"], len(s.cases()), len(s.asks()), len(s.model_calls()))
    for _ in range(5):
        s.run()
    again = (db.q1("SELECT count(*) c FROM events")["c"], len(s.cases()), len(s.asks()), len(s.model_calls()))
    ok(snap == again, "events, cases, asks and model calls unchanged after five more passes", (snap, again))


@scenario("resilience", "A mention the search index delivers late is still picked up")
def late_search_index(s: Sim, ok: Checks):
    s.world.search_lag_s = 900
    s.world.post(RANDOM_CH, DEV, f"{tag(ME)} does checkout support gift cards yet?")
    s.run()
    ok(len(replies(s)) == 0, "not visible yet")
    s.wait(400)
    s.world.search_lag_s = 0
    s.world.post(RANDOM_CH, CARLA, f"{tag(ME)} can you share the checkout roadmap?")
    s.run()
    s.wait(200)
    s.run()
    ok(len(replies(s)) == 2, "both mentions became cases, the late one included", [(c["id"], c["title"][:50]) for c in replies(s)])


@scenario("resilience", "A channel that stops existing does not stop the rest of the intake", config={"watched_channels": [OPS_CH, "C0SIMGONE01"]})
def channel_gone(s: Sim, ok: Checks):
    s.world.post(TEAM_CH, ANA, f"{tag(ME)} can you look at the coupon bug?")
    s.world.post(OPS_CH, CARLA, f"{tag(ME)} can you look at this refund?")
    s.run()
    ok(len(replies(s)) == 2, "both read channels still produce their cases", len(replies(s)))


@scenario("resilience", "A message from someone Slack no longer knows is still handled")
def unknown_user(s: Sim, ok: Checks):
    s.world.post(TEAM_CH, "U0SIMGHOST1", f"{tag(ME)} can you look at the coupon bug?")
    s.run()
    ok(len(replies(s)) == 1 and not [c for c in s.cases() if c["status"] == "blocked"], "one case, not blocked",
       [(c["status"], (c.get("last_error") or "")[:100]) for c in s.cases()])


@scenario("dm", "An English promise by the user in a DM is tracked as his open loop; his follow-up closes it")
def user_promise(s: Sim, ok: Checks):
    s.world.post(CARLA_DM, CARLA, "hi! any news on the double charge?")
    s.world.post(CARLA_DM, ME, "not yet, I'll check the logs and get back to you")
    s.run()
    loops = s.cases("OPEN_LOOP")
    ok(len(loops) == 1, "his promise opens one loop", [(c["kind"], c["title"][:50]) for c in s.cases()])
    s.wait(600)
    s.world.post(CARLA_DM, ME, "found it: a duplicate submit. refund is on its way")
    s.run()
    ok(all(c["status"] == "done" for c in s.cases("OPEN_LOOP")), "his follow-up closes it", [(c["id"], c["status"]) for c in s.cases("OPEN_LOOP")])


# ---- J. what a real model does with it (python -m sim --live) ----------------------------------------------------------------
def _until(s: Sim, done, rounds: int = 6) -> None:
    """Run until `done()` or nothing moves: a real model may first look things up (a short investigation of its own)."""
    for _ in range(rounds):
        ran = s.run(max_steps=20, horizon_s=1800)
        if done() or not ran:
            return


@scenario("live", "A question the repository can answer gets one case and a draft that answers it, sent only after approval, word for word",
          live="only")
def live_question_from_the_code(s: Sim, ok: Checks):
    from ap import worktree
    s.fresh_repos()
    worktree.ensure_mirrors()
    s.world.post(ANA_DM, ANA, "hey Sam, quick one: in the web repo, does discount() in checkout/coupons.py round the discount "
                 "or cut it off? I need to tell support today")
    _until(s, lambda: bool(s.asks()))
    asks = [a for a in s.asks() if a["kind"] == "slack_send"]
    story = [(c["id"], c["kind"], c["status"]) for c in s.cases()]
    ok(len([c for c in replies(s) if c["thread_key"].endswith(":dm")]) == 1, "one reply case for the conversation", story)
    if not ok(len(asks) == 1 and asks[0]["payload"]["channel"] == ANA_DM, "one ask: a message for that DM", story):
        return
    draft = asks[0]["payload"]["text"]
    ok(re.search(r"trunc|cut|round|int\(", draft, re.I) is not None, "the draft answers the question", draft[:300])
    ok("clea" not in draft.lower() and "—" not in draft, "in the user's voice: no assistant named, no em dash", draft[:300])
    ok(s.sent_to_others() == [], "nothing sent before the approval", s.sent_to_others())
    s.approve(asks[0]["id"])
    out = [o for o in s.sent_to_others() if o["kind"] == "slack_post"]
    ok(len(out) == 1 and out[0]["channel"] == ANA_DM and out[0]["text"] == draft, "after approval: exactly that text, in that DM", out)


@scenario("live", "A thread where a teammate already answered the question asks nothing of the user", live="only")
def live_thread_already_answered(s: Sim, ok: Checks):
    root = s.world.post(TEAM_CH, CARLA, f"{tag(ME)} is the checkout service the one that sends the receipt email?")
    s.world.post(TEAM_CH, ANA, "no, that is the notifications service. Checkout only emits the order event", thread_ts=root)
    s.world.post(TEAM_CH, CARLA, "perfect, thanks Ana!", thread_ts=root)
    _until(s, lambda: all(c["status"] in db.TERMINAL for c in replies(s)) and bool(replies(s)))
    ok(len(replies(s)) <= 1, "at most one case for the thread", [(c["id"], c["status"]) for c in replies(s)])
    ok(not [a for a in s.asks() if a["kind"] == "slack_send"], "no message is proposed", [a["payload"].get("text", "")[:120] for a in s.asks()])
    ok(s.sent_to_others() == [], "nothing sent", s.sent_to_others())


# ---- I. work that ends in a pull request ---------------------------------------------------------------------------------------
@scenario("build", "A bug a teammate reports with its cause ends as a draft pull request that fixes it, with a test; nothing is "
          "said in Slack without approval", live="only")
def bug_to_draft_pr(s: Sim, ok: Checks):
    import subprocess
    import tempfile
    from ap import worktree
    from sim.harness import origin_of
    s.fresh_repos()
    worktree.ensure_mirrors()  # what the models read: the repos at their default branch
    s.world.post(TEAM_CH, ANA, f"{tag(ME)} the coupon total is off by a cent: a 15% coupon on $9.99 charges $8.50 and it should be "
                 "$8.49. The discount is 149.85 cents and `discount()` in checkout/coupons.py in the web repo truncates it, "
                 "while its docstring says rounded half up. Can you fix it? ENG-12")
    for _ in range(10):  # the lane is long (investigate, plan, build, gate, review, publish): run until the PR exists
        ran = s.run(max_steps=25, horizon_s=1800)
        if any(o["kind"] == "gh_pr_create" for o in s.world.outbox) or not ran:
            break
    opened = [o for o in s.world.outbox if o["kind"] == "gh_pr_create"]
    story = [(c["id"], c["kind"], c["status"], (c["data"].get("blocked_reason") or c["data"].get("question") or "")[:160]) for c in s.cases()]
    if not ok(len(opened) == 1 and opened[0]["repo"] == "web", "one pull request opened in the web repo", story):
        return
    ok(opened[0]["draft"], "it is a draft: 'ask' never opens one ready for review", opened[0])
    pr = s.world.prs[("web", opened[0]["number"])]
    branch, origin = pr["headRefName"], origin_of("web")
    with tempfile.TemporaryDirectory() as tmp:
        got = subprocess.run(["git", "clone", "-q", "--branch", branch, str(origin), tmp], capture_output=True, text=True)
        if not ok(got.returncode == 0, "its branch is on the remote", got.stderr[-200:]):
            return
        fixed = subprocess.run(["python3", "-c", "from checkout.coupons import discount, apply_coupon; "
                                "assert discount(999, 15) == 150 and apply_coupon(999, 15) == 849 and discount(1000, 15) == 150"],
                               cwd=tmp, capture_output=True, text=True)
        ok(fixed.returncode == 0, "the fix is right: 15% of $9.99 is $1.50 off", fixed.stderr[-300:])
        tests = subprocess.run(["python3", "-m", "unittest", "-q"], cwd=tmp, capture_output=True, text=True)
        ok(tests.returncode == 0, "the repo's tests pass on the branch", tests.stderr[-300:])
        changed = subprocess.run(["git", "diff", "--name-only", "origin/main...HEAD"], cwd=tmp, capture_output=True, text=True).stdout.split()
        ok(any(f.startswith("tests/") for f in changed), "a test covers the bug", changed)
        ok(set(changed) <= {"checkout/coupons.py"} | {f for f in changed if f.startswith("tests/")}, "nothing else was touched", changed)
    ok("ENG-12" in (pr["title"] + pr.get("body", "")), "the pull request names the ticket", pr["title"])
    ok(not [o for o in s.sent_to_others() if o["kind"] == "slack_post"], "nothing was said in Slack in the user's name",
       [o.get("text", "")[:80] for o in s.sent_to_others() if o["kind"] == "slack_post"])
    told = [o["text"] for o in s.world.outbox if o["kind"] in ("slack_post", "slack_edit") and o.get("channel") == DM]
    ok(any(f"web#{opened[0]['number']}" in t or pr["url"] in t for t in told), "the user is told about the pull request in their DM",
       [t[:120] for t in told[-3:]])


# ---- K. a morning to look at in the app (python -m sim -k demo --serve) ---------------------------------------------------------
GUEST_COUPON = '''

GUEST_CAP = 50


def apply_guest_coupon(total_cents: int, percent: int) -> int:
    """What a guest pays: a guest order takes at most 50% off."""
    return total_cents - discount(total_cents, max(percent, GUEST_CAP))
'''


def _teammate_pr(s: Sim, number: int, author: str, title: str, branch: str, body: str) -> dict:
    """A teammate's pull request that exists for real on the repo's origin: a branch with one commit, and the diff git
    gives for it, reachable the way GitHub serves it. This one has a bug worth a review comment (max where min is meant: every guest gets at least 50% off)."""
    import subprocess
    import tempfile
    from sim.harness import origin_of
    git = lambda cwd, *a: subprocess.run(["git", "-c", "user.email=bo@acme.example", "-c", "user.name=Bo Chen", *a], cwd=cwd,
                                         capture_output=True, text=True, check=True).stdout
    with tempfile.TemporaryDirectory() as tmp:
        git(".", "clone", "-q", str(origin_of("web")), tmp)
        git(tmp, "checkout", "-q", "-b", branch)
        with open(f"{tmp}/checkout/coupons.py", "a") as f:
            f.write(GUEST_COUPON)
        git(tmp, "commit", "-qam", title)
        git(tmp, "push", "-q", "origin", branch, f"HEAD:refs/pull/{number}/head")  # where GitHub keeps a pull request's head
        sha, diff = git(tmp, "rev-parse", "HEAD").strip(), git(tmp, "diff", "origin/main...HEAD")
        with open(f"{tmp}/checkout/coupons.py") as f:
            line = next(i for i, l in enumerate(f, 1) if "max(percent" in l)
    return s.world.pr("web", number, author, title, headRefName=branch, headRefOid=sha, diff=diff, body=body, bug_line=line,
                      files=[{"path": "checkout/coupons.py", "additions": 8, "deletions": 0}])


@scenario("demo", "A morning at acme, to look at in the app (python -m sim -k demo --serve): chatter that asks nothing, a question "
          "in a DM, a teammate's pull request to review and, on a real model, a reported bug that ends as a draft pull request",
          live=True)
def demo(s: Sim, ok: Checks):
    from ap import ingest, worktree
    s.fresh_repos()
    worktree.ensure_mirrors()
    pr = _teammate_pr(s, 12, "bo-chen", "feat(checkout): cap guest coupons at 50%", "guest-coupon-cap",
                      "Guests can stack promo codes up to 100% off. This caps a guest order at 50%.\n\nENG-9")
    if not s.live:  # the scripted model writes placeholders: here it says what a model would, so the app reads like a real morning
        s.model.on("reply", lambda prompt, schema: {
            **s.model.fake(schema), "needs_reply": True, "why": "Carla asks him what checkout does with a coupon above 100%",
            "draft": "Hey Carla, it rejects it. apply_coupon() in checkout/coupons.py raises an error for anything outside "
                     "0 to 100, so the order never gets a total. Safe to tell them the coupon is invalid.",
            "needs_facts": False, "checks": [], "prod_problem": False, "someone_else_owns": False, "target": "thread",
            "report": "Carla needs it for a support macro. The code answers it: a percent outside 0 to 100 raises ValueError.",
            "ops_request": False, "school": "", "open_problem": "", "precedent_terms": [], "board_line": "Answer Carla: coupons above 100%"})
        s.model.on("report", lambda prompt, schema: {
            "headline": "Carla asks what checkout does with a coupon above 100%",
            "bullets": ["apply_coupon() rejects any percent outside 0 to 100 (checkout/coupons.py)",
                        "no order total is produced, so nothing is charged"],
            "need": "your OK to send the reply", "analysis": "Read from the web repo at main.", "text": "", "detail": "",
            "board_line": "Answer Carla: coupons above 100%"})
        where = {"file": "checkout/coupons.py", "line": pr["bug_line"]}
        s.model.on("agent-ap-review", lambda prompt, schema: {"findings": [{
            **where, "severity": "major", "claim": "max() makes 50% the smallest discount a guest gets, not the largest",
            "evidence": "apply_guest_coupon(1000, 10) returns 500: discount(total, max(10, 50)) takes 50% off",
            "suggestion": "min(percent, GUEST_CAP)"}]})
        s.model.on("review_verify", lambda prompt, schema: s.model.fake(schema) if "dropped" not in (schema or {}).get("properties", {}) else {
            "body": "Nice one Bo! One thing on the cap itself before it goes in.", "dropped": [],
            "comments": [{"path": where["file"], "line": where["line"], "severity": "major",
                          "body": "Could it be that this should be `min()`? With `max(percent, GUEST_CAP)` a guest with a 10% coupon "
                                  "gets 50% off: `apply_guest_coupon(1000, 10)` returns 500. A test with one coupon under the cap "
                                  "and one over it would pin it."}]})
    s.world.post(RANDOM_CH, DEV, "anyone up for lunch at 1?")
    s.world.post(CARLA_DM, CARLA, "hey Sam, quick one for a support macro: if someone enters a coupon above 100% in checkout, "
                 "do we charge zero or do we reject it?")
    s.world.post(PR_CH, BO, "small one when you have a minute: https://github.com/acme/web/pull/12")
    if s.live:  # the part only a model can do: read the code, fix it, prove it
        s.world.post(TEAM_CH, ANA, f"{tag(ME)} the coupon total is off by a cent: a 15% coupon on $9.99 charges $8.50 and it should "
                     "be $8.49. `discount()` in checkout/coupons.py in the web repo truncates where its docstring says rounded "
                     "half up. Can you fix it? ENG-12")
    reading = ("review_prepare", "review_find", "review_verify")
    done = lambda: bool([a for a in s.asks() if a["kind"] == "slack_send"]) and not [c for c in s.cases("REVIEW") if c["status"] in reading] and (
        not s.live or any(o["kind"] == "gh_pr_create" for o in s.world.outbox))
    for _ in range(12 if s.live else 3):
        ran = s.run(max_steps=25, horizon_s=1800)
        ingest.poll_github()
        if done() or not ran:
            break
    story = [(c["id"], c["kind"], c["status"]) for c in s.cases()]
    ok(len([c for c in replies(s) if c["thread_key"].endswith(":dm")]) == 1, "one case for Carla's question", story)
    ok(len([a for a in s.asks() if a["kind"] == "slack_send" and a["payload"].get("channel") == CARLA_DM]) == 1,
       "its reply waits for approval", [(a["kind"], a["payload"].get("channel")) for a in s.asks()])
    ok(len(s.cases("REVIEW")) == 1, "one review of Bo's pull request", story)
    ok(not [o for o in s.world.outbox if o["kind"] in ("gh_review", "gh_review_comment", "gh_comment_other_pr")],
       "the review is not posted without approval", [o["kind"] for o in s.world.outbox])
    ok(not [o for o in s.sent_to_others() if o["kind"] == "slack_post"], "nothing said in Slack in the user's name",
       [o.get("text", "")[:80] for o in s.sent_to_others() if o["kind"] == "slack_post"])
    if s.live:
        ok(len([o for o in s.world.outbox if o["kind"] == "gh_pr_create" and o["draft"]]) == 1, "the bug ends as one draft pull request", story)


# ---- H. tickets ------------------------------------------------------------------------------------------------------------
JIRA = {"jira_base": "https://acme.atlassian.net", "jira": {"enabled": True, "email": "sam@acme.example", "projects": ["ENG"],
                                                         "assigned": True, "mentions": False}}


def _jira(s: Sim, issues: list[dict]) -> list[str]:
    """Stand in for Jira's search: the scenario decides what the user's query returns. Returns the JQL it was asked."""
    from ap import setup
    asked: list[str] = []

    def search(jql, limit=50):
        asked.append(jql)
        return [{"key": i["key"], "fields": {"summary": i["summary"], "status": {"name": i.get("status", "To Do")},
                                             "priority": {"name": i.get("priority", "Medium")}, "updated": i["updated"],
                                             "description": {"type": "doc", "content": [{"type": "paragraph", "content": [
                                                 {"type": "text", "text": i.get("description", "")}]}]}}} for i in issues]
    setup.jira_search = search
    return asked


def _stamp(days_ago: float) -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S.000+0000", time.localtime(time.time() - days_ago * 86400))


@scenario("tickets", "Connecting Jira is a baseline, not a backlog run: only recent tickets become cases, a few at a time", config=JIRA)
def jira_first_sync(s: Sim, ok: Checks):
    from ap import tickets
    issues = [{"key": f"ENG-{n}", "summary": f"old backlog item {n}", "updated": _stamp(40 + n)} for n in range(1, 31)]
    issues += [{"key": f"ENG-{100 + n}", "summary": f"checkout bug {n}", "updated": _stamp(n - 0.5), "priority": "High"} for n in range(1, 8)]
    asked = _jira(s, issues)
    ok(tickets.enabled(), "the install consumes Jira")
    made = tickets.poll()
    ok(made == tickets.PER_POLL, "the first pass opens only a few cases", made)
    ok(all(c["thread_key"].startswith("jira:ENG-10") for c in s.cases()), "only tickets that moved this week", [c["thread_key"] for c in s.cases()])
    ok("project in (ENG)" in asked[0] and "assignee = currentUser()" in asked[0] and "statusCategory != Done" in asked[0], "the query is what he chose", asked[0])
    tickets.poll()
    tickets.poll()
    keys = sorted(c["thread_key"] for c in s.cases())
    ok(len(keys) == 7 and len(set(keys)) == 7, "the rest of this week's tickets follow, one case each, the old backlog never", keys)


@scenario("tickets", "A ticket is one piece of work: polling again, or the ticket changing, does not open another case", config=JIRA)
def jira_no_repeats(s: Sim, ok: Checks):
    from ap import tickets
    issue = {"key": "ENG-7", "summary": "coupon totals wrong at checkout", "updated": _stamp(0.1), "description": "see order 1234"}
    _jira(s, [issue])
    tickets.poll()
    for _ in range(4):
        tickets.poll()
    ok(len(s.cases()) == 1, "one case after five polls", len(s.cases()))
    _jira(s, [{**issue, "updated": _stamp(0), "status": "In Progress"}])
    tickets.poll()
    ok(len(s.cases()) == 1, "still one after the ticket moved", len(s.cases()))


@scenario("tickets", "The same problem arriving as a ticket and as a Slack report is one piece of work", config=JIRA)
def jira_and_slack_same_ticket(s: Sim, ok: Checks):
    from ap import tickets
    s.world.post(TEAM_CH, ANA, "ENG-7 is broken again: coupon totals wrong at checkout, error 500")
    s.ingest()
    _jira(s, [{"key": "ENG-7", "summary": "coupon totals wrong at checkout", "updated": _stamp(0.1)}])
    tickets.poll()
    work = open_cases(s, "TASK") + open_cases(s, "INCIDENT")
    ok(len(work) == 1, "one open work case for ENG-7", [(c["kind"], c["thread_key"], c["status"]) for c in s.cases()])


# ---- spending: what a case costs before anyone asked for it -----------------------------------------------------------
import time as _time

NEW_INSTALL = {**JIRA, "setup": {"completed_at": int(_time.time())}}
RED = [{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]
GREEN = [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]


def _days_ago(days: float) -> str:
    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() - days * 86400))


@scenario("spend", "A new install starts from now: what was last touched more than two days before it was set up is not taken on", config=NEW_INSTALL)
def starts_from_now(s: Sim, ok: Checks):
    from ap import babysit, core, ingest, site, tickets
    ok(site.intake_since() > 0 and site.backlog(_days_ago(5)) and not site.backlog(_days_ago(1)) and not site.backlog("not a date"),
       "the line is two days before setup, and a time that cannot be read is never called backlog", site.intake_since())
    _jira(s, [{"key": "ENG-1", "summary": "moved five days ago", "updated": _stamp(5)},
              {"key": "ENG-2", "summary": "moved yesterday", "updated": _stamp(1)}])
    tickets.poll()
    tickets.poll()
    ok([c["thread_key"] for c in s.cases("TASK")] == ["jira:ENG-2"], "only the ticket that moved inside the window is a case",
       [c["thread_key"] for c in s.cases("TASK")])
    old = s.world.pr("web", 21, "bo-chen", "feat(cart): an old one", updatedAt=_days_ago(5))
    new = s.world.pr("web", 22, "bo-chen", "feat(cart): a current one")
    made = ingest._maybe_review_item("web", old, requested=True) + ingest._maybe_review_item("web", new, requested=True)
    ok(made == 1 and [c["pr"] for c in s.cases("REVIEW")] == [22], "a review is opened for the current pull request only",
       [(c["pr"], c["status"]) for c in s.cases("REVIEW")])
    s.world.pr("web", 24, "bo-chen", "feat(cart): old, but asked for today", updatedAt=_days_ago(5))
    s.world.post(PR_CH, BO, "can someone look at this one? https://github.com/acme/web/pull/24")
    db.kv_set("pr_channel_read_at", 0)
    ingest.pr_channel_reviews()
    ok(sorted(c["pr"] for c in s.cases("REVIEW")) == [22, 24], "a review someone asks for today counts, however old the pull request",
       [(c["pr"], c["status"]) for c in s.cases("REVIEW")])
    mine = s.world.pr("web", 23, core.CFG["self_github"], "fix(cart): red for a week", updatedAt=_days_ago(5), statusCheckRollup=RED)
    stats = {"prs": 0, "merged": 0, "items": 0}
    babysit._one("web", 23, mine, stats)
    ok(stats["items"] == 0 and not s.cases("CI") and not db.kv_get("pr:web#23"),
       "his own pull request, red and untouched since before the install, is left alone", [c["title"] for c in s.cases("CI")])
    ok(not s.model.calls, "none of it cost a model call", [c[0] for c in s.model.calls])


@scenario("spend", "An install with no setup date (the first one) keeps its own windows: nothing is called backlog")
def no_setup_date_no_line(s: Sim, ok: Checks):
    from ap import ingest, site
    ok(site.intake_since() == 0 and not site.backlog(_days_ago(400)), "no line", site.intake_since())
    old = s.world.pr("web", 21, "bo-chen", "feat(cart): five days old", updatedAt=_days_ago(5))
    ok(ingest._maybe_review_item("web", old, requested=True) == 1, "a five day old pull request is still reviewed there")


def _pr_case(s: Sim, kind: str, status: str, number: int, **pr) -> int:
    from ap import core
    p = s.world.pr("web", number, core.CFG["self_github"], f"fix(cart): case {number}", **pr)
    part = {"repo": "web", "branch": p["headRefName"], "wt": "/nonexistent/sim", "pr": number, "pr_url": p["url"], "state": "pushed"}
    return db.create_item(kind, status, f"{kind.lower()} web#{number}", f"{kind.lower()}:web#{number}", repo="web", pr=number,
                          data={"parts": [part], "cur": 0, "comments": [{"id": 1, "thread_id": "T1", "body": "rename this"}]})


@scenario("spend", "Before a paid stage, a free look at GitHub: when the pull request merged or closed, its check is no longer red or its threads were resolved, no model is asked")
def nothing_left_to_do(s: Sim, ok: Checks):
    from ap import precheck, worker
    pr = s.world.pr("web", 31, "bo-chen", "feat(cart): merged before the review")
    rid = db.create_item("REVIEW", "review_find", "review web#31", "review:web#31", repo="web", pr=31, data={"head_sha": pr["headRefOid"]})
    pr["state"] = "MERGED"
    worker.run_stage(db.item(rid))
    ok(s.case(rid)["status"] == "dropped" and "no longer needed" in s.notes(rid)[-1], "the review of a merged pull request is dropped", s.notes(rid))
    green = _pr_case(s, "CI", "ci_fix", 32, statusCheckRollup=GREEN)
    worker.run_stage(db.item(green))
    ok(s.case(green)["status"] == "ci_wait", "a CI case whose check is green now goes back to reading CI, not to a fix", s.notes(green))
    closed = _pr_case(s, "COMMENTS", "comments_triage", 33, statusCheckRollup=GREEN)
    s.world.prs[("web", 33)]["state"] = "CLOSED"
    worker.run_stage(db.item(closed))
    ok(s.case(closed)["status"] == "dropped", "a comments case on a closed pull request is dropped", s.notes(closed))
    ci = _pr_case(s, "CI", "ci_fix", 34, statusCheckRollup=RED)
    db.create_item("COMMENTS", "waiting", "comments web#34", "comments:web#34", repo="web", pr=34, data=s.case(ci)["data"])
    ok(precheck.settled(db.item(ci)) is False and s.case(ci)["status"] == "ci_fix",
       "a comments case that is only waiting on the same pull request does not end a red CI case", s.notes(ci))
    resolved = _pr_case(s, "COMMENTS", "comments_triage", 35, statusCheckRollup=GREEN)
    worker.run_stage(db.item(resolved))
    ok(s.case(resolved)["status"] == "done", "a comments case whose threads were resolved meanwhile is done", s.notes(resolved))
    ok(not s.model.calls, "none of the four cost a model call", [c[0] for c in s.model.calls])
    red = _pr_case(s, "CI", "ci_fix", 36, statusCheckRollup=RED)
    ok(precheck.settled(db.item(red)) is False and s.case(red)["status"] == "ci_fix", "a check that is still red is still work", s.notes(red))
    s.world.prs.pop(("web", 36))
    ok(precheck.settled(db.item(red)) is False, "a pull request that cannot be read settles nothing: the stage runs as before")


@scenario("spend", "A mechanical fix starts on the lighter model once; when it fails or the problem returns, the case is Opus's from then on")
def mechanical_fix_steps_up(s: Sim, ok: Checks):
    import tempfile
    from ap import tasks
    part = {"repo": "web", "branch": "feature-41", "wt": tempfile.mkdtemp(prefix="sim-fix-"), "state": "built"}

    def case(key: str) -> int:
        return db.create_item("TASK", "gate", f"a fix {key}", f"test:{key}", repo="web", data={"parts": [dict(part)], "cur": 0})

    a = case("returns")
    tasks._to_fix(db.item(a), dict(part), ["lint: unused import in cart.py"], "ci", mechanical=True)
    ok(s.case(a)["status"] == "fix" and s.case(a)["data"]["fix_light"] is True, "a mechanical round is marked light", s.case(a)["data"].get("fix_light"))
    tasks.fix(db.item(a))
    ok(s.model.built[-1] == ("fix", True), "and its session starts on the lighter model", s.model.built)
    tasks._to_fix(db.item(a), dict(part), ["lint: unused import in cart.py"], "gate", mechanical=True)
    d = s.case(a)["data"]
    ok(d["fix_light"] is False and d["fix_stepped_up"] is True, "the same case back in fix is no longer light, even for a mechanical problem", d)
    tasks.fix(db.item(a))
    ok(s.model.built[-1] == ("fix", False), "its next session is Opus's", s.model.built)

    b = case("judgement")
    tasks._to_fix(db.item(b), dict(part), ["test_total_with_coupon fails: 8.50 != 8.49"], "ci", mechanical=False)
    tasks.fix(db.item(b))
    ok(s.case(b)["data"]["fix_light"] is False and s.model.built[-1] == ("fix", False), "a fix that needs judgement never starts light", s.model.built)

    c = case("gives-up")
    tasks._to_fix(db.item(c), dict(part), ["format: run the formatter"], "ci", mechanical=True)
    s.model.fail_light = True
    n = len(s.model.built)
    tasks.fix(db.item(c))
    d = s.case(c)["data"]
    ok(s.model.built[n:] == [("fix", True), ("fix", False)] and d["fix_stepped_up"] is True and s.case(c)["status"] == "gate",
       "a light session that fails is done again by Opus in the same round, and the case moves on", (s.model.built[n:], s.case(c)["status"]))


@scenario("spend", "A billing refusal stops everything: nothing is claimed or retried until the user resumes it or chooses another model")
def billing_stop(s: Sim, ok: Checks):
    from ap import claude, core, providers, worker
    s.world.post(ANA_DM, ANA, "hey Sam, can you check why the checkout total is wrong for coupon orders?")
    s.ingest()
    case = open_cases(s, "REPLY")[0]["id"]
    db.update_item(case, next_run_at=0)
    ok(claude.billing_refusal("API Error: Credit balance is too low") and claude.billing_refusal("You exceeded your current quota")
       and claude.billing_refusal("", 402) and not claude.billing_refusal("usage limit reached, resets 11pm"),
       "a billing refusal is told apart from a usage limit")
    err = claude.provider_stopped("Credit balance is too low")
    ok(isinstance(err, claude.ClaudeLimited) and claude.stopped()["why"] == "Credit balance is too low"
       and claude.paused_until() > core.now() + 365 * 86400, "the refusal stops the install, with no end date", claude.stopped())
    calls = len(s.model.calls)
    for pool, stages in (("quick", worker.QUICK), ("fast", worker.FAST), ("claude", worker.SLOW)):
        worker.pool_turn(pool, stages, None, f"sim-{pool}")
    c = s.case(case)
    ok(c["status"] == "reply_draft" and not c.get("claimed_by") and not c.get("attempts") and len(s.model.calls) == calls,
       "no pool claims a case and no model is asked while stopped", (c["status"], c.get("claimed_by"), c.get("attempts")))
    ok("billing" in worker._pauses() and "limit" not in worker._pauses(), "the board says why, as a stop and not as a usage limit", worker._pauses())
    ok(claude.probe_limited() == [] and claude.stopped(), "nothing probes the provider by itself")
    # worker.tell_stop, not worker.watchdog: the watchdog also sweeps this Mac's processes and containers
    sent = [worker.tell_stop(worker._pauses()), worker.tell_stop(worker._pauses())]
    told = [o for o in s.world.outbox if o.get("channel") == DM and "billing" in (o.get("text") or "")]
    ok(sent == [True, False] and len(told) == 1, "the user is told once, in their own DM", (sent, [o.get("text", "")[:80] for o in s.world.outbox]))

    probe = claude._probe
    try:
        claude._probe = lambda mid: False
        ok(claude.try_resume() == (False, "Credit balance is too low") and claude.stopped(), "resume while it still refuses: the stop stands")
        claude._probe = lambda mid: True
        ok(claude.try_resume() == (True, "") and not claude.stopped() and claude.paused_until() <= core.now(), "resume once it answers: running again")
    finally:
        claude._probe = probe
    worker.pool_turn("quick", worker.QUICK, None, "sim-quick")
    ok(len(s.model.calls) > calls, "and the waiting case is worked", s.case(case)["status"])

    claude.provider_stopped("insufficient_quota")
    state = providers.state
    try:
        providers.state = lambda pid: {"ready": True, "detail": "", "id": pid}
        providers.select("claude-subscription")
    finally:
        providers.state = state
    ok(not claude.stopped(), "choosing another way to reach a model ends the stop by itself")


# The simulator

`python -m sim` runs the real engine on a scratch install against a simulated Slack and GitHub. Nothing real is
reachable from it: no token is read, and a write lands in the simulated world. It is how Clea's behaviour is
specified and how a change is checked.

```
make sim                               # every scenario, on the scripted model (about a minute, no tokens)
python -m sim -k thread -v             # the scenarios about threads, with each one's story
python -m sim -k demo --serve          # then open the app on what the scenario left
python -m sim --live -k bug_to_draft   # the same engine, thinking with your real model (spends tokens)
```

## What a scenario is

People write in the simulated workspace; the engine runs; the scenario states what must be true afterwards.

```python
@scenario("dm", "A question in a DM becomes one case with a draft, asked for approval; approving sends exactly that draft")
def dm_question(s: Sim, ok: Checks):
    s.world.post(ANA_DM, ANA, "hey Sam, can you check why the checkout total is wrong for coupon orders?")
    s.run()
    asks = s.asks()
    ok(len(asks) == 1 and asks[0]["payload"]["channel"] == ANA_DM, "one ask: send in the DM with Ana")
    ok(s.sent_to_others() == [], "nothing sent before the approval")
    s.approve(asks[0]["id"])
    ok(s.sent_to_others()[0]["text"] == asks[0]["payload"]["text"], "after approval: exactly the approved text")
```

After every scripted scenario three things are checked whatever the scenario says:

- one conversation never has two open cases;
- nothing was sent to another person without an approved ask for that exact text;
- an English install never writes Spanish.

## Scripted and live

The **scripted model** answers by simple rules, so a run is exact and free: it tests the engine, not the model. A
scenario can replace any answer (`s.model.on("reply", ...)`) to put the engine in a situation.

**Live** (`--live`) runs the scenarios whose outcome does not depend on a scripted answer on your real model. The
build scenario seeds a small repository with a real bug, has a teammate report it, and passes only if a draft pull
request appears whose branch fixes the bug, adds a test and passes the repository's tests.

## Reporting a bug as a scenario

The most useful bug report is a scenario that fails. Add it to `sim/scenarios.py`, run `python -m sim -k its_name -v`,
and open a pull request with it.

"""Container tests, run locally the way CI runs them, scoped to the diff, torn down after.

the user: "you can run docker tests locally too. just do it smartly and finish
after you're done. for schools its fine. for cometa the problem was that as its a monorepo,
the tests were taking too long".

schools  `make test file=<scoped test files>` (its own postgres test containers, reset per
         run), then `compose down -v` for the test services.
cometa   CI runs ONLY unit tests (`pytest -n auto -m unit`, scripts/ci.sh) with dummy DB URLs,
         so they need no services. The slowness was `docker compose run cometa` booting every
         dependency (rabbitmq, mongo, redis x2, localstack, observability). Here: the CI image,
         `--no-deps`, CI's env file, and only the modules the diff touches (the full unit suite
         when shared code under src/core or src/tests changes).
One global lock serializes container runs (ports and container names are shared). An unreachable
engine is recovered with Docker Desktop's official CLI (start/restart) and verified before running.
"""
from __future__ import annotations

import fcntl
import json
import re
import time
from contextlib import contextmanager
from pathlib import Path

from . import site
from .core import CFG, VAR, log, run

LOCK = VAR / "docker.lock"
COMETA_ENV = """PROJECT_NAME=cometa
API_V1_STR=/api/v1
ENVIRONMENT=test
PORT=8080
SCHOOLS_DATABASE_URL=postgresql+asyncpg://dummy:dummy@localhost:5432/dummy
ADMISSIONS_DATABASE_URL=postgresql+asyncpg://dummy:dummy@localhost:5432/dummy
DYNAMIC_FORMS_DATABASE_URL=postgresql+asyncpg://dummy:dummy@localhost:5432/dummy
ADMISSIONS_COMETA_DATABASE_URL=postgresql+asyncpg://dummy:dummy@localhost:5432/dummy
DYNAMIC_FORMS_COMETA_DATABASE_URL=postgresql+asyncpg://dummy:dummy@localhost:5432/dummy
AWS_REGION=us-east-1
TREASURY_DATABASE_URL=postgresql+asyncpg://treasury_user:treasury_pass@localhost:5432/cometa_test?options=-csearch_path%3Dtreasury
FISCAL_DATABASE_URL=postgresql+asyncpg://fiscal_user:fiscal_pass@localhost:5432/cometa_test?options=-csearch_path%3Dfiscal
BILLING_DATABASE_URL=postgresql+asyncpg://billing_user:billing_pass@localhost:5432/cometa_test?options=-csearch_path%3Dbilling
INVENTORY_DATABASE_URL=postgresql+asyncpg://inventory_user:inventory_pass@localhost:5432/cometa_test?options=-csearch_path%3Dinventory
"""


def docker_ok() -> bool:
    try:
        return run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20).returncode == 0
    except Exception:  # a wedged Docker Desktop hangs `docker info` instead of failing
        return False


class DockerUnavailable(Exception):
    pass


DOCKER_CFG = VAR / "docker-config"


def isolated_docker_env() -> dict:
    """Docker without Docker Desktop's app container: a harness-owned DOCKER_CONFIG with no
    credential helper (the default `credsStore: desktop` reads Group Containers/group.com.docker
    on every pull, which under launchd makes macOS ask "Python would like to access data from
    other apps" on every call) and the engine socket addressed directly."""
    DOCKER_CFG.mkdir(parents=True, exist_ok=True)
    cfg = DOCKER_CFG / "config.json"
    # the plugins (compose, desktop) live with Docker Desktop: without these dirs the isolated config sees none and
    # every container test failed with "unknown shorthand flag: 'f' in -f"
    want = {"auths": {}, "currentContext": "default", "cliPluginsExtraDirs": [str(Path.home() / ".docker/cli-plugins"), "/Applications/Docker.app/Contents/Resources/cli-plugins"]}
    try:
        have = json.loads(cfg.read_text()) if cfg.exists() else {}
    except Exception:
        have = {}
    if have != {**have, **want}:
        cfg.write_text(json.dumps({**have, **want}, indent=1))
    sock = Path.home() / ".docker/run/docker.sock"
    env = {"DOCKER_CONFIG": str(DOCKER_CFG)}
    if sock.exists():
        env["DOCKER_HOST"] = f"unix://{sock}"
    return env


def ensure_docker() -> None:
    """Recover the engine ourselves, properly: Docker Desktop's own CLI (`docker desktop
    start|restart`), which is built for scripted use. Not `open -a Docker` + osascript quit: on
    2026-09-23 that relaunch from a headless shell never brought the Electron tray up (the failure
    was the launch mechanism, not Docker) and it was misreported as Docker being broken. the user's
    rule: do it properly, don't hand it off. Leaves Docker running afterwards (he uses it)."""
    if docker_ok():
        return
    st = run(["docker", "desktop", "status"], timeout=30)
    running = st.returncode == 0 and re.search(r"Status\s+running", st.stdout)
    action = "restart" if running else "start"
    log(f"dtest: engine unreachable, docker desktop {action}")
    r = run(["docker", "desktop", action, "--timeout", "240"], timeout=300)
    if "unknown flag" in (r.stdout + r.stderr):  # some CLI builds have no --timeout on this verb
        r = run(["docker", "desktop", action], timeout=300)
    if docker_ok():
        return
    raise DockerUnavailable(f"docker desktop {action} did not bring the engine up (rc={r.returncode}): "
                            f"{(r.stdout + r.stderr)[-600:]}")


def _free_fixed_names(names: list[str]) -> str | None:
    """compose/test.yml pins container_name, so names are global across projects. `make test` starts them through
    `compose run` and leaves them running after (restart: always), and stops and removes them itself before its next
    run, so a running one is in use only while a test runner (`compose run`, a one-off) of its project is up
."""
    for n in names:
        st = run(["docker", "inspect", "-f", '{{.State.Running}} {{index .Config.Labels "com.docker.compose.project"}}',
                  n], timeout=30)
        if st.returncode != 0:
            continue
        running, _, project = st.stdout.strip().partition(" ")
        if running == "true":
            ps = run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}",
                      "--filter", "label=com.docker.compose.oneoff=True"], timeout=30)
            if not project or ps.returncode != 0 or ps.stdout.strip():
                return f"{n} is running (your own tests?); not touching it"
            log("dtest: removing an idle test container left by make test", name=n, project=project)
        run(["docker", "rm", "-f", "-v", n], timeout=60)
    return None


@contextmanager
def docker_session(timeout_s: int = 3600):
    LOCK.touch()
    f = open(LOCK, "w")
    t0 = time.time()
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() - t0 > timeout_s:
                raise RuntimeError("docker lock busy for too long")
            time.sleep(5)
    try:
        ensure_docker()
        if run(["docker", "network", "inspect", "shared-network"], timeout=30).returncode != 0:
            run(["docker", "network", "create", "shared-network"], timeout=30)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def _clean(txt: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", txt)


def _summary(txt: str) -> str:
    lines = [l for l in _clean(txt).splitlines() if re.search(r"(passed|failed|error|FAILED|ERROR)\b", l)]
    return "\n".join(lines[-15:])


# ---- scoping ----------------------------------------------------------------------
def schools_tests(wt: Path, changed: list[str]) -> list[str]:
    tests = {f for f in changed if re.search(r"(^|/)test_[^/]+\.py$", f) and (wt / f).exists()}
    mods = []
    for f in changed:
        if f.endswith(".py") and "/tests/" not in f and f.startswith("src/"):
            mods.append(f[:-3].replace("/", "."))
    for m in mods:
        tail = ".".join(m.split(".")[-2:])  # e.g. repositories.classroom
        g = run(["git", "grep", "-l", "-F", tail, "--", "src/**/tests/**/*.py", "src/*/tests/*.py"], cwd=wt, timeout=60)
        tests |= {l for l in g.stdout.splitlines() if re.search(r"test_[^/]+\.py$", l)}
    return sorted(tests)[:30]


def cometa_targets(changed: list[str]) -> list[str] | None:
    """Pytest targets relative to backend/. None = run the whole unit suite (shared code changed)."""
    be = [f[len("backend/"):] for f in changed if f.startswith("backend/") and f.endswith(".py")]
    if not be:
        return []
    targets = set()
    for f in be:
        m = re.match(r"src/modules/([^/]+)/", f)
        if m:
            targets.add(f"src/modules/{m.group(1)}")
        elif f.startswith(("src/core/", "src/tests/", "src/shared/", "conftest")) or "/" not in f[len("src/"):]:
            return None
        else:
            targets.add(str(Path(f).parent))
    return sorted(targets)


# ---- runners ------------------------------------------------------------------------
def _own_work(wt: Path) -> bool:
    """Container tests only for the user's own PRs and harness-built branches, never teammates'
    PRs (his rule, 2026-09-23: "its unnecessary"). Harness/own branches are in the branches table."""
    from . import db
    name = wt.name.split("--", 1)[-1]
    return any(name == r["branch"].replace("/", "-")[:60] or name == r["branch"]
               for r in db.q("SELECT branch FROM branches"))


def run_schools(wt: Path, changed: list[str]) -> dict:
    if not _own_work(wt):
        return {"status": "skip", "detail": f"not {site.first_name()}'s PR or a harness branch: container tests are only for his work"}
    tests = schools_tests(wt, changed)
    if not tests:
        return {"status": "fail", "detail": "no test covers the changed Python files (new behavior needs a test)"}
    env = {"COMPOSE_PROJECT_NAME": "ap-schools"}
    try:
        with docker_session():
            busy = _free_fixed_names(["postgres_school_test", "postgres_cometa_test"])
            if busy:
                return {"status": "fail", "detail": f"CONTAINER TESTS DID NOT RUN: {busy}", "env": True}
            return _run_schools_make(wt, tests, env)
    except DockerUnavailable as e:
        return {"status": "fail", "detail": f"CONTAINER TESTS DID NOT RUN: {e}", "env": True}


def _schools_image_current(wt: Path, env: dict) -> str | None:
    """The test image installs src/requirements/*.txt at build time and `make test` never rebuilds it: a dependency
    added on master failed every run at conftest import. Rebuild when
    the requirements or the Dockerfile change. Returns an error text, or None."""
    import hashlib
    from . import db
    files = sorted((wt / "src" / "requirements").glob("*.txt")) + [wt / "Dockerfile"]
    h = hashlib.sha1(b"".join(f.read_bytes() for f in files if f.exists())).hexdigest()[:16]
    if db.kv_get("dtest:schools_image") == h:
        return None
    log("dtest: schools test image out of date, rebuilding", hash=h)
    b = run(["docker", "compose", "-f", "docker-compose.yml", "-f", "compose/test.yml", "build", "backend_school"],
            cwd=wt, timeout=2400, extra_env=env)
    if b.returncode != 0:
        return "image build failed:\n" + _clean(b.stdout + b.stderr)[-1500:]
    db.kv_set("dtest:schools_image", h)
    return None


def _run_schools_make(wt: Path, tests: list[str], env: dict) -> dict:
    stale = _schools_image_current(wt, env)
    if stale:
        return {"status": "fail", "detail": f"CONTAINER TESTS DID NOT RUN: {stale}", "env": True}
    if True:
        try:
            r = run(["make", "test", f"file={' '.join(tests)}"], cwd=wt, timeout=CFG["quality"]["gate_timeout_s"] * 2,
                    extra_env=env)
            if "address pools have been fully subnetted" in r.stdout + r.stderr:
                # leftover networks from old compose projects exhaust the pool (2026-09-23: 31 networks,
                # 3 in use); unused ones older than a day are safe to drop, compose recreates on demand
                run(["docker", "network", "prune", "-f", "--filter", "until=24h"], timeout=120)
                r = run(["make", "test", f"file={' '.join(tests)}"], cwd=wt,
                        timeout=CFG["quality"]["gate_timeout_s"] * 2, extra_env=env)
        finally:
            run(["docker", "compose", "-f", "docker-compose.yml", "-f", "compose/test.yml", "down", "-v",
                 "--remove-orphans"], cwd=wt, timeout=300, extra_env=env)
    txt = r.stdout + r.stderr
    if r.returncode != 0 and not re.search(r"\d+ (passed|failed)", _clean(txt)):
        return {"status": "fail", "detail": "CONTAINER TESTS DID NOT RUN:\n" + _clean(txt)[-2000:]}
    return {"status": "pass" if r.returncode == 0 else "fail", "tests": tests,
            "detail": _summary(txt) if r.returncode == 0 else _clean(txt)[-3000:]}


def run_cometa(wt: Path, changed: list[str]) -> dict:
    if not _own_work(wt):
        return {"status": "skip", "detail": f"not {site.first_name()}'s PR or a harness branch: container tests are only for his work"}
    targets = cometa_targets(changed)
    if targets == []:
        return {"status": "skip", "detail": "no backend Python changes"}
    envf = wt / "backend/.envs/.env.local"
    if not envf.exists():
        envf.parent.mkdir(parents=True, exist_ok=True)
        envf.write_text(COMETA_ENV)
    env = {"COMPOSE_PROJECT_NAME": "ap-cometa"}
    try:
        with docker_session():
            return _run_cometa_compose(wt, targets, env)
    except DockerUnavailable as e:
        return {"status": "fail", "detail": f"CONTAINER TESTS DID NOT RUN: {e}", "env": True}


def _run_cometa_compose(wt: Path, targets, env: dict) -> dict:
    if True:
        b = run(["docker", "compose", "build", "cometa"], cwd=wt, timeout=1800, extra_env=env)
        if b.returncode != 0:
            return {"status": "fail", "detail": "image build failed:\n" + _clean(b.stdout + b.stderr)[-1500:], "env": True}
        try:
            r = run(["docker", "compose", "run", "--rm", "--no-deps", "cometa", "uv", "run", "pytest", "-n", "auto",
                     "-m", "unit", "--no-cov", "-q", *(targets or [])], cwd=wt,
                    timeout=CFG["quality"]["gate_timeout_s"] * 2, extra_env=env)
        finally:
            run(["docker", "compose", "down", "--remove-orphans"], cwd=wt, timeout=300, extra_env=env)
    txt = r.stdout + r.stderr
    scope = "full unit suite (shared code changed)" if targets is None else ", ".join(targets)
    # pytest exit 5 = no tests collected for the unit marker in that scope
    if r.returncode == 5:
        return {"status": "fail", "detail": f"no unit tests collected in {scope}: new behavior needs a unit test"}
    if r.returncode != 0 and not re.search(r"\d+ (passed|failed)", _clean(txt)):
        return {"status": "fail", "detail": "CONTAINER TESTS DID NOT RUN:\n" + _clean(txt)[-2000:]}
    return {"status": "pass" if r.returncode == 0 else "fail", "scope": scope,
            "detail": (scope + "\n" + _summary(txt)) if r.returncode == 0 else _clean(txt)[-3000:]}


def recipe(repo: str):
    """(gate layer name, runner) of the container-test recipe a repo's config names (container_tests), or None: the
    repo is verified by CI alone."""
    return {"make-test": ("container tests (make test, scoped)", run_schools),
            "compose-unit": ("backend unit tests (CI image, --no-deps, scoped)", run_cometa),
            }.get((CFG["repos"].get(repo) or {}).get("container_tests") or "")


if __name__ == "__main__":  # bin/ap-dtest <repo> <worktree> [changed files...]
    import json
    import sys
    repo, wt = sys.argv[1], Path(sys.argv[2]).resolve()
    changed = sys.argv[3:]
    if not changed:
        from .gates import changed_files
        changed = changed_files(wt, repo)
    if not recipe(repo):
        sys.exit(f"{repo}: no container-test recipe configured (repos.{repo}.container_tests)")
    res = recipe(repo)[1](wt, changed)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    sys.exit(0 if res["status"] in ("pass", "skip") else 1)

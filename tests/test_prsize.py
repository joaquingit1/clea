"""PR size budget: only the lines a reviewer reads count."""
from ap import prsize

CFG = {"quality": {"size_budget": {"bug": 250, "feature": 600}}}


def test_tests_lockfiles_and_generated_are_excluded():
    numstat = "\n".join([
        "40\t8\tbackend/src/academic/grades.py",
        "300\t0\tbackend/tests/academic/test_grades.py",
        "120\t30\tfrontend/apps/dashboard/src/grades/Grades.test.tsx",
        "900\t850\tfrontend/pnpm-lock.yaml",
        "2000\t1500\tfrontend/packages/trpc/src/schools/types.ts",
        "12\t2\tfrontend/apps/dashboard/src/grades/Grades.tsx",
        "5\t5\tuv.lock",
        "3\t0\tbackend/conftest.py",
    ])
    m = prsize.measure(numstat)
    assert m["net"] == 48 + 14
    assert [e["path"] for e in m["counted"]] == ["backend/src/academic/grades.py", "frontend/apps/dashboard/src/grades/Grades.tsx"]
    kinds = {e["path"]: e["kind"] for e in m["excluded"]}
    assert kinds["frontend/pnpm-lock.yaml"] == "lockfile"
    assert kinds["uv.lock"] == "lockfile"
    assert kinds["frontend/packages/trpc/src/schools/types.ts"] == "generated"
    assert kinds["backend/tests/academic/test_grades.py"] == "test"
    assert kinds["frontend/apps/dashboard/src/grades/Grades.test.tsx"] == "test"
    assert kinds["backend/conftest.py"] == "test"


def test_renames_count_only_their_edits_and_classify_by_new_path():
    numstat = "\n".join([
        "0\t0\tsrc/academic/{old_name.py => new_name.py}",
        "4\t2\tsrc/{utils => academic}/rounding.py",
        "7\t1\tsrc/helpers.py => tests/test_helpers.py",
    ])
    m = prsize.measure(numstat)
    assert m["net"] == 6
    paths = {e["path"]: e for e in m["counted"]}
    assert paths["src/academic/new_name.py"]["lines"] == 0 and paths["src/academic/new_name.py"]["renamed"]
    assert "src/academic/rounding.py" in paths
    assert m["excluded"][0]["path"] == "tests/test_helpers.py"


def test_new_path_forms():
    assert prsize.new_path("a/{b => c}/d.py") == "a/c/d.py"
    assert prsize.new_path("a/{ => c}/d.py") == "a/c/d.py"
    assert prsize.new_path("a/{b => }/d.py") == "a/d.py"
    assert prsize.new_path("x.py => y.py") == "y.py"
    assert prsize.new_path("plain/file.py") == "plain/file.py"


def test_binary_files_never_count():
    m = prsize.measure("-\t-\tfrontend/public/logo.png\n10\t0\tsrc/a.py\n-\t-\tdocs/{a.pdf => b.pdf}")
    assert m["net"] == 10
    assert m["binary"] == ["frontend/public/logo.png", "docs/b.pdf"]


def test_limits_by_kind_and_over_flag():
    code = "200\t60\tsrc/academic/grades.py"
    bug = prsize.check(code, "bug", "TASK", CFG)
    assert (bug["kind"], bug["limit"], bug["net"], bug["over"]) == ("bug", 250, 260, True)
    feat = prsize.check(code, "feature", "TASK", CFG)
    assert (feat["kind"], feat["limit"], feat["over"]) == ("feature", 600, False)
    assert prsize.check("250\t0\tsrc/a.py", "incident", "INCIDENT", CFG)["over"] is False  # exactly at the limit
    assert prsize.budget_kind(None, "INCIDENT") == "bug"
    assert prsize.budget_kind("ops", "TASK") == "feature"


def test_config_overrides_and_defaults():
    assert prsize.limit_for("bug", {"quality": {"size_budget": {"bug": 120}}}) == 120
    assert prsize.limit_for("feature", {"quality": {"size_budget": {"bug": 120}}}) == 600
    assert prsize.limit_for("bug", {}) == 250


def test_garbage_lines_are_ignored():
    assert prsize.measure("warning: something\n\n3\tx\tsrc/a.py\n")["net"] == 0

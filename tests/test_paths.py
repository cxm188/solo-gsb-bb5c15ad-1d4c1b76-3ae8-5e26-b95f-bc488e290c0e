"""POST /dependency-paths 的端到端测试。

覆盖：最短路径枚举、菱形等长路径全部保留、更长的绕行路径被丢弃、
依赖环不重复节点且终止、自环、目标为根包（单节点路径）、目标不在锁中
（reachable=false）、锁定清单无效时 409 且不执行路径查询、PEP 503
规范化、PEP 440 版本等价性、marker 激活语义、确定性排序与 422 映射，
以及对既有接口的兼容性。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

ENV = {
    "python_version": "3.11",
    "sys_platform": "linux",
    "platform_machine": "x86_64",
}
WIN_ENV = {
    "python_version": "3.11",
    "sys_platform": "win32",
    "platform_machine": "AMD64",
}


def c(version, deps=None):
    return {"version": version, "dependencies": deps or []}


def d(name, spec="", marker=""):
    return {"name": name, "specifier": spec, "marker": marker}


def paths(repo, lock, target, *, root="app", env=None, **overrides):
    body = {
        "repository": repo,
        "root": root,
        "target_environment": env or ENV,
        "current_lock": lock,
        "target": target,
    }
    body.update(overrides)
    return client.post("/dependency-paths", json=body)


def ok(repo, lock, target, **kw):
    r = paths(repo, lock, target, **kw)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------- #
# 1. 基本路径与最短性
# --------------------------------------------------------------------- #
def test_linear_chain_single_path():
    repo = {
        "app": [c("1.0", [d("liba")])],
        "liba": [c("1.0", [d("libb")])],
        "libb": [c("1.0")],
    }
    lock = {"app": "1.0", "liba": "1.0", "libb": "1.0"}
    body = ok(repo, lock, "libb")
    assert body["status"] == "ok"
    assert body["valid"] is True
    assert body["root"] == "app"
    assert body["target"] == "libb"
    assert body["reachable"] is True
    assert body["paths"] == [["app", "liba", "libb"]]


def test_direct_dependency_path():
    repo = {"app": [c("1.0", [d("libx")])], "libx": [c("2.0")]}
    body = ok(repo, {"app": "1.0", "libx": "2.0"}, "libx")
    assert body["paths"] == [["app", "libx"]]


def test_only_shortest_paths_returned():
    """直达边与更长的绕行路径并存时，只返回最短的直达路径。"""
    repo = {
        "app": [c("1.0", [d("target"), d("mid")])],
        "mid": [c("1.0", [d("target")])],
        "target": [c("1.0")],
    }
    lock = {"app": "1.0", "mid": "1.0", "target": "1.0"}
    body = ok(repo, lock, "target")
    assert body["paths"] == [["app", "target"]]


# --------------------------------------------------------------------- #
# 2. 菱形依赖：等长路径全部保留
# --------------------------------------------------------------------- #
def test_diamond_preserves_all_equal_length_paths():
    repo = {
        "app": [c("1.0", [d("right"), d("left")])],
        "left": [c("1.0", [d("shared", ">=1.0")])],
        "right": [c("1.0", [d("shared", "<2.0")])],
        "shared": [c("1.0")],
    }
    lock = {"app": "1.0", "left": "1.0", "right": "1.0", "shared": "1.0"}
    body = ok(repo, lock, "shared")
    assert body["paths"] == [
        ["app", "left", "shared"],
        ["app", "right", "shared"],
    ]


def test_nested_diamond_keeps_equal_length_paths():
    """两层菱形：中间汇合后再分叉，四条等长路径全部保留。"""
    repo = {
        "app": [c("1.0", [d("x"), d("y")])],
        "x": [c("1.0", [d("z")])],
        "y": [c("1.0", [d("z")])],
        "z": [c("1.0", [d("p"), d("q")])],
        "p": [c("1.0", [d("t")])],
        "q": [c("1.0", [d("t")])],
        "t": [c("1.0")],
    }
    lock = {name: "1.0" for name in ("app", "x", "y", "z", "p", "q", "t")}
    body = ok(repo, lock, "t")
    assert body["paths"] == [
        ["app", "x", "z", "p", "t"],
        ["app", "x", "z", "q", "t"],
        ["app", "y", "z", "p", "t"],
        ["app", "y", "z", "q", "t"],
    ]


def test_paths_sorted_lexicographically():
    """路径按节点序列字典序排序，与声明顺序无关。"""
    repo = {
        "app": [c("1.0", [d("beta"), d("alpha")])],
        "alpha": [c("1.0", [d("t")])],
        "beta": [c("1.0", [d("t")])],
        "t": [c("1.0")],
    }
    lock = {"app": "1.0", "alpha": "1.0", "beta": "1.0", "t": "1.0"}
    body = ok(repo, lock, "t")
    assert body["paths"] == [["app", "alpha", "t"], ["app", "beta", "t"]]


# --------------------------------------------------------------------- #
# 3. 依赖环：不重复节点、不无限遍历
# --------------------------------------------------------------------- #
def test_cycle_terminates_without_repeated_nodes():
    repo = {
        "app": [c("1.0", [d("cyc-a")])],
        "cyc-a": [c("1.0", [d("cyc-b")])],
        "cyc-b": [c("1.0", [d("cyc-a")])],
    }
    lock = {"app": "1.0", "cyc-a": "1.0", "cyc-b": "1.0"}
    body = ok(repo, lock, "cyc-b")
    assert body["paths"] == [["app", "cyc-a", "cyc-b"]]
    # 环上回边不产生折返路径。
    assert ok(repo, lock, "cyc-a")["paths"] == [["app", "cyc-a"]]


def test_cycle_back_to_root():
    repo = {
        "app": [c("1.0", [d("child")])],
        "child": [c("1.0", [d("app")])],
    }
    body = ok(repo, {"app": "1.0", "child": "1.0"}, "child")
    assert body["paths"] == [["app", "child"]]


def test_self_loop_does_not_repeat_or_hang():
    repo = {
        "app": [c("1.0", [d("loopy")])],
        "loopy": [c("1.0", [d("loopy", ">=1.0")])],
    }
    body = ok(repo, {"app": "1.0", "loopy": "1.0"}, "loopy")
    assert body["paths"] == [["app", "loopy"]]


def test_cycle_with_diamond_still_enumerates_shortest_only():
    """环与菱形混合：等长最短路径保留，经环折返的更长路径不出现。"""
    repo = {
        "app": [c("1.0", [d("a"), d("b")])],
        "a": [c("1.0", [d("t")])],
        "b": [c("1.0", [d("t"), d("a")])],
        "t": [c("1.0", [d("b")])],
    }
    lock = {"app": "1.0", "a": "1.0", "b": "1.0", "t": "1.0"}
    body = ok(repo, lock, "t")
    assert body["paths"] == [["app", "a", "t"], ["app", "b", "t"]]


# --------------------------------------------------------------------- #
# 4. 目标为根包 / 目标不可达
# --------------------------------------------------------------------- #
def test_target_is_root_returns_single_node_path():
    repo = {"app": [c("1.0", [d("libx")])], "libx": [c("1.0")]}
    body = ok(repo, {"app": "1.0", "libx": "1.0"}, "app")
    assert body["reachable"] is True
    assert body["paths"] == [["app"]]


def test_target_not_in_lock_unreachable():
    repo = {
        "app": [c("1.0", [d("libx")])],
        "libx": [c("1.0")],
        "ghost": [c("1.0")],
    }
    body = ok(repo, {"app": "1.0", "libx": "1.0"}, "ghost")
    assert body["status"] == "ok"
    assert body["valid"] is True
    assert body["reachable"] is False
    assert body["paths"] == []


def test_target_absent_from_snapshot_unreachable():
    repo = {"app": [c("1.0")]}
    body = ok(repo, {"app": "1.0"}, "nowhere")
    assert body["reachable"] is False
    assert body["paths"] == []


# --------------------------------------------------------------------- #
# 5. 规范化与版本等价性
# --------------------------------------------------------------------- #
def test_target_name_normalized_pep503():
    repo = {
        "app": [c("1.0", [d("Some_Lib")])],
        "some-lib": [c("1.0")],
    }
    body = ok(repo, {"app": "1.0", "Some.Lib": "1.0"}, "SOME-LIB")
    assert body["target"] == "some-lib"
    assert body["paths"] == [["app", "some-lib"]]


def test_pep440_version_equivalence_in_lock():
    """锁中 1.0 与快照 1.0.0 按 PEP 440 等价，照常建图。"""
    repo = {
        "app": [c("1.0.0", [d("libx")])],
        "libx": [c("1.0.0", [d("deep")])],
        "deep": [c("2")],
    }
    body = ok(repo, {"app": "1.0", "libx": "1.0", "deep": "2.0"}, "deep")
    assert body["paths"] == [["app", "libx", "deep"]]


# --------------------------------------------------------------------- #
# 6. marker 语义（与 /audit-lock 同一套已激活依赖）
# --------------------------------------------------------------------- #
def test_inactive_marker_dependency_not_in_graph():
    """linux 环境下 win-only 依赖未激活：目标不可达。"""
    repo = {
        "app": [c("1.0", [
            d("real"),
            d("win-only", marker="sys_platform == 'win32'"),
        ])],
        "real": [c("1.0")],
        "win-only": [c("1.0")],
    }
    body = ok(repo, {"app": "1.0", "real": "1.0"}, "win-only", env=ENV)
    assert body["reachable"] is False
    assert body["paths"] == []


def test_marker_activated_path_follows_environment():
    """同一快照：linux 下路径经过 linux-mid，win32 下经过 win-mid。"""
    repo = {
        "app": [c("1.0", [
            d("linux-mid", marker="sys_platform == 'linux'"),
            d("win-mid", marker="sys_platform == 'win32'"),
        ])],
        "linux-mid": [c("1.0", [d("shared")])],
        "win-mid": [c("1.0", [d("shared")])],
        "shared": [c("1.0")],
    }
    lock_linux = {"app": "1.0", "linux-mid": "1.0", "shared": "1.0"}
    body = ok(repo, lock_linux, "shared", env=ENV)
    assert body["paths"] == [["app", "linux-mid", "shared"]]

    lock_win = {"app": "1.0", "win-mid": "1.0", "shared": "1.0"}
    body_win = ok(repo, lock_win, "shared", env=WIN_ENV)
    assert body_win["paths"] == [["app", "win-mid", "shared"]]


# --------------------------------------------------------------------- #
# 7. 锁定清单无效：409 + 审计问题，不执行路径查询
# --------------------------------------------------------------------- #
def test_invalid_lock_returns_409_with_sorted_issues():
    repo = {
        "app": [c("1.0", [
            d("needs-thing"),
            d("old-lib", ">=2.0"),
        ])],
        "old-lib": [c("1.0"), c("2.0")],
    }
    lock = {
        "app": "1.0",
        "old-lib": "1.0",
        "zz-stale": "1.0",
    }
    r = paths(repo, lock, "old-lib")
    assert r.status_code == 409
    body = r.json()
    assert body["status"] == "invalid_lock"
    assert body["valid"] is False
    assert "paths" not in body
    assert [(i["type"], i["name"]) for i in body["issues"]] == [
        ("missing", "needs-thing"),
        ("constraint_mismatch", "old-lib"),
        ("extraneous", "zz-stale"),
    ]


def test_invalid_lock_issues_match_audit_lock():
    """409 携带的 issues 与 /audit-lock 的输出逐字节一致。"""
    repo = {
        "app": [c("1.0", [d("libx", "==2.0"), d("gone")])],
        "libx": [c("1.0"), c("2.0")],
    }
    lock = {"app": "1.0", "libx": "1.0", "extra": "3.0"}
    audit_body = client.post("/audit-lock", json={
        "repository": repo,
        "root": "app",
        "target_environment": ENV,
        "current_lock": lock,
    }).json()
    r = paths(repo, lock, "libx")
    assert r.status_code == 409
    assert r.json()["issues"] == audit_body["issues"]
    assert audit_body["valid"] is False


def test_root_specifier_mismatch_blocks_path_query():
    """根约束不满足同样使锁无效：409 且不返回路径。"""
    repo = {"app": [c("1.0"), c("2.0")]}
    r = paths(repo, {"app": "1.0"}, "app", specifier=">=2.0")
    assert r.status_code == 409
    body = r.json()
    assert body["issues"][0]["type"] == "constraint_mismatch"
    assert body["issues"][0]["name"] == "app"
    assert "paths" not in body


def test_unavailable_locked_version_blocks_path_query():
    repo = {"app": [c("1.0", [d("libx")])], "libx": [c("1.0")]}
    r = paths(repo, {"app": "1.0", "libx": "9.0"}, "libx")
    assert r.status_code == 409
    assert r.json()["issues"][0]["type"] == "unavailable"


# --------------------------------------------------------------------- #
# 8. 确定性
# --------------------------------------------------------------------- #
def test_deterministic_across_repeated_calls_and_key_order():
    repo = {
        "app": [c("1.0", [d("libb"), d("liba")])],
        "liba": [c("1.0", [d("shared")])],
        "libb": [c("1.0", [d("shared")])],
        "shared": [c("1.0"), c("2.0")],
    }
    lock = {"app": "1.0", "liba": "1.0", "libb": "1.0", "shared": "2.0"}
    body1 = paths(repo, lock, "shared").json()
    body2 = paths(
        dict(reversed(list(repo.items()))),
        dict(reversed(list(lock.items()))),
        "shared",
    ).json()
    assert body1 == body2
    assert body1["paths"] == [
        ["app", "liba", "shared"],
        ["app", "libb", "shared"],
    ]


# --------------------------------------------------------------------- #
# 9. 422：非法请求 / 快照 / current_lock
# --------------------------------------------------------------------- #
def test_missing_target_field_422():
    r = client.post("/dependency-paths", json={
        "repository": {"app": [c("1.0")]},
        "root": "app",
        "target_environment": ENV,
        "current_lock": {"app": "1.0"},
    })
    assert r.status_code == 422


def test_invalid_target_name_422():
    r = paths({"app": [c("1.0")]}, {"app": "1.0"}, "-bad-name-")
    assert r.status_code == 422


def test_extra_field_forbidden_422():
    r = paths({"app": [c("1.0")]}, {"app": "1.0"}, "app", unknown_field=1)
    assert r.status_code == 422


def test_invalid_lock_version_422():
    r = paths({"app": [c("1.0")]}, {"app": "not-a-version"}, "app")
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "invalid_snapshot"
    assert "PEP 440" in body["message"]


def test_conflicting_lock_versions_422():
    r = paths({"app": [c("1.0")]}, {"Lib_X": "1.0", "lib-x": "2.0"}, "lib-x")
    assert r.status_code == 422
    assert r.json()["status"] == "invalid_snapshot"


def test_unknown_root_422():
    r = paths({"app": [c("1.0")]}, {}, "app", root="missing")
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "invalid_snapshot"
    assert "missing" in body["message"]


def test_invalid_marker_422():
    repo = {"app": [c("1.0", [d("x", marker="os_name == 'posix'")])]}
    r = paths(repo, {"app": "1.0", "x": "1.0"}, "x")
    assert r.status_code == 422
    assert "os_name" in r.json()["message"]


def test_missing_current_lock_422():
    r = client.post("/dependency-paths", json={
        "repository": {"app": [c("1.0")]},
        "root": "app",
        "target_environment": ENV,
        "target": "app",
    })
    assert r.status_code == 422


# --------------------------------------------------------------------- #
# 10. 不影响既有接口
# --------------------------------------------------------------------- #
def test_existing_endpoints_unaffected():
    repo = {
        "app": [c("1.0", [d("libx")])],
        "libx": [c("1.0"), c("2.0")],
    }
    body = {"repository": repo, "root": "app", "target_environment": ENV}
    before = client.post("/resolve", json=body).json()
    paths(repo, {"app": "1.0", "libx": "2.0"}, "libx")
    after = client.post("/resolve", json=body).json()
    assert before == after
    audit_body = client.post(
        "/audit-lock", json={**body, "current_lock": {"app": "1.0", "libx": "2.0"}}
    ).json()
    assert audit_body == {"status": "ok", "valid": True, "issues": []}

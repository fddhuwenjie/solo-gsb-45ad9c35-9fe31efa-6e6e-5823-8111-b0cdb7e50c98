from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)

PLAIN_FENCE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]],
}

INSIDE = (0.005, 0.005)
OUTSIDE = (0.02, 0.02)

T0, T1 = 100, 300  # 清场窗口（秒）


def pt(device, sec, inside=True):
    lat, lon = INSIDE if inside else OUTSIDE
    return {
        "device_id": device,
        "ts": (BASE + timedelta(seconds=sec)).isoformat(),
        "lat": lat,
        "lon": lon,
    }


def iso(sec):
    return (BASE + timedelta(seconds=sec)).isoformat()


def ts(sec):
    return BASE + timedelta(seconds=sec)


def create_fence(client):
    r = client.post("/fences", json={"name": "清场测试", "geometry": PLAIN_FENCE})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def submit(client, points):
    r = client.post("/points", json={"points": points})
    assert r.status_code == 200, r.text
    return r.json()


def create_task(client, fence_id, start=T0, end=T1, name="应急清场"):
    r = client.post(
        f"/fences/{fence_id}/clearance-tasks",
        json={"name": name, "start": iso(start), "end": iso(end)},
    )
    assert r.status_code == 201, r.text
    return r.json()


def report(client, fence_id, task_id):
    r = client.get(f"/fences/{fence_id}/clearance-tasks/{task_id}/report")
    assert r.status_code == 200, r.text
    return r.json()


def by_device(rep):
    return {row["device_id"]: row for row in rep["results"]}


def test_clearance_full_report_semantics(client):
    fence_id = create_fence(client)
    submit(client, [
        # start 前进入、窗口内离开 -> cleared
        pt("cleared-before", 0), pt("cleared-before", 200, inside=False),
        # 截止时刻仍在场 -> remaining
        pt("still-inside", 50),
        # 恰在开始时刻 enter -> 计入名单，截止前离开 -> cleared
        pt("enter-at-start", T0), pt("enter-at-start", 250, inside=False),
        # 恰在开始时刻 leave -> 排除出名单
        pt("leave-at-start", 0), pt("leave-at-start", T0, inside=False),
        # 窗口内离开后又进入，未再次离开 -> remaining
        pt("reenter-rem", 80), pt("reenter-rem", 150, inside=False), pt("reenter-rem", 200),
        # 窗口内离开后又进入，再次离开并在外至截止 -> cleared，last_leave 取第二次
        pt("reenter-clear", 80), pt("reenter-clear", 150, inside=False),
        pt("reenter-clear", 200), pt("reenter-clear", 260, inside=False),
        # start 前已离开、窗口内再进入再离开 -> 不在起始名单，不出现
        pt("outsider", 0), pt("outsider", 40, inside=False),
        pt("outsider", 150), pt("outsider", 220, inside=False),
        # 窗口后才进入 -> 不出现
        pt("future", 400),
        # 恰在截止时刻 leave -> 边界沿用 [enter, leave)，cleared
        pt("leave-at-end", 0), pt("leave-at-end", T1, inside=False),
        # 恰在截止时刻 enter（在窗口内曾在场）-> remaining
        pt("back-at-end", 0), pt("back-at-end", 200, inside=False), pt("back-at-end", T1),
    ])

    task = create_task(client, fence_id)
    rep = report(client, fence_id, task["id"])

    assert rep["initial_count"] == 7
    assert rep["cleared_count"] == 4
    assert rep["remaining_count"] == 3
    assert [row["device_id"] for row in rep["results"]] == sorted(by_device(rep))

    rows = by_device(rep)
    assert "leave-at-start" not in rows
    assert "outsider" not in rows
    assert "future" not in rows

    assert rows["cleared-before"]["status"] == "cleared"
    assert datetime.fromisoformat(rows["cleared-before"]["last_leave_ts"]) == ts(200)
    assert datetime.fromisoformat(rows["enter-at-start"]["last_leave_ts"]) == ts(250)
    assert datetime.fromisoformat(rows["leave-at-end"]["last_leave_ts"]) == ts(T1)
    # 最后有效离开取再次离开的时刻（260），而非首次离开（150）
    assert datetime.fromisoformat(rows["reenter-clear"]["last_leave_ts"]) == ts(260)

    for d in ("still-inside", "reenter-rem", "back-at-end"):
        assert rows[d]["status"] == "remaining"
        assert rows[d]["last_leave_ts"] is None


def test_results_sorted_by_device_id(client):
    fence_id = create_fence(client)
    submit(client, [
        pt("dev-10", 0), pt("dev-10", 200, inside=False),
        pt("dev-02", 0),
        pt("dev-1", 0), pt("dev-1", 200, inside=False),
        pt("a", 0),
    ])
    task = create_task(client, fence_id)
    rep = report(client, fence_id, task["id"])
    assert [r["device_id"] for r in rep["results"]] == [
        "a", "dev-02", "dev-1", "dev-10"
    ]


def test_empty_roster_when_no_one_present(client):
    fence_id = create_fence(client)
    task = create_task(client, fence_id)
    rep = report(client, fence_id, task["id"])
    assert rep["results"] == []
    assert (rep["initial_count"], rep["cleared_count"], rep["remaining_count"]) == (0, 0, 0)


def test_task_validation_404_and_crud(client):
    # 围栏不存在 -> 404
    r = client.post(
        "/fences/999/clearance-tasks",
        json={"name": "x", "start": iso(0), "end": iso(10)},
    )
    assert r.status_code == 404
    assert client.get("/fences/999/clearance-tasks").status_code == 404

    fence_id = create_fence(client)

    # 开始必须早于截止
    for bad_end in (T0, 50):
        r = client.post(
            f"/fences/{fence_id}/clearance-tasks",
            json={"name": "bad", "start": iso(T0), "end": iso(bad_end)},
        )
        assert r.status_code == 422

    # 名称不能为空
    r = client.post(
        f"/fences/{fence_id}/clearance-tasks",
        json={"name": "", "start": iso(T0), "end": iso(T1)},
    )
    assert r.status_code == 422

    t1 = create_task(client, fence_id, name="任务一")
    t2 = create_task(client, fence_id, start=200, end=400, name="任务二")
    assert [t["id"] for t in client.get(
        f"/fences/{fence_id}/clearance-tasks").json()] == [t1["id"], t2["id"]]

    # 任务不属于该围栏 / 不存在 -> 404
    other = create_fence(client)
    assert client.get(
        f"/fences/{other}/clearance-tasks/{t1['id']}/report").status_code == 404
    assert client.get(
        f"/fences/{fence_id}/clearance-tasks/9999/report").status_code == 404

    assert client.delete(
        f"/fences/{fence_id}/clearance-tasks/{t1['id']}").status_code == 204
    assert client.delete(
        f"/fences/{fence_id}/clearance-tasks/{t1['id']}").status_code == 404
    assert client.get(
        f"/fences/{fence_id}/clearance-tasks/{t2['id']}/report").status_code == 200


def test_report_rebuilt_each_query_after_backfill(client):
    fence_id = create_fence(client)
    submit(client, [
        # dev-never：初始在场，没有离开点 -> remaining
        pt("dev-never", 0),
        # dev-late：初始在场，补录前没有更早离开点
        pt("dev-late", 0), pt("dev-late", 400, inside=False),
        # dev-gone：初始在场，窗口内离开 -> cleared
        pt("dev-gone", 0), pt("dev-gone", 200, inside=False),
    ])
    task = create_task(client, fence_id)

    rep = report(client, fence_id, task["id"])
    assert rep["initial_count"] == 3
    rows = by_device(rep)
    assert rows["dev-never"]["status"] == "remaining"
    assert rows["dev-late"]["status"] == "remaining"
    assert rows["dev-gone"]["status"] == "cleared"

    # 补录乱序点：
    # dev-never 在窗口内补一个离开点 -> cleared
    # dev-late 实际 250 已离开（补录 250 的外部点）-> cleared
    # dev-gone 实际 200 离开后 220 又进入且未再离开 -> remaining
    submit(client, [
        pt("dev-never", 250, inside=False),
        pt("dev-late", 250, inside=False),
        pt("dev-gone", 220),
    ])

    rep = report(client, fence_id, task["id"])
    assert rep["initial_count"] == 3
    assert rep["cleared_count"] == 2
    assert rep["remaining_count"] == 1
    rows = by_device(rep)
    assert rows["dev-never"]["status"] == "cleared"
    assert datetime.fromisoformat(rows["dev-never"]["last_leave_ts"]) == ts(250)
    assert rows["dev-late"]["status"] == "cleared"
    assert datetime.fromisoformat(rows["dev-late"]["last_leave_ts"]) == ts(250)
    assert rows["dev-gone"]["status"] == "remaining"
    assert rows["dev-gone"]["last_leave_ts"] is None


def test_backfill_earlier_enter_adds_device_to_roster(client):
    fence_id = create_fence(client)
    # 初始看起来窗口内无人在场
    submit(client, [pt("dev", 0, inside=False), pt("dev", 200)])
    task = create_task(client, fence_id)
    assert report(client, fence_id, task["id"])["initial_count"] == 0

    # 补录 start 之前的进入点 -> dev 在 start 已在场，名单同步更新
    submit(client, [pt("dev", 20)])
    rep = report(client, fence_id, task["id"])
    assert rep["initial_count"] == 1
    assert rep["remaining_count"] == 1
    rows = by_device(rep)
    assert rows["dev"]["status"] == "remaining"


def test_delete_fence_removes_clearance_tasks(client):
    fence_id = create_fence(client)
    task = create_task(client, fence_id)
    assert client.delete(f"/fences/{fence_id}").status_code == 204
    # 围栏删除后任务一并删除（报告 404）
    assert client.get(
        f"/fences/{fence_id}/clearance-tasks/{task['id']}/report"
    ).status_code == 404


def test_naive_datetimes_interpreted_as_utc(client):
    fence_id = create_fence(client)
    submit(client, [
        pt("dev", 0), pt("dev", 200, inside=False),
    ])
    # 不带时区的时间按 UTC 处理
    r = client.post(
        f"/fences/{fence_id}/clearance-tasks",
        json={
            "name": "naive",
            "start": (BASE + timedelta(seconds=T0)).replace(tzinfo=None).isoformat(),
            "end": (BASE + timedelta(seconds=T1)).replace(tzinfo=None).isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]
    rep = report(client, fence_id, task_id)
    assert rep["initial_count"] == 1
    assert datetime.fromisoformat(rep["start"]) == ts(T0)

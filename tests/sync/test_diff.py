from netbox_windows_dhcp_plugin.sync.diff import KIND_ORDER, Change, Record, diff, index


def rec(kind, key, **fields):
    return Record(kind, key, fields)


def test_identical_sides_give_no_changes():
    side = [rec("scope", "10.1.0.0", name="lan"), rec("reservation", "10.1.0.0/10.1.0.5", name="pc")]
    assert diff(side, list(side)) == []


def test_create_update_delete():
    source = [rec("scope", "10.1.0.0", name="lan"), rec("scope", "10.2.0.0", name="new")]
    target = [rec("scope", "10.1.0.0", name="old"), rec("scope", "10.3.0.0", name="gone")]
    changes = diff(source, target)
    assert changes == [
        Change("delete", "scope", "10.3.0.0", before={"name": "gone"}, after=None),
        Change("update", "scope", "10.1.0.0", before={"name": "old"}, after={"name": "lan"}),
        Change("create", "scope", "10.2.0.0", before=None, after={"name": "new"}),
    ]


def test_update_lists_changed_fields():
    source = [rec("scope", "10.1.0.0", name="lan", state="active")]
    target = [rec("scope", "10.1.0.0", name="lan", state="inactive")]
    (change,) = diff(source, target)
    assert change.changed_fields == ["state"]


def test_deletes_run_children_first_and_creates_parents_first():
    source = [rec("option", "10.1.0.0/6", value=["1.1.1.1"]), rec("scope", "10.1.0.0", name="a")]
    target = [rec("reservation", "10.9.0.0/10.9.0.5"), rec("scope", "10.9.0.0")]
    order = [(c.action, c.kind) for c in diff(source, target)]
    assert order == [
        ("delete", "reservation"),
        ("delete", "scope"),
        ("create", "scope"),
        ("create", "option"),
    ]


def test_kind_order_is_dependency_order():
    assert KIND_ORDER.index("class") < KIND_ORDER.index("optiondef") < KIND_ORDER.index("option")
    assert KIND_ORDER.index("scope") < KIND_ORDER.index("reservation") < KIND_ORDER.index("option")


def test_kinds_filter_ignores_other_kinds():
    source = [rec("scope", "10.1.0.0", name="a"), rec("class", "user/x", data="x")]
    assert [c.kind for c in diff(source, [], kinds={"scope"})] == ["scope"]


def test_custom_equality_per_field():
    source = [rec("option", "server/46", value=["0x8"])]
    target = [rec("option", "server/46", value=["8"])]

    def equal(kind, field, a, b):
        if field == "value":
            return [int(x, 0) for x in a] == [int(x, 0) for x in b]
        return a == b

    assert diff(source, target, equal=equal) == []
    assert len(diff(source, target)) == 1


def test_record_equality_has_type_context_and_preserves_original_fields():
    source = [rec("option", "server/46", type="number", value=["0x8"])]
    target = [rec("option", "server/46", type="number", value=["8"])]

    def equal_records(kind, a, b):
        assert kind == "option"
        return a["type"] == b["type"] and int(a["value"][0], 0) == int(b["value"][0], 0)

    assert diff(source, target, equal_records=equal_records) == []
    target = [rec("option", "server/46", type="number", value=["9"])]
    (change,) = diff(source, target, equal_records=equal_records)
    assert change.after == source[0].fields
    assert change.before == target[0].fields


def test_fields_missing_on_one_side_count_as_changes():
    (change,) = diff([rec("scope", "a", name="x", delay=None)], [rec("scope", "a", name="x")])
    assert change.changed_fields == ["delay"]


def test_duplicate_keys_are_rejected():
    try:
        index([rec("scope", "a"), rec("scope", "a")])
    except ValueError as exc:
        assert "scope a" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_change_round_trips_through_dict():
    change = Change("update", "scope", "a", before={"name": "x"}, after={"name": "y"})
    assert Change.from_dict(change.to_dict()) == change

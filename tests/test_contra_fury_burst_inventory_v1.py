from pathlib import Path

from o2o_dps.contra_fury_burst_inventory_v1 import (
    CONFIGURED_NOT_EMITTED,
    build_contra_fury_burst_inventory_v1,
    resolve_contra_fury_burst_guide_v1,
)
from o2o_dps.sim_bridge import ActionRef, AvailableAction


WORKSPACE = Path(__file__).resolve().parents[2]


def _available(index: int, action: ActionRef, *, legal: bool = True) -> AvailableAction:
    return AvailableAction(
        index=index,
        action=action,
        label=f"action-{index}",
        legal=legal,
        ready_in_ms=0,
        triggers_gcd=False,
    )


def test_current_deployed_toc_loads_minified_runtime_not_readable_counterpart() -> None:
    entries = [
        line.strip()
        for line in (WORKSPACE / "Contra" / "Contra.toc").read_text(
            encoding="utf-8-sig"
        ).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert entries == ["Contra.lua"]

    document = build_contra_fury_burst_inventory_v1().to_dict()
    assert document["runtime_authority"]["deployed"]["toc_entry"] == "Contra.lua"
    assert document["runtime_authority"]["deployed"][
        "readable_counterpart_loaded_by_toc"
    ] is False


def test_runtime_burst_order_and_sweeping_strikes_configuration_gap_are_preserved() -> None:
    runtime = (WORKSPACE / "Contra" / "Contra.lua").read_text(encoding="utf-8-sig")
    start = runtime.index("function Contra.ZS_BAOF1()")
    end = runtime.index("function Contra.ZS_BAOF2()", start)
    burst_one = runtime[start:end]
    ordered_needles = (
        'ContraZSCast("鲁莽")',
        'ContraZSCast("死亡之愿")',
        'ContraZSCast("感知")',
        "UseInventoryItem(13)",
        "UseInventoryItem(14)",
        'Contra.UseItemByName("强效怒气药水")',
        'Contra.UseItemByName("魂能之速")',
        'Contra.UseItemByName("地精工兵炸弹")',
        'Contra.UseItemByName("加速药水")',
        'Contra.UseItemByName("暴怒药水")',
        'Contra.UseItemByName("急速生长药剂")',
    )
    positions = [burst_one.index(needle) for needle in ordered_needles]
    assert positions == sorted(positions)
    assert 'ContraZSCast("横扫攻击")' not in burst_one

    inventory = build_contra_fury_burst_inventory_v1()
    sweeping = next(
        row for row in inventory.actions if row.action_id == "warrior.sweeping_strikes"
    )
    assert sweeping.execution_status == CONFIGURED_NOT_EMITTED
    assert sweeping.executable is False


def test_named_items_and_trinket_slots_do_not_invent_concrete_item_ids() -> None:
    inventory = build_contra_fury_burst_inventory_v1()
    unresolved = [
        row
        for row in inventory.actions
        if row.locator_kind in {"NAMED_ITEM", "EQUIPPED_SLOT"}
    ]
    assert unresolved
    assert all(row.item_id is None for row in unresolved)
    assert {row.inventory_slot for row in unresolved if row.inventory_slot} == {13, 14}
    potion_groups = {
        row.cooldown_group_hint
        for row in unresolved
        if row.category == "POTION"
    }
    assert potion_groups == {"combat_potion"}
    assert all(row.cooldown_ms is None for row in inventory.actions)


def test_guide_resolution_ranks_resolved_sinks_but_keeps_every_legal_action() -> None:
    death_wish = ActionRef(spell_id=12328)
    potion = ActionRef(item_id=90001)
    upper_trinket = ActionRef(item_id=90002)
    sweeping = ActionRef(spell_id=12292)
    unknown = ActionRef(spell_id=99999)
    illegal = ActionRef(spell_id=1719)
    available = (
        _available(0, death_wish),
        _available(1, potion),
        _available(2, upper_trinket),
        _available(3, sweeping),
        _available(4, unknown),
        _available(5, illegal, legal=False),
    )

    result = resolve_contra_fury_burst_guide_v1(
        available,
        named_item_ids={"item.mighty_rage_potion": 90001},
        trinket_item_ids={13: 90002},
    )

    assert set(result.action_priorities) == {
        death_wish,
        potion,
        upper_trinket,
        sweeping,
        unknown,
    }
    assert result.action_priorities[death_wish] > 0
    assert result.action_priorities[upper_trinket] > result.action_priorities[potion]
    assert result.action_priorities[sweeping] == 0
    assert result.action_priorities[unknown] == 0
    assert illegal not in result.action_priorities
    assert "warrior.sweeping_strikes" in result.configured_not_emitted_action_ids
    assert "item.juju_flurry" in result.unresolved_action_ids

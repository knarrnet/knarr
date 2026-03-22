from knarr.commerce.settlement_engine import SettlementInput, evaluate_settlement


def test_netting_and_settlement_engine_produce_same_settlement_amount():
    initial_credit = 10.0
    min_balance = -10.0
    balance = -8.0
    soft_target = 0.5
    credit_range = initial_credit - min_balance

    netting_target_balance = initial_credit - (soft_target * credit_range)
    netting_amount = netting_target_balance - balance
    utilization = (initial_credit - balance) / credit_range

    result = evaluate_settlement(
        SettlementInput(
            peer_key="ab" * 32,
            balance=balance,
            prepaid=0.0,
            pub_tab=0.0,
            soft_limit=initial_credit,
            hard_limit=min_balance,
            credit_limit=credit_range,
            tasks_provided=0,
            tasks_consumed=0,
            utilization=utilization,
        ),
        {"soft_threshold": 0.8, "soft_target": soft_target, "min_settlement_amount": 0.0},
    )

    assert result.action == "settle"
    assert result.amount == netting_amount

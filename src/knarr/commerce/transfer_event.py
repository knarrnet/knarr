from dataclasses import dataclass
from enum import Enum


class ConfirmationStatus(Enum):
    MEMPOOL = "mempool"
    INCLUDED = "included"
    FINALIZED = "finalized"


@dataclass(frozen=True)
class TransferEvent:
    chain_id: str
    tx_hash: str
    tx_index: int
    from_address: str
    to_address: str
    amount: int
    denom: str
    decimals: int
    confirmation: ConfirmationStatus
    slot: int = 0
    block_time: int = 0

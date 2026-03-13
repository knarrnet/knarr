import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any, Optional

from knarr.commerce.transfer_event import ConfirmationStatus, TransferEvent
from knarr.plugins.wallet.solana_rpc_plugin import _rpc_call

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PollResult:
    events: list[TransferEvent]
    latest_signature: Optional[str]
    rpc_ok: bool


class SolanaWatcher:
    def __init__(self, chain_id: str, chain_cfg: dict):
        self.chain_id = chain_id
        self.rpc_url = chain_cfg.get("rpc_url", "https://api.mainnet-beta.solana.com")
        self.commitment = chain_cfg.get("commitment", "finalized")
        self.min_amount_lamports = int(chain_cfg.get("min_amount_lamports", 10_000))
        self.token_mints = dict(chain_cfg.get("token_mints", {}))
        self._mint_to_symbol = {mint: symbol for symbol, mint in self.token_mints.items()}

    async def _call_rpc(self, method: str, params: list) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _rpc_call, self.rpc_url, method, params)

    async def poll_address(self, address: str, last_signature: Optional[str]) -> PollResult:
        opts: dict[str, Any] = {"commitment": self.commitment, "limit": 100}
        if last_signature:
            opts["until"] = last_signature

        resp = await self._call_rpc("getSignaturesForAddress", [address, opts])
        if "error" in resp:
            raise RuntimeError(f"getSignaturesForAddress failed: {resp['error']}")

        signatures = resp.get("result") or []
        if not isinstance(signatures, list):
            return PollResult(events=[], latest_signature=last_signature, rpc_ok=True)

        latest = last_signature
        if signatures:
            first = signatures[0]
            if isinstance(first, dict):
                latest = first.get("signature") or latest

        events: list[TransferEvent] = []
        for sig_info in reversed(signatures):
            if not isinstance(sig_info, dict):
                continue
            signature = sig_info.get("signature")
            if not signature:
                continue
            tx = await self._fetch_transaction(signature)
            if not tx:
                continue
            events.extend(self._parse_transaction(address, signature, tx))

        return PollResult(events=events, latest_signature=latest, rpc_ok=True)

    async def _fetch_transaction(self, signature: str) -> Optional[dict]:
        resp = await self._call_rpc(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": self.commitment,
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
        if "error" in resp:
            log.warning("getTransaction failed for %s: %s", signature, resp["error"])
            return None
        tx = resp.get("result")
        return tx if isinstance(tx, dict) else None

    def _parse_transaction(self, watched_address: str, signature: str, tx: dict) -> list[TransferEvent]:
        slot = self._parse_non_negative_int(tx.get("slot"), default=0)
        block_time = self._parse_non_negative_int(tx.get("blockTime"), default=0)
        out: list[TransferEvent] = []

        for tx_index, inst in enumerate(self._all_instructions(tx)):
            if not isinstance(inst, dict):
                continue
            parsed = inst.get("parsed")
            if not isinstance(parsed, dict):
                continue

            program = inst.get("program")
            inst_type = parsed.get("type")
            info = parsed.get("info")
            if not isinstance(info, dict):
                continue

            if program == "system" and inst_type == "transfer":
                amount = self._parse_positive_amount(info.get("lamports"))
                if amount is None or amount < self.min_amount_lamports:
                    continue
                source = info.get("source", "")
                destination = info.get("destination", "")
                if watched_address not in {source, destination}:
                    continue
                out.append(
                    TransferEvent(
                        chain_id=self.chain_id,
                        tx_hash=signature,
                        tx_index=tx_index,
                        from_address=source,
                        to_address=destination,
                        amount=amount,
                        denom="SOL",
                        decimals=9,
                        confirmation=ConfirmationStatus.FINALIZED,
                        slot=slot,
                        block_time=block_time,
                    )
                )
                continue

            if program == "spl-token" and inst_type in {"transfer", "transferChecked"}:
                amount_value = info.get("amount")
                token_amount = info.get("tokenAmount")
                if amount_value is None and isinstance(token_amount, dict):
                    amount_value = token_amount.get("amount")
                amount = self._parse_positive_amount(amount_value)
                if amount is None or amount < self.min_amount_lamports:
                    continue

                source = info.get("source", "")
                destination = info.get("destination", "")
                if watched_address not in {source, destination}:
                    continue

                mint = info.get("mint") or self._infer_mint(tx, source, destination)
                denom = self._mint_to_symbol.get(mint, "SPL")
                decimals = self._infer_decimals(tx, mint, token_amount)
                out.append(
                    TransferEvent(
                        chain_id=self.chain_id,
                        tx_hash=signature,
                        tx_index=tx_index,
                        from_address=source,
                        to_address=destination,
                        amount=amount,
                        denom=denom,
                        decimals=decimals,
                        confirmation=ConfirmationStatus.FINALIZED,
                        slot=slot,
                        block_time=block_time,
                        mint_address=mint,
                    )
                )

        return out

    @staticmethod
    def _all_instructions(tx: dict) -> list[dict]:
        message = tx.get("transaction", {}).get("message", {})
        top_level = message.get("instructions", []) or []
        instructions: list[dict] = [inst for inst in top_level if isinstance(inst, dict)]

        meta = tx.get("meta", {}) or {}
        for group in meta.get("innerInstructions", []) or []:
            if not isinstance(group, dict):
                continue
            for inst in group.get("instructions", []) or []:
                if isinstance(inst, dict):
                    instructions.append(inst)
        return instructions

    def _infer_mint(self, tx: dict, source: str, destination: str) -> str:
        balances = tx.get("meta", {}).get("postTokenBalances", []) or []
        for balance in balances:
            if not isinstance(balance, dict):
                continue
            mint = balance.get("mint", "")
            owner = balance.get("owner", "")
            if owner in {source, destination} and mint in self._mint_to_symbol:
                return mint
        return ""

    @staticmethod
    def _infer_decimals(tx: dict, mint: str, token_amount: Any) -> int:
        if isinstance(token_amount, dict):
            decimals = SolanaWatcher._parse_non_negative_int(token_amount.get("decimals"), default=-1)
            if decimals >= 0:
                return decimals

        balances = tx.get("meta", {}).get("postTokenBalances", []) or []
        for balance in balances:
            if not isinstance(balance, dict):
                continue
            if mint and balance.get("mint") != mint:
                continue
            ui = balance.get("uiTokenAmount")
            if isinstance(ui, dict):
                decimals = SolanaWatcher._parse_non_negative_int(ui.get("decimals"), default=-1)
                if decimals >= 0:
                    return decimals
        return 0

    @staticmethod
    def _parse_non_negative_int(raw: Any, default: int = 0) -> int:
        parsed = SolanaWatcher._parse_positive_amount(raw)
        if parsed is None:
            if raw in (0, "0", 0.0):
                return 0
            return default
        return parsed

    @staticmethod
    def _parse_positive_amount(raw: Any) -> Optional[int]:
        if raw is None or isinstance(raw, bool):
            return None

        try:
            if isinstance(raw, int):
                amount = raw
            elif isinstance(raw, float):
                if not math.isfinite(raw):
                    return None
                amount = int(raw)
            elif isinstance(raw, str):
                text = raw.strip()
                if not text:
                    return None
                if "." in text or "e" in text.lower():
                    fval = float(text)
                    if not math.isfinite(fval):
                        return None
                    amount = int(fval)
                else:
                    amount = int(text, 10)
            else:
                return None
        except (TypeError, ValueError, OverflowError):
            return None

        if amount <= 0:
            return None
        return amount

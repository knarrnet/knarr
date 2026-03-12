import asyncio
import hashlib
import json
import logging
import math
import os
import re
import ssl
import time
import urllib.request
from typing import Any, Callable, Optional

from .sidecar import TaskContext

logger = logging.getLogger(__name__)


class MailHandlers:
    """Consumer-side system mail handlers extracted from DHTNode."""

    def __init__(self, storage, bus, asset_dir, sidecar):
        self.storage = storage
        self.bus = bus
        self.asset_dir = asset_dir or ""
        self.sidecar = sidecar
        self._enqueue_write: Optional[Callable] = None
        self._get_initial_trust: Optional[Callable[[str], float]] = None
        self._check_credit_restored: Optional[Callable[[str, float, float], None]] = None
        self._store_asset_cb: Optional[Callable[[bytes], str]] = None
        self._signing_key = getattr(sidecar, "_signing_key", None)
        self._public_key_hex = ""
        self._initial_credit = 0.0
        self._debug = False
        self._handler_pool = None

    def bind_runtime(
        self,
        *,
        bus=None,
        asset_dir: Optional[str] = None,
        sidecar=None,
        enqueue_write: Optional[Callable] = None,
        get_initial_trust: Optional[Callable[[str], float]] = None,
        check_credit_restored: Optional[Callable[[str, float, float], None]] = None,
        store_asset_cb: Optional[Callable[[bytes], str]] = None,
        signing_key=None,
        public_key_hex: Optional[str] = None,
        initial_credit: Optional[float] = None,
        debug: Optional[bool] = None,
        handler_pool=None,
    ) -> None:
        if bus is not None:
            self.bus = bus
        if asset_dir is not None:
            self.asset_dir = asset_dir
        if sidecar is not None:
            self.sidecar = sidecar
        if enqueue_write is not None:
            self._enqueue_write = enqueue_write
        if get_initial_trust is not None:
            self._get_initial_trust = get_initial_trust
        if check_credit_restored is not None:
            self._check_credit_restored = check_credit_restored
        if store_asset_cb is not None:
            self._store_asset_cb = store_asset_cb
        if signing_key is not None:
            self._signing_key = signing_key
        elif sidecar is not None and getattr(sidecar, "_signing_key", None) is not None:
            self._signing_key = sidecar._signing_key
        if public_key_hex is not None:
            self._public_key_hex = public_key_hex
        if initial_credit is not None:
            self._initial_credit = float(initial_credit)
        if debug is not None:
            self._debug = bool(debug)
        if handler_pool is not None:
            self._handler_pool = handler_pool

    async def _write(self, fn: Callable, *args):
        if self._enqueue_write is not None:
            return await self._enqueue_write(fn, *args)
        return fn(*args)

    async def _handle_task_result_mail(self, item: dict):
        body = item.get("body", {})
        job_id = body.get("job_id")
        if not job_id:
            return
        job = self.storage.get_async_job(job_id)
        if not job:
            return
        if not job.get("provider_node_id"):
            if self._debug:
                logger.debug("MAIL_TASK_RESULT_SKIP job=%s reason=local_provider", job_id[:8])
            return
        sender = item.get("from_node", "")
        if sender != job["provider_node_id"]:
            logger.warning(
                "MAIL_TASK_RESULT_REJECT job=%s sender=%s expected=%s",
                job_id[:8],
                sender[:16],
                job["provider_node_id"][:16],
            )
            return
        if job.get("status") in ("completed", "failed"):
            if self._debug:
                logger.debug("MAIL_TASK_RESULT_DUP job=%s", job_id[:8])
            return

        await self._write(
            self.storage.update_async_job_status,
            job_id,
            "completed",
            body.get("output_data"),
        )

        receipt = body.get("receipt")
        if receipt:
            receipt_json = json.dumps(receipt) if isinstance(receipt, dict) else receipt
            await self._write(self.storage.store_receipt, job_id, receipt_json)

        credit_note_raw = body.get("credit_note")
        provider_pubkey = job.get("provider_public_key", "")
        credits_charged = 0.0
        if credit_note_raw:
            try:
                from knarr.commerce.receipts import verify_credit_note

                credit_note = (
                    json.loads(credit_note_raw)
                    if isinstance(credit_note_raw, str)
                    else credit_note_raw
                )
                note_json = json.dumps(credit_note) if isinstance(credit_note, dict) else credit_note_raw
                if not verify_credit_note(note_json):
                    logger.warning("MAIL_CREDIT_NOTE_SIG_FAIL job=%s", job_id[:8])
                    if self.bus:
                        self.bus.emit(
                            "security.receipt_forgery",
                            job_id=job_id,
                            issuer=str(credit_note.get("issuer", ""))[:16],
                            reason="signature_invalid",
                            identity=provider_pubkey or self._public_key_hex,
                        )
                elif provider_pubkey and credit_note.get("issuer") != provider_pubkey:
                    logger.warning(
                        "MAIL_CREDIT_NOTE_ISSUER_MISMATCH job=%s expected=%s got=%s",
                        job_id[:8],
                        provider_pubkey[:16],
                        str(credit_note.get("issuer", ""))[:16],
                    )
                    if self.bus:
                        self.bus.emit(
                            "security.receipt_forgery",
                            job_id=job_id,
                            issuer=str(credit_note.get("issuer", ""))[:16],
                            reason="issuer_mismatch",
                            identity=provider_pubkey or self._public_key_hex,
                        )
                else:
                    credits_charged = float(credit_note.get("amount", 0.0))
                    if not provider_pubkey:
                        provider_pubkey = credit_note.get("issuer", "")
                    await self._write(
                        self.storage.store_credit_note,
                        provider_pubkey,
                        job_id,
                        note_json,
                    )
                    if self.bus:
                        self.bus.emit(
                            "receipt.received",
                            note_type=credit_note.get("note_type", "debit"),
                            counterparty=provider_pubkey,
                            amount=credits_charged,
                            reference=job_id,
                            identity=provider_pubkey,
                        )
            except Exception as exc:
                logger.warning("MAIL_CREDIT_NOTE_RECV_FAIL job=%s error=%s", job_id[:8], exc)
                credits_charged = 0.0
        elif receipt:
            try:
                receipt_parsed = json.loads(receipt) if isinstance(receipt, str) else receipt
                receipt_payload = receipt_parsed.get("data") or receipt_parsed.get("payload", {})
                if isinstance(receipt_payload, str):
                    receipt_payload = json.loads(receipt_payload)
                if isinstance(receipt_payload, dict):
                    credits_charged = float(receipt_payload.get("credits_charged", 0.0))
            except Exception:
                pass

        if credits_charged > 0 and math.isfinite(credits_charged) and credits_charged <= 1_000_000:
            if provider_pubkey:
                try:
                    peer_node_id = hashlib.sha256(bytes.fromhex(provider_pubkey)).hexdigest()
                    initial_trust = (
                        self._get_initial_trust(peer_node_id)
                        if self._get_initial_trust is not None
                        else 0.3
                    )
                    await self._write(
                        self.storage.get_or_create_ledger_entry,
                        provider_pubkey,
                        self._initial_credit,
                        initial_trust,
                    )
                    old_balance = self.storage.get_ledger_balance(provider_pubkey)
                    await self._write(
                        self.storage.update_ledger_consumer,
                        provider_pubkey,
                        credits_charged,
                    )
                    if self.bus:
                        self.bus.emit(
                            "credit.change",
                            direction="consumer",
                            counterparty=provider_pubkey,
                            amount=credits_charged,
                            reference=job_id,
                            identity=provider_pubkey,
                        )
                    if self._check_credit_restored is not None:
                        new_balance = self.storage.get_ledger_balance(provider_pubkey)
                        if old_balance is not None and new_balance is not None:
                            self._check_credit_restored(provider_pubkey, old_balance, new_balance)
                except Exception as exc:
                    logger.warning("MAIL_CONSUMER_LEDGER_FAIL job=%s error=%s", job_id[:8], exc)
            else:
                logger.warning("MAIL_CONSUMER_LEDGER_SKIP job=%s reason=no_provider_public_key", job_id[:8])
        elif credits_charged != 0:
            logger.warning("MAIL_CONSUMER_LEDGER_SKIP job=%s credits=%s", job_id[:8], credits_charged)

    async def _handle_task_failed_mail(self, item: dict):
        body = item.get("body", {})
        job_id = body.get("job_id")
        if not job_id:
            return
        job = self.storage.get_async_job(job_id)
        if not job:
            return
        if not job.get("provider_node_id"):
            if self._debug:
                logger.debug("MAIL_TASK_FAILED_SKIP job=%s reason=local_provider", job_id[:8])
            return
        sender = item.get("from_node", "")
        if sender != job["provider_node_id"]:
            logger.warning(
                "MAIL_TASK_FAILED_REJECT job=%s sender=%s expected=%s",
                job_id[:8],
                sender[:16],
                job["provider_node_id"][:16],
            )
            return
        await self._write(
            self.storage.update_async_job_status,
            job_id,
            "failed",
            None,
            body.get("error"),
        )

    async def _handle_asset_fetch_mail(self, item: dict):
        body = item.get("body", {})
        asset_hash = body.get("asset_hash")
        from_node = item.get("from_node")
        if not asset_hash or not from_node:
            return
        if not re.fullmatch(r"[0-9a-f]{64}", asset_hash):
            logger.warning("MAIL_ASSET_FETCH_REJECT from=%s reason=bad_hash", str(from_node)[:16])
            return
        addr = self.storage.get_address(from_node)
        if not addr or not addr.get("sidecar_port"):
            return
        sidecar_port = addr["sidecar_port"]
        if not isinstance(sidecar_port, int) or sidecar_port < 1 or sidecar_port > 65535:
            logger.warning("MAIL_ASSET_FETCH_REJECT from=%s reason=bad_port", str(from_node)[:16])
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self._handler_pool,
            self._fetch_sidecar_asset,
            addr.get("last_ip", ""),
            sidecar_port,
            asset_hash,
        )

    async def _handle_asset_ready_mail(self, item: dict):
        if self._debug:
            logger.debug("MAIL_ASSET_READY id=%s", item.get("item_id", "")[:8])

    def _fetch_sidecar_asset(self, host: str, port: int, asset_hash: str) -> None:
        if not host or not self.asset_dir:
            return
        signing_key = self._signing_key or getattr(self.sidecar, "_signing_key", None)
        if signing_key is None:
            logger.warning("MAIL_ASSET_FETCH_SKIP asset=%s reason=no_signing_key", asset_hash[:16])
            return

        url = f"https://{host}:{port}/assets/{asset_hash}"
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        timestamp = str(int(time.time()))
        pub_key_hex = signing_key.verify_key.encode().hex()
        payload = f"GET:/assets/{asset_hash}:{timestamp}:empty".encode("utf-8")
        signature = signing_key.sign(payload).signature.hex()
        req = urllib.request.Request(
            url,
            headers={
                "x-knarr-publickey": pub_key_hex,
                "x-knarr-signature": signature,
                "x-knarr-timestamp": timestamp,
            },
        )

        try:
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=30) as response:
                data = response.read()
            if self._store_asset_cb is not None:
                self._store_asset_cb(data)
            else:
                TaskContext(self.asset_dir).store_asset(data)
            if self._debug:
                logger.debug("MAIL_ASSET_FETCH_OK asset=%s host=%s port=%s", asset_hash[:16], host, port)
        except Exception as exc:
            logger.warning("MAIL_ASSET_FETCH_FAIL asset=%s host=%s port=%s error=%s", asset_hash[:16], host, port, exc)

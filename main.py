#!/usr/bin/env python3
"""
PixRemix — OTC order book and cross-chain settlement client for the Hurrah contract.

Remix edition: same core as Pixela with extra helpers (health check, CSV export,
batch quote, expiry status, wei formatting, dry-run build, paginated order IDs).
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import os
import re
import random
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

APP_NAME = "PixRemix"
APP_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# DATA MODELS
# ---------------------------------------------------------------------------


@dataclass
class OrderParams:
    """Parameters for a single OTC order (maker side)."""
    side: int  # 0 = buy, 1 = sell
    chain_id_origin: int
    chain_id_settle: int
    asset_in: bytes
    asset_out: bytes
    amount_in: int
    amount_out_min: int
    expiry_block: int

    def to_contract_args(self) -> Dict[str, Any]:
        return {
            "side": self.side,
            "chainIdOrigin": self.chain_id_origin,
            "chainIdSettle": self.chain_id_settle,
            "assetIn": self._bytes32(self.asset_in),
            "assetOut": self._bytes32(self.asset_out),
            "amountIn": self.amount_in,
            "amountOutMin": self.amount_out_min,
            "expiryBlock": self.expiry_block,
        }

    @staticmethod
    def _bytes32(b: bytes) -> str:
        if len(b) >= 32:
            return "0x" + b[:32].hex()
        return "0x" + b.hex().zfill(64)


@dataclass
class OrderView:
    """On-chain order view (from getOrder)."""
    order_id: str
    maker: str
    side: int
    chain_id_origin: int
    chain_id_settle: int
    asset_in: str
    asset_out: str
    amount_in: int
    amount_out_min: int
    amount_filled_in: int
    expiry_block: int
    cancelled: bool
    settled: bool
    posted_at: int


@dataclass
class SettlementView:
    """Settlement record view."""
    order_id: str
    settlement_ref: str
    chain_id_settle: int
    finalized_at: int


@dataclass
class OrderBookConfig:
    """Contract config (fee, limits, pause)."""
    fee_bps: int
    min_order_amount: int
    max_order_amount: int
    paused: bool


@dataclass
class PixRemixSession:
    """Session state: RPC URL, contract address, optional key for writes."""
    rpc_url: str
    contract_address: str
    private_key: Optional[str] = None
    chain_id: Optional[int] = None

    def to_json(self) -> str:
        d = {
            "rpc_url": self.rpc_url,
            "contract_address": self.contract_address,
            "chain_id": self.chain_id,
        }
        return json.dumps(d, indent=2)


# ---------------------------------------------------------------------------
# ORDER ID DERIVATION (matches Hurrah.deriveOrderId)
# ---------------------------------------------------------------------------

HRH_NAMESPACE = hashlib.sha3_256(b"Hurrah.otc.v2").digest()[:32]


def derive_order_id(maker_address: str, salt: bytes, nonce: int) -> str:
    """Derive orderId = keccak256(abi.encodePacked(HRH_NAMESPACE, maker, salt, nonce))."""
    try:
        from eth_abi import encode
        from eth_utils import keccak
    except ImportError:
        return "0x" + hashlib.sha256(
            (maker_address + salt.hex() + str(nonce)).encode()
        ).hexdigest()[:64]
    maker = bytes.fromhex(maker_address.replace("0x", "").lower().zfill(40))
    if len(salt) < 32:
        salt = salt + b"\x00" * (32 - len(salt))
    packed = encode(["bytes32", "address", "bytes32", "uint256"], [HRH_NAMESPACE, maker[:20], salt[:32], nonce])
    return "0x" + keccak(packed).hex()


def random_order_salt() -> bytes:
    return random.randbytes(32)


# ---------------------------------------------------------------------------
# HURRAH ABI FRAGMENTS (for Web3 calls)
# ---------------------------------------------------------------------------

HURRAH_ABI_POST = {
    "inputs": [
        {"name": "orderId", "type": "bytes32"},
        {"name": "side", "type": "uint8"},
        {"name": "chainIdOrigin", "type": "uint64"},
        {"name": "chainIdSettle", "type": "uint64"},
        {"name": "assetIn", "type": "bytes32"},
        {"name": "assetOut", "type": "bytes32"},
        {"name": "amountIn", "type": "uint256"},
        {"name": "amountOutMin", "type": "uint256"},
        {"name": "expiryBlock", "type": "uint64"},
    ],
    "name": "postOrder",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function",
}

HURRAH_ABI_FILL = {
    "inputs": [
        {"name": "orderId", "type": "bytes32"},
        {"name": "fillAmountIn", "type": "uint256"},
        {"name": "fillAmountOut", "type": "uint256"},
    ],
    "name": "fillOrder",
    "outputs": [],
    "stateMutability": "payable",
    "type": "function",
}

HURRAH_ABI_CANCEL = {
    "inputs": [{"name": "orderId", "type": "bytes32"}],
    "name": "cancelOrder",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function",
}

HURRAH_ABI_GET_ORDER = {
    "inputs": [{"name": "orderId", "type": "bytes32"}],
    "name": "getOrder",
    "outputs": [
        {"name": "maker", "type": "address"},
        {"name": "side", "type": "uint8"},
        {"name": "chainIdOrigin", "type": "uint64"},
        {"name": "chainIdSettle", "type": "uint64"},
        {"name": "assetIn", "type": "bytes32"},
        {"name": "assetOut", "type": "bytes32"},
        {"name": "amountIn", "type": "uint256"},
        {"name": "amountOutMin", "type": "uint256"},
        {"name": "amountFilledIn", "type": "uint256"},
        {"name": "expiryBlock", "type": "uint64"},
        {"name": "cancelled", "type": "bool"},
        {"name": "settled", "type": "bool"},
        {"name": "postedAt", "type": "uint64"},
    ],
    "stateMutability": "view",
    "type": "function",
}

HURRAH_ABI_CONFIG = {
    "inputs": [],
    "name": "config",
    "outputs": [
        {"name": "_feeBps", "type": "uint256"},
        {"name": "_minOrderAmount", "type": "uint256"},
        {"name": "_maxOrderAmount", "type": "uint256"},
        {"name": "_paused", "type": "bool"},
    ],
    "stateMutability": "view",
    "type": "function",
}

HURRAH_ABI_ORDER_EXISTS = {
    "inputs": [{"name": "orderId", "type": "bytes32"}],
    "name": "orderExists",
    "outputs": [{"name": "", "type": "bool"}],
    "stateMutability": "view",
    "type": "function",
}

HURRAH_ABI_TOTAL_ORDER_COUNT = {
    "inputs": [],
    "name": "totalOrderCount",
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function",
}

HURRAH_ABI_GET_ORDER_ID_AT = {
    "inputs": [{"name": "index", "type": "uint256"}],
    "name": "getOrderIdAt",
    "outputs": [{"name": "", "type": "bytes32"}],
    "stateMutability": "view",
    "type": "function",
}

HURRAH_ABI_GET_MAKER_ORDER_IDS = {
    "inputs": [{"name": "maker", "type": "address"}],
    "name": "getMakerOrderIds",
    "outputs": [{"name": "", "type": "bytes32[]"}],
    "stateMutability": "view",
    "type": "function",
}

HURRAH_ABI_QUOTE_FILL = {
    "inputs": [
        {"name": "orderId", "type": "bytes32"},
        {"name": "fillAmountIn", "type": "uint256"},
    ],
    "name": "quoteFill",
    "outputs": [
        {"name": "minAmountOut", "type": "uint256"},
        {"name": "feeAmount", "type": "uint256"},
        {"name": "makerReceives", "type": "uint256"},
    ],
    "stateMutability": "view",
    "type": "function",
}

HURRAH_ABI_FINALIZE_SETTLEMENT = {
    "inputs": [
        {"name": "orderId", "type": "bytes32"},
        {"name": "chainIdSettle", "type": "uint64"},
        {"name": "settlementRef", "type": "bytes32"},
    ],
    "name": "finalizeSettlement",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function",
}

HURRAH_ABI_FULL = [
    HURRAH_ABI_POST,
    HURRAH_ABI_FILL,
    HURRAH_ABI_CANCEL,
    HURRAH_ABI_GET_ORDER,
    HURRAH_ABI_CONFIG,
    HURRAH_ABI_ORDER_EXISTS,
    HURRAH_ABI_TOTAL_ORDER_COUNT,
    HURRAH_ABI_GET_ORDER_ID_AT,
    HURRAH_ABI_GET_MAKER_ORDER_IDS,
    HURRAH_ABI_QUOTE_FILL,
    HURRAH_ABI_FINALIZE_SETTLEMENT,
]


# ---------------------------------------------------------------------------
# WEB3 HELPERS
# ---------------------------------------------------------------------------


def _maybe_web3():
    try:
        from web3 import Web3
        return Web3
    except ImportError:
        return None


def _hex_to_bytes32(hex_str: str) -> bytes:
    h = hex_str.replace("0x", "")
    if len(h) < 64:
        h = h.zfill(64)
    return bytes.fromhex(h[:64])


def connect_session(session: PixRemixSession) -> Any:
    """Return a Web3 contract instance for Hurrah; raises if web3 not installed or RPC fails."""
    Web3 = _maybe_web3()
    if Web3 is None:
        raise RuntimeError("Install web3: pip install web3")
    w3 = Web3(Web3.HTTPProvider(session.rpc_url))
    if not w3.is_connected():
        raise RuntimeError("RPC not connected")
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(session.contract_address),
        abi=HURRAH_ABI_FULL,
    )
    return contract, w3


def get_config(session: PixRemixSession) -> OrderBookConfig:
    """Fetch contract config (fee, limits, paused)."""
    contract, _ = connect_session(session)
    fee_bps, min_amt, max_amt, paused = contract.functions.config().call()
    return OrderBookConfig(
        fee_bps=fee_bps,
        min_order_amount=min_amt,
        max_order_amount=max_amt,
        paused=paused,
    )


def get_order(session: PixRemixSession, order_id_hex: str) -> OrderView:
    """Fetch a single order by ID."""
    contract, w3 = connect_session(session)
    oid = _hex_to_bytes32(order_id_hex)
    raw = contract.functions.getOrder(oid).call()
    return OrderView(
        order_id=order_id_hex,
        maker=raw[0],
        side=raw[1],
        chain_id_origin=raw[2],
        chain_id_settle=raw[3],
        asset_in="0x" + raw[4].hex() if isinstance(raw[4], bytes) else raw[4].hex(),
        asset_out="0x" + raw[5].hex() if isinstance(raw[5], bytes) else raw[5].hex(),
        amount_in=raw[6],
        amount_out_min=raw[7],
        amount_filled_in=raw[8],
        expiry_block=raw[9],
        cancelled=raw[10],
        settled=raw[11],
        posted_at=raw[12],
    )


def order_exists(session: PixRemixSession, order_id_hex: str) -> bool:
    """Check if order exists on chain."""
    contract, _ = connect_session(session)
    oid = _hex_to_bytes32(order_id_hex)
    return contract.functions.orderExists(oid).call()


def total_order_count(session: PixRemixSession) -> int:
    """Total number of orders in the book."""
    contract, _ = connect_session(session)
    return contract.functions.totalOrderCount().call()


def get_maker_order_ids(session: PixRemixSession, maker_address: str) -> List[str]:
    """List order IDs for a maker."""
    contract, w3 = connect_session(session)
    maker = w3.to_checksum_address(maker_address)
    raw = contract.functions.getMakerOrderIds(maker).call()
    return ["0x" + (r.hex() if isinstance(r, bytes) else r.hex()) for r in raw]


def quote_fill(session: PixRemixSession, order_id_hex: str, fill_amount_in: int) -> Tuple[int, int, int]:
    """Returns (minAmountOut, feeAmount, makerReceives)."""
    contract, _ = connect_session(session)
    oid = _hex_to_bytes32(order_id_hex)
    return contract.functions.quoteFill(oid, fill_amount_in).call()


def post_order_tx(
    session: PixRemixSession,
    order_id_hex: str,
    params: OrderParams,
    gas_limit: int = 400_000,
) -> str:
    """Build and send postOrder transaction; returns tx hash. Requires session.private_key."""
    if not session.private_key:
        raise RuntimeError("Private key required for postOrder")
    contract, w3 = connect_session(session)
    acct = w3.eth.account.from_key(session.private_key)
    oid = _hex_to_bytes32(order_id_hex)
    asset_in = params.asset_in if len(params.asset_in) >= 32 else params.asset_in + b"\x00" * (32 - len(params.asset_in))
    asset_out = params.asset_out if len(params.asset_out) >= 32 else params.asset_out + b"\x00" * (32 - len(params.asset_out))
    tx = contract.functions.postOrder(
        oid,
        params.side,
        params.chain_id_origin,
        params.chain_id_settle,
        asset_in,
        asset_out,
        params.amount_in,
        params.amount_out_min,
        params.expiry_block,
    ).build_transaction({
        "from": acct.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": gas_limit,
    })

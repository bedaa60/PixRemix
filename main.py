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

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

"""Make tion_btle connect reliably through HA's bleak-retry-connector.

Upstream ``tion_btle.Tion._try_connect()`` performs a single *raw*
``BleakClient.connect()`` (``@retry(retries=1)``). With a Tion 4S that only
offers a connectable advertisement for a very short window after it goes idle,
that single attempt almost always misses the window — the symptom is
``No backend with an available connection slot ... no connectable path`` /
``br-connection-canceled`` a few minutes after the breezer becomes idle, after
which Home Assistant can no longer send commands.

We monkeypatch ``_try_connect`` to use
``bleak_retry_connector.establish_connection``, the helper Home Assistant
recommends: it waits for a connectable advertisement, refreshes the
``BLEDevice`` between attempts and reuses cached GATT services. This is the
single most important fix for command reliability on the 4S.

The patch is intentionally surgical (no fork of the whole library) and
idempotent — call :func:`apply` once at component import time.
"""
from __future__ import annotations

import logging

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .vendor.tion_btle.tion import Tion, retry

_LOGGER = logging.getLogger(__name__)


@retry(retries=2, delay=2)
async def _robust_try_connect(self: Tion) -> bool:
    """Connect via bleak-retry-connector instead of a single raw connect()."""
    # Freshest BLEDevice HA has handed us (via update_btle_device), else the
    # one captured at construction time.
    device = self._next_btle_device if self._next_btle_device is not None else self._mac

    if isinstance(device, BLEDevice):
        def _latest_device() -> BLEDevice:
            nxt = self._next_btle_device
            return nxt if isinstance(nxt, BLEDevice) else device

        self._btle = await establish_connection(
            client_class=BleakClientWithServiceCache,
            device=device,
            name=self.mac,
            ble_device_callback=_latest_device,
        )
        return True

    # Fallback: only a bare MAC string is available (no HA BLEDevice).
    self._btle = BleakClient(device)
    return await self._btle.connect()


# --- Persistent connection -------------------------------------------------
# The host's RTL8761 adapter is unreliable at *establishing* a new BLE
# connection while it is already busy (it shares the radio with the aquarium
# BMS), so the upstream connect-per-command model fails with TimeoutError after
# the breezer has been idle. Instead we keep the link open the whole time (like
# the BMS does): _disconnect() becomes a no-op so the established link is held,
# and a dropped link is re-established by the next coordinator heartbeat poll
# (connect() -> _connect() sees connection_status == "disc" -> _try_connect()).
_original_disconnect = Tion._disconnect


async def _persistent_disconnect(self: Tion) -> None:
    """Hold the BLE link open; only really disconnect when forced."""
    if getattr(self, "_force_disconnect", False):
        await _original_disconnect(self)
    # else: keep the connection open (no-op)


def apply() -> None:
    """Install the robust + persistent connection patches (idempotent)."""
    if getattr(Tion, "_robust_connection_patched", False):
        return
    Tion._try_connect = _robust_try_connect
    Tion._disconnect = _persistent_disconnect
    Tion._robust_connection_patched = True
    _LOGGER.debug(
        "Patched tion_btle.Tion: _try_connect -> bleak_retry_connector, "
        "_disconnect -> persistent (hold link open)"
    )

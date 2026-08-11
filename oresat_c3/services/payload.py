"""
Payload Service:
Manage the operation of a given payload, Will be designed for OSIRIS first, but with the intention
of extending it to other payloads later.
"""

# unused imports will be used for piplasma and osiris.
import time
from pathlib import Path
from time import monotonic

import serial
from canopen.objectdictionary import ODVariable
from olaf import Service, logger
from oresat_configs.constants import Mission

from ..protocols.cachestore import CacheStore
from ..subsystems.opd import OpdNode
from .node_manager import NodeManagerService


class PayloadService(Service):
    def __init__(self, node_mgr: NodeManagerService, mission: Mission, mock: bool = True) -> None:
        super().__init__()
        self._node_mgr = node_mgr
        self._mission = mission
        self._mock = mock
        self._payload_handler = None

    def on_start(self) -> None:
        self._state = self.node.od["payload_ctrl"]["state"]
        self._enabled = self.node.od["payload_ctrl"]["enabled"]

        if self._mission.__str__() == "osiris_b1":
            logger.info("creating osiris payload handler")
            # self._payload_handler = BeeconHandler(self._state)
        elif self._mission.__str__() == "prism":
            logger.info("creating prism payload handler")
            self._payload_handler = PiPlasmaHandler(
                self._state, self._node_mgr, self.node.fwrite_cache, self._mock
            )
        elif self._mission.__str__() == "beecon":
            logger.info("creating beecon payload handler")
            self._payload_handler = BeeconHandler(
                self._state,  self.node.od["beacon"]["delay"], self._mock
            )
        else:
            logger.error("Payload Service started despite mission not having a compatable payload.")
            raise Exception(
                "Payload Service started despite mission not having a compatable payload."
            )

    def on_loop(self) -> None:
        if not self._enabled.value:
            time.sleep(10)
            return
        if self._payload_handler is not None:
            try:
                self._payload_handler.loop()
            except Exception as e:
                logger.error(f"Payload handler got error: {e}")
                self._payload_handler = None
        else:
            time.sleep(10)


class BeeconHandler():
    # Beecon state pseudoenum:
    # 0: off
    # 1: on
    _I2C_BUS_NUM = 2
    _BEECON_DELAY = 10

    def __init__(self, in_state: ODVariable, oresat_beacon_timeout: ODVariable, mock: bool) -> None:
        self._state = in_state
        self._ore_beacon = oresat_beacon_timeout
        self._ore_beacon_default = self._ore_beacon.value
        self._beecon_node = OpdNode(self._I2C_BUS_NUM, "beecon", 0x10, mock=mock)
        self._beecon_node.configure()
        if not self._beecon_node.probe():
            logger.error("Beecon handler could not find science card!")
            raise Exception("Beecon handler could not find science card!")

    def loop(self) -> None:
        """Runs the beecon state machine. Makes sure the beecon is on or off, depending on state"""
        state_val = self._state.value
        logger.warning("looping beecon state machine")
        if state_val == 0:
            if self._beecon_node.is_enabled:
                self._ore_beacon.value = self._ore_beacon_default
                self._beecon_node.disable()
        elif state_val == 1:
            if not self._beecon_node.is_enabled:
                self._ore_beacon.value = 0
                self._beecon_node.enable()
        else:
            logger.error("beecon service got incoherent state")
        time.sleep(self._BEECON_DELAY)


class PiPlasmaHandler():
    THRESHOLD = 524288 # 2^19, ~1 orbit of data.

    def __init__(
        self,
        in_state: ODVariable,
        node_mgr: NodeManagerService,
        store: CacheStore,
        mock: bool
    ):
        self._state = in_state
        self._node_mgr = node_mgr
        self._store = store
        self._filesize = 0
        self._file = None
        self._piplasma = None
        self._mock = mock

        if self._state.value > 1:
            self._state.value = 1

    def __del__(self):
        if (
            self._node_mgr.node_status("piplasma_sci") == 1 or
            self._node_mgr.node_status("piplasma_sci") == 2
        ):
            self._node_mgr.disable("piplasma_sci")

    def loop(self):
        if self._mock:
            time.sleep(10)
            return
        state_val = self._state.value
        if state_val == 0:
            if (
                self._node_mgr.node_status("piplasma_sci") == 1 or
                self._node_mgr.node_status("piplasma_sci") == 2
            ):
                self._node_mgr.disable("piplasma_sci")
            if self._piplasma is not None:
                self._piplasma.close()
                self._piplasma = None
            time.sleep(1)
        elif state_val == 1:
            sci_status = self._node_mgr.node_status("piplasma_sci")
            if sci_status == 1: # wait for the card to boot.
                time.sleep(1)
            elif sci_status == 2: # on. Goto state 2.
                self._piplasma = serial.Serial(port="/dev/ttyS3", baudrate=115200)
                self._state.value = 2
            elif sci_status == 4 or sci_status == 0xFF: # nothing to do.
                time.sleep(10)
            else: # turn the card on.
                self._node_mgr.enable("piplasma_sci")
        elif state_val == 2:
            self._handle_file()
            if self._piplasma is None:
                logger.error("Piplasma reached state 2 before state 1!")
                self._state.value = 1
                return
            while self._piplasma.in_waiting > 72: # it may be better
                self._handle_file()
                out = self._piplasma.read_until(expected=b"\n")
                self._store.write_data(self._file, out, offset=0, from_what=2)
                self._filesize += len(out)
        time.sleep(0.1)

    def _handle_file(self) -> None:
        if self._file is None:
            self._make_file()
            logger.info(
                f"Piplasma payload handler has no active file. Creating new file: {self._file}"
            )
        elif self._filesize > self.THRESHOLD:
            self._filesize = 0
            self._make_file()
            logger.info(
                f"Piplasma payload file has reached threshold. Creating new file: {self._file}"
            )

    def _make_file(self):
        timestamp = int(monotonic())
        new_file = Path(f"c3_piplasma_{timestamp}.txt")
        while self._store.file_exists(path=new_file):
            timestamp += 1
            new_file = Path(f"c3_piplasma_{timestamp}.txt")
        self._store.create_file(new_file)
        self._file = new_file

"""
Payload Service:
Manage the operation of a given payload, Will be designed for OSIRIS first, but with the intention
of extending it to other payloads later.
"""

# unused imports will be used for piplasma and osiris.
import time
from os.path import abspath  # noqa: F401
from time import monotonic  # noqa: F401

from canopen.objectdictionary import ODVariable
from olaf import Service, logger
from oresat_configs.constants import Mission

from ..subsystems.opd import OpdNode
from .node_manager import NodeManagerService


class PayloadService(Service):
    def __init__(self, node_mgr: NodeManagerService, mission: Mission, mock: bool = True) -> None:
        super().__init__()
        self._state = None
        self._node_mgr = node_mgr
        self._mission = mission
        self._mock = mock

    def on_start(self) -> None:
        self._state = self.node.od["payload_ctrl"]["state"]
        self._enabled = self.node.od["payload_ctrl"]["enabled"]

        if self._mission.__str__() == "osiris_b1":
            logger.info("creating osiris payload handler")
            # self._payload_handler = BeeconHandler(self._state)
        if self._mission.__str__() == "prism":
            logger.info("creating prism payload handler")
            # self._payload_handler = BeeconHandler(self._state)
        if self._mission.__str__() == "beecon":
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
        self._payload_handler.loop()

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



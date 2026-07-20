"""
Payload Service:
Manage the operation of a given payload, Will be designed for OSIRIS first, but with the intention
of extending it to other payloads later.
"""

# unused imports will be used for piplasma and osiris.
from os.path import abspath  # noqa: F401
from time import monotonic, time  # noqa: F401

from canopen.objectdictionary import ODVariable
from olaf import Service, logger

from ..subsystems.opd import OpdNode
from .node_manager import NodeManagerService


class PayloadService(Service):
    def __init__(self, node_mgr: NodeManagerService) -> None:
        self._state = None
        self._node_mgr = node_mgr

    def on_start(self) -> None:
        self._state = self.node.od["payload"]["state"]
        self._enabled = self.node.od["payload"]["enabled"]

        if "osiris_sci" in self.node._od_db: # not self._mock_hw and
            logger.info("creating osiris payload handler")
            # self._payload_handler = BeeconHandler(self._state)
        if "piplasma_sci" in self.node._od_db: # not self._mock_hw and
            logger.info("creating osiris payload handler")
            # self._payload_handler = BeeconHandler(self._state)
        if "beecon_sci" in self.node._od_db: # not self._mock_hw and
            logger.info("creating osiris payload handler")
            self._payload_handler = BeeconHandler(self._state)
        else:
            logger.error("Payload Service started despite mission not having a compatable payload.")
            raise Exception(
                "Payload Service started despite mission not having a compatable payload."
            )

    def on_loop(self) -> None:
        if not self._enabled:
            time.sleep(10)
            return
        self._payload_handler.loop()

class BeeconHandler():
    # Beecon state pseudoenum:
    # 0: off
    # 1: on
    _I2C_BUS_NUM = 2

    def __init__(self, in_state: ODVariable) -> None:
        self._state = in_state
        self._beecon_node = OpdNode(self._I2C_BUS_NUM, "beecon", 0x10)

    def loop(self) -> None:
        """Runs the beecon state machine. Makes sure the beecon is on or off, depending on state"""
        state_val = self._state.value
        if state_val == 0:
            # check if the beecon is off. If not, turn it off.
            if self._beecon_node.is_enabled:
                self._beecon_node.disable()
        elif state_val == 1:
            if not self._beecon_node.is_enabled:
                self._beecon_node.enable()
        else:
            logger.error("beecon service got incoherent state")
        time.sleep(1)



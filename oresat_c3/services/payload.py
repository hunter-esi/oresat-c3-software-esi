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

    def __init__(self, in_state: ODVariable, node_mgr: NodeManagerService) -> None:
        self._state = in_state
        self._node_mgr = node_mgr

    def loop(self) -> None:
        """Runs the beecon state machine. Makes sure the beecon is on or off, depending on state"""
        nmgr_beecon = self._node_mgr.node_status("beecon_sci")

        if nmgr_beecon == 0xFF:
            logger.error("OPD says beecon is dead!")
            time.sleep(10)
            return
        elif nmgr_beecon == 4:
            logger.error("Could not find beecon science card through OPD!")
            time.sleep(10)
            return

        state_val = self._state.value
        if state_val == 0:
            # check if the beecon is off. If not, turn it off.
            if nmgr_beecon != 0:
                self._node_mgr.disable("beecon_sci")
        elif state_val == 1:
            # check if the beecon is off. If not, turn it off.
            if nmgr_beecon == 2:
                pass
            elif nmgr_beecon == 0:
                self._node_mgr.enable("beecon_sci")
            elif nmgr_beecon == 1:
                logger.debug("beecon science card is booting")
            elif nmgr_beecon == 3:
                logger.debug("beecon science card is booting")
            elif nmgr_beecon in (5, 6):
                logger.debug(f"beecon science card is in incoherent state! {nmgr_beecon}")
        else:
            logger.error("beecon service got incoherent state")
        time.sleep(1)



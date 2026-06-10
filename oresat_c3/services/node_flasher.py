"""
Node Flasher Service
Handles flashing Zephyr/MCUboot images to nodes via CANopen.
"""

import os
import time
from queue import Empty, SimpleQueue

from olaf import Service, logger

H1F50_PROGRAM_DATA = 0x1F50
H1F51_PROGRAM_CTRL = 0x1F51
H1F56_PROGRAM_SWID = 0x1F56
H1F57_FLASH_STATUS = 0x1F57

PROGRAM_CTRL_STOP = 0x00
PROGRAM_CTRL_START = 0x01
PROGRAM_CTRL_CLEAR = 0x03

DEFAULT_STATUS_TIMEOUT = 30.0
DEFAULT_BOOTUP_TIMEOUT = 20.0
DEFAULT_DOWNLOAD_BUFFER_SIZE = 889
QUEUE_GET_TIMEOUT = 1.0
STATUS_POLL_DELAY = 0.5
NMT_STATE_CHANGE_DELAY = 0.5
FLASH_STATUS_OK = 0
SDO_SUBINDEX = 1


class NodeFlasherService(Service):
    def __init__(
        self,
        cache_dir: str,
        node_mgr,
        throttle_delay: float = 0.0,
        # request_crc defaults to False due to a possible CRC calculation mismatch in
        # Zephyr's CANopenNode integration (depends on versioning). This is safe because
        # MCUboot checks hash signatures of the firmware image before booting.
        request_crc: bool = False,
        sdo_timeout: float = 3.0,
        sdo_retries: int = 3,
    ):
        super().__init__()
        self.cache_dir = cache_dir
        self.node_mgr = node_mgr
        self.command_queue = SimpleQueue()

        self.status_timeout = DEFAULT_STATUS_TIMEOUT
        self.bootup_timeout = DEFAULT_BOOTUP_TIMEOUT
        self.download_buffer_size = DEFAULT_DOWNLOAD_BUFFER_SIZE
        self.block_transfer = True
        self.throttle_delay = throttle_delay
        self.request_crc = request_crc
        self.sdo_timeout = sdo_timeout
        self.sdo_retries = sdo_retries

    def enqueue_flash(
        self, node_id: int, filename: str, throttle_delay: float = None, block_transfer: bool = None
    ):
        """Called by EdlService to trigger a flash."""
        if throttle_delay is None:
            throttle_delay = self.throttle_delay
        if block_transfer is None:
            block_transfer = self.block_transfer

        self.command_queue.put(
            {
                "node_id": node_id,
                "filename": filename,
                "throttle_delay": throttle_delay,
                "block_transfer": block_transfer,
            }
        )
        logger.info(
            f"Queued flash for Node 0x{node_id:02X} with file {filename} "
            f"(throttle_delay={throttle_delay}, block_transfer={block_transfer})"
        )

    def on_loop(self):
        try:
            cmd = self.command_queue.get(timeout=QUEUE_GET_TIMEOUT)
            self._execute_flash(
                cmd["node_id"], cmd["filename"], cmd["throttle_delay"], cmd["block_transfer"]
            )
        except Empty:
            pass
        except Exception as e:
            logger.exception(f"Node flasher exception: {e}")

    def _wait_flash_status_ok(self, flash_sdo, timeout_s):
        end = time.time() + timeout_s
        status = int(flash_sdo.raw)
        while status != FLASH_STATUS_OK and time.time() < end:
            time.sleep(STATUS_POLL_DELAY)
            status = int(flash_sdo.raw)
        return status

    def _execute_flash(
        self, node_id: int, filename: str, throttle_delay: float, block_transfer: bool
    ):
        filepath = os.path.join(self.cache_dir, filename)

        if not os.path.isfile(filepath):
            logger.error(f"Node flasher aborted: File not found at {filepath}")
            return

        node_name = self.node_mgr.node_id_to_name.get(node_id)
        if node_name is None:
            logger.error(f"Node flasher aborted: Node 0x{node_id:02X} not in node_id_to_name.")
            return

        if node_name not in self.node.remote_nodes:
            logger.error(
                f"Node flasher aborted: Node {node_name} (0x{node_id:02X}) not in remote_nodes."
            )
            return

        target_node = self.node.remote_nodes[node_name]
        logger.debug(f"target_node has_network: {target_node.has_network()}")
        logger.debug(f"target_node.sdo.network: {target_node.sdo.network}")

        target_node.sdo.RESPONSE_TIMEOUT = self.sdo_timeout
        target_node.sdo.MAX_RETRIES = self.sdo_retries

        data_sdo = target_node.sdo[H1F50_PROGRAM_DATA][SDO_SUBINDEX]
        ctrl_sdo = target_node.sdo[H1F51_PROGRAM_CTRL][SDO_SUBINDEX]
        flash_sdo = target_node.sdo[H1F57_FLASH_STATUS][SDO_SUBINDEX]

        # Optionally throttle CAN bus sends (needed for some adapters like VulCAN over slcan)
        original_send = None
        if throttle_delay > 0.0:
            original_send = self.node.network._network.bus.send

            def throttled_send(msg, timeout=None):
                original_send(msg, timeout)
                time.sleep(throttle_delay)

            self.node.network._network.bus.send = throttled_send
            logger.debug(f"Throttle enabled: {throttle_delay}s delay between CAN frames")

        logger.info(f"Starting flash of {filename} to Node {node_name} (0x{node_id:02X})")
        try:
            self.node_mgr.set_node_updating(node_id, True)

            logger.debug("Setting NMT state to PRE-OPERATIONAL")

            target_node.nmt.state = "PRE-OPERATIONAL"
            time.sleep(NMT_STATE_CHANGE_DELAY)
            logger.debug(f"NMT state after set: {target_node.nmt.state}")

            # Clear old image
            ctrl_sdo.raw = PROGRAM_CTRL_STOP
            ctrl_sdo.raw = PROGRAM_CTRL_CLEAR
            if self._wait_flash_status_ok(flash_sdo, self.status_timeout) != FLASH_STATUS_OK:
                raise Exception("CLEAR command failed or timed out.")

            # Download new image
            file_size = os.path.getsize(filepath)
            logger.info(f"Downloading {file_size} bytes...")
            with open(filepath, "rb") as infile:
                outfile = data_sdo.open(
                    "wb",
                    buffering=self.download_buffer_size,
                    size=file_size,
                    block_transfer=block_transfer,
                    request_crc_support=self.request_crc,
                )
                outfile.write(infile.read())
                outfile.close()

            if self._wait_flash_status_ok(flash_sdo, self.status_timeout) != FLASH_STATUS_OK:
                raise Exception("Download failed or timed out.")

            # Reboot node
            logger.info("Download complete. Rebooting node...")
            ctrl_sdo.raw = PROGRAM_CTRL_START
            target_node.nmt.wait_for_bootup(timeout=self.bootup_timeout)
            logger.info(f"Node {node_name} (0x{node_id:02X}) flashed and rebooted successfully.")

        except Exception as e:
            logger.error(f"Node flasher failed during execution: {e}")
        finally:
            # Restore original send if throttled
            if original_send is not None:
                self.node.network._network.bus.send = original_send

            self.node_mgr.set_node_updating(node_id, False)

            try:
                os.remove(filepath)
                logger.info(f"Cleaned up {filename} from cache.")
            except OSError as e:
                logger.warning(f"Could not delete {filename}: {e}")

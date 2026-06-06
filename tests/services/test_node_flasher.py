"""Test the Node Flasher service."""

import os
import tempfile
import unittest
from queue import Empty
from unittest.mock import MagicMock, patch

from oresat_c3.services.node_flasher import (
    FLASH_STATUS_OK,
    NodeFlasherService,
)

# Test Constants
TEST_NODE_ID = 0x2A
TEST_NODE_NAME = "test_node"
TEST_FILENAME = "test_fw.bin"
DUMMY_FIRMWARE_DATA = b"dummy_firmware_data"

# CANopen SDO Indices
SDO_DATA_IDX = 0x1F50
SDO_CTRL_IDX = 0x1F51
SDO_FLASH_IDX = 0x1F57


class TestNodeFlasherService(unittest.TestCase):
    """Test the Node Flasher service."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = self.temp_dir.name

        self.test_filepath = os.path.join(self.cache_dir, TEST_FILENAME)
        with open(self.test_filepath, "wb") as f:
            f.write(DUMMY_FIRMWARE_DATA)

        self.mock_node_mgr = MagicMock()
        self.mock_node_mgr.node_id_to_name = {TEST_NODE_ID: TEST_NODE_NAME}
        self.mock_target_node = MagicMock()
        self.mock_target_node.has_network.return_value = True
        self.mock_data_sdo = MagicMock()
        self.mock_ctrl_sdo = MagicMock()
        self.mock_flash_sdo = MagicMock()

        self.mock_target_node.sdo.__getitem__.side_effect = lambda key: {
            SDO_DATA_IDX: {1: self.mock_data_sdo},
            SDO_CTRL_IDX: {1: self.mock_ctrl_sdo},
            SDO_FLASH_IDX: {1: self.mock_flash_sdo},
        }[key]

        self.service = NodeFlasherService(
            cache_dir=self.cache_dir,
            node_mgr=self.mock_node_mgr,
            throttle_delay=0.0,
            request_crc=False,
        )

        self.service.node = MagicMock()
        self.service.node.remote_nodes = {TEST_NODE_NAME: self.mock_target_node}

        self.mock_bus_send = MagicMock()
        self.service.node.network._network.bus.send = self.mock_bus_send

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch.object(NodeFlasherService, "_execute_flash")
    def test_on_loop_success(self, mock_execute):
        """Test that on_loop calls _execute_flash correctly."""
        self.service.command_queue.put(
            {
                "node_id": TEST_NODE_ID,
                "filename": "fw.bin",
                "throttle_delay": 0.5,
                "block_transfer": True,
            }
        )

        self.service.on_loop()

        mock_execute.assert_called_once_with(TEST_NODE_ID, "fw.bin", 0.5, True)

    @patch.object(NodeFlasherService, "_execute_flash")
    def test_on_loop_empty(self, mock_execute):
        """Test that on_loop handles an empty queue."""
        self.service.on_loop()
        mock_execute.assert_not_called()

    def test_enqueue_flash(self):
        """Test that enqueue_flash correctly adds items to the queue."""
        # Test defaults
        self.service.enqueue_flash(TEST_NODE_ID, "fw1.bin")
        cmd = self.service.command_queue.get(timeout=1.0)
        self.assertEqual(cmd["node_id"], TEST_NODE_ID)
        self.assertEqual(cmd["filename"], "fw1.bin")
        self.assertEqual(cmd["throttle_delay"], 0.0)
        self.assertEqual(cmd["block_transfer"], True)

        # Test overrides
        self.service.enqueue_flash(0x7C, "fw2.bin", throttle_delay=0.05, block_transfer=False)
        cmd = self.service.command_queue.get(timeout=1.0)
        self.assertEqual(cmd["node_id"], 0x7C)
        self.assertEqual(cmd["filename"], "fw2.bin")
        self.assertEqual(cmd["throttle_delay"], 0.05)
        self.assertEqual(cmd["block_transfer"], False)

    @patch("oresat_c3.services.node_flasher.time.sleep")
    def test_execute_flash_success(self, mock_sleep):
        """Test a successful flash."""

        self.mock_flash_sdo.raw = FLASH_STATUS_OK

        mock_outfile = MagicMock()
        self.mock_data_sdo.open.return_value = mock_outfile

        self.service._execute_flash(
            TEST_NODE_ID, TEST_FILENAME, throttle_delay=0.0, block_transfer=True
        )

        self.mock_node_mgr.set_node_updating.assert_any_call(TEST_NODE_ID, True)
        self.assertEqual(self.mock_target_node.nmt.state, "PRE-OPERATIONAL")

        # Assert SDO operations
        self.mock_data_sdo.open.assert_called_once_with(
            "wb",
            buffering=self.service.download_buffer_size,
            size=len(DUMMY_FIRMWARE_DATA),
            block_transfer=True,
            request_crc_support=False,
        )
        mock_outfile.write.assert_called_once_with(DUMMY_FIRMWARE_DATA)
        self.mock_target_node.nmt.wait_for_bootup.assert_called_once()
        self.mock_node_mgr.set_node_updating.assert_called_with(TEST_NODE_ID, False)

        # Ensure file was cleaned up
        self.assertFalse(os.path.exists(self.test_filepath))

    @patch("oresat_c3.services.node_flasher.time.sleep")
    def test_execute_flash_with_throttling(self, mock_sleep):
        """Test execution when sends are throttled."""
        self.mock_flash_sdo.raw = FLASH_STATUS_OK

        original_send = self.service.node.network._network.bus.send

        self.service._execute_flash(
            TEST_NODE_ID, TEST_FILENAME, throttle_delay=0.1, block_transfer=True
        )

        self.assertEqual(self.service.node.network._network.bus.send, original_send)
        self.assertFalse(os.path.exists(self.test_filepath))

    def test_execute_flash_file_not_found(self):
        """Test when file is missing."""
        self.service._execute_flash(
            TEST_NODE_ID, "missing.bin", throttle_delay=0.0, block_transfer=True
        )
        self.mock_node_mgr.set_node_updating.assert_not_called()

    def test_execute_flash_invalid_node(self):
        """Test when node ID is invalid or not in network."""
        # Unmapped node ID
        self.service._execute_flash(0x9999, TEST_FILENAME, throttle_delay=0.0, block_transfer=True)
        self.mock_node_mgr.set_node_updating.assert_not_called()

        # Node mapped but not in remote_nodes
        self.mock_node_mgr.node_id_to_name[0x98] = "weird_node"
        self.service._execute_flash(0x98, TEST_FILENAME, throttle_delay=0.0, block_transfer=True)
        self.mock_node_mgr.set_node_updating.assert_not_called()

    @patch("oresat_c3.services.node_flasher.time.sleep")
    def test_execute_flash_status_timeout(self, mock_sleep):
        """Test execution when status SDO does not return OK."""
        # Set to return an error code
        self.mock_flash_sdo.raw = 99

        # Speedup test
        self.service.status_timeout = 0.1

        self.service._execute_flash(
            TEST_NODE_ID, TEST_FILENAME, throttle_delay=0.0, block_transfer=True
        )

        self.mock_node_mgr.set_node_updating.assert_called_with(TEST_NODE_ID, False)
        self.mock_target_node.nmt.wait_for_bootup.assert_not_called()

        # Ensure file was cleaned up
        self.assertFalse(os.path.exists(self.test_filepath))

"""
This class should eventually be used to define a mission database. For now, it will be used to
define GPS information.
"""

from os.path import abspath
from time import monotonic

from olaf import Service, logger, new_oresat_file

from .node_manager import NodeManagerService


class MissionDatabaseService(Service):
    """Mission Database Service"""

    BAT_LEVEL_LOW = 6500  # mV

    def __init__(self, node_mgr_service: NodeManagerService) -> None:
        super().__init__()
        self._node_mgr_service = node_mgr_service

        # This should eventually be defined by something in oresat configs.
        # For now will be hardcoded.
        self._refresh_delay = 0
        self._data_per_file = 0
        self._max_num_files = 0
        self.next_gps_index = 1
        self.data = ""
        self.current_datapoints = 0

    def on_start(self) -> None:
        self._ecef_x = self.node.od["gps"]["skytraq_ecef_x"]
        self._ecef_y = self.node.od["gps"]["skytraq_ecef_y"]
        self._ecef_z = self.node.od["gps"]["skytraq_ecef_z"]
        self._ecef_vx = self.node.od["gps"]["skytraq_ecef_vx"]
        self._ecef_vy = self.node.od["gps"]["skytraq_ecef_vy"]
        self._ecef_vz = self.node.od["gps"]["skytraq_ecef_vz"]
        self._scet = self.node.od["scet"]

        self.active = self.node.od["mdb"]["active"]
        self._refresh_delay = self.node.od["mdb"]["refresh_delay"]
        self._data_per_file = self.node.od["mdb"]["data_per_file"]
        self._active_timeout = self.node.od["mdb"]["active_timeout"]
        self._idle_timeout = self.node.od["mdb"]["idle_timeout"]

        bat_2_rec = self.node.od["battery_2"]
        self._vbatt_bp1_obj = bat_2_rec["pack_1_vbatt"]
        self._vbatt_bp2_obj = bat_2_rec["pack_2_vbatt"]

    def on_loop(self) -> None:
        """"""
        if self.active.value is False:
            self.sleep(self._refresh_delay.value)
            return

        if self._node_mgr_service.node_status("gps") == 0xFF:  # Dead
            self.active.value = False

        if (
            self._node_mgr_service.node_status("gps") != 1  # On.
            and self._node_mgr_service.node_status("gps") != 2  # Boot
            and self.is_bat_lvl_good
        ):
            self._node_mgr_service.enable("gps")
            self.sleep(self._refresh_delay.value)
            self._enabled_time = monotonic()
            return
        elif self.is_bat_lvl_good is False:
            self.sleep(self._refresh_delay.value)
            return

        self._set_csv_gps()
        self._set_od_gps()
        if self.current_datapoints > self._data_per_file.value:
            self.update_files()

        if (
            self.is_bat_lvl_good is False
            or self._enabled_time - monotonic() > self._active_timeout.value
        ):
            self._idle()
        self.sleep(self._refresh_delay.value)

    def _idle(self) -> None:
        # Turn off the gps if it is on and we are not in state EDL or self._was_enabled
        # If the gps is on enter a loop where we check to make sure that the battery is good.
        # if not, shut it down.
        self._node_mgr_service.disable("gps")
        self.sleep(self._idle_timeout.value)
        self._enabled_time = monotonic()

    def update_files(self) -> None:
        new_file_name = new_oresat_file("gps-data", "c3", -1, ".csv")
        new_file_path = abspath(self.node.cache_base_dir + new_file_name)
        logger.error(f"Making file {new_file_name}")
        with open(new_file_path, "w") as f:
            f.write(self.data)

        self.node.fread_cache.add(new_file_path, True)
        self.data = ""

        files = self.node.fread_cache.files("gps-data")
        if len(files) > self._max_num_files.value:
            files = sorted(files)
            logger.error(f"Deleting file {files[0].name}")
            self.node.fread_cache.remove(files[0])

        self.current_datapoints = 0

    def _set_csv_gps(self) -> None:
        self.current_datapoints += 1
        self.data += str(self._ecef_x.value) + ","
        self.data += str(self._ecef_y.value) + ","
        self.data += str(self._ecef_z.value) + ","
        self.data += str(self._ecef_vx.value) + ","
        self.data += str(self._ecef_vy.value) + ","
        self.data += str(self._ecef_vz.value) + ","

        fulltime = self._scet.value.to_bytes(8, "little")
        secondstime = int.from_bytes(fulltime[:4], "little")
        self.data += str(secondstime) + "\n"

    def _set_od_gps(self) -> None:
        append = f"_{self.next_gps_index}"

        self.node.od["hist_ecef_x"]["ecef_x" + append].value = self._ecef_x.value
        self.node.od["hist_ecef_y"]["ecef_y" + append].value = self._ecef_y.value
        self.node.od["hist_ecef_z"]["ecef_z" + append].value = self._ecef_z.value
        self.node.od["hist_ecef_vx"]["ecef_vx" + append].value = self._ecef_vx.value
        self.node.od["hist_ecef_vy"]["ecef_vy" + append].value = self._ecef_vy.value
        self.node.od["hist_ecef_vz"]["ecef_vz" + append].value = self._ecef_vz.value

        fulltime = self._scet.value.to_bytes(8, "little")
        secondstime = int.from_bytes(fulltime[:4], "little")
        self.node.od["hist_unix_time"]["time" + append].value = secondstime
        self.next_gps_index += 1
        if self.next_gps_index > 32:
            self.next_gps_index = 1

    # stolen from state service. probably should use that one instead of redefining it.
    @property
    def is_bat_lvl_good(self) -> bool:
        """bool: Helper property to check if the battery levels are good."""

        return (
            self._vbatt_bp1_obj.value > self.BAT_LEVEL_LOW
            and self._vbatt_bp2_obj.value > self.BAT_LEVEL_LOW
        )

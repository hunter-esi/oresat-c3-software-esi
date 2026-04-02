"""'
Beacon Service

Handles making the beacon packets.
"""

import zlib
import time

import canopen
from canopen.objectdictionary import ODVariable
from olaf import Service, logger, scet_int_from_time

from .. import C3State
from ..protocols.ax25 import ax25_pack
from .radios import RadiosService

from pathlib import Path, PurePath

from cfdppy import CfdpState, PacketDestination, get_packet_destination
from cfdppy.exceptions import NoRemoteEntityCfgFound
from cfdppy.handler.dest import DestHandler
from cfdppy.handler.source import SourceHandler
from cfdppy.mib import (
    CheckTimerProvider,
    DefaultFaultHandlerBase,
    IndicationCfg,
    LocalEntityCfg,
    RemoteEntityCfg,
    RemoteEntityCfgTable,
)
from cfdppy.request import PutRequest
from cfdppy.user import (
    CfdpUserBase,
    FileSegmentRecvdParams,
    MetadataRecvParams,
    TransactionFinishedParams,
    TransactionId,
    TransactionParams,
)
from olaf import OreSatFile
from spacepackets.cfdp import CfdpLv
from spacepackets.cfdp.defs import ChecksumType, ConditionCode, TransmissionMode
from spacepackets.cfdp.tlv import (
    DirectoryListingRequest,
    DirectoryParams,
    ProxyPutRequest,
    ProxyPutRequestParams,
)
from spacepackets.countdown import Countdown
from spacepackets.seqcount import SeqCountProvider
from spacepackets.util import ByteFieldU8

from ..protocols.edl_packet import SRC_DEST_UNICLOGS, EdlPacket

from ..subsystems.antennas import Antennas
from threading import Lock


#TODO: WORK WITH KAMERON TO GET PACKAGE UPLOAD FOR PICOZED


#TODO: these were imported from the edl file upload script, but I don't actually know how necessary they are.
class PrintFaults(DefaultFaultHandlerBase):
    """Prints all faults to stdout"""

    def notice_of_suspension_cb(self, transaction_id, cond, progress):
        print(f"Transaction {transaction_id} suspended: {cond}. Progress {progress}")

    def notice_of_cancellation_cb(self, transaction_id, cond, progress):
        print(f"Transaction {transaction_id} cancelled: {cond}. Progress {progress}")

    def abandoned_cb(self, transaction_id, cond, progress):
        print(f"Transaction {transaction_id} abandoned: {cond}. Progress {progress}")

    def ignore_cb(self, transaction_id, cond, progress):
        print(f"Transaction {transaction_id} ignored: {cond}. Progress {progress}")


#TODO: these were imported from the edl file upload script, but I don't actually know how necessary they are.
class PrintUser(CfdpUserBase):
    """Prints all indications to sdtout"""

    def transaction_indication(self, transaction_indication_params: TransactionParams):
        print(f"Indication: Transaction. {transaction_indication_params}")

    def eof_sent_indication(self, transaction_id: TransactionId):
        print(f"Indication: EOF Sent for {transaction_id}.")

    def transaction_finished_indication(self, params: TransactionFinishedParams):
        print(f"Indication: Transaction Finished. {params}")

    def metadata_recv_indication(self, params: MetadataRecvParams):
        print(f"Indication: Metadata Recv. {params}")

    def file_segment_recv_indication(self, params: FileSegmentRecvdParams):
        print(f"Indication: File Segment Recv. {params}")

    def report_indication(self, transaction_id: TransactionId, status_report: Any):
        print("Indication: Report for {transaction_id}. {status_report}")

    def suspended_indication(self, transaction_id: TransactionId, cond_code: ConditionCode):
        print("Indication: Suspended for {transaction_id}. {cond_code}")

    def resumed_indication(self, transaction_id: TransactionId, progress: int):
        print("Indication: Resumed for {transaction_id}. {progress}")

    def fault_indication(
        self, transaction_id: TransactionId, cond_code: ConditionCode, progress: int
    ):
        print("Indication: Fault for {transaction_id}. {cond_code}. {progress}")

    def abandoned_indication(
        self, transaction_id: TransactionId, cond_code: ConditionCode, progress: int
    ):
        print("Indication: Abandoned for {transaction_id}. {cond_code}. {progress}")

    def eof_recv_indication(self, transaction_id: TransactionId):
        print("Indication: EOF Recv for {transaction_id}")


class CountdownProvider(CheckTimerProvider):
    """Copied from the cfdppy example. # actually copied from edl file upload.

    I think this is to allow for custom timeouts based on latency between local and remote
    entities? It doesn't set all the timers though, ACK timer I'm looking at you.
    """

    def provide_check_timer(self, local_entity_id, remote_entity_id, entity_type) -> Countdown: # TODO: figure out what this does.
        return Countdown(timedelta(seconds=5.0))


class OsirisService(Service):
    """OSIRIS Service."""

    def __init__(self):
        super().__init__()
        self.filenames = []

        filename_backup = open(self.form_path("filenames"), "r+", encoding="utf-8") # TODO: if this doesn't exist, create it.
        print(self.form_path(".filenames"))

        filenames_from_file = filename_backup.readlines() 
        for name in filenames_from_file:
            self.filenames.append(name[:-1])
        # state after: filename_backup is at the end of the file
        #              filenames has a fresh copy of everything in filename_backup


        #TODO: add the ability to check if the sdr is on and add that to the beacon (or some other downlink data.)
        self._payload_tx_enabled: canopen.objectdictionary.Variable = None
        # self._gpio_helical_1 = Gpio("FIRE_HELICAL_1", mock)
        # self._gpio_helical_2 = Gpio("FIRE_HELICAL_2", mock)

        # some timeouts that will prevent the need for more threads
        self.septentrio_timeout = time.time()
        self.picozed_timeout = time.time()
        self.sdr_timeout = time.time()

        self.septentrio_dif = False
        self.picozed_dif = False
        self.sdr_dif = False
        
        self.lock = Lock()


        self.localcfg = LocalEntityCfg(
            local_entity_id=ByteFieldU8(0),
            indication_cfg=IndicationCfg(),
            default_fault_handlers=PrintFaults(),
        )

        self.remote_entities = RemoteEntityCfgTable(
            [
                RemoteEntityCfg(
                    entity_id=ByteFieldU8(1),
                    max_file_segment_len=None,
                    max_packet_len=32768,
                    closure_requested=False,
                    crc_on_transmission=False,
                    default_transmission_mode=TransmissionMode.UNACKNOWLEDGED,
                    crc_type=ChecksumType.CRC_32,
                ),
            ]
        )

        self.src = SourceHandler(
            cfg=self.localcfg,
            user=PrintUser(),
            remote_cfg_table=self.remote_entities,
            check_timer_provider=CountdownProvider(),
            seq_num_provider=SeqCountProvider(16), #TODO: Fix the sequence numbers.
        )

    def on_start(self):
        # beacon_rec = self.node.od["beacon"]
        
        self.new_state = 1
        self.osiris_state = 1
        # self._payload_tx_enabled = self.node.od["sdr"]["tx_enabled"]  #what the fuck is this supposed to be
        self.attempted_power_on = False
        self.ready_to_transmit = False #TODO: Rethink 
        

        # self.node.add_sdo_callbacks("inst", "new_data", None, self.grab_data)
        

    def _start_sdr(self):
        # self._gpio_helical_1.mode = GPIO_OUT
        # self._gpio_helical_2.mode = GPIO_OUT

        # self._gpio_helical_1.high()
        # self._gpio_helical_2.high()
        return #temp until we can get the exact procedure worked out

    def _stop_sdr(self):
        # self._gpio_helical_1.low()
        # self._gpio_helical_2.low()

        # self._gpio_helical_1.mode = GPIO_IN
        # self._gpio_helical_2.mode = GPIO_IN
        return #temp until we can get the exact procedure worked out


    def _init_off():
        # normalize conditions based on where we were (still held in osiris_state)
        if self.osiris_state == 1: # on
            # send the command to get the last of the data


            # turn of the oscilator
            self.node.sdo_write("osiris", "power", "picozed", 0)
            self.node.sdo_write("osiris", "power", "septentrio", 0)
            self.node.sdo_write("osiris", "power", "ocxo", 0)

            # Turn off osiris
            #OPD.turn_off(osiris)
        elif self.osiris_state == 2: # trans
            self._stop_sdr()
            self.attempted_power_on = False
            self.ready_to_transmit = False



    def _init_on():
        # tell the OPD to turn the science card on

        # TODO: Fix the magic numbers
        print("Turning on OXCO")
        self.node.sdo_write("osiris", "power", "ocxo", 1)
        self.septentrio_timeout = time.time() + 300
        self.septentrio_dif = True


    def _off (self): # I don't yet know if this will be needed for anything, so at this point it does nothing
        return

    def _on (self):

        print("Checking for new data.")
        new_data = self.node.sdo_read("inst", "new_data", None)
        if new_data:
            print("There is new data.")
            bytes_400kb = self.node.sdo_read("inst", "400kb", "data")
            name_400kb = self.node.sdo_read("inst", "400kb", "name")

            with open(self.form_path(name_400kb), "w", encoding="utf-8") as f:
                f.write(str(bytes_400kb))

            filename_backup = open(self.form_path("filenames"), "r+", encoding="utf-8")
            filename_backup.seek(0,2)

            self.filenames.append(name_400kb)
            filename_backup.write(name_400kb + "\n")


            bytes_100kb = self.node.sdo_read("inst", "100kb_1", "data")
            name_100kb = self.node.sdo_read("inst", "100kb_1", "name")

            with open(self.form_path(name_100kb), "w", encoding="utf-8") as f:
                f.write(str(bytes_100kb))

            self.filenames.append(name_100kb)
            filename_backup.write(name_100kb + "\n")


            bytes_100kb = self.node.sdo_read("inst", "100kb_2", "data")
            name_100kb = self.node.sdo_read("inst", "100kb_2", "name")

            with open(self.form_path(name_100kb), "w", encoding="utf-8") as f:
                f.write(str(bytes_100kb))

            self.filenames.append(name_100kb)
            filename_backup.write(name_100kb + "\n")
            self.node.sdo_write("inst", "new_data", None, 0)

    def _trans (self):
        #TODO: work out some kind of timeout when sending power on command.
        if self._payload_tx_enabled.value and not self.attempted_power_on:
            self._start_sdr()
            self.attempted_power_on = True

        elif  self._payload_tx_enabled.value and self.attempted_power_on and not self.ready_to_transmit:
            sdr_status = self.node.sdo_read("sdr", "sdr_status", None)
            if sdr_status == 1:
                print("Recieved signal that SDR is booting up.")
                self.sleep(0.25) # wait 0.25 seconds before checking again.
            elif sdr_status == 2:
                print("Recieved signal that SDR is ready to transmit")
                self.ready_to_transmit = True #TODO: this is bad. Should only transmit if sdr_status == 2.

        elif  self._payload_tx_enabled.value and self.ready_to_transmit == True:

            self.send(".filenames")
            while file in filenames:
                file = file[:-1]
                self.send(file)
            
            self.send(".filenames")
            while file in filenames:
                self.send(file)

            self.filename_backup.seek(0,0)
            self.filename_backup.truncate()

            
            
            self._stop_sdr()
            self._payload_tx_enabled.value = False
            self.attempted_power_on = False
            self.ready_to_transmit = False


    def on_loop(self):
        self.lock.acquire() # state changes are probably going to be controlled by the c3 (might need to implement delayed commands)
        # so that might cause some issues with initializing states.

        # timeout control
        if self.septentrio_dif and time.time() > self.septentrio_timeout:
            self.node.sdo_write("osiris", "power", "septentrio", 1)
            self.picozed_timeout = time.time() + 15
            self.septentrio_dif = False
            self.picozed_dif = True
            print("Turning on Septentrio")
        if self.picozed_dif and time.time() > self.picozed_timeout:
            self.node.sdo_write("osiris", "power", "picozed", 1)
            self.picozed_dif = False
            print("Turning on Picozed")

        # similar mechanism, different goal. Tries again to turn on the banc3 and resets the timer.
        # also note down in some beacon information if we've got issues.
        if self.sdr_dif and time.time() > self.sdr_timeout:
            # TODO: that.
            a = 1


        if self.new_state != self.osiris_state:
            if self.new_state == 0:
                _init_off()
                self.osiris_state = self.new_state


        if self.osiris_state == 0: # off
            self._off()
        elif self.osiris_state == 1: # off
            self._on()
        elif self.osiris_state == 2: # trans
            self._trans()


        self.lock.release()

        self.sleep(1)
			# self.node.od["400kb"]["data"].value =   self.data

    def form_path (self, file_path: str):
        return Path(PurePath(Path.home(), Path("OSIRIS_unsent_data/" + file_path)))

    def downlink_file(self, file_path: str):
        """Create and send CFDP packets to the SDR card."""
        path = Path(PurePath(Path.home(), Path("OSIRIS_unsent_data/" + file_path)))

        put = PutRequest(
            destination_id=ByteFieldU8(1),
            source_file=path,
            dest_file=Path(file_path),
            trans_mode=None,
            closure_requested=None,
        )

        assert self.src.put_request(put)
        self.src.state_machine()

        while self.src.packets_ready:
            pdu = self.src.get_next_packet().pdu
            packet = EdlPacket(pdu, 0, SRC_DEST_UNICLOGS)
            data = packet.pack(bytes(32))
            # packet = EdlPacket(payload, self._sequence_number, SRC_DEST_UNICLOGS) #TODO: get the hmac and seqnum
            # data = packet.pack(self._hmac_key)
            self.node.sdo_write("sdr", "tx_data", None, data)

            # crash
            self.src.state_machine()

        # print("\n\n\nDone sending file\n\n\n")

    def grab_data(self, value: int):
        if (value == 1):
            print("there's new data.")
            # bytes_400kb = self._network['inst'].sdo["400kb"]["data"]
            bytes_400kb = self.node.sdo_read("inst", "400kb", "data").decode("utf8")
            print(bytes_400kb)
            name_400kb = self.node.sdo_read("inst", "400kb", "name").decode("utf8")
            return

            with open(name_400kb, "w", encoding="utf-8") as f:
                f.write(bytes_400kb)

            self.filenames.append(name_400kb)
            self.filename_backup.write(name_400kb + "\n")

            bytes_100kb = self.node.sdo_read("inst", "100kb_1", "data").decode("utf8")
            name_100kb = self.node.sdo_read("inst", "100kb_1", "name").decode("utf8")

            with open(name_100kb, "w", encoding="utf-8") as f:
                f.write(bytes_100kb)

            self.filenames.append(name_100kb)
            self.filename_backup.write(name_100kb + "\n")
            self.node.sdo_write("inst", "new_data", None, 1)
        


    def _on_write_send_now(self, value: bool):
        """SDO write callback to send a beacon immediately."""

        if value:
            new_osiris_state = 2

import threading
import data_frame_factory
import queue
import arq_session
import helpers

class ARQSessionIRS(arq_session.ARQSession):

    STATE_CONN_REQ_RECEIVED = 0
    STATE_WAITING_INFO = 1
    STATE_WAITING_DATA = 2
    STATE_FAILED = 3
    STATE_ENDED = 10

    RETRIES_CONNECT = 3
    RETRIES_TRANSFER = 3 # we need to increase this

    TIMEOUT_CONNECT = 6
    TIMEOUT_DATA = 6

    def __init__(self, config: dict, tx_frame_queue: queue.Queue, dxcall: str, session_id: int):
        super().__init__(config, tx_frame_queue, dxcall)

        self.id = session_id
        self.speed = 0
        self.frames_per_burst = 3
        self.version = 1
        self.snr = 0
        self.dx_snr = 0
        self.retries = self.RETRIES_TRANSFER

        self.state = self.STATE_CONN_REQ_RECEIVED

        self.event_info_received = threading.Event()
        self.event_data_received = threading.Event()
        
        self.frame_factory = data_frame_factory.DataFrameFactory(self.config)

        self.received_frame = None
        self.received_data = None
        self.received_bytes = 0
        self.received_crc = None

    def generate_id(self):
        pass

    def set_state(self, state):
        self.log(f"ARQ Session IRS {self.id} state {self.state}")
        self.state = state

    def set_modem_decode_modes(self, modes):
        pass

    def _all_data_received(self):
        return self.received_bytes == len(self.received_data)

    def _final_crc_check(self):
        return self.received_crc == helpers.get_crc_32(bytes(self.received_data)).hex()

    def handshake_session(self):
        if self.state in [self.STATE_CONN_REQ_RECEIVED, self.STATE_WAITING_INFO]:
            self.send_open_ack()
            self.set_state(self.STATE_WAITING_INFO)
            return True
        return False

    def handshake_info(self):
        if self.state == self.STATE_WAITING_INFO and not self.event_info_received.wait(self.TIMEOUT_CONNECT):
            return False

        self.send_info_ack()
        self.set_state(self.STATE_WAITING_DATA)
        return True

    def send_info_ack(self):
            info_ack = self.frame_factory.build_arq_session_info_ack(
                self.id, self.received_crc, self.snr, 
                self.speed_level, self.frames_per_burst)
            self.transmit_frame(info_ack)


    def receive_data(self):
        self.retries = self.RETRIES_TRANSFER
        while self.retries > 0 and not self._all_data_received():
            if self.event_data_received.wait(self.TIMEOUT_DATA):
                self.process_incoming_data()
                self.send_data_ack_nack(True)
                self.retries = self.RETRIES_TRANSFER
            else:
                self.send_data_ack_nack(False)
            self.retries -= 1

        if self._all_data_received():
            if self._final_crc_check():
                self.set_state(self.STATE_ENDED)
            else:
                self.logger.warning("CRC check failed.")
                self.set_state(self.STATE_FAILED)

        else:
            self.set_state(self.STATE_FAILED)


    def runner(self):

        if not self.handshake_session():
            return False

        if not self.handshake_info():
            return False

        if not self.receive_data(): 
            return False
        return True

    def run(self):
        self.set_state(self.STATE_CONN_REQ_RECEIVED)
        self.thread = threading.Thread(target=self.runner, 
                                       name=f"ARQ IRS Session {self.id}", daemon=False)
        self.thread.start()

    def send_open_ack(self):
        ack_frame = self.frame_factory.build_arq_session_open_ack(
            self.id,
            self.dxcall, 
            self.version,
            self.snr)
        self.transmit_frame(ack_frame)

    def send_data_ack_nack(self, ack: bool):
        if ack:
            builder = self.frame_factory.build_arq_burst_ack
        else:
            builder = self.frame_factory.build_arq_burst_nack

        frame = builder (
            self.id, self.received_bytes, 
            self.speed_level, self.frames_per_burst, self.snr)
        
        self.transmit_frame(frame)

    def calibrate_speed_settings(self):

        # decrement speed level after the 2nd retry
        if self.RETRIES_TRANSFER - self.retries >= 2:
            self.speed -= 1
            self.speed = max(self.speed, 0)
            return

        # increment speed level after 2 successfully sent bursts and fitting snr
        # TODO





        self.speed = self.speed
        self.frames_per_burst = self.frames_per_burst

    def on_info_received(self, frame):
        if self.state != self.STATE_WAITING_INFO:
            self.logger.warning("Discarding received INFO.")
            return
        
        self.received_data = bytearray(frame['total_length'])
        self.received_crc = frame['total_crc']
        self.dx_snr = frame['snr']

        self.calibrate_speed_settings()
        self.set_modem_decode_modes(None)

        self.event_info_received.set()

    def on_data_received(self, frame):
        if self.state != self.STATE_WAITING_DATA:
            self.logger.warning(f"ARQ Session: Received data while in state {self.state}. Ignoring.")
            return
        
        self.received_frame = frame
        self.event_data_received.set()

    def process_incoming_data(self):
        if self.received_frame['offset'] != self.received_bytes:
            self.logger.info(f"Discarding data frame due to wrong offset", frame=self.frame_received)
            return False

        remaining_data_length = len(self.received_data) - self.received_bytes

        # Is this the last data part?
        if remaining_data_length <= len(self.received_frame['data']):
            # we only want the remaining length, not the entire frame data
            data_part = self.received_frame['data'][:remaining_data_length]
        else:
            # we want the entire frame data
            data_part = self.received_frame['data']

        self.received_data[self.received_frame['offset']:] = data_part
        self.received_bytes += len(data_part)

        return True

    def on_burst_ack_received(self, ack):
        self.event_transfer_ack_received.set()
        self.speed_level = ack['speed_level']

    def on_burst_nack_received(self, nack):
        self.speed_level = nack['speed_level']

    def on_disconnect_received(self):
        self.abort()

    def abort(self):
        self.state = self.STATE_DISCONNECTED

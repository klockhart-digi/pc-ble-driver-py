#
# Copyright (c) 2016 Nordic Semiconductor ASA
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
#   1. Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
#   2. Redistributions in binary form must reproduce the above copyright notice, this
#   list of conditions and the following disclaimer in the documentation and/or
#   other materials provided with the distribution.
#
#   3. Neither the name of Nordic Semiconductor ASA nor the names of other
#   contributors to this software may be used to endorse or promote products
#   derived from this software without specific prior written permission.
#
#   4. This software must only be used in or with a processor manufactured by Nordic
#   Semiconductor ASA, or in or with a processor manufactured by a third party that
#   is used in combination with a processor manufactured by Nordic Semiconductor.
#
#   5. Any software provided in binary or object form under this license must not be
#   reverse engineered, decompiled, modified and/or disassembled.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
# ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
import Queue
import logging
import wrapt
from threading  import Condition, Lock
from ble_driver import *
from exceptions import NordicSemiException

logger  = logging.getLogger(__name__)

class DbConnection(object):
    def __init__(self):
        self.services     = list()


    def get_char_value_handle(self, uuid):
        assert isinstance(uuid, BLEUUID), 'Invalid argument type'

        for s in self.services:
            for c in s.chars:
                if (c.uuid.value == uuid.value) and (c.uuid.base.type == uuid.base.type):
                    for d in c.descs:
                        if d.uuid.value == uuid.value:
                            return d.handle
        return None


    def get_cccd_handle(self, uuid):
        assert isinstance(uuid, BLEUUID), 'Invalid argument type'

        for s in self.services:
            for c in s.chars:
                if (c.uuid.value == uuid.value) and (c.uuid.base.type == uuid.base.type):
                    for d in c.descs:
                        if (d.uuid.value == BLEUUID.Standard.cccd):
                            return d.handle
                    break
        return None


    def get_char_handle(self, uuid):
        assert isinstance(uuid, BLEUUID), 'Invalid argument type'

        for s in self.services:
            for c in s.chars:
                if (c.uuid.value == uuid.value) and (c.uuid.base.type == uuid.base.type):
                    return c.handle_decl
        return None


    def get_char_uuid(self, handle):
        for s in self.services:
            for c in s.chars:
                if (c.handle_decl <= handle) and (c.end_handle >= handle):
                    return c.uuid


class EvtSync(object):
    def __init__(self, events):
        self.conds = dict()
        for evt in events:
            self.conds[evt] = Condition(Lock())
        self.data = None


    def wait(self, evt, timeout = 5):
        with self.conds[evt]:
            self.conds[evt].wait(timeout=timeout)
            return self.data


    def notify(self, evt, data=None):
        with self.conds[evt]:
            self.data = data
            self.conds[evt].notify_all()



class BLEAdapterObserver(object):
    def __init__(self, *args, **kwargs):
        super(BLEAdapterObserver, self).__init__()


    def on_notification(self, ble_adapter, conn_handle, uuid, data):
        pass
        
        
    def on_conn_param_update_request(self, ble_adapter, conn_handle, conn_params):
        # Default behaviour is to accept connection parameter update
        ble_adapter.conn_param_update(conn_handle, conn_params)


class BLEAdapter(BLEDriverObserver):
    observer_lock   = Lock()
    def __init__(self, ble_driver):
        super(BLEAdapter, self).__init__()
        self.driver             = ble_driver
        self.driver.observer_register(self)

        self.conn_in_progress   = False
        self.observers          = list()
        self.db_conns           = dict()
        self.evt_sync           = dict()


    def connect(self, address, scan_params=None, conn_params=None):
        if self.conn_in_progress:
            return
        self.driver.ble_gap_connect(address     = address,
                                    scan_params = scan_params,
                                    conn_params = conn_params)
        self.conn_in_progress = True


    def disconnect(self, conn_handle):
        self.driver.ble_gap_disconnect(conn_handle)


    @wrapt.synchronized(observer_lock)
    def observer_register(self, observer):
        self.observers.append(observer)


    @wrapt.synchronized(observer_lock)
    def observer_unregister(self, observer):
        self.observers.remove(observer)


    @NordicSemiErrorCheck(expected = BLEGattStatusCode.success)
    def service_discovery(self, conn_handle, uuid=None):
        self.driver.ble_gattc_prim_srvc_disc(conn_handle, uuid, 0x0001)

        while True:
            response = self.evt_sync[conn_handle].wait(evt = BLEEvtID.gattc_evt_prim_srvc_disc_rsp)

            if response['status'] == BLEGattStatusCode.success:
                self.db_conns[conn_handle].services.extend(response['services'])
            elif response['status'] == BLEGattStatusCode.attribute_not_found:
                break
            else:
                return response['status']

            if response['services'][-1].end_handle == 0xFFFF:
                break
            else:
                self.driver.ble_gattc_prim_srvc_disc(conn_handle,
                                                     uuid,
                                                     response['services'][-1].end_handle + 1)

        for s in self.db_conns[conn_handle].services:
            self.driver.ble_gattc_char_disc(conn_handle, s.start_handle, s.end_handle)
            while True:
                response = self.evt_sync[conn_handle].wait(evt = BLEEvtID.gattc_evt_char_disc_rsp)

                if response['status'] == BLEGattStatusCode.success:
                    map(s.char_add, response['characteristics'])
                elif response['status'] == BLEGattStatusCode.attribute_not_found:
                    break
                else:
                    return response['status']

                self.driver.ble_gattc_char_disc(conn_handle,
                                                response['characteristics'][-1].handle_decl + 1,
                                                s.end_handle)

            for ch in s.chars:
                self.driver.ble_gattc_desc_disc(conn_handle, ch.handle_value, ch.end_handle)
                while True:
                    response = self.evt_sync[conn_handle].wait(evt = BLEEvtID.gattc_evt_desc_disc_rsp)

                    if response['status'] == BLEGattStatusCode.success:
                        ch.descs.extend(response['descriptions'])
                    elif response['status'] == BLEGattStatusCode.attribute_not_found:
                        break
                    else:
                        return response['status']

                    if response['descriptions'][-1].handle == ch.end_handle:
                        break
                    else:
                        self.driver.ble_gattc_desc_disc(conn_handle,
                                                        response['descriptions'][-1].handle + 1,
                                                        ch.end_handle)
        return BLEGattStatusCode.success


    @NordicSemiErrorCheck(expected = BLEGattStatusCode.success)
    def enable_notification(self, conn_handle, uuid):
        cccd_list = [1, 0]

        handle = self.db_conns[conn_handle].get_cccd_handle(uuid)
        if handle == None:
            raise NordicSemiException('CCCD not found')

        write_params = BLEGattcWriteParams(BLEGattWriteOperation.write_req,
                                           BLEGattExecWriteFlag.unused,
                                           handle,
                                           cccd_list,
                                           0)

        self.driver.ble_gattc_write(conn_handle, write_params)
        result = self.evt_sync[conn_handle].wait(evt = BLEEvtID.gattc_evt_write_rsp)
        return result['status']

    
    @NordicSemiErrorCheck(expected = BLEGattStatusCode.success)
    def disable_notification(self, conn_handle, uuid):
        cccd_list = [0, 0]

        handle = self.db_conns[conn_handle].get_cccd_handle(uuid)
        if handle == None:
            raise NordicSemiException('CCCD not found')

        write_params = BLEGattcWriteParams(BLEGattWriteOperation.write_req,
                                           BLEGattExecWriteFlag.unused,
                                           handle,
                                           cccd_list,
                                           0)

        self.driver.ble_gattc_write(conn_handle, write_params)
        result = self.evt_sync[conn_handle].wait(evt = BLEEvtID.gattc_evt_write_rsp)
        return result['status']
       

    def conn_param_update(self, conn_handle, conn_params):
        self.driver.ble_gap_conn_param_update(conn_handle, conn_params)

    
    @NordicSemiErrorCheck(expected = BLEGattStatusCode.success)
    def write_req(self, conn_handle, uuid, data):
        handle = self.db_conns[conn_handle].get_char_value_handle(uuid)
        if handle == None:
            raise NordicSemiException('Characteristic value handler not found')
        write_params = BLEGattcWriteParams(BLEGattWriteOperation.write_req,
                                           BLEGattExecWriteFlag.unused,
                                           handle,
                                           data,
                                           0)
        self.driver.ble_gattc_write(conn_handle, write_params)
        result = self.evt_sync[conn_handle].wait(evt = BLEEvtID.gattc_evt_write_rsp)
        return result['status']


    def write_cmd(self, conn_handle, uuid, data):
        handle = self.db_conns[conn_handle].get_char_value_handle(uuid)
        if handle == None:
            raise NordicSemiException('Characteristic value handler not found')
        write_params = BLEGattcWriteParams(BLEGattWriteOperation.write_cmd,
                                           BLEGattExecWriteFlag.unused,
                                           handle,
                                           data,
                                           0)
        self.driver.ble_gattc_write(conn_handle, write_params)
        self.evt_sync[conn_handle].wait(evt = BLEEvtID.evt_tx_complete)


    def on_gap_evt_connected(self, ble_driver, conn_handle, peer_addr, own_addr, role, conn_params):
        self.db_conns[conn_handle]  = DbConnection()
        self.evt_sync[conn_handle]  = EvtSync(events = BLEEvtID)
        self.conn_in_progress       = False

    
    def on_gap_evt_disconnected(self, ble_driver, conn_handle, reason):
        del self.db_conns[conn_handle]
        del self.evt_sync[conn_handle]


    def on_gap_evt_timeout(self, ble_driver, conn_handle, src):
        if src == BLEGapTimeoutSrc.conn:
            self.conn_in_progress = False


    def on_evt_tx_complete(self, ble_driver, conn_handle, **kwargs):
        self.evt_sync[conn_handle].notify(evt = BLEEvtID.evt_tx_complete, data = kwargs)


    def on_gattc_evt_write_rsp(self, ble_driver, conn_handle, **kwargs):
        self.evt_sync[conn_handle].notify(evt = BLEEvtID.gattc_evt_write_rsp, data = kwargs)


    def on_gattc_evt_prim_srvc_disc_rsp(self, ble_driver, conn_handle, **kwargs):
        self.evt_sync[conn_handle].notify(evt = BLEEvtID.gattc_evt_prim_srvc_disc_rsp, data = kwargs)


    def on_gattc_evt_char_disc_rsp(self, ble_driver, conn_handle, **kwargs):
        self.evt_sync[conn_handle].notify(evt = BLEEvtID.gattc_evt_char_disc_rsp, data = kwargs)


    def on_gattc_evt_desc_disc_rsp(self, ble_driver, conn_handle, **kwargs):
        self.evt_sync[conn_handle].notify(evt = BLEEvtID.gattc_evt_desc_disc_rsp, data = kwargs)

    
    @wrapt.synchronized(observer_lock)
    def on_gap_evt_conn_param_update_request(self, ble_driver, conn_handle, conn_params):
        for obs in self.observers:
            obs.on_conn_param_update_request(ble_adapter = self,
                                             conn_handle = conn_handle, 
                                             conn_params = conn_params)

    
    @wrapt.synchronized(observer_lock)
    def on_gattc_evt_hvx(self, ble_driver, conn_handle, status, error_handle, attr_handle, hvx_type, data):
        if status != BLEGattStatusCode.success:
            logger.error("Error. Handle value notification failed. Status {}.".format(status))
            return

        if hvx_type == BLEGattHVXType.notification:
            uuid = self.db_conns[conn_handle].get_char_uuid(attr_handle)
            if uuid == None:
                raise NordicSemiException('UUID not found')

            for obs in self.observers:
                obs.on_notification(ble_adapter = self,
                                    conn_handle = conn_handle, 
                                    uuid        = uuid,
                                    data        = data)

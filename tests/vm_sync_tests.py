#!/opt/rocks/bin/python

import sys, os
lib_path = os.path.abspath('src/img-storage')
sys.path.insert(1, lib_path)

import unittest
from mock import MagicMock, ANY
import mock
from imgstorage.imgstoragevm import VmDaemon
from imgstorage.rabbitmqclient import RabbitMQCommonClient

import uuid
import time

from pysqlite2 import dbapi2 as sqlite3

from pika.spec import BasicProperties
from StringIO import StringIO

class TestVmSyncFunctions(unittest.TestCase):

    def mock_rabbitcli(self, exchange, exchange_type, process_message=None):
        class MockRabbitMQCommonClient(RabbitMQCommonClient):
            def publish_message(self, message, routing_key=None, reply_to=None, exchange=None, correlation_id=None, on_fail=None):
                return
        return MockRabbitMQCommonClient

    @mock.patch('imgstorage.imgstoragevm.RabbitMQCommonClient')
    def setUp(self, mock_rabbit_vm):
        self.vm_client = VmDaemon()
        mock_rabbit_vm.publish_message = MagicMock()
        self.vm_client.process_message = MagicMock()
        
        self.vm_client.SQLITE_DB = '/tmp/test_db_%s'%uuid.uuid4()
        self.vm_client.run()

        with sqlite3.connect(self.vm_client.SQLITE_DB) as con:
            cur = con.cursor()
            cur.execute('INSERT INTO zvol_calls VALUES (?,?,?,?,?,?,?)',('vol1', 'iqn.2001-04.com.nas-0-1-vol1', 12345, 'reply_to', 'corr_id', 0, 1))
            con.commit()

    def tearDown(self):
        os.remove(self.vm_client.SQLITE_DB)

    @mock.patch('imgstorage.imgstoragevm.runCommand')
    def test_run_sync_initial_synced(self, mockRunCommand):
        zvol = 'vol1'
        target = 'iqn.2001-04.com.nas-0-1-%s'%zvol
        bdev = 'sdc'
        mockRunCommand.side_effect = self.create_iscsiadm_side_effect(target, bdev, 32)

        self.vm_client.run_sync()
        print mockRunCommand.mock_calls
        mockRunCommand.assert_any_call(['dmsetup', 'suspend', '/dev/mapper/%s-snap'%zvol])
        mockRunCommand.assert_any_call(['dmsetup', 'reload', '/dev/mapper/%s-snap'%zvol, '--table', '0 12345 snapshot-merge /dev/zvol/tank/%s /dev/zvol/tank/%s-temp-write P 16'%(zvol, zvol)])
        mockRunCommand.assert_any_call(['dmsetup', 'resume', '/dev/mapper/%s-snap'%zvol])
        mockRunCommand.assert_any_call(['dmsetup', 'reload', '/dev/mapper/%s-snap'%zvol, '--table', '0 12345 linear /dev/zvol/tank/%s 0'%(zvol)])
        mockRunCommand.assert_any_call(['zfs', 'destroy', 'tank/%s-temp-write'%zvol])
        mockRunCommand.assert_any_call(['iscsiadm', '-m', 'node', '-T', 'iqn.2001-04.com.nas-0-1-%s'%zvol, '-u'])

        print  mockRunCommand.call_count
        assert 10 == mockRunCommand.call_count
        self.vm_client.queue_connector.publish_message.assert_called_with({'action': 'zvol_synced', 'status': 'success', 'zvol': u'vol1'}, u'reply_to', correlation_id=u'corr_id')


    @mock.patch('imgstorage.imgstoragevm.runCommand')
    def test_run_sync_initial_not_synced(self, mockRunCommand):
        zvol = 'vol1'
        target = 'iqn.2001-04.com.nas-0-1-%s'%zvol
        bdev = 'sdc'
        mockRunCommand.side_effect = self.create_iscsiadm_side_effect(target, bdev, 100000)

        self.vm_client.run_sync()
        print mockRunCommand.mock_calls
        mockRunCommand.assert_any_call(['dmsetup', 'suspend', '/dev/mapper/%s-snap'%zvol])
        mockRunCommand.assert_any_call(['dmsetup', 'reload', '/dev/mapper/%s-snap'%zvol, '--table', '0 12345 snapshot-merge /dev/zvol/tank/%s /dev/zvol/tank/%s-temp-write P 16'%(zvol, zvol)])
        mockRunCommand.assert_any_call(['dmsetup', 'resume', '/dev/mapper/%s-snap'%zvol])

        print  mockRunCommand.call_count
        assert 4 == mockRunCommand.call_count
        assert not self.vm_client.queue_connector.publish_message.called, 'rabbotmq message was sent and should not have been'

    """ Testing zvol sync """
    @mock.patch('imgstorage.imgstoragevm.runCommand')
    def test_sync_zvol_success(self, mockRunCommand):
        zvol= 'vol2'
        target = 'iqn.2001-04.com.nas-0-1-%s'%zvol
        bdev = 'sdc'
        mockRunCommand.side_effect = self.create_iscsiadm_side_effect(target, bdev, 0)

        self.vm_client.sync_zvol(
            {'action': 'sync_zvol', 'zvol':zvol, 'target':target},
            BasicProperties(reply_to='reply_to', message_id='message_id'))

    def create_iscsiadm_side_effect(self, target, bdev, cur_sync_stats_int):
        def iscsiadm_side_effect(*args, **kwargs):
            if args[0][:3] == ['iscsiadm', '-m', 'session']:        return (iscsiadm_session_response%(target, bdev)).splitlines() # list local devices
            elif args[0][:3] == ['iscsiadm', '-m', 'discovery']:    return (iscsiadm_discovery_response%target).splitlines() # find remote targets
            elif args[0][:3] == ['iscsiadm', '-m', 'node']:         return '\n'.splitlines() # connect to iscsi target
            elif args[0][:2] == ['dmsetup', 'status']:         return (dmsetup_status_response%cur_sync_stats_int).splitlines()
            elif args[0][0] == 'blockdev':                          return '12345'.splitlines()
        return iscsiadm_side_effect



    def check_zvol_busy(self, zvol):
        with sqlite3.connect(self.vm_client.SQLITE_DB) as con:
            cur = con.cursor()
            cur.execute('SELECT count(*) from zvol_calls where zvol = ?',[zvol])
            num_rows = cur.fetchone()[0]
            return num_rows > 0


tgtadm_response = """
Target 1: iqn.2001-04.com.nas-0-1-%s
    System information:
        Driver: iscsi
        State: ready
    I_T nexus information:
        I_T nexus: 1
            Initiator: iqn.1994-05.com.redhat:dd87ffb48f6e
            Connection: 0
                IP Address: 10.2.20.250
    LUN information:
        LUN: 0
            Type: controller
            SCSI ID: IET     00010000
            SCSI SN: beaf10
            Size: 0 MB, Block size: 1
            Online: Yes
            Removable media: No
            Prevent removal: No
            Readonly: No
            Backing store type: null
            Backing store path: None
            Backing store flags:
        LUN: 1
            Type: disk
            SCSI ID: IET     00010001
            SCSI SN: beaf11
            Size: 1074 MB, Block size: 512
            Online: Yes
            Removable media: No
            Prevent removal: No
            Readonly: No
            Backing store type: rdwr
            Backing store path: /dev/tank/%s
            Backing store flags:
    Account information:
    ACL information:
        10.2.20.250"""


tgt_setup_lun_response = """
Using transport: iscsi
Creating new target (name=iqn.2001-04.com.nas-0-1-%s, tid=1)
Adding a logical unit (/dev/tank/%s) to target, tid=1
Accepting connections only from 10.1.1.1"""

iscsiadm_session_response = """
iSCSI Transport Class version 2.0-870
version 6.2.0-873.10.el6
Target: iqn.2001-04.com.nas-0-1-compute-0-2-0-vol
    Current Portal: 10.2.20.254:3260,1
    Persistent Portal: 10.2.20.254:3260,1
        **********
        Interface:
        **********
        Iface Name: default
        Iface Transport: tcp
        Iface Initiatorname: iqn.1994-05.com.redhat:6453f1cc15cf
        Iface IPaddress: 10.2.20.252
        Iface HWaddress: <empty>
        Iface Netdev: <empty>
        SID: 49
        iSCSI Connection State: LOGGED IN
        iSCSI Session State: LOGGED_IN
        Internal iscsid Session State: NO CHANGE
        *********
        Timeouts:
        *********
        Recovery Timeout: 120
        Target Reset Timeout: 30
        LUN Reset Timeout: 30
        Abort Timeout: 15
        *****
        CHAP:
        *****
        username: <empty>
        password: ********
        username_in: <empty>
        password_in: ********
        ************************
        Negotiated iSCSI params:
        ************************
        HeaderDigest: None
        DataDigest: None
        MaxRecvDataSegmentLength: 262144
        MaxXmitDataSegmentLength: 8192
        FirstBurstLength: 65536
        MaxBurstLength: 262144
        ImmediateData: Yes
        InitialR2T: Yes
        MaxOutstandingR2T: 1
        ************************
        Attached SCSI devices:
        ************************
        Host Number: 54 State: running
        scsi54 Channel 00 Id 0 Lun: 0
        scsi54 Channel 00 Id 0 Lun: 1
            Attached scsi disk sdb      State: running
Target: %s
    Current Portal: 10.2.20.247:3260,1
    Persistent Portal: 10.2.20.247:3260,1
        **********
        Interface:
        **********
        Iface Name: default
        Iface Transport: tcp
        Iface Initiatorname: iqn.1994-05.com.redhat:6453f1cc15cf
        Iface IPaddress: <empty>
        Iface HWaddress: <empty>
        Iface Netdev: <empty>
        SID: 69
        iSCSI Connection State: TRANSPORT WAIT
        iSCSI Session State: FREE
        Internal iscsid Session State: REOPEN
        *********
        Timeouts:
        *********
        Recovery Timeout: 120
        Target Reset Timeout: 30
        LUN Reset Timeout: 30
        Abort Timeout: 15
        *****
        CHAP:
        *****
        username: <empty>
        password: ********
        username_in: <empty>
        password_in: ********
        ************************
        Negotiated iSCSI params:
        ************************
        HeaderDigest: None
        DataDigest: None
        MaxRecvDataSegmentLength: 262144
        MaxXmitDataSegmentLength: 8192
        FirstBurstLength: 65536
        MaxBurstLength: 262144
        ImmediateData: Yes
        InitialR2T: Yes
        MaxOutstandingR2T: 1
        ************************
        Attached SCSI devices:
        ************************
        Host Number: 74 State: running
        scsi74 Channel 00 Id 0 Lun: 0
        scsi74 Channel 00 Id 0 Lun: 1
            Attached scsi disk %s      State: transport-offline"""

zfs_list_response = """
NAME                                             USED  AVAIL  REFER  MOUNTPOINT
tank/vol2@1407871125717                         1.66K      -  26.6K  -
tank/vol2@1407871144409                             0      -  26.6K  -
tank/vol3@1407871530335                         1.66K      -  26.6K  -
tank/vol3@1407871537905                             0      -  26.6K  -
tank/vol3@1407871835844                             0      -  26.6K  -
tank/vol3@1407872122976                             0      -   303M  -
tank/vol3@1407872222305                             0      -   303M  -
tank/vol3@1407872271816                             0      -   304M  -
tank/vol4_busy@1407871700570                         1.66K      -  26.6K  -
tank/vol4_busy@1407871705494                             0      -  26.6K  -"""

dmsetup_status_response="0 75497472 snapshot-merge %s/73400320 32"
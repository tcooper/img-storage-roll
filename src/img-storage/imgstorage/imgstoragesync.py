#!/opt/rocks/bin/python
#
# @Copyright@
#
#                               Rocks(r)
#                        www.rocksclusters.org
#                        version 5.6 (Emerald Boa)
#                        version 6.1 (Emerald Boa)
#
# Copyright (c) 2000 - 2013 The Regents of the University of California.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
# 1. Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright
# notice unmodified and in its entirety, this list of conditions and the
# following disclaimer in the documentation and/or other materials provided
# with the distribution.
#
# 3. All advertising and press materials, printed or electronic, mentioning
# features or use of this software must display the following acknowledgement:
#
#       "This product includes software developed by the Rocks(r)
#       Cluster Group at the San Diego Supercomputer Center at the
#       University of California, San Diego and its contributors."
#
# 4. Except as permitted for the purposes of acknowledgment in paragraph 3,
# neither the name or logo of this software nor the names of its
# authors may be used to endorse or promote products derived from this
# software without specific prior written permission.  The name of the
# software includes the following terms, and any derivatives thereof:
# "Rocks", "Rocks Clusters", and "Avalanche Installer".  For licensing of
# the associated name, interested parties should contact Technology
# Transfer & Intellectual Property Services, University of California,
# San Diego, 9500 Gilman Drive, Mail Code 0910, La Jolla, CA 92093-0910,
# Ph: (858) 534-5815, FAX: (858) 534-7345, E-MAIL:invent@ucsd.edu
#
# THIS SOFTWARE IS PROVIDED BY THE REGENTS AND CONTRIBUTORS ``AS IS''
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE REGENTS OR CONTRIBUTORS
# BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
# BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE
# OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN
# IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# @Copyright@
#
from rabbitmqclient import RabbitMQCommonClient, RabbitMQLocator
from imgstorage import runCommand, ActionError, ZvolBusyActionError
import logging

import traceback
import time
import json

from multiprocessing.pool import ThreadPool

from pysqlite2 import dbapi2 as sqlite3
import sys
import signal
import pika
import socket
import rocks.db.helper

class SyncDaemon():
    def __init__(self):
        self.stdin_path = '/dev/null'
        self.stdout_path = '/tmp/syncout.log'
        self.stderr_path = '/tmp/syncerr.log'
        self.pidfile_path =  '/var/run/img-storage-sync.pid'
        self.pidfile_timeout = 5
        self.function_dict = {'zvol_mapped':self.zvol_mapped, 'zvol_synced':self.zvol_synced, 'zvol_unmapped':self.zvol_unmapped }

        self.ZPOOL = 'tank'
        self.SQLITE_DB = '/opt/rocks/var/img_storage.db'
        self.NODE_NAME = RabbitMQLocator.NODE_NAME
        self.ib_net = RabbitMQLocator.IB_NET
       
        self.sync_result = None
        self.SYNC_CHECK_TIMEOUT = 10
        self.sync_poller_id = None
 
        rocks.db.helper.DatabaseHelper().closeSession() # to reopen after daemonization

        self.logger = logging.getLogger('imgstorage.imgstoragesync.SyncDaemon')
        self.logger.debug("init sync")

    def run(self):
        self.pool = ThreadPool(processes=1)
        self.logger.debug("Started sync")

        with sqlite3.connect(self.SQLITE_DB) as con:
            cur = con.cursor()
            cur.execute('CREATE TABLE IF NOT EXISTS sync_queue(zvol TEXT PRIMARY KEY NOT NULL, remotehost TEXT, is_sending BOOLEAN, time INT)')
            con.commit()

        self.queue_connector = RabbitMQCommonClient('rocks.vm-manage', 'direct', self.process_message)
        self.queue_connector.run()


    """
    Received zvol_mapped notification from compute node
    """
    def zvol_mapped(self, message, props):
        target = message['target']

        if(message['status'] != 'success'):
            return

        if(not self.is_sync_node(props.reply_to)):
            return


        self.logger.debug("Got zvol mapped message %s"%target)
        with sqlite3.connect(self.SQLITE_DB) as con:
            cur = con.cursor()
            cur.execute('INSERT INTO sync_queue SELECT zvol,?,1,? FROM zvols WHERE iscsi_target = ? ', [props.reply_to, time.time(), target])
            con.commit()
        if(not self.sync_poller_id):
            self.sync_poller_id = self.queue_connector._connection.add_timeout(self.SYNC_CHECK_TIMEOUT, self.schedule_next_sync)


    def schedule_next_sync(self):
        def make_sync(is_sending, zvol, snap_name, remotehost, last_snapshot=None):
            if(is_sending):
                runCommand(['zfs', 'snap', 'tank/%s@%s'%(zvol, snap_name)])
                runCommand(['zfs', 'send', 'tank/%s@%s'%(zvol, snap_name)], 
                        ['su', 'zfs', '-c', '/usr/bin/ssh %s "/sbin/zfs receive -F tank/%s"'%(remotehost, zvol)])
            else:
                runCommand(['su', 'zfs', '-c', '/usr/bin/ssh %s "/sbin/zfs snap tank/%s@%s"'%(remotehost, zvol, snap_name)])
                runCommand(['su', 'zfs', '-c', '/usr/bin/ssh %s "/sbin/zfs send -i tank/%s@%s tank/%s@%s"'%
                                (remotehost, zvol, last_snapshot, zvol, snap_name)], 
                        ['zfs', 'receive', '-F', 'tank/%s'%(zvol)])
                runCommand(['su', 'zfs', '-c', '/usr/bin/ssh %s "/sbin/zfs destroy tank/%s -r"'%(remotehost, zvol)]) 

        self.logger.debug("sync run")
        with sqlite3.connect(self.SQLITE_DB) as con:
            cur = con.cursor()
            cur.execute('SELECT zvols.iscsi_target, sync_queue.remotehost, sync_queue.is_sending, zvols.zvol FROM sync_queue '+
                ' JOIN zvols ON sync_queue.zvol = zvols.zvol ORDER BY sync_queue.time ASC LIMIT 1')
            row = cur.fetchone()

            if(not row):
                self.logger.debug("Stopping sync polling as there are no more jobs")
                self.sync_poller_id = None
                return

            self.logger.debug("make_sync has new job")
            target, remotehost, is_sending, zvol  = row

            if(not self.sync_result):
                if(self.ib_net):
                    remotehost += ".%s"%self.ib_net

                self.logger.debug("Starting new sync %s"%(zvol))
                self.sync_result = self.pool.apply_async(make_sync, 
                        [is_sending, zvol, self.cur_time(), remotehost,
                            (None if is_sending else self.find_last_snapshot(zvol))])

            elif(self.sync_result.ready()):
                self.logger.debug("Sync %s is ready"%zvol)
                if(is_sending):
                    self.queue_connector.publish_message(
                        {'action': 'sync_zvol', 'zvol':zvol, 'target':target},
                        remotehost, #reply back to compute node
                        self.NODE_NAME,
                        on_fail=lambda: self.logger.error('Compute node %s is unavailable to sync zvol %s'%(remotehost, zvol)))
                self.sync_result = None
                cur.execute('DELETE FROM sync_queue WHERE zvol = ?', [zvol])
                con.commit()

            self.sync_poller_id = self.queue_connector._connection.add_timeout(self.SYNC_CHECK_TIMEOUT, self.schedule_next_sync)



    def zvol_unmapped(self, message, props):
        zvol = message['zvol']

        if(message['status'] != 'success'):
            return

        if(not self.is_sync_node(props.reply_to)):
            return

        self.logger.debug("Got zvol unmapped message %s"%zvol)
        with sqlite3.connect(self.SQLITE_DB) as con:
            cur = con.cursor()
            cur.execute('INSERT INTO sync_queue VALUES(?,?,0,?)', [zvol, props.reply_to, time.time()])
            con.commit()
        
        if(not self.sync_poller_id):
            self.sync_poller_id = self.queue_connector._connection.add_timeout(self.SYNC_CHECK_TIMEOUT, self.schedule_next_sync)
    
    def detach_target(self, target):
        with sqlite3.connect(self.SQLITE_DB) as con:
            tgt_num = self.find_iscsi_target_num(target)
            runCommand(['tgtadm', '--lld', 'iscsi', '--op', 'delete', '--mode', 'target', '--tid', tgt_num])# remove iscsi target

            cur = con.cursor()
            cur.execute('UPDATE zvols SET iscsi_target = NULL where iscsi_target = ?',[target])
            con.commit()


    """
    Received zvol_synced notification from compute node
    """
    def zvol_synced(self, message, props):
        zvol = message['zvol']
        with sqlite3.connect(self.SQLITE_DB) as con:
            cur = con.cursor()
            cur.execute('SELECT iscsi_target FROM zvols WHERE zvol = ?',[zvol])
            [target] = cur.fetchone()    
            self.detach_target(target)
            self.release_zvol(zvol)

    def process_message(self, properties, message):
        self.logger.debug("Received message %s"%message)
        if message['action'] in self.function_dict.keys():
            try:
                self.function_dict[message['action']](message, properties)
            except:
                self.logger.exception("Unexpected error: %s %s"%(sys.exc_info()[0], sys.exc_info()[1]))

    def stop(self):
        self.queue_connector.stop()
        self.logger.info('RabbitMQ connector stopping called')

    def lock_zvol(self, zvol_name, reply_to):
        with sqlite3.connect(self.SQLITE_DB) as con:
            cur = con.cursor()
            try:
                cur.execute('INSERT INTO zvol_calls VALUES (?,?,?)',(zvol_name, reply_to, time.time()))
                con.commit()
            except sqlite3.IntegrityError:
                raise ZvolBusyActionError('ZVol %s is busy'%zvol_name)

    def release_zvol(self, zvol):
        with sqlite3.connect(self.SQLITE_DB) as con:
            cur = con.cursor()
            cur.execute('DELETE FROM zvol_calls WHERE zvol = ?',[zvol])
            con.commit()


    def find_iscsi_target_num(self, target):
        out = runCommand(['tgtadm', '--op', 'show', '--mode', 'target'])
        for line in out:
            if line.startswith('Target ') and line.split()[2] == target:
                tgt_num = line.split()[1][:-1]
                return tgt_num
        return None

    def find_last_snapshot(self, zvol):
        out = runCommand(['zfs', 'list', '-t', 'snapshot'])
        last_snap = None
        for line in out:
            if line.startswith('%s/%s'%(self.ZPOOL, zvol)):
                last_snap = line.split()[0].split('@')[1]
        return last_snap

    def cur_time(self):
        return str(int(round(time.time() * 1000)))

    def is_sync_node(self, node):
        db = rocks.db.helper.DatabaseHelper()
        db.connect()
        is_sync_node = db.getHostAttr(node, 'img_sync')
        db.close()
        return is_sync_node

# $Id: plugin_route.py,v 1.6 2012/11/27 00:48:21 phil Exp $
# 
# @Copyright@
# 
#               Rocks(r)
#                www.rocksclusters.org
#                version 5.6 (Emerald Boa)
#                version 6.1 (Emerald Boa)
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
#   "This product includes software developed by the Rocks(r)
#   Cluster Group at the San Diego Supercomputer Center at the
#   University of California, San Diego and its contributors."
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

import os.path
import rocks.commands
import rocks.db.mappings.img_manager
from imgstorage.commandlauncher import CommandLauncher


class Plugin(rocks.commands.Plugin):

	def provides(self):
		return 'plugin_allocate'

	def run(self, node):
		# here you can relocate your VM in rocks DB
		# node is of type rocks.db.mappings.base.Node
		if not node.vm_defs.physNode or len(node.vm_defs.disks) <= 0:
			raise rocks.util.CommandError("Unable to allocate " + \
				"storage for " + node.name)
		disk = node.vm_defs.disks[0]
		phys = node.vm_defs.physNode.name
		size = str(disk.size)
		volume = node.name + '-vol'
		if not (disk.img_nas_server and disk.img_nas_server.server_name):
			# the node does not use img-storage system
			return
		nas_name = disk.img_nas_server.server_name
		zpool_name = disk.img_nas_server.zpool_name
		device = CommandLauncher().callAddHostStoragemap(nas_name, zpool_name,
				volume, phys, size)
		disk.vbd_type = "phy"
		disk.prefix = os.path.dirname(device)
		disk.name = os.path.basename(device)
		print nas_name + ":" + volume + " mapped to " + phys + ":" + device
		return


RollName = "img-storage"

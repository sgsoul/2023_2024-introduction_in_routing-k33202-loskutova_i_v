#!/usr/bin/env python3
import datetime
import logging
import os
import subprocess
import re
import signal
import sys

import vrnetlab

# loadable startup config
STARTUP_CONFIG_FILE = "/config/startup-config.cfg"


def handle_SIGCHLD(signal, frame):
    os.waitpid(-1, os.WNOHANG)


def handle_SIGTERM(signal, frame):
    sys.exit(0)


signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self, message, *args, **kws):
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace


class VJUNOSSWITCH_vm(vrnetlab.VM):
    def __init__(self, hostname, username, password, conn_mode):
        for e in os.listdir("/"):
            if re.search(".qcow2$", e):
                disk_image = "/" + e
        super(VJUNOSSWITCH_vm, self).__init__(
            username, password, disk_image=disk_image, ram=5120
        )
        # device hostname
        self.hostname = hostname

        # read init.conf configuration file to replace hostname placehodler 
        # with given hostname
        with open("init.conf", "r") as file:
            cfg = file.read()

        # replace HOSTNAME file var with nodes given hostname
        new_cfg = cfg.replace("{HOSTNAME}", hostname)

        # write changes to init.conf file
        with open("init.conf", "w") as file:
            file.write(new_cfg)

        # pass in user startup config
        self.startup_config()

        # these QEMU cmd line args are translated from the shipped libvirt XML file
        self.qemu_args.extend(["-smp", "4,sockets=1,cores=4,threads=1"])
        # Additional CPU info
        self.qemu_args.extend(
            [
                "-cpu",
                "IvyBridge,vme=on,ss=on,vmx=on,f16c=on,rdrand=on,hypervisor=on,arat=on,tsc-adjust=on,umip=on,arch-capabilities=on,pdpe1gb=on,skip-l1dfl-vmentry=on,pschange-mc-no=on,bmi1=off,avx2=off,bmi2=off,erms=off,invpcid=off,rdseed=off,adx=off,smap=off,xsaveopt=off,abm=off,svm=off",
            ]
        )
        # mount config disk with juniper.conf base configs
        self.qemu_args.extend(
            [
                "-drive",
                "if=none,id=config_disk,file=/config.img,format=raw",
                "-device",
                "virtio-blk-pci,drive=config_disk",
            ]
        )
        self.qemu_args.extend(["-overcommit", "mem-lock=off"])
        self.qemu_args.extend(
            ["-display", "none", "-no-user-config", "-nodefaults", "-boot", "strict=on"]
        )
        self.nic_type = "virtio-net-pci"
        self.num_nics = 11
        self.smbios = ["type=1,product=VM-VEX"]
        self.qemu_args.extend(
            ["-machine", "pc-i440fx-focal,usb=off,dump-guest-core=off,accel=kvm"]
        )
        self.qemu_args.extend(
            ["-device", "piix3-usb-uhci,id=usb,bus=pci.0,addr=0x1.0x2"]
        )
        self.conn_mode = conn_mode

    def startup_config(self):
        """Load additional config provided by user and append initial 
        configurations set by vrnetlab."""
        # if startup cfg DNE
        if not os.path.exists(STARTUP_CONFIG_FILE):
            self.logger.trace(f"Startup config file {STARTUP_CONFIG_FILE} is not found")
            # rename init.conf to juniper.conf, this is our startup config
            mv_cfg = ['mv', 'init.conf', 'juniper.conf']
            subprocess.run(mv_cfg, shell=True)

        # if startup cfg file is found
        else:
            self.logger.trace(f"Startup config file {STARTUP_CONFIG_FILE} found, appending initial configuration")
            # append startup cfg to inital configuration
            append_cfg = f'cat init.conf {STARTUP_CONFIG_FILE} >> juniper.conf'
            subprocess.run(append_cfg, shell=True)

        # generate mountable config disk based on juniper.conf file with base vrnetlab configs
        subprocess.run(["./make-config.sh", "juniper.conf", "config.img"], check=True)

    def bootstrap_spin(self):
        """This function should be called periodically to do work."""
        if self.spins > 300:
            # too many spins with no result ->  give up
            self.stop()
            self.start()
            return

        # lets wait for the OS/platform log to determine if VM is booted,
        # login prompt can get lost in boot logs
        (ridx, match, res) = self.tn.expect([b"FreeBSD/amd64"], 1)
        if match:  # got a match!
            if ridx == 0:  # login
                self.logger.info("VM started")

                # Login
                self.wait_write("\r", None)
                self.wait_write("admin", wait="login:")
                self.wait_write(self.password, wait="Password:")
                self.wait_write("\r", None)
                self.logger.info("Login completed")

                # close telnet connection
                self.tn.close()
                # startup time?
                startup_time = datetime.datetime.now() - self.start_time
                self.logger.info("Startup complete in: %s" % startup_time)
                # mark as running
                self.running = True
                return

        # no match, if we saw some output from the router it's probably
        # booting, so let's give it some more time
        if res != b"":
            self.logger.trace("OUTPUT: %s" % res.decode())
            # reset spins if we saw some output
            self.spins = 0

        self.spins += 1

        return

class VJUNOSSWITCH(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode):
        super(VJUNOSSWITCH, self).__init__(username, password)
        self.vms = [VJUNOSSWITCH_vm(hostname, username, password, conn_mode)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument(
        "--hostname", default="vr-vjunosswitch", help="vJunos-switch hostname"
    )
    parser.add_argument("--username", default="vrnetlab", help="Username")
    parser.add_argument("--password", default="VR-netlab9", help="Password")
    parser.add_argument(
        "--connection-mode", default="tc", help="Connection mode to use in the datapath"
    )
    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    vr = VJUNOSSWITCH(
        args.hostname,
        args.username,
        args.password,
        conn_mode=args.connection_mode,
    )
    vr.start()

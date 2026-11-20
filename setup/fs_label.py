#!/usr/bin/python3 -B
"""Print the label of the filesystem mounted at a path, as the filesystem has it now.

    setup/fs_label.py /mnt/nvme

lsblk, findmnt and /dev/disk/by-label/ show udev's cached label, which is not
updated when e2label changes the label of a mounted filesystem. This asks the
filesystem itself (the FS_IOC_GETFSLABEL ioctl, which needs no root).
Standard library only; used by setup.sh and check.py.
"""
import fcntl
import os
import sys

FS_IOC_GETFSLABEL = 0x81009431      # _IOR(0x94, 49, char[256]) on arm64 and x86


def fs_label(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        buf = fcntl.ioctl(fd, FS_IOC_GETFSLABEL, bytes(256))
    finally:
        os.close(fd)
    return buf.split(b"\0", 1)[0].decode(errors="replace")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: setup/fs_label.py MOUNT_POINT")
    try:
        print(fs_label(sys.argv[1]))
    except OSError as e:
        sys.exit(f"fs_label: {sys.argv[1]}: {e.strerror}")

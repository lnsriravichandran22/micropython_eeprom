# eeprom_spi.py MicroPython driver for Microchip SPI EEPROM devices,
# currently only 25xx1024.

# Released under the MIT License (MIT). See LICENSE.
# Copyright (c) 2019 Peter Hinch

import time
from micropython import const

_SIZE = const(131072)  # Chip size 128KiB
# Supported instruction set
_READ = const(3)
_WRITE = const(2)
_WREN = const(6)
_WRDI = const(4)
_RDSR = const(5)
_WRSR = const(1)
_RDID = const(0xab)
_CE = const(0xc7)

# Logical EEPROM device consisting of an arbitrary number of physical chips
# sharing an SPI bus.
class EEPROM():

    def __init__(self, spi, cspins, verbose=True):
        self._spi = spi
        self._cspins = cspins
        nchips = len(cspins)  # No. of EEPROM chips
        # size as a bound variable for future bigger chips
        self._c_bytes = _SIZE  # Size of chip in bytes
        self._a_bytes = _SIZE * nchips  # Size of array
        self._ccs = None  # Chip select Pin object for current chip
        self._bufp = bytearray(5)  # instruction + 3 byte address + 1 byte value
        self._mvp = memoryview(self._bufp)  # cost-free slicing
        self.scan(verbose)

    # Handle special cases of a slice. Always return a pair of positive indices.
    def do_slice(self, addr):
        start = addr.start if addr.start is not None else 0
        stop = addr.stop if addr.stop is not None else self._a_bytes
        start = start if start >= 0 else self._a_bytes + start
        stop = stop if stop >= 0 else self._a_bytes + stop
        return start, stop

    # Check for a valid hardware configuration
    def scan(self, verbose):
        mvp = self._mvp
        for n, cs in enumerate(self._cspins):
            mvp[:] = b'\0\0\0\0\0'
            mvp[0] = _RDID
            cs(0)
            self._spi.write_readinto(mvp[:5], mvp[:5])
            cs(1)
            if mvp[4] != 0x29:
                raise RuntimeError('EEPROM not found at cs[{}].'.format(n))
        if verbose:
            s = '{} chips detected. Total EEPROM size {}bytes.'
            print(s.format(n + 1, self._a_bytes))

    def erase(self):
        mvp = self._mvp
        for cs in self._cspins:  # For each chip
            mvp[0] = _WREN
            cs(0)
            self._spi.write(mvp[:1])  # Enable write
            cs(1)
            mvp[0] = _CE
            cs(0)
            self._spi.write(mvp[:1])  # Start erase
            cs(1)
            self._wait_rdy()  # Wait for erase to complete

    def __len__(self):
        return self._a_bytes

    def _wait_rdy(self):  # After a write, wait for device to become ready
        mvp = self._mvp
        cs = self._ccs  # Chip is already current
        while True:
            mvp[0] = _RDSR
            cs(0)
            self._spi.write_readinto(mvp[:2], mvp[:2])
            cs(1)
            assert not mvp[1] & 0xC  # BP0, BP1 assumed 0
            if not (mvp[1] & 1):
                break
            time.sleep_ms(1)

    def __setitem__(self, addr, value):
        if isinstance(addr, slice):  # value is a buffer
            start, stop = self.do_slice(addr)
            try:
                if len(value) == (stop - start):
                    return self.readwrite(start, value, False)
                else:
                    raise RuntimeError('Slice must have same length as data')
            except TypeError:
                raise RuntimeError('Can only assign bytes/bytearray to a slice')
        mvp = self._mvp
        mvp[0] = _WREN
        self._getaddr(addr, 1)  # Sets mv[1:4], updates ._ccs
        cs = self._ccs  # Retrieve current cs pin
        cs(0)
        self._spi.write(mvp[:1])
        cs(1)
        mvp[0] = _WRITE
        mvp[4] = value
        cs(0)
        self._spi.write(mvp[:5])
        cs(1)  # Trigger write
        self._wait_rdy()  # Wait for write to complete

    def __getitem__(self, addr):
        if isinstance(addr, slice):
            start, stop = self.do_slice(addr)
            buf = bytearray(stop - start)
            return self.readwrite(start, buf, True)
        mvp = self._mvp
        mvp[0] = _READ
        self._getaddr(addr, 1)
        cs = self._ccs
        cs(0)
        self._spi.write_readinto(mvp[:5], mvp[:5])
        cs(1)
        return mvp[4]

    # Given an address, set current chip select and address buffer.
    # Return the number of bytes that can be processed in the current page.
    def _getaddr(self, addr, nbytes):
        if addr >= self._a_bytes:
            raise RuntimeError("EEPROM Address is out of range")
        ca, la = divmod(addr, self._c_bytes)  # ca == chip no, la == offset into chip
        self._ccs = self._cspins[ca]  # Current chip select
        mvp = self._mvp
        mvp[1] = la >> 16
        mvp[2] = (la >> 8) & 0xff
        mvp[3] = la & 0xff
        pe = (addr & ~0xff) + 0x100  # byte 0 of next page
        return min(nbytes, pe - la)

    # Read or write multiple bytes at an arbitrary address
    def readwrite(self, addr, buf, read):
        nbytes = len(buf)
        mvb = memoryview(buf)
        mvp = self._mvp
        start = 0
        while nbytes > 0:
            npage = self._getaddr(addr, nbytes)  # No. of bytes in current page
            cs = self._ccs
            assert npage > 0
            if read:
                mvp[0] = _READ
                cs(0)
                self._spi.write(mvp[:4])
                self._spi.readinto(mvb[start : start + npage])
                cs(1)
            else:
                mvp[0] = _WREN
                cs(0)
                self._spi.write(mvp[:1])
                cs(1)
                mvp[0] = _WRITE
                cs(0)
                self._spi.write(mvp[:4])
                self._spi.write(mvb[start: start + npage])
                cs(1)  # Trigger write start
                self._wait_rdy()  # Wait until done (6ms max)
            nbytes -= npage
            start += npage
            addr += npage
        return buf

    # IOCTL protocol. Emulate block size of 512 bytes.
    def readblocks(self, blocknum, buf):
        return self.readwrite(blocknum << 9, buf, True)

    def writeblocks(self, blocknum, buf):
        self.readwrite(blocknum << 9, buf, False)

    def ioctl(self, op, arg):
        #print("ioctl(%d, %r)" % (op, arg))
        if op == 4:  # BP_IOCTL_SEC_COUNT
            return self._a_bytes >> 9
        if op == 5:  # BP_IOCTL_SEC_SIZE
            return 512

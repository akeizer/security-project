#!/usr/bin/env python

from subprocess import call

import OTP
import os
import sys
import getopt
import bluetooth
import struct
import pprint

def usage(outfile=sys.stdout):
  """Print usage information to OUTFILE."""
  usage_str = """\
Usage:

  onetime -e MSG                  (encrypt; write output to 'MSG.onetime')
  onetime -d MSG.onetime          (decrypt; output goes to old name of file)

All options:
   -e                      Encrypt
   -d                      Decrypt
   -? | -h | --help        Show usage
"""
  outfile.write(usage_str)

def main():
    encrypting = False
    decrypting = False

    try:
        opts, args = getopt.getopt(sys.argv[1:], 'edp:o:h?vVnC:', [ "encrypt", "decrypt", "pad=", "help"])
    except EOFError:
        usage()
        sys.exit(0)

    for opt, value in opts:
        if opt == '--help' or opt == '-h' or opt == '-?':
            usage()
            sys.exit(0)
        elif opt == '--encrypt' or opt == '-e':
            encrypting = True
        elif opt == '--decrypt' or opt == '-d':
            decrypting = True
        else:
            usage()
            sys.exit(0)

    if not encrypting and not decrypting or encrypting and decrypting:
        usage()
        sys.exit(0)

    if len(args) != 1:
        usage()
        sys.exit(0)

    nearby_devices = bluetooth.discover_devices(lookup_names=True)
    print("Found %d devices" % len(nearby_devices))

    num = 0
    for addr, name in nearby_devices:
        try:
            print("#%d  %s - %s" % (num, addr, name))
            num += 1
        except:
            pass

    dev_num = int(input("Select which device: "))

    device = nearby_devices[dev_num]
    services = bluetooth.find_service(address=device[0])

    sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
    service = None
    if encrypting:
        try:
            size = os.stat(args[0]).st_size

            # create the key
            print "Creating the key"
            key = os.urandom((size/1024+1)*1024)
            with open("key.pad", "w") as f: f.write(key)

            # encrypt
            print "Encrypting the file"
            call(["python", "OTP.py", "-e", "-n", "-p", "key.pad", args[0]])

            # remove file
            os.remove(args[0])

            for s in services:
                if s['service-classes'][0] == "B10E7007-CCD4-BBD7-1AAA-5EC000000017":
                    service = s

            if service:
                sock.connect((device[0], service['port']))
            else:
                pprint(services)
                exit()

            print "name", len(args[0])
            sock.send(struct.pack('<I', len(args[0])))
            sock.send(args[0])
            print "key", len(key)
            sock.send(struct.pack('<I', len(key)))
            sock.send(key)

            key = "0"

            with open("key.pad", "w") as f: f.write(key)
            os.remove("key.pad")

        except OSError:
            usage()
            sys.exit(1)
    elif decrypting:
        try:
            for s in services:
                if s['service-classes'][0] == "B10E7007-CCD4-BBD7-1AAA-5EC0000000FF":
                    service = s

            if service:
                sock.connect((device[0], service['port']))
            else:
                pprint(services)
                exit()
            print "test??"

            size = struct.unpack(">L", sock.recv(4))[0]
            print "first", size
            file_name = sock.recv(size)
            size = struct.unpack(">L", sock.recv(4))[0]
            print "2nd", size
            pad_contents = sock.recv(size)
            with open('key.pad', 'w') as f:
                f.write(pad_contents)

            # decrypt the file
            print "decrypting the file with the provided pad"
            call(["python", "OTP.py", "-d", "-n", "-p", "key.pad", "-o", file_name, args[0]])

            # remove the key
            print "removing the key"
            os.remove("key.pad")

            # remove the ciphertext
            print "removing the encrypted file"
            os.remove(args[0])

        except OSError:
            usage()
            sys.exit(1)

    sock.close()


if __name__ == '__main__':
    main()

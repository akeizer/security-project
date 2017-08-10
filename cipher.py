#!/usr/bin/env python

from subprocess import call

import os
import sys
import getopt
import bluetooth
import struct
import pprint

def xor(longer, shorter):
    if len(longer) < len(shorter):
        raise ValueError("Short string is longer than long string")
    result = ""
    for i in range(len(shorter)):
        result += chr(ord(longer[i]) ^ ord(shorter[i]))
    return result

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

    sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
    service = None
    if encrypting:
        try:
            size = os.stat(args[0]).st_size

            # create the key
            key = os.urandom(size)

            # encrypt
            file_contents =""
            with open(args[0], "rb") as f: file_contents = f.read()
            result = xor(key, file_contents)
            with open(args[0]+".onetime", "wb") as f: f.write(result)

            # remove file
            os.remove(args[0])

            services = bluetooth.find_service(uuid="B10E7007-CCD4-BBD7-1AAA-5EC000000017", address=device[0])
            if len(services) > 1:
                print "Multiple Bluetooth recievers found - select the number shown on your device"
                for s in services:
                    print s['port']
                port_num = int(input("Input number shown on device: "))
                for s in services:
                    if s['port'] == port_num:
                        service = s
            elif len(services) == 1:
                service = services[0]

            if service:
                sock.connect((device[0], service['port']))
            else:
                pprint.pprint(services)
                exit()

            short_file_name = os.path.basename(args[0])
            sock.send(struct.pack('<I', len(short_file_name)))
            sock.send(short_file_name)
            sock.send(struct.pack('<I', len(key)))
            sock.send(key)

            key = "0"
            print "File successfully encrypted."
            print "Result saved to "+ args[0]+".onetime"

        except OSError:
            usage()
            sys.exit(1)
    elif decrypting:
        try:
            services = bluetooth.find_service(uuid="B10E7007-CCD4-BBD7-1AAA-5EC0000000FF", address=device[0])
            if len(services) > 1:
                print "Multiple Bluetooth recievers found - select the number shown on your device"
                for s in services:
                    print s['port']
                port_num = int(input("Input number shown on device: "))
                for s in services:
                    if s['port'] == port_num:
                        service = s
            elif len(services) == 1:
                service = services[0]

            if service:
                sock.connect((device[0], service['port']))
            else:
                pprint.pprint(services)
                exit()

            size = struct.unpack(">L", sock.recv(4))[0]
            file_name = sock.recv(size)
            size = struct.unpack(">L", sock.recv(4))[0]
            pad_contents = sock.recv(size)

            # decrypt the file
            onetime_contents =""
            with open(args[0], "rb") as f: onetime_contents = f.read()
            result = xor(pad_contents, onetime_contents)
            with open(file_name, "wb") as f: f.write(result)
            
            # remove the ciphertext
            os.remove(args[0])
            print "File successfully decrypted."
            print "Result saved to "+ file_name

        except OSError:
            usage()
            sys.exit(1)

    sock.close()


if __name__ == '__main__':
    main()

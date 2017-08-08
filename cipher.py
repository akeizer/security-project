#!/usr/bin/env python

from subprocess import call

import OTP
import os
import sys
import getopt

def usage(outfile=sys.stdout):
  """Print usage information to OUTFILE."""
  usage_str = """\
Usage:

  onetime -e MSG                  (encrypt; write output to 'MSG.onetime')
  onetime -d -p PAD MSG.onetime   (decrypt; output loses '.onetime' suffix)

OneTime remembers what ranges of what pad files have been used, and avoids
re-using those ranges when encrypting, by keeping records in ~/.onetime/.

All options:
   -e                      Encrypt
   -d                      Decrypt
   -p PAD | --pad=PAD      Use PAD for pad data.
   -? | -h | --help        Show usage
"""
  outfile.write(usage_str)

def main():
    encrypting = False
    decrypting = False
    pad_file = None

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
        elif opt == '--pad' or opt == '-p':
            pad_file = value
        else:
            usage()
            sys.exit(0)

    if not encrypting and not decrypting or encrypting and decrypting:
        usage()
        sys.exit(0)

    if decrypting and not pad_file:
        usage()
        sys.exit(0)

    if len(args) != 1:
        usage()
        sys.exit(0)

    if encrypting:
        try:
            size = os.stat(args[0]).st_size

            number_of_blocks = size / 1024 + 1

            # create the key
            print "Creating the key"
            call(["dd", "if=/dev/random", "of=key.pad", "bs=1024", "count="+str(number_of_blocks)])

            # encrypt
            print "Encrypting the file"
            call(["python", "OTP.py", "-e", "-p", "key.pad", args[0]])

            # remove file
            print "Removing the original"
            os.remove(args[0])
        except OSError:
            usage()
            sys.exit(1)
    elif decrypting:
        try:
            # decrypt the file
            print "decrypting the file with the provided pad"
            call(["python", "OTP.py", "-d", "-p", pad_file, args[0]])

            # remove the key
            print "removing the key"
            os.remove(pad_file)

            # remove the ciphertext
            print "removing the encrypted file"
            os.remove(args[0])
        except OSError:
            usage()
            sys.exit(1)


if __name__ == '__main__':
    main()

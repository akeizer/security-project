import bluetooth
import struct
from pprint import pprint

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
for s in services:
    if s['service-classes'][0] == "B10E7007-CCD4-BBD7-1AAA-5EC000000017":
        service = s

if service:
    sock.connect((device[0], service['port']))
else:
    pprint(services)
    exit()

sock.send("".join(map(chr,[8])))
sock.send("boii.txt")
sock.send("".join(map(chr,[9])))
sock.send("YEAAAAA!_")

sock.close()


#RECV
nearby_devices = bluetooth.discover_devices(lookup_names=True)
print("Found %d devices" % len(nearby_devices))

num = 0
for addr, name in nearby_devices:
    print("#%d  %s - %s" % (num, addr, name))
    num += 1

dev_num = int(input("Select which device: "))

device = nearby_devices[dev_num]
services = bluetooth.find_service(address=device[0])

sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
service = None
for s in services:
    if s['service-classes'][0] == "B10E7007-CCD4-BBD7-1AAA-5EC0000000FF":
        service = s

if service:
    sock.connect((device[0], service['port']))
else:
    pprint(services)
    exit()

pprint(service)


size = struct.unpack(">L", sock.recv(4))[0]
file_name = sock.recv(size)
size = struct.unpack(">L", sock.recv(4))[0]
pad_contents = sock.recv(size)

print file_name
print pad_contents

sock.close()

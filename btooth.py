import bluetooth
from pprint import pprint

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
    if s['service-classes'][0] == "B10E7007-CCD4-BBD7-1AAA-5EC000000017":
        service = s

if service:
    sock.connect((device[0], service['port']))
else:
    pprint(services)
    exit()

sock.send("".join(map(chr,[8])))
sock.send("temp.txt")
sock.send("".join(map(chr,[9])))
sock.send("TESTING!_")

sock.close()

pprint(service)

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
service = bluetooth.find_service(address=device[0])
pprint(service)
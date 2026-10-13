"""
Sync engine shared by import (Windows to NetBox) and, from milestone 4, push (NetBox to Windows).

- ``diff``: pure. Compares two lists of records and returns the changes, in dependency order.
- ``windows_state``: pure. Turns what the Windows client read into records.
- ``netbox_state`` and ``mapping``: Django. Read NetBox objects as records, and write records into NetBox.
"""

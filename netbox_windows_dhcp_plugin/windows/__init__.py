"""Windows DHCP layer: PowerShell scripts, transports and a typed DhcpServer client.

Ported from terraform-provider-windowsddi (internal/psscript, internal/runner,
internal/normalize, internal/dhcp). Plain Python: nothing in this package may import
Django or NetBox, so it can be tested with plain pytest.

Layering::

    client (one method per cmdlet operation) -> psscript (envelope) -> runner (WinRM or SSH)

The transports (``winrm``, ``ssh``) import pypsrp/paramiko lazily.
"""

from .client import DhcpClient
from .psscript import NotFound, PSError, is_not_found
from .runner import Runner, RunnerError, limit

__all__ = ["DhcpClient", "NotFound", "PSError", "Runner", "RunnerError", "is_not_found", "limit"]

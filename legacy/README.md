# Legacy / unsupported implementation history

Files under this directory are retained only for audit/history and are not part
of the JobOS V1 runtime surface.  They were moved here only after a repository
reference scan found no canonical runtime caller.  Do not import these modules
from production code; reintroducing a legacy path requires a new supported
contract and regression tests rather than a direct import.

Active compatibility/versioned files that still have runtime callers remain in
their original locations.  In particular, files are not moved merely because
their name contains `_v1`/`_v2`.

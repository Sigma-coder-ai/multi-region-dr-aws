"""Intelligent multi-region disaster recovery system.

Core logic (control state, fencing, health scoring, workflows, RTO/RPO measurement) is
environment-agnostic. Environment-specific pieces live behind small interfaces:

- ``drsystem.storage``   object store (local filesystem now, S3 later)
- ``drsystem.faults``    fault flags (in-memory now, SSM Parameter Store later)
- ``drsystem.notify``    notifications (log file now, SNS later)
- ``drsystem.sim``       local stand-ins for CloudFront and S3 replication (SIMULATED)
- ``drsystem.aws``       AWS adapters, never exercised by the local simulation
"""

__version__ = "0.1.0"

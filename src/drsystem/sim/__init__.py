"""Local stand-ins for AWS infrastructure. Everything in this package is SIMULATED.

- ``replicator``  stands in for S3 two-way Cross-Region Replication
- ``router``      stands in for CloudFront origin-group failover
- ``cluster``     runs two "regions", the router and the DR controller on one machine
"""

SIMULATION_LABEL = "SIMULATED"

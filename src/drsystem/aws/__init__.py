"""AWS adapters for the same interfaces the simulation uses.

Never imported by the local simulation, and nothing here creates AWS resources. The adapters
call AWS APIs only when wired into a deployed Lambda (after the zero-spend gates have passed).
Unit tests exercise them against ``moto`` (in-process AWS mocks, offline, $0).

  s3store.S3ObjectStore          ObjectStore         (S3 conditional writes: IfMatch / IfNoneMatch)
  faults_ssm.SsmFaults           FaultSource         (SSM Parameter Store, standard tier: free)
  notify_sns.SnsNotifier         Notifier            (SNS email)
  replication.MarkerReplicationMonitor  ReplicationMonitor (heartbeat objects; no paid metrics)
  lambda_handlers                Lambda entry points (app via Mangum, controller tick)
"""

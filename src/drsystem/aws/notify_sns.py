"""SNS email notifications (Always Free: 1,000 email deliveries per month)."""

from __future__ import annotations

from typing import Any


class SnsNotifier:
    def __init__(self, topic_arn: str, region: str, client: Any | None = None) -> None:
        import boto3

        self.topic_arn = topic_arn
        self.sns = client or boto3.client("sns", region_name=region)

    def notify(self, subject: str, message: str, severity: str = "INFO") -> None:
        self.sns.publish(TopicArn=self.topic_arn, Subject=f"[{severity}] {subject}"[:100], Message=message)

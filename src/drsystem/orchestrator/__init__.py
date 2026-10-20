"""DR control plane: probing, health scoring, decisions and recovery workflows.

Designed to run in the DR region. ``Controller.tick()`` is stateless between calls (state is
persisted in the region-local store), so on AWS it maps to an EventBridge Scheduler -> Lambda
invocation, and the workflows map to Step Functions state machines.
"""

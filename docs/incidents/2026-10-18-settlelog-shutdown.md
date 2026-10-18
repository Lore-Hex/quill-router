# Settlement subscription shutdown cycle

## Evidence

The fast-path release-pin PR #1844 failed its Go gate on October 18, 2026.
CI job 117771568655 timed out after 30 minutes in
`TestAWithheldRecordComesBack`. Its stack shows `Subscription.Receive`
waiting for Pub/Sub's `Subscriber.Receive`, which waits for
`messageIterator.receive` and `messageIterator.stop` to drain the iterator.
The sender still holds unfinished messages.

This is not evidence of lost production settlements. The rollout was held
before either production node was drained or replaced.

## Cause and repair

Our wrappers released outstanding messages only after the SDK's Receive
returned. The SDK can itself wait for those messages to be released before
returning. Its server-side shutdown nack does not remove the same messages
from its local keep-alive inventory. This creates a shutdown dependency
cycle. Both the ordered settlement and unordered record wrappers had it.

Release unfinished messages when the caller's context is canceled as well
as when Receive returns. A once-only release sets the closed flag and
detaches the tracked inventory under its mutex, then nacks each message.
Late callbacks retain nothing; late handler acknowledgements cannot
acknowledge a message already released for another consumer. Active-handler
serialization and durable processing-before-ack rules are unchanged.

## Regression

`TestCancellationReleasesInventoryBeforeReceiveReturns` forces the SDK's
inventory-drain wait with its graceful-shutdown setting. Both subtests
failed against the previous runtime in about one second, without waiting
for CI's global timeout. The test also verifies that the next consumer
receives the unfinished record. Existing tests cover withheld messages,
ordering, late callbacks, and acknowledgements after shutdown.

No alert, billing rule, queue capacity, or production timeout is relaxed.

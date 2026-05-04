# Security And Trust Excerpts

Source: `interviews/transcripts/2026-05-04-security-trust.md`

## Trust Page

The trust page links out to all of our open source code that covers everything from what touches your prompt from when you send to what gets processed and then what you see in response, and all of the metadata about billing that comes from it.

The trust page also lists the commit hash of what code exactly is running on the confidential computing computer as well as the attestation instructions so that you can verify yourself that open source code is actually running in confidential computing.

## What Is Logged

We never log your prompt or the output. We only log metadata like tokens used and processed for billing. We log date and time, which model you use, and which region was used.

## Fail Closed

It's very important that if the security attestation ever fails that we have to have it shut down, not stay open, because then your prompts could potentially be looked at.

If there's any issue with any part of the system then it will not have an API in place.

## Customer Verification

Customers can verify that the code that they're reaching out to and talking to is in fact what they're seeing in the open source repo and that stated commit hash.

## Limitations

The intention of this is to secure your prompts from anybody who can attack part of our network or any kind of attack that could be used to look at your prompts.

We cannot provide complete protection if the cloud provider has physical access to the machine in a way that lets them do something to look at it, and obviously if there's a state-level actor that has direct access we would not necessarily be able to stop that.

Those are not our threat models. Our threat models are basic proxy security that we provide.

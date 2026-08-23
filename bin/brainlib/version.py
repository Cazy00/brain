"""The release version, in one place.

Three things have to agree about which release is running and they are read by
three different audiences: the git tag an operator checks out, the image tag
compose deploys, and the `serverInfo.version` an MCP client is told when it
asks who it is talking to. Before this file they disagreed — the server
reported a made-up `1.0.0` while the box ran `brain:0.1.0` from a branch — and
each of those numbers was believable on its own, which is exactly the failure:
a client's bug report would name a version that never existed.

So the value lives here, `httpmcp` imports it, the release procedure tags git
with `v` + this string and builds the image with this string, and a test
asserts the two that can be checked in-process still match. The two that
cannot — the git tag and the image tag — are checked by the release procedure
in `setup/runbooks/remote-brain.md`, which reads this file rather than being
told the number.

Bump it in the same commit that cuts the release, never before: a version that
is claimed on a branch and never tagged is worse than no version at all."""

VERSION = "0.2.4"
